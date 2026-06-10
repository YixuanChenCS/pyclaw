## Local Execution Runtime Architecture

`execution_runtime` is the local-first service layer responsible for durable run execution. It owns run queueing, state transitions, event persistence and replay, local command execution, local patch application, cancellation, finalization, and minimal approval suspension.

It is not responsible for deployment, distributed worker coordination, remote sandboxing, LLM orchestration, repo intelligence, or approval resume and resolution. Those remain separate concerns or future work.

## Module Structure

- [services/execution_runtime/local.py](/Users/yixuanchen/Downloads/Agent_Study/pyclaw-clean/services/execution_runtime/local.py): orchestration entrypoint implementing `ExecutionRuntimeService`
- [services/execution_runtime/sqlite_store.py](/Users/yixuanchen/Downloads/Agent_Study/pyclaw-clean/services/execution_runtime/sqlite_store.py): durable SQLite repository for runs, run events, artifacts, and approvals
- [services/execution_runtime/state_machine.py](/Users/yixuanchen/Downloads/Agent_Study/pyclaw-clean/services/execution_runtime/state_machine.py): finite-state-machine rules for valid run transitions
- [services/execution_runtime/events.py](/Users/yixuanchen/Downloads/Agent_Study/pyclaw-clean/services/execution_runtime/events.py): event serialization helpers, event-type mapping, and replay sequence validation
- [services/execution_runtime/command.py](/Users/yixuanchen/Downloads/Agent_Study/pyclaw-clean/services/execution_runtime/command.py): local subprocess execution with cwd validation, timeout handling, cancellation-aware shutdown, and stdout/stderr caps
- [services/execution_runtime/patch.py](/Users/yixuanchen/Downloads/Agent_Study/pyclaw-clean/services/execution_runtime/patch.py): conservative unified-diff parsing, workspace path validation, and local text patch application

## Durable State Model

The runtime uses SQLite as the system of record.

- `runs`: current snapshot of each run. This table stores the latest status, lease ownership, attempt count, and timestamps.
- `run_events`: append-only durable event log with per-run sequence numbers. This is the replay source for `stream_events`.
- `artifacts`: metadata for persisted outputs such as applied patches.
- `approvals`: durable approval requests created when a run is suspended pending human review.

The snapshot and event log serve different purposes. `runs` answers “what is the current state now?” while `run_events` answers “what happened over time?”.

## Run Lifecycle

The current MVP lifecycle is:

- `QUEUED -> RUNNING -> SUCCEEDED`
- `QUEUED -> RUNNING -> FAILED`
- `QUEUED -> RUNNING -> CANCELLING -> CANCELLED`
- `RUNNING -> WAITING_FOR_APPROVAL`

Minimal approval support only suspends the run. Approve, reject, and resume are intentionally not implemented yet. `WAITING_FOR_APPROVAL` is therefore terminal from the service behavior perspective for now, even though it is not a terminal status in the FSM.

## Why SQLite Is The Source Of Truth

SQLite gives the local runtime a single durable authority for queue state, event history, approvals, and artifacts without depending on a separate service. This keeps local execution deterministic and recoverable after process crashes.

The runtime does not treat in-memory queues or in-memory event buffers as authoritative. In-memory state is used only for live process bookkeeping such as active subprocess tasks. Durable state always lives in SQLite.

## Event Replay

`stream_events` replays historical and live events by polling `run_events` ordered by per-run sequence. This avoids missing events across restarts and guarantees replay can reconstruct the durable history. Sequence validation detects gaps or order violations instead of silently skipping inconsistent state.

## Claim, Lease, Heartbeat, And Recovery

Run claiming is SQLite-backed and atomic:

- a worker claims the oldest queued run
- the claim updates the run snapshot to `RUNNING`
- the worker lease, attempt count, and heartbeat timestamps are stored on the run
- `run.started` is appended durably

Heartbeats extend the lease for an owned `RUNNING` run. Crash recovery scans for stale leased runs on startup and then either requeues them or parks them for manual recovery depending on durable side-effect history.

SQLite also serializes active mutating runs per workspace. A queued run cannot be claimed if another `RUNNING` run for the same workspace still has a valid lease. The exclusion check and the claim happen in the same SQLite transaction, so it does not rely on in-memory worker coordination.

## Recovery Policy

Stale `RUNNING` recovery is now conservative and event-history-aware.

- if a stale `RUNNING` run has no durable side-effect events after `run.started`, it is requeued
- if a stale `RUNNING` run has durable side-effect events such as `command.started` or `patch.applied`, it is not requeued automatically
- instead, the run moves to `WAITING_FOR_APPROVAL`
- the expired worker lease is cleared
- a durable approval/manual-recovery record is persisted
- replayable recovery events are appended so operators can see why the run stopped

This avoids blindly restarting work that may already have mutated the workspace.

## Command Execution

`execute_command` uses `LocalCommandExecutor` and `asyncio.create_subprocess_exec`.

- `argv` must be non-empty
- `cwd` is resolved relative to the workspace root and cannot escape it
- `env` merges with the current process environment
- stdout and stderr are captured with truncation caps
- timeout causes terminate, then kill if needed
- cancellation returns a `CommandResult` with `cancelled=True`

The runtime emits durable command events:

- `command.started`
- `command.completed`
- `command.failed`
- `command.timeout`
- `command.cancelled`

`execute_command` does not finalize the run. Command execution and run finalization stay separate.

## Cancellation

`cancel_run` handles queued and active runs.

- queued runs transition directly to `CANCELLED`
- running runs transition to `CANCELLING`
- active subprocesses are cancelled and terminated
- cancellation completion appends durable cancellation events and transitions the run to `CANCELLED`

SQLite remains the authoritative record for the resulting state and event history.

## Patch Application

`apply_patch` loads the durable run, requires a valid executable state, resolves the workspace, and applies a conservative unified diff locally.

Safety checks include:

- all target paths must stay within the workspace root
- `../` escape is rejected
- absolute paths outside the workspace are rejected
- symlink-parent escapes are rejected
- hunks must apply cleanly or patch application fails

On success the runtime persists patch artifact metadata and durable patch events. On failure it emits a durable failure event and raises a typed error. The patcher is intentionally strict and does not implement fuzzy application.

## Finalization

`finalize_run` is the explicit end of the run lifecycle. It validates the `RunResult`, requires a terminal status, persists the final run snapshot through the repository transition path, emits the matching terminal event, and releases any held workspace lease.

Patch application, command execution, and approval suspension do not finalize the run on their own.

## Minimal Approval Suspension

`request_approval` is intentionally narrow in this MVP.

- the run must exist
- the run must be `RUNNING`
- the approval request is persisted in SQLite
- the run transitions to `WAITING_FOR_APPROVAL`
- `approval.requested` is appended durably

What is not implemented yet:

- approval resolution
- approval rejection
- approval expiry handling
- run resume from `WAITING_FOR_APPROVAL`
- continuation-aware recovery after manual inspection

## End-To-End Test Coverage

[tests/integration/test_execution_runtime_e2e.py](/Users/yixuanchen/Downloads/Agent_Study/pyclaw-clean/tests/integration/test_execution_runtime_e2e.py) proves the local MVP works as a complete flow:

- create a temporary workspace
- enqueue and claim a run
- apply a patch that fixes a buggy Python module
- execute local unit tests through the runtime
- finalize the run
- replay the durable event sequence from SQLite

That test verifies patch application and command execution both leave the run active until explicit finalization.

## Current Limitations And Future Work

- approval currently only suspends the run
- approve, reject, and resume are future work
- deployment remains stubbed
- remote sandbox execution is future work
- distributed workers are future work
- patch application is strict and does not do fuzzy matching
- full approval-based recovery and continuation remain future work
