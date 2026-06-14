# Legacy PyClaw Parity Report

This report compares the legacy `pyclaw/` coding-agent behavior against the new layered implementation.

The legacy code is the behavioral reference, not the architectural template. New work should preserve the service split across:

- `services/agent_core`
- `services/execution_runtime`
- `services/repo_intelligence`
- `apps/cli`
- shared contracts and persistence

## Comparison

| Legacy feature | Legacy location | New status | New owner | Tests to add / keep |
| --- | --- | --- | --- | --- |
| Plan -> edit -> verify -> continue loop | `pyclaw/coders/base_coder.py` around `run()`, `send_message()`, `apply_updates()` | Partial | `services/agent_core.local`, `services/agent_core.runner` | Runner loop order, retry/reflection, completion sequencing |
| Repo map and file-context prompt assembly | `pyclaw/coders/base_coder.py`, `pyclaw/repomap.py` | Partial | `services/repo_intelligence`, `services/agent_core.prompts` | repo-intelligence unit/integration tests, prompt coverage |
| Auto-add missing file context from model mentions | `pyclaw/coders/base_coder.py` `check_for_file_mentions()` | Missing | `services/agent_core` using `repo_intelligence` | loop tests for missing-context expansion |
| Read-only/reference files in context | `pyclaw/coders/base_coder.py`, `pyclaw/commands.py` `/read-only` | Missing | `services/agent_core` session state + `apps/cli` adapter | session serialization, prompt rendering, CLI entrypoints |
| Multiple edit formats / architect-editor split | `pyclaw/coders/*`, `pyclaw/commands.py` `/architect` | Mostly missing | `services/agent_core` policy + model-client/config boundary | plan/action tests, CLI config tests |
| Structured patch validation before filesystem mutation | legacy coders + parser/apply flow | Partial | `services/agent_core.validation`, `services/execution_runtime.patch` | existing patch validation tests, multi-file proposal tests |
| Multi-file patch proposals | legacy edit block / diff coders | Missing | `services/agent_core` proposal generation + validation | generator/review tests, runtime patch apply tests |
| Create / delete file edits | legacy coders and repo mutation paths | Missing | `services/agent_core` proposal policy + `services/execution_runtime.patch` | safety tests for create/delete/rollback |
| Auto lint after edits | `pyclaw/coders/base_coder.py` `lint_edited()` | Partial: deterministic `py_compile` runs after successful Python patches | `services/agent_core.runner` dispatch policy + `execution_runtime` command execution | post-patch verification tests |
| Auto test after edits | `pyclaw/coders/base_coder.py` `auto_test` path | Partial: deterministic focused `pytest <matched_test_file>` runs when path mapping finds a related unit test, and an explicit allowlisted `fallback_test_command` can stand in for legacy `test_cmd` when no focused test is found | `services/agent_core.runner` + `execution_runtime` | command selection/reflection tests |
| Reflection on lint/test failures | `pyclaw/coders/base_coder.py` `reflected_message` loop | Partial: retryable command failures now record details, expand context, and continue into repair/rerun flow | `services/agent_core` session/failure policy, `services/repo_intelligence` | retryable failure loop tests |
| Shell-command suggestion flow | `pyclaw/coders/base_coder.py` `run_shell_commands()` | Missing | defer until core parity is stronger | later CLI/product tests |
| Durable run lifecycle, event replay, recovery | legacy code did not have this separation | New implementation is stronger | `services/execution_runtime` | existing runtime unit/integration tests |
| Approval checkpoints and resume | legacy interactive confirms | Partial | `services/execution_runtime`, `services/agent_core.runner` | approval persistence/resume tests |
| Repo index refresh after edits | implicit through in-memory file/chat state | Missing before phase 1 | `services/agent_core.runner` calling `repo_intelligence` | patch-success refresh tests |
| Symbol search for context expansion | legacy repo-map and mention heuristics | Partial: loop-integrated for command/test failure recovery | `services/repo_intelligence` + `services/agent_core.runner` | symbol-search-driven context tests |
| Impact analysis after edits | legacy behavior was ad hoc | Partial: used after patch success and command/test failure recovery | `services/repo_intelligence` + `services/agent_core.runner` | impact-driven context rebuild tests |
| Watch mode / browser / voice / GUI | `pyclaw/watch.py`, `pyclaw/gui.py`, `pyclaw/commands.py` | Deferred | product surfaces later | defer until coding loop parity is stronger |

## Current Migration Decision

Phase 1 should focus on loop correctness instead of product surface parity.

The first migrated capabilities are:

- refresh repo intelligence after a successful patch
- rebuild impacted repo context for the next agent step
- record retryable command/test failure details and rebuild failure-focused repo context
- continue from command/test failures by proposing a repair patch and rerunning verification
- run deterministic post-patch `py_compile` verification for changed Python files
- run deterministic focused `pytest` verification when a changed source file maps to a known unit test
- optionally run an explicit allowlisted fallback pytest command when syntax passes but focused test discovery finds nothing
- record `functional_verification_missing` when no focused test or configured fallback command is available, instead of implying full test coverage
- keep refresh best-effort so a successful runtime patch does not fail just because indexing refresh had an issue

This is the smallest change that improves parity with the legacy "edits immediately affect later context" behavior without collapsing the new service boundaries.
