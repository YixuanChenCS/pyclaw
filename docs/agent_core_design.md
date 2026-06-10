# Agent Core Design

## Responsibility Boundary

`agent_core` is the headless coding-agent decision layer. It owns conversation state, session state, planning loops, prompt construction strategy, model/tool orchestration policy, edit proposal policy, and the logic that decides what should happen next.

`execution_runtime` is the execution layer underneath it. It owns durable run lifecycle, SQLite persistence, event replay, command execution, patch application, cancellation, approval suspension, finalization, workspace exclusion, and stale-run recovery.

That boundary is intentional: `agent_core` decides, `execution_runtime` executes.

## Why Agent Core Must Be Headless

The new service must be deterministic in tests and reusable from CLI, API, and future workers without inheriting terminal assumptions from the legacy app.

Headless here means:

- no terminal reads
- no direct printing
- no shell execution
- no patch application
- no git mutation
- no direct UI approval prompts

All of those behaviors belong either in adapters or in `execution_runtime`.

## Legacy Coder Audit

The legacy `pyclaw/coders` implementation is useful as design input, but it is not an acceptable dependency for the new service boundary.

### What The Legacy Coders Do Well

- They make edit formats explicit. `EditBlockCoder`, `UnifiedDiffCoder`, `WholeFileCoder`, and `PatchCoder` each define a clear contract between prompt and parser.
- They treat malformed model output as a first-class error path and reflect that back into the loop.
- They encode a useful conceptual split between planning/prompting and edit parsing/application, even though the implementation mixes them together.

### How Prompt Construction Works In Legacy Code

`pyclaw/coders/base_coder.py` builds prompts by combining:

- a coder-specific system prompt from `pyclaw/coders/*_prompts.py`
- platform/environment details such as shell, date, language, and lint/test preferences
- repo-map summaries
- in-chat file contents and read-only file contents
- prior conversation history and example messages

That is a useful reference for future `agent_core.prompts`, but the current implementation is tightly coupled to runtime environment inspection and interactive chat behavior.

### How Model Output Is Parsed

- `EditBlockCoder` parses fenced `SEARCH/REPLACE` blocks and tries exact or whitespace-tolerant replacement.
- `UnifiedDiffCoder` parses unified diffs and applies strict hunk matching.
- `WholeFileCoder` parses fenced whole-file replacements.
- `base_coder.py` also has partial function-call argument parsing for tool-style responses.

The useful idea is strict structured parsing. The reusable code is limited because the parsers are still wired directly to file IO and legacy coder state.

### How Diffs And Edits Are Handled

Legacy coders implement multiple patch styles:

- search/replace blocks for local targeted edits
- unified diffs for hunk-based edits
- whole-file replacement for simpler overwrite workflows
- patch-oriented coders for multi-action patch descriptions

Those formats are worth preserving conceptually, but they should surface in the new service as structured proposals, not immediate workspace mutation.

### Why Legacy Coders Are Not Reused Directly

`pyclaw/coders/base_coder.py` mixes too many concerns into one object:

- terminal IO through `InputOutput`
- git state through `GitRepo`
- repo context through legacy `RepoMap`
- model calls through legacy LLM integration
- file mutation through `io.write_text`
- shell execution through `run_cmd`
- lint/test retries
- confirmation prompts and approval-like interaction
- auto-commit behavior and undo-oriented workflow state

That makes it unsuitable for a headless service layer. Importing it into `services/agent_core` would reintroduce direct side effects, old application state, and legacy `pyclaw` dependencies into the new architecture.

## Reuse Decision

The new `services/agent_core` package should reuse concepts from legacy coders, not the modules themselves.

Keep:

- explicit action and edit formats
- strict validation
- parse-before-apply discipline
- a clean separation between plan generation and action generation

Do not import directly:

- `pyclaw/coders/base_coder.py`
- any `pyclaw/coders/*_coder.py`
- legacy prompt modules as runtime dependencies
- legacy `RepoMap`, terminal, git, or shell helpers

## Planned Future Phases

- `create_plan`: deterministic planning contract for the run
- `next_action`: choose the next structured action from session state
- `strict_validation`: validate action shape, patch structure, and state transitions
- `review_patch`: critique/refine patch proposals before execution
- `summarize_run`: create structured completion summaries
- `runtime_integration`: hand validated actions to `execution_runtime`
