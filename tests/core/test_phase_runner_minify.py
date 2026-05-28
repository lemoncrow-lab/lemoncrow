"""Tests for PhaseRunner reader/writer profile dispatch — LINEAR-03 / D-09, D-11.

Wave-0 RED scaffolds for the two profile-dispatch cases (13-02-04..05):
* writer profile delivers exact bytes and records no minify telemetry;
* reader profile delivers minified bytes and appends a MinificationDelta
  entry per read.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from atelier.core.capabilities.context_compression.minify import minify_source

from atelier.core.capabilities.context_reuse.models import Phase, PhasePlan
from atelier.core.capabilities.context_reuse.phase_runner import PhaseRunner
from atelier.core.capabilities.prefix_cache.diagnostics import PrefixCacheDiagnostics
from atelier.core.capabilities.prefix_cache.planner import PrefixCachePlanner
from atelier.infra.runtime.run_ledger import RunLedger

_PYTHON_BODY = "def hello(x):   \n" "    if x:\t\n" "        return x   \n" "\n" "\n" "\n" "    return 0\n"


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> tuple[str, int, int, int, int]:
        self.calls.append(copy.deepcopy(messages))
        return ("ok <phase-complete>", 100, 50, cache_read, cache_write)


def _make_read_tool(body: str, lang: str):
    def _read(path: str) -> tuple[str, str]:
        return body, lang

    return _read


def _build_plan(profile: str, phase_name: str) -> PhasePlan:
    phase = Phase(
        name=phase_name,
        kind="agent",
        profile=profile,
        objective_path=f"{phase_name}.md",
        continue_from=None,
        next=None,
    )
    return PhasePlan(name="t", entry=phase_name, phases={phase_name: phase})


def _make_runner(
    tmp_path: Path,
    plan: PhasePlan,
    *,
    read_tool,
    bootstrap_reads: dict[str, list[str]],
) -> tuple[PhaseRunner, _FakeProvider]:
    ledger_path = tmp_path / ".atelier" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = RunLedger(root=ledger_path.parent, agent="t", task="t", domain="d")
    planner = PrefixCachePlanner()
    diag = PrefixCacheDiagnostics()
    provider = _FakeProvider()
    runner = PhaseRunner(
        plan,
        provider=provider,
        ledger=ledger,
        planner=planner,
        diag=diag,
        read_tool=read_tool,
        bootstrap_reads=bootstrap_reads,
    )
    return runner, provider


def test_writer_profile_exact_bytes(tmp_path: Path) -> None:
    """13-02-04: writer-profile reads inject body byte-identically; no deltas."""
    plan = _build_plan("writer", "implement")
    runner, provider = _make_runner(
        tmp_path,
        plan,
        read_tool=_make_read_tool(_PYTHON_BODY, "python"),
        bootstrap_reads={"implement": ["src/foo.py"]},
    )
    results = runner.run()
    # Find the read body in the message list delivered to the provider.
    msgs = provider.calls[0]
    bodies = [m["content"] for m in msgs if m.get("role") == "tool"]
    assert _PYTHON_BODY in bodies
    # Byte-identity: no minify mutation.
    minified, _, _ = minify_source(_PYTHON_BODY, "python")
    assert minified != _PYTHON_BODY  # sanity: there is something to strip
    assert minified not in bodies
    # No telemetry written for the writer profile.
    assert results["implement"].cache_stats.minify_deltas == []


def test_minify_telemetry(tmp_path: Path) -> None:
    """13-02-05: reader-profile reads inject minified body + one delta entry."""
    plan = _build_plan("reader", "survey")
    runner, provider = _make_runner(
        tmp_path,
        plan,
        read_tool=_make_read_tool(_PYTHON_BODY, "python"),
        bootstrap_reads={"survey": ["src/foo.py"]},
    )
    results = runner.run()
    minified, original_tokens, minified_tokens = minify_source(_PYTHON_BODY, "python")
    msgs = provider.calls[0]
    bodies = [m["content"] for m in msgs if m.get("role") == "tool"]
    assert minified in bodies
    # Original (with trailing WS + blank runs) NOT delivered.
    assert _PYTHON_BODY not in bodies

    deltas = results["survey"].cache_stats.minify_deltas
    assert len(deltas) == 1
    d = deltas[0]
    assert set(d.keys()) >= {"path", "lang", "original_tokens", "minified_tokens", "saved_tokens"}
    assert d["path"] == "src/foo.py"
    assert d["lang"] == "python"
    assert d["original_tokens"] == original_tokens
    assert d["minified_tokens"] == minified_tokens
    assert d["original_tokens"] >= d["minified_tokens"]
    assert d["saved_tokens"] == max(0, original_tokens - minified_tokens)
