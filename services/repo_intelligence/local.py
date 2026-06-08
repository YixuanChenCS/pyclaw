from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Sequence

from packages.shared_types import (
    ErrorCode,
    ErrorCodeContractError,
    FileSummary,
    ImpactAnalysis,
    RepoContextRequest,
    RepoContextResult,
    SymbolMatch,
    WatchSubscription,
    WorkspaceRef,
)

from .service import RepoIntelligenceService

DEFAULT_MAP_TOKENS = 2048
DEFAULT_MAX_FILES = 16
DEFAULT_OTHER_FILES_LIMIT = 64
MAX_FILE_BYTES = 1024 * 1024
MAX_SYMBOL_RESULTS = 50
MAX_SNIPPET_LINES = 3
MAX_SNIPPET_CHARS = 240
IDENT_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_-]{2,}\b")
GENERATED_OR_VENDOR_MARKERS = (
    "node_modules/",
    "vendor/",
    ".venv/",
    "__pycache__/",
    "dist/",
    "build/",
    ".git/",
    ".mypy_cache/",
    ".pytest_cache/",
    "coverage/",
)

try:
    from grep_ast import filename_to_lang as _filename_to_lang
except ImportError:
    def _filename_to_lang(_fname: str) -> str | None:
        return None


class _RepoServiceIO:
    def __init__(self) -> None:
        self.encoding = "utf-8"
        self.outputs: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def read_text(self, fname: str) -> str:
        try:
            return Path(fname).read_text(encoding=self.encoding, errors="replace")
        except (FileNotFoundError, IsADirectoryError, OSError):
            return ""

    def tool_output(self, message: str = "") -> None:
        if message:
            self.outputs.append(str(message))

    def tool_warning(self, message: str = "") -> None:
        if message:
            self.warnings.append(str(message))

    def tool_error(self, message: str = "") -> None:
        if message:
            self.errors.append(str(message))


class _ApproxTokenCounter:
    def token_count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text) / 4))


def _safe_abs_path(path: Path | str) -> str:
    try:
        from pyclaw.utils import safe_abs_path as helper

        return helper(path)
    except Exception:
        return str(Path(path).resolve())


def _is_image_name(file_name: str) -> bool:
    try:
        from pyclaw.utils import is_image_file as helper

        return helper(file_name)
    except Exception:
        return Path(file_name).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".tiff",
            ".webp",
            ".pdf",
        }


def _load_git_backend() -> tuple[tuple[type[BaseException], ...], Any, Any]:
    try:
        from pyclaw.repo import ANY_GIT_ERROR, GitRepo, git

        return ANY_GIT_ERROR, GitRepo, git
    except Exception:
        return (OSError, ValueError, RuntimeError), None, None


def _load_gitignores(paths: Sequence[Path]) -> Any:
    try:
        from pyclaw.watch import load_gitignores as helper

        return helper(list(paths))
    except Exception:
        return None


def _find_src_files(directory: str) -> list[str]:
    try:
        from pyclaw.repomap import find_src_files as helper

        return helper(directory)
    except Exception:
        root = Path(directory)
        if root.is_file():
            return [str(root)]
        return [str(path) for path in root.rglob("*") if path.is_file()]


def _filter_important_files(file_paths: Sequence[str]) -> list[str]:
    try:
        from pyclaw.special import filter_important_files as helper

        return helper(list(file_paths))
    except Exception:
        important_names = {"README.md", "CONTRIBUTING.md", "LICENSE.txt", "pyproject.toml", "pytest.ini"}
        return [path for path in file_paths if Path(path).name in important_names]


class LocalRepoIntelligenceService(RepoIntelligenceService):
    """Local adapter around pyclaw's existing repository analysis behavior."""

    def __init__(
        self,
        *,
        map_tokens: int = DEFAULT_MAP_TOKENS,
        default_max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.map_tokens = map_tokens
        self.default_max_files = default_max_files
        self.max_file_bytes = max_file_bytes
        self._workspaces: dict[str, WorkspaceRef] = {}

    async def inspect_workspace(self, workspace: WorkspaceRef) -> WorkspaceRef:
        raw_root = Path(workspace.root_path).expanduser()
        if not workspace.root_path.strip():
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_PATH_INVALID,
                "Workspace path must not be empty.",
            )

        if raw_root.is_symlink():
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_SYMLINK_ESCAPE,
                f"Workspace path resolves through a symlink: {workspace.root_path}",
                details={"path": workspace.root_path},
            )

        resolved_root = self._resolve_workspace_root(raw_root)
        git_root, branch, commit_sha = self._detect_git_workspace(resolved_root)
        inspected = replace(
            workspace,
            root_path=str(git_root),
            branch=branch,
            commit_sha=commit_sha,
        )
        self._workspaces[str(inspected.workspace_id)] = inspected
        return inspected

    async def build_context(self, request: RepoContextRequest) -> RepoContextResult:
        workspace = self._require_workspace(request.workspace_id)
        warnings: list[str] = []
        git_repo, repo_io = self._open_git_repo(workspace)

        context_files, other_files, selection_warnings = self._select_context_files(
            workspace,
            request.target_paths,
            request.max_files,
            git_repo,
        )
        warnings.extend(selection_warnings)

        if git_repo is None:
            warnings.append(self._warning(ErrorCode.WORKSPACE_NOT_GIT_REPO, workspace.root_path))
        elif getattr(git_repo, "git_repo_error", None):
            warnings.append(self._warning(ErrorCode.REPO_INDEX_FAILED, str(git_repo.git_repo_error)))

        root = Path(workspace.root_path)
        file_summaries = tuple(
            await self.summarize_files(
                workspace,
                tuple(self._rel_path(root, path) for path in context_files),
            )
        )

        repo_map = None
        if other_files:
            try:
                mentioned_fnames = {Path(path).name for path in request.target_paths}
                mentioned_idents = set(IDENT_PATTERN.findall(request.prompt or ""))
                repo_map = self._build_repo_map(
                    workspace,
                    context_files,
                    other_files,
                    mentioned_fnames,
                    mentioned_idents,
                )
            except Exception as exc:
                warnings.append(self._warning(ErrorCode.REPO_INDEX_FAILED, str(exc)))

        if repo_io.errors:
            warnings.extend(self._warning(ErrorCode.REPO_INDEX_FAILED, error) for error in repo_io.errors)
        if repo_io.warnings:
            warnings.extend(self._warning(ErrorCode.REPO_INDEX_STALE, warn) for warn in repo_io.warnings)

        dependency_hints = tuple(
            _filter_important_files(
                [self._rel_path(root, path) for path in context_files + other_files]
            )[:10]
        )

        return RepoContextResult(
            workspace_id=request.workspace_id,
            run_id=request.run_id,
            file_summaries=file_summaries,
            repo_map=repo_map,
            dependency_hints=dependency_hints,
            warnings=tuple(warnings),
        )

    async def refresh_index(self, workspace: WorkspaceRef, changed_files: Sequence[str]) -> None:
        # TODO: Replace this validation-only no-op when repo_intelligence owns durable index state.
        inspected = await self.inspect_workspace(workspace)
        root = Path(inspected.root_path)
        for changed_file in changed_files:
            self._resolve_workspace_member(root, changed_file, must_exist=False)

    async def summarize_files(
        self,
        workspace: WorkspaceRef,
        files: Sequence[str],
    ) -> Sequence[FileSummary]:
        root = Path(workspace.root_path)
        summaries: list[FileSummary] = []
        for raw_file in files:
            try:
                path = self._resolve_workspace_member(root, raw_file)
            except ErrorCodeContractError as exc:
                summaries.append(FileSummary(path=str(raw_file), summary=exc.error_code.value))
                continue

            rel_path = self._rel_path(root, path)
            language = _filename_to_lang(path.name) or path.suffix.lstrip(".") or None
            size = path.stat().st_size

            if size > self.max_file_bytes:
                summaries.append(
                    FileSummary(
                        path=rel_path,
                        summary=f"skipped: {ErrorCode.WORKSPACE_FILE_TOO_LARGE.value} ({size} bytes)",
                        language=language,
                    )
                )
                continue

            if self._is_generated_or_vendor_file(rel_path):
                summaries.append(
                    FileSummary(
                        path=rel_path,
                        summary=f"skipped: {ErrorCode.WORKSPACE_GENERATED_OR_VENDOR_FILE.value}",
                        language=language,
                    )
                )
                continue

            if self._is_binary_file(path):
                summaries.append(
                    FileSummary(
                        path=rel_path,
                        summary=f"skipped: {ErrorCode.WORKSPACE_BINARY_FILE.value}",
                        language=language,
                    )
                )
                continue

            snippet = self._read_text_snippet(path)
            summary = f"{size} bytes"
            if snippet:
                summary = f"{summary} | {snippet}"
            summaries.append(FileSummary(path=rel_path, summary=summary, language=language))

        return summaries

    async def search_symbols(self, workspace: WorkspaceRef, query: str) -> Sequence[SymbolMatch]:
        if not query.strip():
            return ()

        git_repo, _repo_io = self._open_git_repo(workspace)
        files, _warnings = self._list_workspace_files(workspace, git_repo)
        repo_map = self._new_repo_map(workspace)
        if repo_map is None:
            raise ErrorCodeContractError(
                ErrorCode.SYMBOL_SEARCH_FAILED,
                f"Symbol search dependencies are unavailable for query {query!r}.",
                details={"query": query},
            )

        root = Path(workspace.root_path)
        matches: list[SymbolMatch] = []
        needle = query.lower()
        try:
            for path in files[:DEFAULT_OTHER_FILES_LIMIT]:
                rel_path = self._rel_path(root, path)
                for tag in repo_map.get_tags(str(path), rel_path):
                    if needle not in tag.name.lower():
                        continue
                    matches.append(
                        SymbolMatch(
                            name=tag.name,
                            kind=tag.kind,
                            path=rel_path,
                            line=(tag.line + 1) if tag.line >= 0 else None,
                        )
                    )
                    if len(matches) >= MAX_SYMBOL_RESULTS:
                        return tuple(matches)
        except Exception as exc:
            raise ErrorCodeContractError(
                ErrorCode.SYMBOL_SEARCH_FAILED,
                f"Unable to search symbols for query {query!r}.",
                details={"query": query, "reason": str(exc)},
            ) from exc

        return tuple(matches)

    async def analyze_impact(
        self,
        workspace: WorkspaceRef,
        files: Sequence[str],
    ) -> ImpactAnalysis:
        root = Path(workspace.root_path)
        normalized: list[str] = []
        warnings: list[str] = []
        for raw_file in files:
            try:
                path = self._resolve_workspace_member(root, raw_file)
            except ErrorCodeContractError as exc:
                warnings.append(self._warning(exc.error_code, str(raw_file)))
                continue
            normalized.append(self._rel_path(root, path))

        # TODO: Replace this echo model with dependency-aware impact analysis after repo index state
        # becomes durable and can be queried by agent_core/runtime.
        return ImpactAnalysis(
            changed_paths=tuple(normalized),
            impacted_paths=tuple(normalized),
            warnings=tuple(warnings),
        )

    async def watch_workspace(self, workspace: WorkspaceRef) -> WatchSubscription:
        root = Path(workspace.root_path)
        ignore_spec = _load_gitignores(self._gitignore_paths(root))
        watched_paths: list[str] = []
        for child in root.iterdir():
            rel_path = child.relative_to(root).as_posix() + ("/" if child.is_dir() else "")
            if ignore_spec and ignore_spec.match_file(rel_path):
                continue
            watched_paths.append(str(child))

        if not watched_paths:
            watched_paths = [str(root)]

        # TODO: Bind this descriptor to a live watcher once execution_runtime manages subscriptions.
        return WatchSubscription(
            workspace_id=workspace.workspace_id,
            subscription_id=f"watch_{workspace.workspace_id}",
            watched_paths=tuple(watched_paths),
        )

    def _resolve_workspace_root(self, raw_root: Path) -> Path:
        try:
            resolved = raw_root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                f"Workspace path does not exist: {raw_root}",
                details={"path": str(raw_root)},
            ) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_PATH_INVALID,
                f"Workspace path is invalid: {raw_root}",
                details={"path": str(raw_root)},
            ) from exc

        if not resolved.is_dir():
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_PATH_INVALID,
                f"Workspace path must be a directory: {raw_root}",
                details={"path": str(raw_root)},
            )

        return resolved

    def _detect_git_workspace(self, resolved_root: Path) -> tuple[Path, str | None, str | None]:
        any_git_error, _git_repo_cls, git_module = _load_git_backend()
        if git_module is not None:
            try:
                repo = git_module.Repo(resolved_root, search_parent_directories=True)
                git_root = Path(_safe_abs_path(repo.working_tree_dir))
                branch = None
                commit_sha = None
                try:
                    branch = repo.active_branch.name
                except (TypeError, ValueError, AttributeError) + any_git_error:
                    branch = None
                try:
                    commit_sha = repo.head.commit.hexsha
                except (TypeError, ValueError, AttributeError) + any_git_error:
                    commit_sha = None
                return git_root, branch, commit_sha
            except any_git_error:
                return resolved_root, None, None

        git_root = self._run_git(resolved_root, "rev-parse", "--show-toplevel")
        if not git_root:
            return resolved_root, None, None
        branch = self._run_git(resolved_root, "branch", "--show-current")
        commit_sha = self._run_git(resolved_root, "rev-parse", "HEAD")
        return Path(git_root), branch, commit_sha

    def _require_workspace(self, workspace_id: str) -> WorkspaceRef:
        workspace = self._workspaces.get(str(workspace_id))
        if workspace is None:
            raise ErrorCodeContractError(
                ErrorCode.NOT_FOUND,
                f"Workspace {workspace_id} has not been inspected.",
                details={"workspace_id": str(workspace_id)},
            )
        return workspace

    def _open_git_repo(self, workspace: WorkspaceRef) -> tuple[Any | None, _RepoServiceIO]:
        _any_git_error, git_repo_cls, _git_module = _load_git_backend()
        io = _RepoServiceIO()
        if git_repo_cls is None:
            return None, io
        try:
            repo = git_repo_cls(io=io, fnames=[], git_dname=workspace.root_path)
        except FileNotFoundError:
            return None, io
        return repo, io

    def _list_workspace_files(
        self,
        workspace: WorkspaceRef,
        git_repo: Any | None,
    ) -> tuple[list[Path], list[str]]:
        root = Path(workspace.root_path)
        raw_paths: list[Path] = []
        warnings: list[str] = []
        if git_repo is not None:
            for rel_path in git_repo.get_tracked_files():
                raw_paths.append(root / rel_path)
        else:
            raw_paths.extend(Path(path) for path in _find_src_files(str(root)))
        return self._filter_workspace_files(root, raw_paths, warnings)

    def _select_context_files(
        self,
        workspace: WorkspaceRef,
        target_paths: Sequence[str],
        max_files: int | None,
        git_repo: Any | None,
    ) -> tuple[list[Path], list[Path], list[str]]:
        root = Path(workspace.root_path)
        available_files, warnings = self._list_workspace_files(workspace, git_repo)
        available_by_resolved = {path.resolve(): path for path in available_files}

        requested_files: list[Path] = []
        for target_path in target_paths:
            for path in self._expand_target_path(root, target_path):
                try:
                    resolved = path.resolve(strict=True)
                except FileNotFoundError:
                    continue
                if resolved in available_by_resolved:
                    requested_files.append(available_by_resolved[resolved])

        requested_files = self._dedupe_paths(requested_files)
        important_files = self._important_files(root, available_files)
        max_primary = max_files or self.default_max_files

        if requested_files:
            context_files = requested_files[:max_primary]
            if len(requested_files) > max_primary:
                warnings.append(
                    self._warning(
                        ErrorCode.REPO_CONTEXT_OVERFLOW,
                        f"trimmed target file set from {len(requested_files)} to {max_primary}",
                    )
                )
        else:
            context_files = important_files[:max_primary]
            if len(context_files) < max_primary:
                for path in available_files:
                    if path in context_files:
                        continue
                    context_files.append(path)
                    if len(context_files) >= max_primary:
                        break

        context_files = self._dedupe_paths(context_files)
        context_resolved = {path.resolve() for path in context_files}
        remaining_files = [path for path in available_files if path.resolve() not in context_resolved]
        remaining_files = self._dedupe_paths(self._important_files(root, remaining_files) + remaining_files)

        other_limit = max(max_primary * 4, DEFAULT_OTHER_FILES_LIMIT)
        other_files = remaining_files[:other_limit]
        if len(remaining_files) > other_limit:
            warnings.append(
                self._warning(
                    ErrorCode.REPO_CONTEXT_OVERFLOW,
                    f"trimmed background context from {len(remaining_files)} to {other_limit}",
                )
            )

        return context_files, other_files, warnings

    def _filter_workspace_files(
        self,
        root: Path,
        raw_paths: Iterable[Path],
        warnings: list[str],
    ) -> tuple[list[Path], list[str]]:
        ignore_spec = _load_gitignores(self._gitignore_paths(root))
        kept: list[Path] = []
        seen: set[str] = set()
        for raw_path in raw_paths:
            try:
                path = raw_path.resolve(strict=True)
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError, ValueError):
                warnings.append(self._warning(ErrorCode.WORKSPACE_PATH_INVALID, str(raw_path)))
                continue

            if not path.is_file():
                continue

            try:
                rel_path = path.relative_to(root).as_posix()
            except ValueError:
                warnings.append(self._warning(ErrorCode.WORKSPACE_SYMLINK_ESCAPE, str(raw_path)))
                continue

            if ignore_spec and ignore_spec.match_file(rel_path):
                continue
            if self._is_generated_or_vendor_file(rel_path):
                warnings.append(self._warning(ErrorCode.WORKSPACE_GENERATED_OR_VENDOR_FILE, rel_path))
                continue
            if rel_path in seen:
                continue

            size = path.stat().st_size
            if size > self.max_file_bytes:
                warnings.append(self._warning(ErrorCode.WORKSPACE_FILE_TOO_LARGE, rel_path))
                continue
            if self._is_binary_file(path):
                warnings.append(self._warning(ErrorCode.WORKSPACE_BINARY_FILE, rel_path))
                continue

            kept.append(path)
            seen.add(rel_path)

        return kept, warnings

    def _expand_target_path(self, root: Path, target_path: str) -> list[Path]:
        path = self._resolve_workspace_member(root, target_path, must_exist=False)
        if not path.exists():
            return []
        if path.is_dir():
            return [Path(found) for found in _find_src_files(str(path))]
        return [path]

    def _resolve_workspace_member(
        self,
        root: Path,
        raw_path: str,
        *,
        must_exist: bool = True,
    ) -> Path:
        try:
            candidate = (root / raw_path).resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                f"Workspace member does not exist: {raw_path}",
                details={"path": raw_path},
            ) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_PATH_INVALID,
                f"Workspace member path is invalid: {raw_path}",
                details={"path": raw_path},
            ) from exc

        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ErrorCodeContractError(
                ErrorCode.WORKSPACE_SYMLINK_ESCAPE,
                f"Path escapes the workspace root: {raw_path}",
                details={"path": raw_path},
            ) from exc

        return candidate

    def _important_files(self, root: Path, files: Sequence[Path]) -> list[Path]:
        if not files:
            return []
        important_rel = _filter_important_files([self._rel_path(root, path) for path in files])
        return [path for path in files if self._rel_path(root, path) in important_rel]

    def _build_repo_map(
        self,
        workspace: WorkspaceRef,
        context_files: Sequence[Path],
        other_files: Sequence[Path],
        mentioned_fnames: set[str],
        mentioned_idents: set[str],
    ) -> str | None:
        repo_map = self._new_repo_map(workspace)
        if repo_map is None:
            return None
        return repo_map.get_repo_map(
            chat_files=[str(path) for path in context_files],
            other_files=[str(path) for path in other_files],
            mentioned_fnames=mentioned_fnames,
            mentioned_idents=mentioned_idents,
        )

    def _new_repo_map(self, workspace: WorkspaceRef) -> Any | None:
        try:
            from pyclaw.repomap import RepoMap
        except Exception:
            return None
        return RepoMap(
            map_tokens=self.map_tokens,
            root=workspace.root_path,
            main_model=_ApproxTokenCounter(),
            io=_RepoServiceIO(),
            refresh="files",
        )

    def _gitignore_paths(self, root: Path) -> list[Path]:
        candidates = [root / ".gitignore"]
        git_exclude = root / ".git" / "info" / "exclude"
        if git_exclude.exists():
            candidates.append(git_exclude)
        return candidates

    def _is_binary_file(self, path: Path) -> bool:
        if _is_image_name(path.name):
            return True

        try:
            chunk = path.read_bytes()[:4096]
        except OSError:
            return True

        if b"\x00" in chunk:
            return True

        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True

        return False

    def _is_generated_or_vendor_file(self, rel_path: str) -> bool:
        normalized = rel_path.replace("\\", "/")
        if any(marker in normalized for marker in GENERATED_OR_VENDOR_MARKERS):
            return True
        name = Path(normalized).name
        return name.endswith((".min.js", ".map", ".pyc"))

    def _read_text_snippet(self, path: Path) -> str | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None

        snippet = " ".join(lines[:MAX_SNIPPET_LINES])[:MAX_SNIPPET_CHARS]
        return snippet or None

    def _dedupe_paths(self, paths: Sequence[Path]) -> list[Path]:
        seen: set[str] = set()
        result: list[Path] = []
        for path in paths:
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    def _rel_path(self, root: Path, path: Path) -> str:
        return path.resolve().relative_to(root).as_posix()

    def _warning(self, code: ErrorCode, detail: str) -> str:
        return f"{code.value}: {detail}"

    def _run_git(self, root: Path, *args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None
