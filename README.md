# pyclaw

pyclaw is an experimental local coding-agent platform for learning how to refactor a monolithic CLI coding assistant into modular services.

## Status

This repository is an early development skeleton. The current focus is on service boundaries, shared contracts, and local-first agent runtime design.

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
