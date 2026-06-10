# pyclaw

pyclaw is an experimental local coding-agent platform for learning how to refactor a monolithic CLI coding assistant into modular services.

## Status

This repository is an early development skeleton. The current focus is on service boundaries, shared contracts, and local-first agent runtime design.

Current repo-intelligence migration status is tracked in `docs/repo_intelligence_migration_status.md`.

## Architecture

```text
apps/
  cli/
  api/
  dashboard/

services/
  repo_intelligence/
  execution_runtime/
  agent_core/
  ops_observability/

packages/
  shared_types/
  provider_adapters/
```

## Local Execution Runtime

The local execution runtime MVP is implemented under `services/execution_runtime/`.

- durable SQLite-backed runs, events, artifacts, and approvals
- replayable event stream sourced from SQLite
- local command execution with timeout and cancellation handling
- local patch application for workspace files
- run cancellation and explicit finalization
- minimal approval suspension via `RUNNING -> WAITING_FOR_APPROVAL`
- end-to-end integration coverage in `tests/integration/test_execution_runtime_e2e.py`

Architecture details and current limitations are documented in [docs/execution_runtime_architecture.md](docs/execution_runtime_architecture.md). Approval resume, deployment, remote sandboxing, and distributed workers are future work.

## Roadmap

- Finish shared contracts
- Implement repo intelligence
- Implement local execution runtime
- Implement headless agent core
- Add API layer
- Add minimal dashboard
- Add observability and health checks

## Development

```bash
pytest tests/unit -q
pytest tests/integration -q
pytest tests/optional -q
pytest tests/online -q
```
