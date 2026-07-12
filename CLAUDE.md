# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

**All Python commands must use `uv run`** — the project uses `uv` for dependency management and there is no activated venv. Direct `python3` calls will fail or use the wrong environment.

```bash
uv run python -c "..."          # one-off Python
uv run pytest ...               # tests
uv run mypy src                 # type-check
uv run lemoncrow ...                   # CLI
```

The `.venv` is at `.venv/bin/python3` if you need the path explicitly.

## Common Commands

```bash
# Test
uv run pytest -q                          # all tests (slow tests excluded by default)
uv run pytest -q -x -m "not slow"        # fast, stop on first failure
uv run pytest tests/path/test_file.py -q # single file
uv run pytest -q -k "test_name"          # single test by name

# Lint / format / typecheck
make lint           # ruff
make format         # ruff --fix + black + prettier (frontend)
make typecheck      # mypy --strict src

# Full pre-commit gate
make pre-commit     # format + lint + typecheck + docs + test

# Docs governance
make sync-agent-context   # regenerate host instruction files from integrations/agents/shared/

# Install Claude plugin (after changing integrations/claude/plugin/)
bash scripts/install_claude.sh
```

|     |     |     |
| --- | --- | --- |
|     |     |     |

## Coding Guidelines

The full guidelines (think before coding, simplicity first, surgical changes, goal-driven execution) are embedded in every LemonCrow persona. Source of truth: `integrations/agents/shared/coding-guidelines.md` — do not restate them here.
