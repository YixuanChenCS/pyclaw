# API Smoke Test

## Start The Server

Run the local FastAPI control plane with a project-matched Python 3.10+ interpreter:

```bash
export PYCLAW_API_WORKSPACE_ROOTS="$PWD"
export PYCLAW_API_DB_PATH="$PWD/.execution_runtime/runtime.sqlite3"
export PYCLAW_API_BEARER_TOKEN="local-dev-token"
uv run --with uvicorn uvicorn apps.api.main:app --reload
```

Using `--with uvicorn` avoids falling back to a globally installed `uvicorn` binary that may be bound to an unsupported Python version.

`GET /health` does not require auth. All other routes require `Authorization: Bearer $PYCLAW_API_BEARER_TOKEN` when `PYCLAW_API_BEARER_TOKEN` is set.

## Execution Model

`POST /runs` only enqueues a run in `execution_runtime`.

- the API process persists the queued run in SQLite
- the API process does not start a background worker loop on FastAPI startup
- nothing in `apps.api.main` or `create_app()` automatically calls `claim_next_run()` or `coordinator.run()`
- a separate caller must claim and execute the run

Today, the built-in consumer is the local CLI/coordinator flow, which explicitly claims a run before invoking agent execution.

- enqueue happens through the API or CLI
- claim happens through `execution_runtime.claim_run()` or `claim_next_run()`
- execution happens through `AgentCoreCoordinator.run()`

If you want a standalone API server that also executes queued runs, that requires an additional worker process or an explicit in-process worker loop that is not implemented by the current FastAPI entrypoint.

## Recovery And Scaling

The local runtime does run stale-run recovery, but that is not the same thing as consuming queued work.

- `LocalExecutionRuntimeService._ensure_started()` calls `recover_stale_runs()` on first runtime use in that process
- this can requeue stale runs with no durable side effects
- this can move side-effected stale runs into explicit recovery state instead of replaying them blindly
- recovery does not itself claim and execute newly queued work

Current scaling guidance:

- the current design is local-first and SQLite-backed
- queue claiming is SQLite-atomic, so ownership is durable and not in-memory
- there is no built-in worker supervisor in the API server
- distributed worker orchestration is still future work and should not be assumed from `POST /runs` alone

## Environment Variables

- `PYCLAW_API_BEARER_TOKEN`: Optional. Enables bearer-token auth when set.
- `PYCLAW_API_ALLOWED_ORIGINS`: Optional comma-separated CORS origins such as `http://localhost:3000,http://127.0.0.1:5173`.
- `PYCLAW_API_WORKSPACE_ROOTS`: Optional comma-separated workspace roots allowed by `POST /runs`. If omitted, the API defaults to the current working directory.
- `PYCLAW_API_DB_PATH`: Optional runtime SQLite DB path override for the API entrypoint.
- `EXECUTION_RUNTIME_DB_PATH`: Existing runtime DB env var. Used when `PYCLAW_API_DB_PATH` is not set.

## Health

```bash
curl http://127.0.0.1:8000/health
```

## Create A Run

```bash
curl -X POST http://127.0.0.1:8000/runs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN" \
  -d '{
    "workspace_path": "'"$PWD"'",
    "prompt": "Summarize the current repo status",
    "target_paths": ["apps/api/app.py"]
  }'
```

## List Runs

```bash
curl http://127.0.0.1:8000/runs \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN"
```

## Get A Run

```bash
curl http://127.0.0.1:8000/runs/<run_id> \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN"
```

## Replay Run Events

```bash
curl http://127.0.0.1:8000/runs/<run_id>/events \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN"
```

## Submit An Approval Decision

Current approval decisions are posted to `/approvals/{approval_id}/decision`.

```bash
curl -X POST http://127.0.0.1:8000/approvals/<approval_id>/decision \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN" \
  -d '{
    "decision": "approved",
    "comment": "Looks good"
  }'
```

## List Run Artifacts

```bash
curl http://127.0.0.1:8000/runs/<run_id>/artifacts \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN"
```

## Get An Artifact

```bash
curl http://127.0.0.1:8000/artifacts/<artifact_id> \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN"
```

## Trigger A Deployment

Without a configured deployment adapter, this returns a stable `deployment_unavailable` error.

```bash
curl -X POST http://127.0.0.1:8000/deployments \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PYCLAW_API_BEARER_TOKEN" \
  -d '{
    "run_id": "<run_id>",
    "workspace_id": "<workspace_id>",
    "target": "staging"
  }'
```
