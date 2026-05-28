# Coding Conventions

**Analysis Date:** 2026-05-28

## Naming Patterns

**Files:**
- Module files use `snake_case.py`: e.g., `context_compressor.py`, `run_ledger.py`, `rubric_gate.py`
- Test files prefixed with `test_`: e.g., `test_store.py`, `test_domains.py`, `test_service_api.py`
- Config files use `snake_case` without extension where conventional

**Functions:**
- Public functions: `snake_case` — e.g., `upsert_block`, `record_trace`, `search_blocks`
- Private/internal helpers: leading underscore `_snake_case` — e.g., `_build_trace_search_query`, `_connect`, `_seed_packaged_rubrics`
- Test helper factories: leading underscore — e.g., `_block(...)`, `_write(...)`, `_count_tiktoken(...)`

**Classes:**
- `PascalCase` for all classes — e.g., `ContextStore`, `ReasonBlock`, `ContextRuntime`, `AtelierClient`
- Abstract base classes use `ABC` from `abc` module — e.g., `AtelierClient(ABC)` in `src/atelier/gateway/sdk/client.py`
- Pydantic models subclass `BaseModel` — e.g., `ReasonBlock(BaseModel)`, `Trace(BaseModel)`
- Protocol classes: `PascalCase` with leading underscore for private protocols — e.g., `_ServiceClient(Protocol)`

**Variables/Constants:**
- Local variables: `snake_case`
- Module-level constants: `UPPER_SNAKE_CASE` — e.g., `SCHEMA`, `TRACE_FTS_COLUMNS`, `JOB_CONSOLIDATE_BLOCKS`
- Private module constants: `_UPPER_SNAKE_CASE` — e.g., `_TASK_TYPE_BUDGET_MULTIPLIER`, `_LAZY_EXPORTS`

**Type Aliases / Literals:**
- Type aliases use `PascalCase` — e.g., `BlockStatus = Literal["active", "deprecated", "quarantined"]`, `TraceStatus`, `Severity`

## Code Style

**Formatting:**
- Tool: `black` with `line-length = 120`, `target-version = ["py311"]`
- Run: `uv run black src tests`

**Linting:**
- Tool: `ruff` with `line-length = 100`, `target-version = "py311"`
- Rule sets selected: `E, F, I, B, UP, SIM, RUF`
- `E501` (line-length) ignored (black handles formatting)
- Run: `uv run ruff check src`
- Auto-fix: `uv run ruff check --fix src`

**Type Checking:**
- Tool: `mypy` with `strict = true`, `warn_unused_ignores = true`, `ignore_missing_imports = true`
- Python version: 3.11
- `untyped-decorator` errors disabled for `atelier.core.service.api` and `atelier.gateway.adapters.http_api`
- `ignore_errors = true` for `atelier.gateway.cli.app` (CLI file has many `# type: ignore` annotations on Click decorators)
- Run: `uv run mypy --strict src`

## Import Organization

**Universal first line:**
Every source file starts with `from __future__ import annotations` for deferred type evaluation.

**Order (ruff `I` rules enforce isort):**
1. `from __future__ import annotations`
2. Standard library imports
3. Third-party imports
4. Local `atelier` imports

**Example from `src/atelier/core/foundation/models.py`:**
```python
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
```

**TYPE_CHECKING guard:**
Used to avoid circular imports at runtime — types imported only for annotations go under `if TYPE_CHECKING:`:
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from atelier.gateway.adapters.runtime import ContextRuntime
```

**Lazy exports in `__init__.py`:**
Top-level `src/atelier/__init__.py` uses a `_LAZY_EXPORTS` dict and `__getattr__` to avoid loading heavy modules at import time.

## Error Handling

**Patterns:**
- Broad `except Exception` is used for fail-open paths, always followed by `logger.warning(...)` — never silently swallowed
- Specific exceptions caught by type where appropriate: `except sqlite3.OperationalError as exc:`
- `raise ValueError(...)` for data validation errors (e.g., escaping store root)
- `raise RuntimeError(...)` for broken-invariant situations (e.g., missing schema tables)
- Context managers (`contextlib.suppress`, `contextlib.closing`) used for resource cleanup

**Example:**
```python
except Exception as exc:
    logger.warning("failed to seed packaged rubrics: %s", exc)
```

## Logging

**Framework:** Standard library `logging` module

**Setup:**
```python
import logging
logger = logging.getLogger(__name__)
```

**Patterns:**
- Module-level logger via `logging.getLogger(__name__)`
- `logger.warning("msg: %s", exc)` — use `%s` style (not f-strings) for lazy formatting
- No direct `print()` calls in library code (CLI output uses `rich` or `click.echo`)

## Comments and Docstrings

**Module docstrings:**
Every module has a docstring explaining its purpose, design decisions, and key usage patterns.
Example from `src/atelier/core/foundation/store.py`:
```python
"""Persistent storage for ReasonBlocks, traces, and rubrics.

Backend: SQLite + FTS5 (no external services).

Design:
- One table per entity, JSON column for the full payload.
...
"""
```

**Section dividers:**
Visual separators used to group related functions in long files:
```python
# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #
```

**Function docstrings:**
Short one-line docstrings on public methods and test functions when the function name alone is insufficient:
```python
def slugify(text: str) -> str:
    """Lowercase, dash-separated slug. Used for stable block/rubric IDs."""
```

**Test docstrings:**
Single-sentence descriptions on test functions explaining the expected behaviour:
```python
def test_domain_manager_lists_builtins(tmp_path: Path) -> None:
    """DomainManager should return built-in bundles when no user bundles exist."""
```

**Inline comments:**
Sparingly used for non-obvious decisions, often explaining why (not what).

## Function Design

**Signatures:**
- All parameters and return values are fully type-annotated
- Return type `-> None` always explicit
- Keyword-only parameters enforced with `*` where intent matters — e.g., `upsert_block(block, *, write_markdown=True)`
- Default parameters use factory callables via `Field(default_factory=list)` in Pydantic models

**Size:**
- Functions are focused and generally short; complex logic is factored into private helper methods

## Module and Class Design

**Pydantic Models:**
- Subclass `BaseModel`
- Use `model_config = ConfigDict(extra="forbid")` to prevent unexpected fields
- Validators use `@field_validator` and `@model_validator` decorators
- Field defaults use `Field(default_factory=...)` not mutable defaults

**Abstract Base Classes:**
- Use `from abc import ABC, abstractmethod`
- `AtelierClient(ABC)` defines interface contract for SDK clients (`src/atelier/gateway/sdk/client.py`)

**Protocols:**
- Used for structural typing — e.g., `_ServiceClient(Protocol)` in `src/atelier/gateway/sdk/remote.py`

**`__all__` exports:**
- All public packages define `__all__` explicitly
- Subpackages with nothing to re-export use `__all__ = []`

## Package Runner

- `uv` is the project's package manager and script runner — all commands use `uv run ...`
- `make` targets wrap `uv run` invocations for developer convenience

---

*Convention analysis: 2026-05-28*
