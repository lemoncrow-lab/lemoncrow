"""Runtime side-effect observation: normalization, base/head diff, producer, flow grounding (Phase 9R R7)."""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.behavior import BehaviorContext
from lemoncrow.pro.capabilities.review.execution import SinkCapture
from lemoncrow.pro.capabilities.review.flow_traces import ground_flow, observed_traces, sequence_steps
from lemoncrow.pro.capabilities.review.runtime_observation import (
    ObservedEffect,
    RuntimeObservation,
    RuntimeObservationProducer,
    TraceEdge,
    build_observation,
    diff_observations,
    parse_strace,
)

ENV = "envfp:sha256:" + "a" * 64
OTHER_ENV = "envfp:sha256:" + "b" * 64


def _obs(side: str, *effects: ObservedEffect, tier: str = "strace", truncated: bool = False) -> RuntimeObservation:
    queries = sum(item.count for item in effects if item.category == "db_query")
    return RuntimeObservation(side=side, tier=tier, effects=effects, query_count=queries, truncated=truncated)


SELECT = ObservedEffect("db_query", "SELECT * FROM users WHERE id = ?", count=1)
WRITE = ObservedEffect("file_write", "./cache/out.json")


def test_parse_strace_reads_exec_connect_writes_and_renames() -> None:
    text = "\n".join(
        [
            'execve("/usr/bin/git", ["git", "--version"], 0x7ffd /* 9 vars */) = 0',
            'connect(3<socket:[1]>, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("203.0.113.7")}, 16)'
            " = -1 ENETUNREACH (Network is unreachable)",
            'openat(AT_FDCWD</workspace>, "out.txt", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 3</workspace/out.txt>',
            'openat(AT_FDCWD</workspace>, "app.py", O_RDONLY|O_CLOEXEC) = 3</workspace/app.py>',
            'renameat2(AT_FDCWD</workspace>, "a.tmp", AT_FDCWD</workspace>, "a.json", 0) = 0',
            'unlinkat(AT_FDCWD</workspace>, "old.log", 0) = 0',
        ]
    )
    assert parse_strace(text) == [
        {"t": "exec", "path": "/usr/bin/git", "argv": ["git", "--version"]},
        {"t": "connect", "addr": "203.0.113.7", "port": 443, "result": "ENETUNREACH"},
        {"t": "write", "op": "open", "path": "/workspace/out.txt"},
        {"t": "write", "op": "rename", "path": "/workspace/a.json"},
        {"t": "write", "op": "delete", "path": "/workspace/old.log"},
    ]


def test_build_observation_normalizes_bounds_and_redacts_untrusted_rows() -> None:
    # Credential-shaped values are assembled at runtime (the mirror scan rejects literals).
    token = "gh" + "p_" + "Z" * 36
    raw = {
        "tier": "hooks",
        "rows": [
            {"t": "connect", "addr": "127.0.0.1", "port": 8080, "result": "ok"},  # sandbox loopback: skipped
            {"t": "dns", "host": "203.0.113.7"},  # an address literal is not a lookup
            {"t": "dns", "host": "API.Example.com"},
            {"t": "connect", "addr": "203.0.113.7", "port": 443, "result": "ENETUNREACH"},
            {"t": "write", "op": "open", "path": "/workspace/__pycache__/app.cpython-313.pyc"},
            {"t": "write", "op": "open", "path": "/workspace/out.txt", "n": 3},
            {"t": "sql", "sql": "SELECT * FROM users WHERE id = 42"},
            {"t": "sql", "sql": "SELECT * FROM users WHERE id = 7"},
            {"t": "sql", "sql": "BEGIN"},
            {"t": "sql", "sql": "ALTER TABLE users ADD COLUMN token TEXT"},
            {"t": "log", "level": "ERROR", "message": f"auth failed with {token}"},
            {"t": "call", "caller": "run.py", "caller_fn": "main", "callee": "app.py", "callee_fn": "handle"},
            "not a row",
        ],
    }
    observation = build_observation(
        side="head",
        raw=raw,
        sink_captures=(
            SinkCapture(kind="http", destination="https://hooks.example.com", method="CONNECT", blocked=True),
        ),
        workspace_changes=(("out.txt", "created"), ("notes/new.md", "created")),
        output="Traceback (most recent call last):\nValueError: bad input 12\n",
    )
    identities = {(item.category, item.identity, item.outcome) for item in observation.effects}
    assert identities == {
        ("network", "dns api.example.com", "blocked"),
        ("network", "connect 203.0.113.7:443", "blocked"),
        ("network", "https hooks.example.com (TLS)", "blocked"),
        ("file_write", "./out.txt", ""),
        ("file_write", "./notes/new.md", ""),
        ("db_query", "SELECT * FROM users WHERE id = ?", ""),
        ("schema_mutation", "ALTER TABLE users ADD COLUMN token TEXT", ""),
        ("console_error", next(i for c, i, _ in identities if c == "console_error" and i.startswith("ERROR")), ""),
        ("console_error", "ValueError: bad input #", ""),
    }
    assert observation.query_count == 2
    assert observation.effect("db_query:SELECT * FROM users WHERE id = ?").count == 2  # type: ignore[union-attr]
    assert token not in json.dumps(observation.to_wire())
    assert observation.trace == (TraceEdge("run.py", "main", "app.py", "handle"),)
    assert observation.tier == "hooks"


def test_diff_reports_exact_differences_and_nothing_for_unchanged_behavior() -> None:
    base = _obs("base", SELECT, WRITE)
    head = _obs(
        "head",
        ObservedEffect("db_query", SELECT.identity, count=3),
        ObservedEffect("network", "connect 203.0.113.7:443", outcome="blocked"),
        ObservedEffect("process", "git"),
        ObservedEffect("schema_mutation", "CREATE TABLE t (id INTEGER)"),
    )
    diff = diff_observations(base, head, base_environment=ENV, head_environment=ENV)
    assert diff.comparable is True
    assert [(item.category, item.identity, item.change) for item in diff.changes] == [
        ("db_query", SELECT.identity, "changed"),
        ("file_write", "./cache/out.json", "removed"),
        ("network", "connect 203.0.113.7:443", "added"),
        ("process", "git", "added"),
        ("schema_mutation", "CREATE TABLE t (id INTEGER)", "added"),
    ]
    assert diff.query_count == (1, 3)

    same = diff_observations(base, _obs("head", SELECT, WRITE), base_environment=ENV, head_environment=ENV)
    assert same.comparable is True and same.changes == () and same.unchanged == 2
    # A count change is a difference for queries only; a file written twice is the same effect.
    twice = diff_observations(
        base,
        _obs("head", SELECT, ObservedEffect("file_write", WRITE.identity, count=2)),
        base_environment=ENV,
        head_environment=ENV,
    )
    assert twice.changes == ()


def test_a_tracer_that_wrote_nothing_is_no_observation_and_never_no_side_effects(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.runtime_observation import ObservationSetup, ObservationSpec, collect_raw

    out = tmp_path / "out"
    out.mkdir()
    raw = collect_raw(out, tier="strace", command=["python3", "run_tests.py"])
    assert raw["tier"] == "unobserved" and raw["rows"] == []
    silent = build_observation(side="new", raw=raw)
    diff = diff_observations(_obs("old", SELECT), silent, base_environment=ENV, head_environment=ENV)
    assert diff.comparable is False and diff.changes == () and diff.differences == ("observer",)

    # A tracer that cannot attach (ptrace denied) falls back to the hook tier before the run.
    import lemoncrow.pro.capabilities.review.runtime_observation as module

    monkeypatch.setattr(module, "sandbox_strace", lambda: "/usr/bin/strace")
    monkeypatch.setattr(module, "strace_works", lambda _path: False)
    setup = ObservationSetup.create(tmp_path / "observe", ObservationSpec(), sandbox_root="/workspace")
    assert setup.tier == "hooks" and setup.wrap(["python3"]) == ["python3"]
    setup.cleanup()


def test_a_fingerprint_or_tier_mismatch_is_not_comparable_and_a_truncated_head_claims_no_removal() -> None:
    base, head = _obs("base", SELECT), _obs("head", WRITE)
    mismatch = diff_observations(
        base, head, base_environment=ENV, head_environment=OTHER_ENV, differences=("toolchain",)
    )
    assert mismatch.comparable is False and mismatch.changes == () and mismatch.differences == ("toolchain",)
    tiers = diff_observations(base, _obs("head", WRITE, tier="hooks"), base_environment=ENV, head_environment=ENV)
    assert tiers.comparable is False and "observer tiers differ" in tiers.reasons[1]
    unknown = diff_observations(base, head, base_environment="", head_environment="")
    assert unknown.comparable is False
    truncated = diff_observations(base, _obs("head", WRITE, truncated=True), base_environment=ENV, head_environment=ENV)
    assert [item.change for item in truncated.changes] == ["added"]
    assert "removals are not claimed" in truncated.reasons[0]


def test_truncated_base_never_invents_an_added_effect_or_query_regression() -> None:
    """Missing base observations are not evidence that the head introduced a behavior."""
    base = _obs("base", truncated=True)
    head = _obs("head", SELECT, WRITE)
    diff = diff_observations(base, head, base_environment=ENV, head_environment=ENV)
    assert diff.comparable is True
    assert diff.changes == ()
    assert diff.query_count == (0, 1)
    assert diff.query_count_comparable is False
    assert any("base" in reason and "truncated" in reason for reason in diff.reasons)
    assert RuntimeObservationProducer().produce(_context(_record(diff))).deltas == ()


def test_truncated_head_never_invents_removed_effect_or_query_regression() -> None:
    base = _obs("base", SELECT, WRITE)
    head = _obs("head", truncated=True)
    diff = diff_observations(base, head, base_environment=ENV, head_environment=ENV)
    assert diff.changes == ()
    assert diff.query_count_comparable is False
    assert RuntimeObservationProducer().produce(_context(_record(diff))).deltas == ()


def test_runtime_absence_is_worded_as_not_observed_rather_than_not_happening() -> None:
    from lemoncrow.pro.capabilities.review.runtime_observation import EffectChange, change_summary

    summary = change_summary(EffectChange("file_write", WRITE.identity, "removed", WRITE, None))
    assert "not observed" in summary and "no longer does" not in summary
    added = change_summary(EffectChange("file_write", WRITE.identity, "added", None, WRITE))
    assert "not observed" in added and "New file" not in added


def test_unresolved_runtime_evidence_is_visible_and_blocks_readiness() -> None:
    from lemoncrow.pro.capabilities.review.readiness import _behavior_condition

    partial = diff_observations(
        _obs("base", truncated=True),
        _obs("head", WRITE),
        base_environment=ENV,
        head_environment=ENV,
    )
    production = RuntimeObservationProducer().produce(_context(_record(partial)))
    assert production.deltas == ()
    assert any("runtime observation incomplete" in note for note in production.degraded)
    condition = _behavior_condition(0, 0, False, 0, incomplete_observations=len(production.degraded))
    assert condition.state == "unknown"
    assert condition.reason == "incomplete_runtime_observation"
    assert "re-run" in condition.detail

    incomparable = diff_observations(
        _obs("base", WRITE),
        _obs("head", SELECT),
        base_environment=ENV,
        head_environment=OTHER_ENV,
    )
    inconsistent = RuntimeObservationProducer().produce(_context(_record(incomparable)))
    assert any("runtime observation incomplete" in note for note in inconsistent.degraded)


def test_old_truncated_record_cannot_reintroduce_unproven_added_effects() -> None:
    """Persisted v1 results predate the direction-aware truncation fix."""
    base = _obs("base")
    head = _obs("head", SELECT, WRITE)
    diff = diff_observations(base, head, base_environment=ENV, head_environment=ENV)
    record = _record(diff)
    record["diff"].pop("query_count_comparable")
    record["diff"]["reasons"] = ["observation was truncated on at least one side; removals are not claimed"]
    assert RuntimeObservationProducer().produce(_context(record)).deltas == ()


def test_truncated_common_effect_does_not_invent_changed_query_count() -> None:
    base = _obs("base", SELECT, truncated=True)
    head = _obs("head", ObservedEffect("db_query", SELECT.identity, count=8))
    diff = diff_observations(base, head, base_environment=ENV, head_environment=ENV)
    assert diff.changes == ()
    assert diff.query_count_comparable is False
    assert RuntimeObservationProducer().produce(_context(_record(diff))).deltas == ()


def _record(diff: Any, *, comparable_env: str = ENV) -> dict[str, Any]:
    return {
        "v": 1,
        "kind": "diff",
        "recipe": {"id": "unit", "title": "unit", "digest": "d"},
        "executor_class": "author_machine",
        "environment": {"base": comparable_env, "head": comparable_env},
        "sides": {
            "base": {"evidence_ref": "check:test:runtime-observation:b", "observation": None},
            "head": {"evidence_ref": "check:test:runtime-observation:h", "observation": None},
        },
        "diff": diff.to_wire(),
    }


def _context(*records: dict[str, Any]) -> BehaviorContext:
    from lemoncrow.pro.capabilities.review.runtime_observation import RUNTIME_OBSERVATION_MEDIA_TYPE

    revision = SimpleNamespace(id="rev-1", review_id="review-1")
    payloads: dict[str, bytes] = {}
    evidence = []
    for index, record in enumerate(records):
        payload = json.dumps(record).encode()
        payloads[f"ev-{index}"] = payload
        evidence.append(
            SimpleNamespace(
                id=f"ev-{index}",
                revision_id="rev-1",
                mime_type=RUNTIME_OBSERVATION_MEDIA_TYPE,
                execution_intent="current",
                created_at=float(index),
                content_hash=hashlib.sha256(payload).hexdigest(),
            )
        )
    return BehaviorContext(
        review_id="review-1",
        revision=revision,  # type: ignore[arg-type]
        evidence=tuple(evidence),  # type: ignore[arg-type]
        read_artifact=lambda item: payloads.get(item.id),
    )


def test_the_producer_emits_side_effect_deltas_only_for_comparable_differences() -> None:
    head = _obs("head", SELECT, ObservedEffect("network", "connect 203.0.113.7:443", outcome="blocked"))
    diff = diff_observations(_obs("base", SELECT), head, base_environment=ENV, head_environment=ENV)
    production = RuntimeObservationProducer().produce(_context(_record(diff)))
    [delta] = production.deltas
    assert delta.kind == "side_effect" and delta.change == "added" and delta.severity == "high"
    assert delta.subject == "runtime:network:connect 203.0.113.7:443"
    assert "blocked by the sandbox and recorded" in delta.summary
    assert (
        delta.base is None and delta.head is not None and delta.head.evidence_ref == "check:test:runtime-observation:h"
    )
    assert [ref.ref for ref in delta.evidence] == [
        "artifact:ev-0",
        "check:test:runtime-observation:b",
        "check:test:runtime-observation:h",
    ]
    assert production.unchanged == 1 and {scope.side for scope in production.executed} == {"base", "head"}

    counted = diff_observations(
        _obs("base", SELECT),
        _obs("head", ObservedEffect("db_query", SELECT.identity, count=4)),
        base_environment=ENV,
        head_environment=ENV,
    )
    subjects = {delta.subject for delta in RuntimeObservationProducer().produce(_context(_record(counted))).deltas}
    assert subjects == {f"runtime:db_query:{SELECT.identity}", "runtime:db_query_count:unit"}

    not_comparable = diff_observations(_obs("base", SELECT), head, base_environment=ENV, head_environment=OTHER_ENV)
    quiet = RuntimeObservationProducer().produce(_context(_record(not_comparable)))
    assert quiet.deltas == () and len(quiet.executed) == 2

    tampered = _context(_record(diff))
    tampered = BehaviorContext(
        review_id=tampered.review_id,
        revision=tampered.revision,
        evidence=tampered.evidence,
        read_artifact=lambda _item: b"{}",
    )
    degraded = RuntimeObservationProducer().produce(tampered)
    assert degraded.deltas == () and "does not match its digest" in degraded.degraded[0]


def test_flow_steps_are_observed_only_with_a_matching_trace_and_otherwise_stay_inferred() -> None:
    def side(*edges: TraceEdge) -> dict[str, Any]:
        return {
            "evidence_ref": "check:test:runtime-observation:x",
            "observation": RuntimeObservation(side="head", tier="strace", trace=edges).to_wire(),
        }

    call = TraceEdge("api/app.py", "main", "core/engine.py", "run")
    gone = TraceEdge("api/app.py", "main", "store/legacy.py", "save")
    records = {
        "unit": {
            "evidence_id": "ev-9",
            "diff": {"comparable": True},
            "sides": {"base": side(gone), "head": side(call)},
        }
    }
    traces = observed_traces(records)
    flow = {
        "applicable": True,
        "steps": [
            {"edge_id": "e1", "from": "api/app.py", "to": "core/engine.py", "delta_status": "added"},
            {"edge_id": "e2", "from": "core/engine.py", "to": "store/db.py", "delta_status": "added"},
            {"edge_id": "e3", "from": "api/app.py", "to": "store/legacy.py", "delta_status": "removed"},
        ],
    }
    grounded = ground_flow(flow, traces)
    observed, inferred, removed = grounded["steps"]
    assert observed["grounding"] == "observed"
    assert observed["trace"] == {
        "side": "head",
        "caller_fn": "main",
        "callee_fn": "run",
        "recipe": "unit",
        "evidence_ref": "check:test:runtime-observation:x",
        "record_id": "ev-9",
    }
    assert inferred["grounding"] == "inferred" and inferred["trace"] is None
    assert removed["grounding"] == "observed" and removed["trace"]["side"] == "base"
    summary = grounded["observed"]
    assert (summary["grounded_steps"], summary["inferred_steps"], summary["comparable"]) == (2, 1, True)
    assert sequence_steps(summary["added"]) == ["api/app.py:main -> core/engine.py:run"]
    assert sequence_steps(summary["removed"]) == ["api/app.py:main -> store/legacy.py:save"]

    # Without a comparable pair, sequences are shown but no added/removed message is claimed.
    records["unit"]["diff"] = {"comparable": False}
    unpaired = ground_flow(flow, observed_traces(records))["observed"]
    assert unpaired["comparable"] is False and unpaired["added"] == [] and unpaired["head"]
    # No trace at all: every step is inferred.
    bare = ground_flow(flow, observed_traces({}))
    assert {step["grounding"] for step in bare["steps"]} == {"inferred"}
    assert bare["observed"]["available"] is False


def test_the_entry_budget_limits_later_recipes_and_says_so(tmp_path: Any) -> None:
    from lemoncrow.pro.capabilities.review.execution import ExecutorClass
    from lemoncrow.pro.capabilities.review.recipes import CheckRecipe
    from lemoncrow.pro.capabilities.review.runtime_observation_runs import (
        ObservationRunInputs,
        run_observation_entry,
    )
    from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store = ReviewStore(tmp_path / "store")
    revision = _store_revision(store, tmp_path)
    recipes = tuple(CheckRecipe(name, "test", ("python3", "-c", "pass")) for name in ("first", "second", "third"))
    calls: list[tuple[str, str]] = []
    metered: list[float] = []

    def run_side(runtime: Any, surface: Any, side: str) -> Any:
        calls.append((runtime.id, side))
        time.sleep(0.05)
        return SurfaceRunResult(surface.id, surface.provider, side, "passed", "ok", "command", runtime.id)

    report = run_observation_entry(
        ObservationRunInputs(
            store=store,
            revision=revision,
            recipes=recipes,
            executor_class=ExecutorClass.AUTHOR_MACHINE,
            policy_digest="p",
            budget_seconds=0.12,
        ),
        run_side,
        lambda _execution, wall: metered.append(wall),
    )
    assert calls == [("observe:first", "old"), ("observe:first", "new")]
    assert len(metered) == 2 and report.executions == 2
    assert report.limited_by_budget is True
    assert report.notes == ["Limited by budget: not observed: second, third"]
    assert [row["status"] for row in report.results] == ["observed", "limited_by_budget", "limited_by_budget"]
    # The first recipe had no observation (the fake runner returns none): not comparable, never a delta.
    assert report.results[0]["diff"]["comparable"] is False


def _store_revision(store: Any, tmp_path: Any) -> Any:
    import pygit2

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
    from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
    from tests.gateway.test_review_check_runs_api import _commit, _write

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    _write(repo_root, "app.py", "x = 1\n")
    base = _commit(repo, "base", 0)
    _write(repo_root, "app.py", "x = 2\n")
    _commit(repo, "head", 1)
    rng = resolve_rev_range(repo_root, base=base, head="HEAD")
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store.root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store.root, build=build)
    store.publish_snapshot(session.id, revision.id)
    return revision
