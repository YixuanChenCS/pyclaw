from __future__ import annotations

import ast
from dataclasses import dataclass, replace
import math
from pathlib import Path, PurePosixPath
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

from .important_files import filter_important_files as _select_important_files
from .service import RepoIntelligenceService

DEFAULT_MAP_TOKENS = 2048
DEFAULT_MAX_FILES = 16
DEFAULT_OTHER_FILES_LIMIT = 64
DEFAULT_SYMBOL_SEARCH_FILE_LIMIT = 512
MAX_FILE_BYTES = 1024 * 1024
MAX_SYMBOL_RESULTS = 50
MAX_SNIPPET_LINES = 3
MAX_SNIPPET_CHARS = 240
IDENT_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_-]{2,}\b")
FILE_MENTION_QUOTE_CHARS = "\"'`*_"
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

try:
    from pathspec import PathSpec as _PathSpec
    from pathspec.patterns import GitWildMatchPattern as _GitWildMatchPattern
except ImportError:
    _PathSpec = None
    _GitWildMatchPattern = None

try:
    import git as _git
except ImportError:
    _git = None

_ANY_GIT_ERROR = (
    OSError,
    IndexError,
    BufferError,
    TypeError,
    ValueError,
    AttributeError,
    AssertionError,
    TimeoutError,
)
if _git is not None:
    _ANY_GIT_ERROR = (
        _git.exc.ODBError,
        _git.exc.GitError,
        _git.exc.InvalidGitRepositoryError,
        _git.exc.GitCommandNotFound,
    ) + _ANY_GIT_ERROR

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".pdf"}
DEFAULT_GITIGNORE_PATTERNS = (
    ".pyclaw*",
    ".git",
    "*~",
    "*.bak",
    "*.swp",
    "*.swo",
    "\\#*\\#",
    ".#*",
    "*.tmp",
    "*.temp",
    "*.orig",
    "*.pyc",
    "__pycache__/",
    ".DS_Store",
    "Thumbs.db",
    "*.svg",
    "*.pdf",
    ".idea/",
    ".vscode/",
    "*.sublime-*",
    ".project",
    ".settings/",
    "*.code-workspace",
    ".env",
    ".venv/",
    "node_modules/",
    "vendor/",
    "*.log",
    ".cache/",
    ".pytest_cache/",
    "coverage/",
)
@dataclass(slots=True)
class _StaticFileInfo:
    rel_path: str
    path: Path
    module_name: str | None
    is_package: bool
    imports: frozenset[str]
    symbols: frozenset[str]
    identifiers: frozenset[str]


@dataclass(slots=True)
class _WorkspaceState:
    workspace: WorkspaceRef
    static_info_cache: dict[str, _StaticFileInfo]


class _LocalGitRepo:
    def __init__(self, root: str, io: _RepoServiceIO) -> None:
        self.root = root
        self.io = io
        self.git_repo_error: str | None = None

    def get_tracked_files(self) -> list[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", self.root, "ls-files", "-z", "--cached"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            self.git_repo_error = str(exc)
            self.io.tool_error(f"Unable to list files in git repo: {exc}")
            return []

        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip() or "git ls-files failed"
            self.git_repo_error = error
            self.io.tool_error(f"Unable to list files in git repo: {error}")
            return []

        tracked = []
        for entry in completed.stdout.split("\0"):
            if not entry:
                continue
            tracked.append(str(PurePosixPath(entry)))
        return tracked


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
        return str(Path(path).resolve())
    except Exception:
        return str(Path(path).absolute())


def _is_image_name(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in IMAGE_EXTENSIONS


def _load_gitignores(paths: Sequence[Path]) -> Any:
    if _PathSpec is None or _GitWildMatchPattern is None:
        return None
    patterns = list(DEFAULT_GITIGNORE_PATTERNS)
    for path in paths:
        if not path.exists():
            continue
        try:
            patterns.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return _PathSpec.from_lines(_GitWildMatchPattern, patterns) if patterns else None


def _find_src_files(directory: str) -> list[str]:
    root = Path(directory)
    if root.is_file():
        return [str(root)]
    return [str(path) for path in root.rglob("*") if path.is_file()]


def _filter_important_files(file_paths: Sequence[str]) -> list[str]:
    return _select_important_files(file_paths)


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
        self._workspaces: dict[str, _WorkspaceState] = {}

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
        workspace_key = str(inspected.workspace_id)
        existing = self._workspaces.get(workspace_key)
        static_info_cache: dict[str, _StaticFileInfo] = {}
        if existing is not None and self._can_reuse_workspace_cache(existing.workspace, inspected):
            static_info_cache = existing.static_info_cache
        self._workspaces[workspace_key] = _WorkspaceState(
            workspace=inspected,
            static_info_cache=static_info_cache,
        )
        return inspected

    async def build_context(self, request: RepoContextRequest) -> RepoContextResult:
        workspace = self._require_workspace(request.workspace_id)
        warnings: list[str] = []
        git_repo, repo_io = self._open_git_repo(workspace)
        available_files, inventory_warnings = self._list_workspace_files(workspace, git_repo)
        warnings.extend(inventory_warnings)

        root = Path(workspace.root_path)
        requested_target_paths = tuple(request.target_paths)
        requested_reference_paths = tuple(request.reference_paths)
        mentioned_paths = ()
        if request.auto_context_mentions and request.prompt:
            mentioned_paths = self._find_prompt_file_mentions(
                root,
                request.prompt,
                available_files,
                existing_rel_paths=requested_target_paths + requested_reference_paths,
            )
        effective_target_paths = tuple(
            dict.fromkeys((*requested_target_paths, *mentioned_paths))
        )

        context_files, other_files, selection_warnings = self._select_context_files(
            workspace,
            effective_target_paths,
            request.max_files,
            git_repo,
            available_files=available_files,
        )
        warnings.extend(selection_warnings)
        reference_files, reference_warnings = self._select_reference_files(
            root,
            requested_reference_paths,
            exclude_rel_paths=effective_target_paths,
        )
        warnings.extend(reference_warnings)
        reference_resolved = {path.resolve() for path in reference_files}
        other_files = [
            path for path in other_files
            if path.resolve() not in reference_resolved
        ]

        if git_repo is None:
            warnings.append(self._warning(ErrorCode.WORKSPACE_NOT_GIT_REPO, workspace.root_path))
        elif getattr(git_repo, "git_repo_error", None):
            warnings.append(self._warning(ErrorCode.REPO_INDEX_FAILED, str(git_repo.git_repo_error)))

        file_summaries = tuple(
            await self.summarize_files(
                workspace,
                tuple(self._rel_path(root, path) for path in context_files),
                include_content_paths=effective_target_paths,
            )
        )
        reference_file_summaries = tuple(
            await self.summarize_files(
                workspace,
                tuple(self._rel_path(root, path) for path in reference_files),
                include_content_paths=tuple(self._rel_path(root, path) for path in reference_files),
            )
        )

        repo_map = None
        if other_files:
            try:
                mentioned_fnames = {Path(path).name for path in (*effective_target_paths, *requested_reference_paths)}
                mentioned_idents = set(IDENT_PATTERN.findall(request.prompt or ""))
                repo_map = self._build_repo_map(
                    workspace,
                    self._dedupe_paths([*context_files, *reference_files]),
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
                [
                    self._rel_path(root, path)
                    for path in [*context_files, *reference_files, *other_files]
                ]
            )[:10]
        )

        warnings = self._dedupe_strings(warnings)

        return RepoContextResult(
            workspace_id=request.workspace_id,
            run_id=request.run_id,
            file_summaries=file_summaries,
            reference_file_summaries=reference_file_summaries,
            repo_map=repo_map,
            mentioned_paths=mentioned_paths,
            dependency_hints=dependency_hints,
            warnings=tuple(warnings),
        )

    async def refresh_index(self, workspace: WorkspaceRef, changed_files: Sequence[str]) -> None:
        inspected = await self.inspect_workspace(workspace)
        state = self._require_workspace_state(inspected.workspace_id)
        root = Path(inspected.root_path)
        for changed_file in changed_files:
            try:
                candidate = self._resolve_workspace_member(root, changed_file, must_exist=False)
            except ErrorCodeContractError:
                state.static_info_cache.pop(changed_file, None)
                raise
            rel_path = self._rel_path(root, candidate)
            state.static_info_cache.pop(rel_path, None)

    async def summarize_files(
        self,
        workspace: WorkspaceRef,
        files: Sequence[str],
        include_content_paths: Sequence[str] = (),
    ) -> Sequence[FileSummary]:
        root = Path(workspace.root_path)
        include_content_rel_paths = {
            self._rel_path(root, self._resolve_workspace_member(root, raw_path))
            for raw_path in include_content_paths
        }
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
            content = None
            if rel_path in include_content_rel_paths:
                content = path.read_text(encoding="utf-8", errors="replace")
            summaries.append(
                FileSummary(
                    path=rel_path,
                    summary=summary,
                    language=language,
                    content=content,
                )
            )

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
        seen_matches: set[tuple[str, str, str, int | None]] = set()
        needle = query.lower()
        try:
            for path in self._rank_symbol_search_files(root, files, query):
                rel_path = self._rel_path(root, path)
                for tag in repo_map.get_tags(str(path), rel_path):
                    if needle not in tag.name.lower():
                        continue
                    line = (tag.line + 1) if tag.line >= 0 else None
                    match_key = (tag.name, tag.kind, rel_path, line)
                    if match_key in seen_matches:
                        continue
                    seen_matches.add(match_key)
                    matches.append(
                        SymbolMatch(
                            name=tag.name,
                            kind=tag.kind,
                            path=rel_path,
                            line=line,
                        )
                    )
        except Exception as exc:
            raise ErrorCodeContractError(
                ErrorCode.SYMBOL_SEARCH_FAILED,
                f"Unable to search symbols for query {query!r}.",
                details={"query": query, "reason": str(exc)},
            ) from exc
        finally:
            self._close_repo_map(repo_map)

        matches.sort(key=lambda match: self._symbol_match_rank(match, needle))
        return tuple(matches[:MAX_SYMBOL_RESULTS])

    async def analyze_impact(
        self,
        workspace: WorkspaceRef,
        files: Sequence[str],
    ) -> ImpactAnalysis:
        root = Path(workspace.root_path)
        normalized: list[str] = []
        warnings: list[str] = []
        changed_paths: list[Path] = []
        for raw_file in files:
            try:
                path = self._resolve_workspace_member(root, raw_file)
            except ErrorCodeContractError as exc:
                warnings.append(self._warning(exc.error_code, str(raw_file)))
                continue
            rel_path = self._rel_path(root, path)
            normalized.append(rel_path)
            changed_paths.append(path)

        git_repo, _repo_io = self._open_git_repo(workspace)
        workspace_files, inventory_warnings = self._list_workspace_files(workspace, git_repo)
        warnings.extend(inventory_warnings)

        workspace_by_rel = {self._rel_path(root, path): path for path in workspace_files}
        static_infos, info_warnings = self._collect_static_file_info(root, workspace_files, workspace)
        warnings.extend(info_warnings)

        changed_infos = [
            static_infos[rel_path]
            for rel_path in normalized
            if rel_path in static_infos
        ]
        impacted_paths = list(normalized)
        impacted_seen = set(impacted_paths)

        module_to_paths: dict[str, set[str]] = {}
        for info in static_infos.values():
            if info.module_name:
                module_to_paths.setdefault(info.module_name, set()).add(info.rel_path)

        changed_modules = {info.module_name for info in changed_infos if info.module_name}
        changed_symbols: set[str] = set()
        for info in changed_infos:
            changed_symbols.update(info.symbols)

        for info in changed_infos:
            for imported_module in info.imports:
                for rel_path in self._resolve_local_module_paths(imported_module, module_to_paths):
                    if rel_path not in impacted_seen:
                        impacted_paths.append(rel_path)
                        impacted_seen.add(rel_path)

        for info in static_infos.values():
            if info.rel_path in impacted_seen:
                continue
            if self._imports_changed_module(info.imports, changed_modules):
                impacted_paths.append(info.rel_path)
                impacted_seen.add(info.rel_path)
                continue
            if changed_symbols and changed_symbols.intersection(info.identifiers):
                impacted_paths.append(info.rel_path)
                impacted_seen.add(info.rel_path)

        for changed_path in changed_paths:
            rel_path = self._rel_path(root, changed_path)
            if rel_path in workspace_by_rel:
                continue
            if self._is_generated_or_vendor_file(rel_path):
                warnings.append(self._warning(ErrorCode.WORKSPACE_GENERATED_OR_VENDOR_FILE, rel_path))
            elif changed_path.stat().st_size > self.max_file_bytes:
                warnings.append(self._warning(ErrorCode.WORKSPACE_FILE_TOO_LARGE, rel_path))
            elif self._is_binary_file(changed_path):
                warnings.append(self._warning(ErrorCode.WORKSPACE_BINARY_FILE, rel_path))

        warnings = self._dedupe_strings(warnings)

        return ImpactAnalysis(
            changed_paths=tuple(normalized),
            impacted_paths=tuple(impacted_paths),
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

        # This MVP only returns a watch descriptor for runtime to consume later.
        # It does not start a live watcher or emit change events.
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
        if _git is not None:
            try:
                repo = _git.Repo(resolved_root, search_parent_directories=True)
                git_root = Path(_safe_abs_path(repo.working_tree_dir))
                branch = None
                commit_sha = None
                try:
                    branch = repo.active_branch.name
                except (TypeError, ValueError, AttributeError) + _ANY_GIT_ERROR:
                    branch = None
                try:
                    commit_sha = repo.head.commit.hexsha
                except (TypeError, ValueError, AttributeError) + _ANY_GIT_ERROR:
                    commit_sha = None
                return git_root, branch, commit_sha
            except _ANY_GIT_ERROR:
                return resolved_root, None, None

        git_root = self._run_git(resolved_root, "rev-parse", "--show-toplevel")
        if not git_root:
            return resolved_root, None, None
        branch = self._run_git(resolved_root, "branch", "--show-current")
        commit_sha = self._run_git(resolved_root, "rev-parse", "HEAD")
        return Path(git_root), branch, commit_sha

    def _require_workspace(self, workspace_id: str) -> WorkspaceRef:
        return self._require_workspace_state(workspace_id).workspace

    def _require_workspace_state(self, workspace_id: str) -> _WorkspaceState:
        workspace_state = self._workspaces.get(str(workspace_id))
        if workspace_state is None:
            raise ErrorCodeContractError(
                ErrorCode.NOT_FOUND,
                f"Workspace {workspace_id} has not been inspected.",
                details={"workspace_id": str(workspace_id)},
            )
        return workspace_state

    def _open_git_repo(self, workspace: WorkspaceRef) -> tuple[Any | None, _RepoServiceIO]:
        io = _RepoServiceIO()
        if not workspace.commit_sha and not workspace.branch:
            return None, io
        return _LocalGitRepo(workspace.root_path, io), io

    def _list_workspace_files(
        self,
        workspace: WorkspaceRef,
        git_repo: Any | None,
        *,
        collect_filter_warnings: bool = False,
    ) -> tuple[list[Path], list[str]]:
        root = Path(workspace.root_path)
        raw_paths: list[Path] = []
        warnings: list[str] = []
        if git_repo is not None:
            for rel_path in git_repo.get_tracked_files():
                raw_paths.append(root / rel_path)
        else:
            raw_paths.extend(Path(path) for path in _find_src_files(str(root)))
        return self._filter_workspace_files(
            root,
            raw_paths,
            warnings,
            collect_filter_warnings=collect_filter_warnings,
        )

    def _select_context_files(
        self,
        workspace: WorkspaceRef,
        target_paths: Sequence[str],
        max_files: int | None,
        git_repo: Any | None,
        *,
        available_files: Sequence[Path] | None = None,
    ) -> tuple[list[Path], list[Path], list[str]]:
        root = Path(workspace.root_path)
        if available_files is None:
            available_files, warnings = self._list_workspace_files(workspace, git_repo)
        else:
            available_files = list(available_files)
            warnings = []
        available_by_resolved = {path.resolve(): path for path in available_files}
        ignore_spec = _load_gitignores(self._gitignore_paths(root))

        requested_files: list[Path] = []
        for target_path in target_paths:
            for path in self._expand_target_path(root, target_path):
                try:
                    resolved = path.resolve(strict=True)
                except FileNotFoundError:
                    continue
                if resolved in available_by_resolved:
                    requested_files.append(available_by_resolved[resolved])
                    continue
                warning = self._target_filter_warning(root, resolved, ignore_spec)
                if warning is not None:
                    warnings.append(warning)

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

    def _select_reference_files(
        self,
        root: Path,
        reference_paths: Sequence[str],
        *,
        exclude_rel_paths: Sequence[str] = (),
    ) -> tuple[list[Path], list[str]]:
        reference_files: list[Path] = []
        warnings: list[str] = []
        excluded = set(exclude_rel_paths)
        ignore_spec = _load_gitignores(self._gitignore_paths(root))
        for raw_path in reference_paths:
            try:
                path = self._resolve_workspace_member(root, raw_path)
            except ErrorCodeContractError as exc:
                warnings.append(self._warning(exc.error_code, raw_path))
                continue

            rel_path = self._rel_path(root, path)
            if rel_path in excluded:
                continue

            warning = self._target_filter_warning(root, path, ignore_spec)
            if warning is not None:
                warnings.append(warning)
                continue
            reference_files.append(path)

        return self._dedupe_paths(reference_files), warnings

    def _filter_workspace_files(
        self,
        root: Path,
        raw_paths: Iterable[Path],
        warnings: list[str],
        *,
        collect_filter_warnings: bool = False,
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
                if collect_filter_warnings:
                    warnings.append(self._warning(ErrorCode.WORKSPACE_GENERATED_OR_VENDOR_FILE, rel_path))
                continue
            if rel_path in seen:
                continue

            size = path.stat().st_size
            if size > self.max_file_bytes:
                if collect_filter_warnings:
                    warnings.append(self._warning(ErrorCode.WORKSPACE_FILE_TOO_LARGE, rel_path))
                continue
            if self._is_binary_file(path):
                if collect_filter_warnings:
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

    def _find_prompt_file_mentions(
        self,
        root: Path,
        prompt: str,
        available_files: Sequence[Path],
        *,
        existing_rel_paths: Sequence[str] = (),
    ) -> tuple[str, ...]:
        words = self._prompt_words(prompt)
        if not words:
            return ()

        normalized_words = {word.replace("\\", "/") for word in words}
        existing_rel_path_set = set(existing_rel_paths)
        existing_basenames = {Path(path).name for path in existing_rel_paths}
        mentioned_rel_paths: set[str] = set()
        basename_to_paths: dict[str, list[str]] = {}

        for path in available_files:
            rel_path = self._rel_path(root, path)
            if rel_path in existing_rel_path_set:
                continue

            normalized_rel_path = rel_path.replace("\\", "/")
            if normalized_rel_path in normalized_words:
                mentioned_rel_paths.add(rel_path)

            basename = Path(rel_path).name
            if any(marker in basename for marker in (".", "_", "-")):
                basename_to_paths.setdefault(basename, []).append(rel_path)

        for basename, rel_paths in basename_to_paths.items():
            if basename in existing_basenames:
                continue
            if len(rel_paths) == 1 and basename in words:
                mentioned_rel_paths.add(rel_paths[0])

        return tuple(sorted(mentioned_rel_paths))

    def _prompt_words(self, prompt: str) -> set[str]:
        words = {word for word in prompt.split() if word}
        words = {word.rstrip(",.!;:?") for word in words}
        words = {word.strip(FILE_MENTION_QUOTE_CHARS) for word in words}
        return {word for word in words if word}

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
        try:
            return repo_map.get_repo_map(
                chat_files=[str(path) for path in context_files],
                other_files=[str(path) for path in other_files],
                mentioned_fnames=mentioned_fnames,
                mentioned_idents=mentioned_idents,
            )
        finally:
            self._close_repo_map(repo_map)

    def _new_repo_map(self, workspace: WorkspaceRef) -> Any | None:
        try:
            from .repomap import RepoMap
        except ImportError:
            return None
        return RepoMap(
            map_tokens=self.map_tokens,
            root=workspace.root_path,
            main_model=_ApproxTokenCounter(),
            io=_RepoServiceIO(),
            refresh="files",
        )

    def _close_repo_map(self, repo_map: Any | None) -> None:
        if repo_map is None:
            return
        close = getattr(repo_map, "close", None)
        if callable(close):
            close()

    def _target_filter_warning(
        self,
        root: Path,
        path: Path,
        ignore_spec: Any,
    ) -> str | None:
        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            return self._warning(ErrorCode.WORKSPACE_SYMLINK_ESCAPE, str(path))

        if ignore_spec and ignore_spec.match_file(rel_path):
            return None
        if self._is_generated_or_vendor_file(rel_path):
            return self._warning(ErrorCode.WORKSPACE_GENERATED_OR_VENDOR_FILE, rel_path)

        size = path.stat().st_size
        if size > self.max_file_bytes:
            return self._warning(ErrorCode.WORKSPACE_FILE_TOO_LARGE, rel_path)
        if self._is_binary_file(path):
            return self._warning(ErrorCode.WORKSPACE_BINARY_FILE, rel_path)
        return None

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

    def _collect_static_file_info(
        self,
        root: Path,
        files: Sequence[Path],
        workspace: WorkspaceRef | None = None,
    ) -> tuple[dict[str, _StaticFileInfo], list[str]]:
        infos: dict[str, _StaticFileInfo] = {}
        warnings: list[str] = []
        cache = self._static_info_cache_for(workspace)
        for path in files:
            rel_path = self._rel_path(root, path)
            info = cache.get(rel_path)
            if info is None:
                info = self._build_static_file_info(root, path, warnings)
                cache[rel_path] = info
            infos[info.rel_path] = info
        return infos, warnings

    def _build_static_file_info(
        self,
        root: Path,
        path: Path,
        warnings: list[str],
    ) -> _StaticFileInfo:
        rel_path = self._rel_path(root, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(self._warning(ErrorCode.REPO_INDEX_FAILED, f"{rel_path}: {exc}"))
            text = ""

        module_name = self._module_name_for_path(root, path)
        is_package = path.name == "__init__.py"
        imports: set[str] = set()
        symbols: set[str] = set()
        identifiers = list(dict.fromkeys(IDENT_PATTERN.findall(text)))

        if path.suffix == ".py" and text:
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                warnings.append(self._warning(ErrorCode.REPO_INDEX_FAILED, f"{rel_path}: {exc.msg}"))
            else:
                imports, symbols = self._extract_python_symbols_and_imports(
                    tree,
                    module_name,
                    is_package,
                )

        return _StaticFileInfo(
            rel_path=rel_path,
            path=path,
            module_name=module_name,
            is_package=is_package,
            imports=frozenset(imports),
            symbols=frozenset(symbols),
            identifiers=frozenset(identifiers[:256]),
        )

    def _module_name_for_path(self, root: Path, path: Path) -> str | None:
        if path.suffix != ".py":
            return None
        parts = list(path.relative_to(root).with_suffix("").parts)
        if not parts:
            return None
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            return None
        return ".".join(parts)

    def _extract_python_symbols_and_imports(
        self,
        tree: ast.AST,
        module_name: str | None,
        is_package: bool,
    ) -> tuple[set[str], set[str]]:
        imports: set[str] = set()
        symbols: set[str] = set()

        for node in getattr(tree, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(node.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
                continue

            if not isinstance(node, ast.ImportFrom):
                continue

            resolved_module = self._resolve_import_from_module(
                module_name,
                is_package,
                node.module,
                node.level,
            )
            if not resolved_module:
                continue
            imports.add(resolved_module)
            for alias in node.names:
                if alias.name != "*":
                    imports.add(f"{resolved_module}.{alias.name}")

        return imports, symbols

    def _resolve_import_from_module(
        self,
        module_name: str | None,
        is_package: bool,
        imported_module: str | None,
        level: int,
    ) -> str | None:
        if level <= 0:
            return imported_module

        if not module_name:
            return imported_module

        current_parts = module_name.split(".")
        base_parts = current_parts if is_package else current_parts[:-1]
        trim = max(level - 1, 0)
        if trim > len(base_parts):
            base_parts = []
        elif trim:
            base_parts = base_parts[:-trim]

        imported_parts = imported_module.split(".") if imported_module else []
        resolved_parts = [part for part in [*base_parts, *imported_parts] if part]
        return ".".join(resolved_parts) or None

    def _resolve_local_module_paths(
        self,
        module_name: str,
        module_to_paths: dict[str, set[str]],
    ) -> list[str]:
        matches: list[str] = []
        seen: set[str] = set()
        parts = module_name.split(".")
        for size in range(len(parts), 0, -1):
            candidate = ".".join(parts[:size])
            for rel_path in module_to_paths.get(candidate, ()):
                if rel_path in seen:
                    continue
                matches.append(rel_path)
                seen.add(rel_path)
        return matches

    def _imports_changed_module(
        self,
        imports: frozenset[str],
        changed_modules: set[str],
    ) -> bool:
        for imported_module in imports:
            for changed_module in changed_modules:
                if imported_module == changed_module:
                    return True
                if imported_module.startswith(f"{changed_module}."):
                    return True
                if changed_module.startswith(f"{imported_module}."):
                    return True
        return False

    def _static_info_cache_for(
        self,
        workspace: WorkspaceRef | None,
    ) -> dict[str, _StaticFileInfo]:
        if workspace is None:
            return {}
        state = self._workspaces.get(str(workspace.workspace_id))
        if state is None:
            return {}
        return state.static_info_cache

    def _can_reuse_workspace_cache(
        self,
        existing: WorkspaceRef,
        updated: WorkspaceRef,
    ) -> bool:
        if existing.root_path != updated.root_path:
            return False
        if existing.branch != updated.branch:
            return False
        if existing.commit_sha != updated.commit_sha:
            return False
        return True

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

    def _dedupe_strings(self, values: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _rank_symbol_search_files(
        self,
        root: Path,
        files: Sequence[Path],
        query: str,
    ) -> list[Path]:
        needle = query.lower()
        important_files = {
            str(path.resolve())
            for path in self._important_files(root, files)
        }

        def rank_key(path: Path) -> tuple[int, int, int, int, str]:
            rel_path = self._rel_path(root, path)
            path_key = rel_path.lower()
            return (
                0 if needle in path.name.lower() else 1,
                0 if needle in path_key else 1,
                self._symbol_search_scope_rank(rel_path),
                0 if _filename_to_lang(path.name) else 1,
                0 if str(path.resolve()) in important_files else 1,
                path_key,
            )

        ranked = sorted(self._dedupe_paths(files), key=rank_key)
        return ranked[:DEFAULT_SYMBOL_SEARCH_FILE_LIMIT]

    def _symbol_match_rank(self, match: SymbolMatch, needle: str) -> tuple[int, int, int, int, str, int]:
        name_key = match.name.lower()
        path_key = match.path.lower()
        return (
            0 if name_key == needle else 1,
            0 if name_key.startswith(needle) else 1,
            self._symbol_search_scope_rank(match.path),
            0 if match.kind == "def" else 1,
            path_key,
            match.line or 0,
        )

    def _symbol_search_scope_rank(self, rel_path: str) -> int:
        normalized = rel_path.replace("\\", "/")
        if normalized.startswith("services/"):
            return 0
        if normalized.startswith("packages/"):
            return 1
        if normalized.startswith("apps/"):
            return 2
        if normalized.startswith("scripts/"):
            return 3
        if normalized.startswith("pyclaw/"):
            return 4
        if normalized.startswith("tests/"):
            return 5
        return 6

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
