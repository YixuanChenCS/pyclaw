# Repo Intelligence Migration Status

This document records the current migration state for `repo_intelligence` so later work on `execution_runtime` does not mix the new service implementation with legacy `pyclaw` code by accident.

## Current Source Of Truth

- Service-side `RepoMap` implementation: `services/repo_intelligence/repomap.py`
- Service-side entrypoint that constructs it: `services/repo_intelligence/local.py`
- Service-side important file filtering: `services/repo_intelligence/important_files.py`
- Service-side query resources: `services/repo_intelligence/queries/`
- Service-side integration coverage: `tests/integration/test_repomap.py` and `tests/integration/test_repo_intelligence_repomap.py`

If you are working in `services/`, `packages/`, `apps/api/`, or `apps/cli/` service wiring, this is the implementation you should use.

## What Has Been Migrated

- `RepoMap` class copied into `services/repo_intelligence/repomap.py` and adapted for service use
- Query resources copied into `services/repo_intelligence/queries/tree-sitter-languages/`
- Query resources copied into `services/repo_intelligence/queries/tree-sitter-language-pack/`
- Important-file filtering extracted into `services/repo_intelligence/important_files.py`
- `LocalRepoIntelligenceService._new_repo_map()` now imports `services.repo_intelligence.repomap.RepoMap`
- Integration tests for repo map now target `services.repo_intelligence.repomap.RepoMap`
- Cache namespace renamed from `.pyclaw.tags.cache.*` to `.repo_intelligence.tags.cache.*`
- Resource cleanup added so repo-map cache handles do not leak warnings

## What Is Still Legacy

These files still exist and are intentionally retained as references or because legacy code still depends on them:

- `pyclaw/repomap.py`
- `pyclaw/queries/`
- `pyclaw/coders/base_coder.py`
- `pyclaw/website/docs/languages.md`

Their presence does not mean they are the preferred implementation for service work.

## Rules During Runtime Migration

- Do not add new imports of `pyclaw.repomap.RepoMap` in any `services/*`, `packages/*`, `apps/api/*`, or `apps/cli/*` code.
- New repo-intelligence tests should target `services.repo_intelligence.*`, not `pyclaw.*`.
- If `execution_runtime` needs repository context, wire it through `RepoIntelligenceService` or `services.repo_intelligence.*`, not through legacy `pyclaw` modules.
- Legacy `pyclaw/*` imports may remain only where old CLI or old agent code has not been migrated yet.
- If a missing capability is discovered, port the needed helper into `services/repo_intelligence/` instead of reintroducing a `pyclaw.*` dependency.

## Known Remaining Legacy References

- `pyclaw/coders/base_coder.py` still imports legacy `RepoMap`
- old website/docs generation still references legacy repo-map helpers

That means old `pyclaw` agent flows are not fully migrated. It does not block `repo_intelligence` service work.

## Practical Boundary

For the current phase, treat the codebase as split like this:

- `services/repo_intelligence/*`: active migration target and service MVP
- `pyclaw/*`: legacy reference implementation and old app surface

When in doubt, prefer the service-side module unless you are explicitly fixing or maintaining old `pyclaw` behavior.

## Current Phase Decision

`repo_intelligence` is now treated as frozen at MVP for the purpose of sequencing the broader platform migration.

- Do small bugfixes and targeted integration fixes only.
- Do not start another large internal migration pass inside `repo_intelligence`.
- Start `execution_runtime` next.
- Revisit `repo_intelligence` only after runtime integration exposes concrete gaps or bad interfaces.

This means the default engineering decision has changed from "keep improving repo intelligence in isolation" to "integrate it and let runtime pressure reveal what actually still matters."

## Exit Criteria Before Deleting Legacy RepoMap

Do not delete `pyclaw/repomap.py` until all of the following are true:

- no production code imports `pyclaw.repomap.RepoMap`
- no tests that matter for current product behavior rely on the legacy module
- docs or helper scripts no longer require legacy exports
- new service-side repo map remains green in unit and integration coverage

Until then, legacy code stays for reference only.
