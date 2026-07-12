"""Shared CLI-only glue used across multiple LemonCrow command modules.

These helpers are moved verbatim from ``app.py``. They are *CLI-only* plumbing
(output emission, store/runtime construction, memory input handling, tag
parsing) -- NOT business logic (CLAUDE.md:55). Command modules import them from
here; ``app.py`` re-imports them so every existing call site is unchanged.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import click

# ── Auto-install flag ─────────────────────────────────────────────────────
# Set True by commands/__init__.py when any command module import fails.
# app.py checks this at startup and runs ``uv sync`` to install deps.
_IMPORT_FAILED: bool = False

_REDACTION_PLACEHOLDER_RE = re.compile(r"<redacted[^>]*>")


def _load_store(root: Path) -> Any:
    from lemoncrow.infra.storage.factory import create_store

    try:
        store = create_store(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    db_path = getattr(store, "db_path", None)
    if db_path is not None and not Path(db_path).exists():
        raise click.ClickException(f"No LemonCrow store at {root}. Run `lc init` first.")
    return store


def _core_runtime(root: Path) -> Any:
    from lemoncrow.core.runtime import LemonCrowRuntimeCore

    try:
        return LemonCrowRuntimeCore(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def _emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        click.echo(data)


def require_pro(feature: str, label: str) -> None:
    """Gate a Pro-only CLI control surface.

    Raises :class:`click.ClickException` with an upgrade hint unless the
    signed-in account's plan grants ``feature``. On a Free install the command
    is blocked with a clear upsell.
    """
    from lemoncrow.core.capabilities import licensing

    if licensing.has_feature(feature):
        return
    # The user may have purchased seconds ago: bypass the 6 h cache once and
    # re-check live before blocking (one HTTP call, only on locked commands).
    licensing.refresh_plan()
    if not licensing.has_feature(feature):
        raise click.ClickException(f"{label} is an LemonCrow Pro feature. Unlock at {licensing.pro_url()}")


def _redact_memory_input(text: str, field_name: str) -> str:
    from lemoncrow.core.foundation.redaction import redact

    redacted = redact(text)
    if not text:
        return redacted
    remaining = _REDACTION_PLACEHOLDER_RE.sub("", redacted)
    if len(remaining.strip()) < len(text.strip()) * 0.5:
        raise click.ClickException(f"{field_name} rejected: likely secret leakage")
    return redacted


def _read_memory_value(value: str) -> str:
    if not value.startswith("@"):
        return value
    path_text = value[1:]
    if path_text == "/dev/stdin" or path_text == "-":
        return sys.stdin.read()
    return Path(path_text).read_text(encoding="utf-8")


def _parse_tags(values: tuple[str, ...]) -> list[str]:
    tags: list[str] = []
    for value in values:
        tags.extend(tag.strip() for tag in value.split(",") if tag.strip())
    return tags


def _smart_state_path(root: Path) -> Path:
    return Path(root) / "smart_state.json"


def _load_smart_state(root: Path) -> dict[str, Any]:
    p = _smart_state_path(root)
    if not p.exists():
        return {"mode": "shadow", "cache": {}, "savings": {"calls_avoided": 0, "tokens_saved": 0}}
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def _save_smart_state(root: Path, state: dict[str, Any]) -> None:
    p = _smart_state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dhm])", value.strip())
    if not match:
        raise click.ClickException("duration must look like 7d, 12h, or 30m")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _ledger_dir(root: Path) -> Path:
    # Run ledgers live in the canonical sessions/YYYY/MM/DD/<host>/<id>/run.json
    # tree alongside the trace files -- see lemoncrow.core.foundation.paths.session_dir.
    return Path(root) / "sessions"


def _latest_ledger_path(root: Path) -> Path | None:
    runs = _ledger_dir(root)
    if not runs.is_dir():
        return None
    paths = sorted(runs.glob("*/*/*/*/*/run.json"))
    return paths[-1] if paths else None


def _ledger_path(root: Path, session_id: str | None) -> Path:
    if session_id:
        from lemoncrow.core.foundation.paths import find_session_dir

        session_dir = find_session_dir(root, session_id)
        if session_dir is None:
            raise click.ClickException(f"no run ledger found for session {session_id}.")
        return session_dir / "run.json"
    latest = _latest_ledger_path(root)
    if latest is None:
        raise click.ClickException("no run ledger found. Pass --session-id or record one first.")
    return latest
