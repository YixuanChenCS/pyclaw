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
