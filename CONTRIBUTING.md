# Contributing

This repository is an early development skeleton for a local coding-agent platform.
Keep contributions small, focused, and aligned with the current architecture work.

## What to contribute

- Bug fixes
- Small improvements to shared contracts, services, or developer tooling
- Tests for new behavior
- Documentation updates when behavior or structure changes

For large design changes, open an issue or start a discussion before sending a PR.

## Development setup

Pyclaw currently supports Python `>=3.10,<3.15`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -r requirements/requirements-dev.txt
```

Optional dependencies live under `requirements/` if your change needs them.

## Running checks

Run the relevant tests before opening a PR:

```bash
pytest tests/unit -q
pytest tests/integration -q
```

If your change touches optional or networked behavior, also run:

```bash
pytest tests/optional -q
pytest tests/online -q
```

You can run the full suite with:

```bash
pytest
```

## Style

- Follow the existing project structure in `apps/`, `services/`, `packages/`, and `pyclaw/`.
- Keep changes minimal and targeted.
- Add or update tests when behavior changes.
- Use `black`, `isort`, and `flake8` conventions already configured in the repo.

Optional:

```bash
pre-commit install
pre-commit run --all-files
```

## Pull requests

- Describe what changed and why.
- Mention any follow-up work that is intentionally left out.
- Include docs updates when user-facing behavior changes.
