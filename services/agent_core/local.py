from __future__ import annotations

from collections.abc import Mapping
from typing import Sequence

from packages.shared_types import ArtifactRef, RepoContextResult
from packages.shared_types.ids import RunId, WorkspaceId

from .model_client import ModelClient
from .models import AgentAction, AgentContextBudget, AgentFailure, AgentPlan, AgentSession, AgentStep
from .prompts import render_create_plan_prompt
from .service import AgentCoreService
from .validation import parse_json_object, validate_plan_payload, validate_session_basic_shape


class LocalAgentCoreService(AgentCoreService):
    """Headless local agent-core skeleton with no runtime or model side effects."""

    def __init__(self, *, model_client: ModelClient | None = None) -> None:
        self._model_client = model_client

    @property
    def model_client(self) -> ModelClient | None:
        return self._model_client

    def create_session(
        self,
        *,
        run_id: RunId,
        workspace_id: WorkspaceId,
        user_request: str,
        repo_context: RepoContextResult | None = None,
        current_plan: AgentPlan | None = None,
        prior_artifacts: Sequence[ArtifactRef] = (),
        iteration_count: int = 0,
        failure_history: Sequence[AgentFailure] = (),
        context_budget: AgentContextBudget | None = None,
    ) -> AgentSession:
        session = AgentSession(
            run_id=run_id,
            workspace_id=workspace_id,
            user_request=user_request,
            repo_context=repo_context,
            current_plan=current_plan,
            prior_artifacts=list(prior_artifacts),
            iteration_count=iteration_count,
            failure_history=list(failure_history),
            context_budget=context_budget,
        )
        validate_session_basic_shape(session)
        return session

    async def create_plan(self, session: AgentSession) -> AgentPlan:
        validate_session_basic_shape(session)

        if self._model_client is None:
            raise RuntimeError("LocalAgentCoreService requires a model_client for create_plan")

        prompt = render_create_plan_prompt(session)
        response = await self._model_client.complete_json(prompt)

        if isinstance(response, str):
            payload = parse_json_object(response)
        elif isinstance(response, Mapping):
            payload = response
        else:
            raise TypeError("ModelClient.complete_json must return a JSON string or mapping")

        normalized = validate_plan_payload(payload)
        steps = [
            AgentStep(
                kind=step["kind"],
                description=step["description"],
                target_files=step["target_files"],
                rationale=step["rationale"],
            )
            for step in normalized["steps"]
        ]
        return AgentPlan(
            goal=normalized["goal"],
            steps=steps,
            summary=normalized["summary"],
        )

    async def next_action(self, session: AgentSession) -> AgentAction:
        raise NotImplementedError("next_action is not implemented in this phase")

    async def review_patch(
        self,
        session: AgentSession,
        proposed_action: AgentAction,
    ) -> AgentAction:
        raise NotImplementedError("review_patch is not implemented in this phase")

    async def summarize_run(self, session: AgentSession) -> AgentAction:
        raise NotImplementedError("summarize_run is not implemented in this phase")
