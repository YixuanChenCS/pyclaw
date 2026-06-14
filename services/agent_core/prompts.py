"""Prompt scaffolding for future headless agent-core planning."""

from __future__ import annotations

from json import dumps

from .models import AgentSession

SYSTEM_PROMPT_TEMPLATE = """You are the headless agent_core orchestration layer.
You create plans and propose structured actions.
You do not execute commands, apply patches, or interact with a terminal directly.
"""

CREATE_PLAN_PROMPT_TEMPLATE = """Create a high-level execution plan for the request below.

Rules:
- Return JSON only.
- Do not claim that work has already been done.
- Do not produce file patches.
- Do not produce shell commands as executable output.
- You may include command-oriented work only as a high-level planned step with kind "command".
- Keep steps structured and high-level.
- Use only these step kinds: inspect, patch, command, approval, summarize, complete.
- Use the exact step-kind strings above only. Do not use synonyms such as "approve"; use "approval".

Return a JSON object with this shape:
{{
  "goal": "short plan goal",
  "steps": [
    {{
      "kind": "inspect",
      "description": "high-level step description",
      "target_files": ["optional/path.py"],
      "rationale": "optional reason"
    }}
  ]
}}

Session:
{session_outline}

Repo context:
{repo_context}

Prior artifacts:
{prior_artifacts}

Context budget:
{context_budget}
"""

GENERATE_PATCH_PROMPT_TEMPLATE = """Generate structured search/replace patch edits for the requested change below.

Rules:
- Return JSON only.
- Do not claim the patch has been applied.
- Treat your output as untrusted edit intent, not as an executable patch.
- Produce exactly one JSON object with these required string fields:
  - "path"
  - "search"
  - "replace"
- Only modify these target files: {target_files}
- Do not reference files outside the target files.
- "path" must name exactly one target file.
- "search" must be the exact old text to find once.
- "replace" must be the exact replacement text.
- Do not invent @@ hunk headers.
- Do not return unified diff text.
- Do not return arrays, wrappers, prose, or explanations.
- Do not delete files.
- Use the exact target file contents below as the source of truth for patch hunks.
- Preserve unchanged lines and indentation exactly.
- If there were recent failures, correct them instead of repeating the same mistake.

Return a JSON object with this shape:
{{
  "path": "file.py",
  "search": "old text",
  "replace": "new text"
}}

Session:
{session_outline}

Current patch action:
{action_outline}

Current plan:
{current_plan}

Recent failures:
{recent_failures}

Target file contents:
{target_file_contents}

Repo context:
{repo_context}
"""

GENERATE_COMMAND_PROMPT_TEMPLATE = """Generate a concrete shell command payload for the requested command step below.

Rules:
- Return JSON only.
- Do not claim the command has been executed.
- Produce exactly one JSON object with a non-empty "command_argv" array of strings.
- Keep the command high-signal and minimal for the requested verification step.
- Do not include shell wrappers unless they are necessary.
- If a working directory is needed, provide it as "cwd"; otherwise omit it or set it to null.

Return a JSON object with this shape:
{{
  "command_argv": ["python", "-m", "unittest", "tests.unit.test_example"],
  "cwd": null
}}

Session:
{session_outline}

Current command action:
{action_outline}

Current plan:
{current_plan}

Repo context:
{repo_context}
"""

PLANNED_FUTURE_PHASES = (
    "create_plan",
    "next_action",
    "strict_validation",
    "review_patch",
    "summarize_run",
    "runtime_integration",
)


def render_session_outline(session: AgentSession) -> str:
    """Return a deterministic summary of session state for future prompt builders."""

    return (
        f"run_id={session.run_id}\n"
        f"workspace_id={session.workspace_id}\n"
        f"iteration_count={session.iteration_count}\n"
        f"user_request={session.user_request}"
    )


def render_create_plan_prompt(session: AgentSession) -> str:
    repo_context = "none"
    if session.repo_context is not None:
        repo_context = dumps(session.repo_context.to_dict(), sort_keys=True)

    prior_artifacts = "[]"
    if session.prior_artifacts:
        prior_artifacts = dumps(
            [artifact.to_dict() for artifact in session.prior_artifacts],
            sort_keys=True,
        )

    context_budget = "none"
    if session.context_budget is not None:
        context_budget = dumps(session.context_budget.to_dict(), sort_keys=True)

    return CREATE_PLAN_PROMPT_TEMPLATE.format(
        session_outline=render_session_outline(session),
        repo_context=repo_context,
        prior_artifacts=prior_artifacts,
        context_budget=context_budget,
    )


def render_generate_patch_prompt(session: AgentSession, proposed_action) -> str:
    repo_context = "none"
    if session.repo_context is not None:
        repo_context = dumps(session.repo_context.to_dict(), sort_keys=True)

    current_plan = "none"
    if session.current_plan is not None:
        current_plan = dumps(session.current_plan.to_dict(), sort_keys=True)

    action_outline = dumps(proposed_action.to_dict(), sort_keys=True)
    target_files = dumps(list(proposed_action.target_files), sort_keys=True)
    target_file_contents = _render_target_file_contents(session, proposed_action.target_files)
    recent_failures = _render_recent_failures(session)

    return GENERATE_PATCH_PROMPT_TEMPLATE.format(
        session_outline=render_session_outline(session),
        action_outline=action_outline,
        current_plan=current_plan,
        recent_failures=recent_failures,
        repo_context=repo_context,
        target_files=target_files,
        target_file_contents=target_file_contents,
    )


def render_generate_command_prompt(session: AgentSession, proposed_action) -> str:
    repo_context = "none"
    if session.repo_context is not None:
        repo_context = dumps(session.repo_context.to_dict(), sort_keys=True)

    current_plan = "none"
    if session.current_plan is not None:
        current_plan = dumps(session.current_plan.to_dict(), sort_keys=True)

    action_outline = dumps(proposed_action.to_dict(), sort_keys=True)

    return GENERATE_COMMAND_PROMPT_TEMPLATE.format(
        session_outline=render_session_outline(session),
        action_outline=action_outline,
        current_plan=current_plan,
        repo_context=repo_context,
    )


def _render_target_file_contents(session: AgentSession, target_files: tuple[str, ...]) -> str:
    if session.repo_context is None or not session.repo_context.file_summaries:
        return "none"

    summaries_by_path = {item.path: item for item in session.repo_context.file_summaries}
    rendered_blocks: list[str] = []
    for path in target_files:
        summary = summaries_by_path.get(path)
        if summary is None:
            continue
        if summary.content is not None:
            rendered_blocks.append(f"FILE: {path}\n```\n{summary.content}\n```")
            continue
        rendered_blocks.append(f"FILE: {path}\nSUMMARY: {summary.summary or 'none'}")

    if not rendered_blocks:
        return "none"
    return "\n\n".join(rendered_blocks)


def _render_recent_failures(session: AgentSession) -> str:
    if not session.failure_history:
        return "none"
    recent_failures = [failure.to_dict() for failure in session.failure_history[-3:]]
    return dumps(recent_failures, sort_keys=True)
