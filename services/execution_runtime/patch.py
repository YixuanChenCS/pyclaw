from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from packages.shared_types import ErrorCode, ErrorCodeContractError, PatchProposal

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True, slots=True)
class _DiffLine:
    prefix: str
    text: str
    no_newline: bool = False


@dataclass(frozen=True, slots=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[_DiffLine, ...]


@dataclass(frozen=True, slots=True)
class _FilePatch:
    old_path: str
    new_path: str
    hunks: tuple[_Hunk, ...]


class LocalPatchApplier:
    """Apply unified diff patches to a local workspace with conservative safety checks."""

    def __init__(self, workspace_root: str | Path) -> None:
        self._workspace_root = self._resolve_workspace_root(workspace_root)

    def apply(self, proposal: PatchProposal) -> tuple[str, ...]:
        file_patches = self._parse_unified_diff(proposal.unified_diff)
        if not file_patches:
            raise ErrorCodeContractError(
                ErrorCode.PATCH_MALFORMED,
                "Patch proposal does not contain any file diffs.",
            )

        if proposal.target_paths:
            expected_paths = {path for path in proposal.target_paths}
        else:
            expected_paths = None

        operations: list[tuple[Path, str, str | None]] = []
        changed_paths: list[str] = []
        for file_patch in file_patches:
            target_path = self._target_path_for_patch(file_patch)
            if expected_paths is not None and target_path not in expected_paths:
                raise ErrorCodeContractError(
                    ErrorCode.PATCH_MALFORMED,
                    f"Patch target path is not declared in proposal.target_paths: {target_path}",
                    details={"target_path": target_path},
                )

            resolved_path = self._resolve_workspace_path(target_path)
            current_text = self._read_current_text(file_patch, resolved_path)
            updated_text = self._apply_file_patch(file_patch, current_text)
            operation = "delete" if file_patch.new_path == "/dev/null" else "write"
            operations.append((resolved_path, operation, updated_text))
            changed_paths.append(target_path)

        self._apply_operations(operations)
        return tuple(changed_paths)

    def _resolve_workspace_root(self, workspace_root: str | Path) -> Path:
        root = Path(workspace_root).expanduser()
        try:
            resolved = root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                f"Workspace root not found: {workspace_root}",
            ) from exc
        if not resolved.is_dir():
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_PATH_INVALID,
                f"Workspace root must be a directory: {workspace_root}",
            )
        return resolved

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (self._workspace_root / candidate).resolve(strict=False)

        if not resolved.is_relative_to(self._workspace_root):
            raise ErrorCodeContractError(
                ErrorCode.AGENT_WRITE_OUTSIDE_WORKSPACE,
                f"Patch target must stay inside workspace: {raw_path}",
                details={
                    "workspace_root": str(self._workspace_root),
                    "resolved_path": str(resolved),
                },
            )

        parent = resolved.parent.resolve(strict=True)
        if not parent.is_relative_to(self._workspace_root):
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_SYMLINK_ESCAPE,
                f"Patch target resolves through a symlink outside the workspace: {raw_path}",
                details={"resolved_parent": str(parent)},
            )
        return resolved

    def _read_current_text(self, file_patch: _FilePatch, path: Path) -> str:
        if file_patch.old_path == "/dev/null":
            return ""

        if not path.exists():
            raise ErrorCodeContractError(
                ErrorCode.PATCH_CONFLICT,
                f"Patch target file is missing: {path.name}",
                details={"path": str(path)},
            )

        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_BINARY_FILE,
                f"Patch target is not valid UTF-8 text: {path.name}",
                details={"path": str(path)},
            ) from exc

    def _apply_file_patch(self, file_patch: _FilePatch, current_text: str) -> str | None:
        current_lines = current_text.splitlines(keepends=True)
        updated_lines: list[str] = []
        cursor = 0

        for hunk in file_patch.hunks:
            start_index = max(hunk.old_start - 1, 0)
            if start_index < cursor or start_index > len(current_lines):
                raise ErrorCodeContractError(
                    ErrorCode.PATCH_CONFLICT,
                    f"Patch hunk position is invalid for {self._target_path_for_patch(file_patch)}.",
                )

            updated_lines.extend(current_lines[cursor:start_index])
            line_index = start_index
            for entry in hunk.lines:
                if entry.prefix == " ":
                    line_index = self._match_existing_line(
                        current_lines,
                        line_index,
                        entry.text,
                        file_patch,
                    )
                    updated_lines.append(current_lines[line_index - 1])
                elif entry.prefix == "-":
                    line_index = self._match_existing_line(
                        current_lines,
                        line_index,
                        entry.text,
                        file_patch,
                    )
                elif entry.prefix == "+":
                    updated_lines.append(entry.text + ("" if entry.no_newline else "\n"))
                else:
                    raise ErrorCodeContractError(
                        ErrorCode.PATCH_MALFORMED,
                        f"Unsupported diff line prefix {entry.prefix!r}.",
                    )
            cursor = line_index

        updated_lines.extend(current_lines[cursor:])
        updated_text = "".join(updated_lines)
        if file_patch.new_path == "/dev/null":
            return None
        return updated_text

    def _match_existing_line(
        self,
        current_lines: list[str],
        line_index: int,
        expected_text: str,
        file_patch: _FilePatch,
    ) -> int:
        if line_index >= len(current_lines):
            raise ErrorCodeContractError(
                ErrorCode.PATCH_CONFLICT,
                f"Patch hunk exceeds file length for {self._target_path_for_patch(file_patch)}.",
            )

        actual_text = current_lines[line_index]
        normalized = actual_text[:-1] if actual_text.endswith("\n") else actual_text
        if normalized != expected_text:
            raise ErrorCodeContractError(
                ErrorCode.PATCH_CONFLICT,
                f"Patch hunk does not apply cleanly to {self._target_path_for_patch(file_patch)}.",
                details={
                    "expected": expected_text,
                    "actual": normalized,
                    "line_index": str(line_index + 1),
                },
            )
        return line_index + 1

    def _apply_operations(self, operations: list[tuple[Path, str, str | None]]) -> None:
        backups: dict[Path, str | None] = {}
        try:
            for path, action, content in operations:
                backups[path] = path.read_text(encoding="utf-8") if path.exists() else None
                if action == "delete":
                    if path.exists():
                        path.unlink()
                else:
                    path.write_text(content or "", encoding="utf-8")
        except Exception:
            for path, original_text in reversed(list(backups.items())):
                if original_text is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_text(original_text, encoding="utf-8")
            raise

    def _parse_unified_diff(self, unified_diff: str) -> tuple[_FilePatch, ...]:
        lines = unified_diff.splitlines()
        file_patches: list[_FilePatch] = []
        index = 0

        while index < len(lines):
            line = lines[index]
            if not line.startswith("--- "):
                index += 1
                continue
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise ErrorCodeContractError(
                    ErrorCode.PATCH_MALFORMED,
                    "Unified diff is missing a +++ header after ---.",
                )

            old_path = self._normalize_patch_path(lines[index][4:])
            new_path = self._normalize_patch_path(lines[index + 1][4:])
            index += 2
            hunks: list[_Hunk] = []

            while index < len(lines) and lines[index].startswith("@@ "):
                hunk_header = lines[index]
                match = _HUNK_HEADER_RE.match(hunk_header)
                if match is None:
                    raise ErrorCodeContractError(
                        ErrorCode.PATCH_MALFORMED,
                        f"Invalid hunk header: {hunk_header}",
                    )
                index += 1
                hunk_lines: list[_DiffLine] = []
                while index < len(lines):
                    hunk_line = lines[index]
                    if hunk_line.startswith(("--- ", "@@ ")):
                        break
                    if hunk_line == r"\ No newline at end of file":
                        if not hunk_lines:
                            raise ErrorCodeContractError(
                                ErrorCode.PATCH_MALFORMED,
                                "Unexpected no-newline marker in diff.",
                            )
                        previous = hunk_lines[-1]
                        hunk_lines[-1] = _DiffLine(previous.prefix, previous.text, True)
                        index += 1
                        continue
                    if not hunk_line or hunk_line[0] not in {" ", "+", "-"}:
                        raise ErrorCodeContractError(
                            ErrorCode.PATCH_MALFORMED,
                            f"Unsupported diff content line: {hunk_line}",
                        )
                    hunk_lines.append(_DiffLine(hunk_line[0], hunk_line[1:]))
                    index += 1

                hunks.append(
                    _Hunk(
                        old_start=int(match.group("old_start")),
                        old_count=int(match.group("old_count") or "1"),
                        new_start=int(match.group("new_start")),
                        new_count=int(match.group("new_count") or "1"),
                        lines=tuple(hunk_lines),
                    )
                )

            file_patches.append(_FilePatch(old_path=old_path, new_path=new_path, hunks=tuple(hunks)))

        return tuple(file_patches)

    def _normalize_patch_path(self, raw_path: str) -> str:
        path = raw_path.split("\t", 1)[0].strip()
        if path in {"/dev/null", ""}:
            return path or "/dev/null"
        if path.startswith("a/") or path.startswith("b/"):
            return path[2:]
        return path

    def _target_path_for_patch(self, file_patch: _FilePatch) -> str:
        if file_patch.new_path != "/dev/null":
            return file_patch.new_path
        return file_patch.old_path


__all__ = ["LocalPatchApplier"]
