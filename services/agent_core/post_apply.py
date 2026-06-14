from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import py_compile
import tempfile
from typing import Protocol, Sequence

from packages.shared_types import ErrorCodeContractError
from services.execution_runtime.patch import apply_file_patch_to_text, parse_unified_diff

from .models import AgentSession


@dataclass(frozen=True, slots=True)
class PostApplyValidationCandidate:
    path: str
    before_text: str
    after_text: str


class PostApplyValidationFailure(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.failure_code = code


class PostApplyValidator(Protocol):
    def validate(self, candidate: PostApplyValidationCandidate) -> None:
        """Validate one post-apply file candidate."""


class PythonCompileValidator:
    def validate(self, candidate: PostApplyValidationCandidate) -> None:
        if not candidate.path.endswith(".py"):
            return

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                encoding="utf-8",
                delete=False,
            ) as handle:
                handle.write(candidate.after_text)
                temp_path = handle.name
            py_compile.compile(temp_path, doraise=True)
        except py_compile.PyCompileError as exc:
            raise PostApplyValidationFailure(
                "post_apply_validation_failed",
                f"Post-apply validation failed for {candidate.path!r}: {exc.msg}",
            ) from exc
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)


def build_post_apply_candidates(
    session: AgentSession,
    *,
    patch_diff: str,
) -> tuple[PostApplyValidationCandidate, ...]:
    repo_context = session.repo_context
    if repo_context is None:
        return ()

    content_by_path = {
        item.path: item.content
        for item in repo_context.file_summaries
        if item.content is not None
    }

    candidates: list[PostApplyValidationCandidate] = []
    for file_patch in parse_unified_diff(patch_diff):
        if file_patch.old_path == "/dev/null":
            before_text = ""
            target_path = file_patch.new_path
        else:
            target_path = file_patch.new_path if file_patch.new_path != "/dev/null" else file_patch.old_path
            before_text = content_by_path.get(target_path)
            if before_text is None:
                continue

        try:
            after_text = apply_file_patch_to_text(file_patch, before_text)
        except ErrorCodeContractError as exc:
            raise PostApplyValidationFailure(
                "post_apply_validation_failed",
                str(exc),
            ) from exc

        candidates.append(
            PostApplyValidationCandidate(
                path=target_path,
                before_text=before_text,
                after_text=after_text,
            )
        )

    return tuple(candidates)


def run_post_apply_validators(
    candidates: Sequence[PostApplyValidationCandidate],
    *,
    validators: Sequence[PostApplyValidator] = (PythonCompileValidator(),),
) -> None:
    for candidate in candidates:
        for validator in validators:
            validator.validate(candidate)
