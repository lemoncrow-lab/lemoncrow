"""Stop hook `_format_stats`: normal formatting + resilience to a stale
installed `lemon` package.

This file is copied verbatim to ``~/.lemoncrow/claude-plugin/hooks/stop.py`` by
``scripts/install_claude.sh`` and is kept in sync with the dev repo
independently of the *installed* ``lemon`` package (``uv tool install``).
A prebuilt/older install can lag behind and miss recently-added private
helpers (e.g. ``_fmt_tok``/``_fmt_usd`` in ``savings_summary.py``) -- that
mismatch must degrade to an equivalent inline formatter, never crash the hook.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from lemoncrow.core.capabilities import savings_summary

_STOP = Path("integrations/claude/plugin/hooks/stop.py")


def _load_stop() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lemoncrow_stop_hook_format_stats", _STOP)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stats(**overrides: object) -> dict:
    base = {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_read_tokens": 2_000,
        "cache_write_tokens": 300,
        "tool_calls": 4,
        "turns": 2,
        "est_cost_usd": 0.05,
        "tools_used": {"read": 3, "edit": 1},
    }
    base.update(overrides)
    return base


def _savings(**overrides: object) -> dict:
    base = {
        "saved_usd": 1.5,
        "tokens_saved": 12_000,
        "calls_avoided": 3,
        "routing_usd": 0.0,
        "carry_usd": 0.0,
        "carry_tokens": 0,
        "output_usd": 0.0,
        "output_tokens": 0,
    }
    base.update(overrides)
    return base


def test_format_stats_normal_path_uses_canonical_formatters() -> None:
    stop = _load_stop()
    out = stop._format_stats(_stats(), _savings())
    assert "tokens: 1.3k in (1.0k new + 300 cW) · 2.0k cR · 500 out · 3.8k total" in out
    assert "savings: $1.50 · 12.0k tok · 3 calls avoided" in out
    assert "top tools: read×3 · edit×1" in out  # noqa: RUF001


def test_format_stats_falls_back_when_installed_package_lacks_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the reported bug: an installed `lemon` predating
    `_fmt_tok`/`_fmt_usd` must not crash `_format_stats` with an ImportError --
    it should fall back to an equivalent inline formatter and render the same
    output.
    """
    monkeypatch.delattr(savings_summary, "_fmt_tok", raising=True)
    monkeypatch.delattr(savings_summary, "_fmt_usd", raising=True)

    stop = _load_stop()
    out = stop._format_stats(_stats(), _savings())

    assert "tokens: 1.3k in (1.0k new + 300 cW) · 2.0k cR · 500 out · 3.8k total" in out
    assert "savings: $1.50 · 12.0k tok · 3 calls avoided" in out


def test_format_stats_fallback_matches_canonical_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback formatter must render byte-identical output to the
    canonical one -- a stale install should be invisible to the user, not
    just non-crashing."""
    stats, savings = _stats(), _savings(output_usd=0.4, output_tokens=800, carry_usd=0.1, carry_tokens=200)

    canonical_stop = _load_stop()
    canonical_out = canonical_stop._format_stats(stats, savings)

    monkeypatch.delattr(savings_summary, "_fmt_tok", raising=True)
    monkeypatch.delattr(savings_summary, "_fmt_usd", raising=True)
    fallback_stop = _load_stop()
    fallback_out = fallback_stop._format_stats(stats, savings)

    assert fallback_out == canonical_out
