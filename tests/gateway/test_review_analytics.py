"""P13-ANALYTICS: policy effectiveness, review quality and cost analytics (Phase 13 items 1-8).

Each guard is asserted beside its counter-case: stale and retracted findings
never count as current, customer and managed spend never merge, incident links
never count as escaped defects, every aggregate drills down to exactly the
records it counted, bounds hold at and past the cap, and analytics can neither
write Review state nor be imported by anything that decides readiness.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.analytics import (
    AnalyticsQueryError,
    AnalyticsReport,
    AnalyticsScope,
    RepositorySource,
    Viewer,
    analyze,
    parse_drill_key,
    parse_scope,
)
from lemoncrow.pro.capabilities.review.analytics.facts import (
    OPTIONAL_SOURCES,
    READ_METHODS,
    AnalyticsWriteRefused,
    ComputeFact,
    FindingFact,
    HostUsage,
    ReadOnlyStore,
    ReviewFacts,
    UsageFact,
    cost_class_of,
    review_facts,
)
from lemoncrow.pro.capabilities.review.analytics.load import reviewer_load
from lemoncrow.pro.capabilities.review.analytics.report import DrillIndex, build_report, select_facts
from lemoncrow.pro.capabilities.review.analytics.scope import (
    MAX_DRILL_KEY_CHARS,
    MAX_PERIOD_DAYS,
    MAX_REVIEWS,
    MAX_SCOPE_PATHS,
    MAX_SCOPE_REPOS,
)
from lemoncrow.pro.capabilities.review.analytics.service import sessions_in_period
from lemoncrow.pro.capabilities.review.anchors import build_anchor
from lemoncrow.pro.capabilities.review.behavior import behavior_fingerprint, behavior_key
from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, DetectedCapabilities
from lemoncrow.pro.capabilities.review.compute_ledger import ComputeLedger
from lemoncrow.pro.capabilities.review.inbox_views import InboxRow
from lemoncrow.pro.capabilities.review.outcomes.outcome_feeds import EscapedDefect
from lemoncrow.pro.capabilities.review.run_plan import (
    PlanInputs,
    SettingOrigin,
    build_run_plan,
    parse_run_settings,
    persist_run_plan,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    ReviewIntelligenceRun,
    ReviewMark,
    ReviewRevision,
    ReviewSession,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
RULE = "rule:repository:no-raw-sql@sha256:" + "a" * 64
REPO = SettingOrigin("trusted_base_repository", "base Snapshot .lemoncrow/review.yaml")


def _at(days: float = 0.0, *, base: datetime = NOW) -> str:
    return (base - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _values(**values: Any) -> Callable[[str], list[str]]:

    def read(name: str) -> list[str]:
        raw = values.get(name)
        if raw is None:
            return []
        return [str(item) for item in raw] if isinstance(raw, (list, tuple)) else [str(raw)]

    return read


def _scope(*, now: datetime = NOW, teams: bool = True, **values: Any) -> AnalyticsScope:
    return parse_scope(_values(**values), now=now, teams_supported=teams)


def _change(
    store: ReviewStore, *, title: str = "Change", created: str = "", actor: str = "agent", revisions: int = 1
) -> tuple[ReviewSession, list[ReviewRevision]]:
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root="/repo/analytics",
            range_mode="commit_range",
            title=title,
            actor_type=actor,
            created_at=created or _at(5),
            updated_at=created or _at(5),
        )
    )
    out: list[ReviewRevision] = []
    for number in range(revisions):
        out.append(
            store.add_revision(
                ReviewRevision(
                    id=new_revision_id(),
                    review_id=session.id,
                    revision_number=0,
                    range_mode="commit_range",
                    tree_fingerprint=f"tree-{number}",
                    packet_schema_version=1,
                    head_sha=f"{number + 1:040d}",
                ),
                [],
            )
        )
    current = store.get_session(session.id)
    assert current is not None
    return (current, out)


def _finding(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    disposition: str = "open",
    reason: str = "",
    state: str = "open",
    rules: tuple[str, ...] = (RULE,),
    path: str = "src/app.py",
    source: str = "ai_review",
) -> Annotation:
    ai = source == "ai_review"
    return store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=build_anchor(path=path, text="one\ntwo\n", start_line=1, end_line=1),
            body=f"{disposition} {state}",
            title=f"Finding {disposition} {state}",
            state=state,
            created_by="LemonCrow AI Review" if ai else "lemoncrow",
            created_by_actor="agent" if ai else "unknown",
            source=source,
            source_id="secrets" if not ai else "finding",
            finding_disposition=disposition if ai else "",
            finding_disposition_reason=reason,
            rule_refs=rules if ai else (),
        )
    )


def _run(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    capability: str = "finding_investigation",
    calls: int = 3,
    cost: float | None = 0.25,
    currency: str = "USD",
    key: str = "k1",
) -> None:
    row, _created = store.claim_intelligence_run(
        ReviewIntelligenceRun(
            review_id=session.id,
            id="",
            revision_id=revision.id,
            snapshot_identity=revision.id,
            capability=capability,
            run_key=f"{capability}:{key}",
            scope_digest="s",
            context_digest="c",
            policy_digest="p",
            schema_digest="d",
            driver="api",
            model="model-x",
            prompt_version=1,
            schema_version=1,
            status="pending",
        )
    )
    usage: dict[str, Any] = {"calls": calls}
    if cost is not None:
        usage.update(provider_cost=cost, currency=currency)
    assert store.complete_intelligence_run(
        session.id, row.id, status="ready", usage_json=json.dumps({"usage": usage}), model_calls=calls
    )


def _plan(store: ReviewStore, session: ReviewSession, revision: ReviewRevision, *, run_on: str = "local") -> None:
    layer = parse_run_settings({"effort": "thorough", "run_on": run_on}, origin=REPO)
    assert layer.errors == (), layer.errors
    plan = build_run_plan(
        PlanInputs(
            review_id=session.id,
            revision_id=revision.id,
            trigger="open",
            settings=resolve_run_settings([layer]),
            detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
            changed_paths=("src/app.py",),
            allowed_run_on=(run_on,),
        )
    )
    persist_run_plan(store, plan)


def _drills(wire: Any) -> Iterator[tuple[str, Mapping[str, Any] | None]]:
    """Every drill key the wire names, with the row that names it (``None`` for a bare reference)."""
    if isinstance(wire, Mapping):
        drill = wire.get("drill")
        if isinstance(drill, str):
            yield (drill, wire)
        elif isinstance(drill, Mapping):
            for key in drill.values():
                yield (str(key), None)
        for name in ("numerator_drill", "denominator_drill"):
            value = wire.get(name)
            for key in [value] if isinstance(value, str) else value or ():
                yield (str(key), None)
        for value in wire.values():
            yield from _drills(value)
    elif isinstance(wire, list):
        for item in wire:
            yield from _drills(item)


def _assert_provenance(report: AnalyticsReport) -> int:
    """Every aggregate the wire shows re-adds from its drill-down records."""
    seen = 0
    for key, row in _drills(report.wire):
        drill = report.drill(key, limit=10000)
        assert drill["known"], key
        assert drill["count"] == len(drill["records"]), key
        assert drill["value"] == pytest.approx(sum(item["value"] for item in drill["records"]), abs=1e-05), key
        assert all(item["review_path"] == f"/r/{item['review_id']}" for item in drill["records"]), key
        if row is not None:
            assert row["count"] == drill["count"], key
            assert row["reviews"] == len({(item["repo_id"], item["review_id"]) for item in drill["records"]}), key
            if "value" in row:
                assert row["value"] == pytest.approx(drill["value"], abs=1e-05), key
        seen += 1
    return seen


def _source(store: ReviewStore, repo_id: str = "repo-a", **extra: Any) -> RepositorySource:
    sessions = store.list_sessions(status="", limit=None)
    return RepositorySource(repo_id=repo_id, store=store, sessions=sessions, repo_name=repo_id, **extra)


def test_review_analytics_scope_contract() -> None:
    """case: test_scope_defaults_and_every_bound_holds_at_and_past_its_cap"""
    scope = _scope()
    assert scope.until == NOW and scope.since == NOW - timedelta(days=30)
    assert _scope(days=MAX_PERIOD_DAYS).since == NOW - timedelta(days=MAX_PERIOD_DAYS)
    with pytest.raises(AnalyticsQueryError) as caught:
        _scope(days=MAX_PERIOD_DAYS + 1)
    assert caught.value.field == "days" and caught.value.limit == MAX_PERIOD_DAYS
    assert _scope(since="2025-10-04", until="2026-10-05").since == datetime(2025, 10, 4, tzinfo=UTC)
    with pytest.raises(AnalyticsQueryError):
        _scope(since="2025-10-03", until="2026-10-05")
    for bad in ({"since": "2026-10-01", "days": 3}, {"since": "2026-10-06"}, {"days": "x"}, {"since": "soon"}):
        with pytest.raises(AnalyticsQueryError):
            _scope(**bad)
    repos = [f"r{index}" for index in range(MAX_SCOPE_REPOS)]
    assert len(_scope(repo=repos).repos) == MAX_SCOPE_REPOS
    with pytest.raises(AnalyticsQueryError) as caught:
        _scope(repo=[*repos, "one-more"])
    assert caught.value.field == "repo"
    paths = [f"src/p{index}" for index in range(MAX_SCOPE_PATHS)]
    assert len(_scope(path=paths).paths) == MAX_SCOPE_PATHS
    with pytest.raises(AnalyticsQueryError):
        _scope(path=[*paths, "src/extra"])
    for unsafe in ("../etc", "/abs", "a/../../b"):
        with pytest.raises(AnalyticsQueryError):
            _scope(path=unsafe)
    for bad_id in ("x" * 201, "a;b", "<script>"):
        with pytest.raises(AnalyticsQueryError):
            _scope(repo=bad_id)
    with pytest.raises(AnalyticsQueryError) as caught:
        _scope(team="payments", teams=False)
    assert caught.value.field == "team"
    assert parse_drill_key("k" * MAX_DRILL_KEY_CHARS) == "k" * MAX_DRILL_KEY_CHARS
    for bad_key in ("k" * (MAX_DRILL_KEY_CHARS + 1), "  "):
        with pytest.raises(AnalyticsQueryError):
            parse_drill_key(bad_key)
    "case: test_path_filters_match_directories_and_globs_only_inside_the_repository"
    scope = _scope(path=["src/billing", "docs/*.md"])
    assert scope.path_selected("src/billing/charge.py") and scope.path_selected("src/billing")
    assert not scope.path_selected("src/billing_old/x.py") and (not scope.path_selected("src/app.py"))
    assert scope.path_selected("docs/intro.md") and (not scope.path_selected("docs/intro.txt"))
    "case: test_drill_down_lists_at_most_its_limit_but_reports_the_exact_total"
    index = DrillIndex()
    facts = [_facts(f"rev-{index_}") for index_ in range(3)]
    report = build_report(facts, scope=_scope(), now=NOW, candidates=3, analyzed=3, load_index=index)
    drill = report.drill("volume.reviews", limit=2)
    assert drill["count"] == 3 and len(drill["records"]) == 2 and drill["truncated"]
    assert report.drill("volume.reviews", limit=3)["truncated"] is False
    unknown = report.drill("no.such.key")
    assert unknown["known"] is False and unknown["count"] == 0
    "case: test_stale_and_retracted_findings_never_inflate_current_metrics"
    findings = (
        _ai("open", "open"),
        _ai("acted", "acted_on"),
        _ai("dismissed", "dismissed", reason="incorrect"),
        _ai("stale", "invalidated", basis="stale"),
        _ai("retracted", "invalidated", basis="retracted"),
    )
    report = build_report([_facts("rev-1", findings=findings)], scope=_scope(), now=NOW, candidates=1, analyzed=1)
    wire = report.wire
    assert wire["findings"]["current"]["count"] == 3
    assert {row["key"]: row["count"] for row in wire["findings"]["invalidated"]} == {"stale": 1, "retracted": 1}
    assert wire["catch_contribution"]["useful"]["count"] == 1
    assert wire["catch_contribution"]["acted_on_rate"] == pytest.approx(1 / 3)
    [rule] = wire["rules"]
    assert rule["rule"] == RULE and rule["hits"] == 3 and (rule["dismissed"] == 1) and (rule["false_positive"] == 1)
    current = {item["record_id"] for item in report.drill("findings.current")["records"]}
    assert current == {"open", "acted", "dismissed"}
    assert _assert_provenance(report) > 20
    "case: test_rule_signal_names_the_exact_rule_and_needs_enough_judged_findings"
    acted = tuple(_ai(f"a{index}", "acted_on") for index in range(3))
    dismissed = tuple(_ai(f"d{index}", "dismissed", reason="not_applicable") for index in range(2))
    report = build_report(
        [_facts("rev-1", findings=acted + dismissed[:1])], scope=_scope(), now=NOW, candidates=1, analyzed=1
    )
    assert report.wire["rules"][0]["signal"] == "insufficient_data"
    report = build_report(
        [_facts("rev-1", findings=acted + dismissed)], scope=_scope(), now=NOW, candidates=1, analyzed=1
    )
    [rule] = report.wire["rules"]
    assert rule["signal"] == "signal" and report.wire["policy_effectiveness"]["signal"] == [RULE]
    assert rule["acted_on_rate"] == pytest.approx(0.6) and rule["dismissal_rate"] == pytest.approx(0.4)


def test_review_analytics_cost_and_signal_contract() -> None:
    """case: test_customer_and_managed_cost_are_reported_separately_and_currencies_never_add"""
    usage = (
        UsageFact("intelligence_run", "u1", "r1", "local", "customer", 4, 0.4, "USD", "finding_investigation"),
        UsageFact("intelligence_run", "u2", "r1", "managed", "managed", 2, 0.1, "USD", "finding_investigation"),
        UsageFact("intelligence_run", "u3", "r1", "worker", "customer", 1, 3.0, "EUR", "hypothesis_generation"),
        UsageFact("intelligence_run", "u4", "r1", "review_host", "managed", 5, None, "", "review_brief"),
    )
    compute = (
        ComputeFact("compute_usage", "c1", "", "local", "customer", "run", 2.0),
        ComputeFact("hosted_jobs", "c2", "", "managed", "managed", "review_intelligence", 1.5),
    )
    facts = _facts("rev-1", usage=usage, compute=compute, findings=(_ai("a", "acted_on"),))
    report = build_report([facts], scope=_scope(), now=NOW, candidates=1, analyzed=1)
    cost = report.wire["cost"]
    spend = {(row["cost_class"], row["currency"]): row["value"] for row in cost["provider_spend"]}
    assert spend == {("customer", "USD"): 0.4, ("customer", "EUR"): 3.0, ("managed", "USD"): 0.1}
    calls = {(row["cost_class"], row["locus"]): row["value"] for row in cost["model_calls"]}
    assert calls == {
        ("customer", "local"): 4,
        ("customer", "worker"): 1,
        ("managed", "managed"): 2,
        ("managed", "review_host"): 5,
    }
    assert [(row["cost_class"], row["value"]) for row in cost["calls_without_cost"]] == [("managed", 5)]
    minutes = {row["cost_class"]: row["value"] for row in cost["compute_minutes"]}
    assert minutes == {"customer": 2.0}
    assert [(row["cost_class"], row["value"]) for row in cost["job_minutes"]] == [("managed", 1.5)]
    per_useful = {
        (row["measure"], row["cost_class"], row.get("currency", "")): row["per_useful_finding"]
        for row in report.wire["cost_per_useful_finding"]
    }
    assert per_useful["provider_spend", "customer", "USD"] == pytest.approx(0.4)
    assert per_useful["provider_spend", "managed", "USD"] == pytest.approx(0.1)
    assert per_useful["model_calls", "customer", ""] == pytest.approx(5.0)
    _assert_provenance(report)
    "case: test_auto_approval_overrides_and_escalation_name_the_exact_gate_condition_and_trigger"

    def decision(eligible: bool, *failed: tuple[str, str]) -> dict[str, Any]:
        return {
            "eligible": eligible,
            "policy_generation": "gen-7",
            "decision_digest": f"d-{eligible}-{len(failed)}",
            "gates": [{"id": "ai_review", "label": "AI review ran", "passed": True, "reason": ""}]
            + [{"id": gate, "label": gate.title(), "passed": False, "reason": reason} for gate, reason in failed],
        }

    facts = [
        _facts("rev-1", auto_approval=decision(True), depth="auto"),
        _facts(
            "rev-2",
            auto_approval=decision(False, ("paths", "outside_allowed_paths"), ("size", "too_large")),
            depth="thorough",
            escalation_state="escalated",
            escalation_triggers=("criticality",),
            findings=(_ai("esc", "acted_on"),),
            usage=(
                UsageFact("intelligence_run", "u", "r1", "local", "customer", 4, None, "", "finding_investigation"),
            ),
        ),
        _facts(
            "rev-3",
            auto_approval=decision(False, ("paths", "outside_allowed_paths")),
            break_glass=(
                {
                    "id": "bg_1",
                    "revision_id": "rev-3-r1",
                    "bypassed": [{"id": "required_checks", "label": "Required checks"}],
                },
            ),
        ),
    ]
    report = build_report(facts, scope=_scope(), now=NOW, candidates=3, analyzed=3)
    auto = report.wire["auto_approval"]
    assert (auto["evaluated"]["count"], auto["eligible"]["count"], auto["not_eligible"]["count"]) == (3, 1, 2)
    gates = {(row["gate"], row["reason"]): row["count"] for row in auto["failed_gates"]}
    assert gates == {("paths", "outside_allowed_paths"): 2, ("size", "too_large"): 1}
    assert auto["failed_gates"][0]["policy_generations"] == ["gen-7"]
    overrides = report.wire["overrides"]
    assert overrides["break_glass"]["count"] == 1 and overrides["bypassed"][0]["key"] == "required_checks"
    escalation = report.wire["escalation"]
    assert escalation["escalated"]["reviews"]["count"] == 1 and escalation["standard"]["reviews"]["count"] == 2
    assert escalation["escalated"]["useful_per_review"] == 1 and escalation["escalated"]["model_calls_per_useful"] == 4
    assert [row["key"] for row in escalation["triggers"]] == ["criticality"]
    assert {row["key"]: row["count"] for row in report.wire["depth"]["distribution"]} == {
        "auto": 1,
        "thorough": 1,
        "unknown": 1,
    }
    _assert_provenance(report)
    "case: test_cost_class_follows_who_pays_for_the_executor"
    assert {cost_class_of(locus, review_host="customer") for locus in ("local", "worker", "customer")} == {"customer"}
    assert cost_class_of("managed", review_host="customer") == "managed"
    assert cost_class_of("review_host", review_host="customer") == "customer"
    assert cost_class_of("review_host", review_host="managed") == "managed"
    assert cost_class_of("somewhere", review_host="managed") == "unattributed"
    "case: test_incident_links_never_count_as_escaped_defects"

    def escape(kind: str, **extra: Any) -> EscapedDefect:
        return EscapedDefect("rev-1", "r1", "a" * 40, "production", kind, f"subject-{kind}", **extra)

    escapes = (
        escape("contradicted", depth="thorough", checks=("static.semgrep",)),
        escape("deployment_failed", depth="auto"),
        escape("incident_linked"),
        escape("incident"),
    )
    report = build_report([_facts("rev-1", escapes=escapes)], scope=_scope(), now=NOW, candidates=1, analyzed=1)
    wire = report.wire["escaped_defects"]
    assert wire["total"]["count"] == 2
    assert {row["key"] for row in wire["by_kind"]} == {"contradicted", "deployment_failed"}
    assert {row["key"]: row["count"] for row in wire["by_depth"]} == {"thorough": 1, "auto": 1}
    assert [row["key"] for row in wire["by_check"]] == ["static.semgrep"]
    _assert_provenance(report)


def test_review_analytics_privacy_contract() -> None:
    """case: test_team_and_path_filters_select_reviews_and_findings"""
    payments = _facts(
        "rev-pay",
        teams=frozenset({"payments"}),
        paths=("src/billing/charge.py", "src/app.py"),
        findings=(_ai("in", "open", path="src/billing/charge.py"), _ai("out", "open", path="src/app.py")),
    )
    web = _facts("rev-web", teams=frozenset({"web"}), paths=("web/index.ts",))
    assert [item.review_id for item in select_facts([payments, web], _scope(team="payments"))] == ["rev-pay"]
    assert select_facts([payments, web], _scope(team="infra")) == []
    scope = _scope(path="src/billing")
    selected = select_facts([payments, web], scope)
    assert [item.review_id for item in selected] == ["rev-pay"]
    report = build_report(selected, scope=scope, now=NOW, candidates=2, analyzed=2)
    assert {item["record_id"] for item in report.drill("findings.current")["records"]} == {"in"}
    "case: test_reviewer_load_names_sides_not_people_and_hides_other_teams"
    rows = [
        _inbox_row("rev-1", "needs_review", ("payments",), _at(2), views=("to_review",)),
        _inbox_row("rev-2", "waiting_on_author", ("web",), _at(1)),
        _inbox_row("rev-3", "waiting_on_author", (), _at(1), rule="agent_turn"),
        _inbox_row("rev-4", "needs_review", ("web-private",), _at(4)),
        _inbox_row("rev-5", "needs_review", ("payments",), _at(9), status="closed"),
    ]
    index = DrillIndex()
    waiting, load = reviewer_load(
        rows, index=index, now=NOW, viewer_teams=frozenset({"payments"}), all_teams_visible=False
    )
    sides = {row["key"]: row["count"] for row in waiting}
    assert sides == {"reviewers": 2, "author": 1, "agent": 1, "checks": 0, "decision": 0}
    reviewers = next(row for row in waiting if row["key"] == "reviewers")
    assert reviewers["max_hours"] == pytest.approx(96)
    assert load["teams_shown"] == "yours" and [row["key"] for row in load["by_team"]] == ["payments"]
    assert load["mine"]["to_review"]["count"] == 1 and load["individuals"] == "not_reported"
    assert "web-private" not in json.dumps(load) and (not any("web-private" in key for key in index.rows))
    _waiting, everything = reviewer_load(
        rows, index=DrillIndex(), now=NOW, viewer_teams=frozenset(), all_teams_visible=True
    )
    assert {row["key"] for row in everything["by_team"]} == {"payments", "web-private"}
    "case: test_nothing_that_decides_readiness_imports_analytics"
    root = Path(__file__).resolve().parents[2] / "src" / "lemoncrow" / "pro" / "capabilities" / "review"
    checked = 0
    for path in sorted(root.rglob("*.py")):
        if "analytics" in path.relative_to(root).parts or path.stem not in READINESS_MODULES:
            continue
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: Sequence[str] = ()
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(f"{node.module}.{alias.name}" for alias in node.names)]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            assert not any("review.analytics" in name for name in names), f"{path} imports analytics"
    assert checked >= 4
    "case: test_every_analytics_import_ships_in_the_review_engine_or_is_an_optional_source"
    shipped = {(_ROOT / relative).resolve() for relative in _engine_files()}
    optional = {
        f"lemoncrow.pro.capabilities.review.{module}" for src in OPTIONAL_SOURCES.values() for module in src.modules
    }
    assert all(_source_of(module) is not None for module in optional), "an optional source names a missing module"
    stray: list[str] = []
    for path in _ANALYTICS_FILES:
        assert path.resolve() in shipped, f"{path} is not in the Review engine"
        for module in _every_import(path):
            source = _source_of(module)
            if source is not None and source.resolve() not in shipped and (module not in optional):
                stray.append(f"{path.name}: {module}")
    assert stray == []


def test_review_cap_is_exact_at_the_bound_and_reports_truncation_past_it(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    for index in range(MAX_REVIEWS + 1):
        store.create_session(
            ReviewSession(
                id=new_session_id(),
                subject_type="pull_request",
                repo_root="/repo",
                range_mode="commit_range",
                title=f"c{index}",
                created_at=_at(1 + index / 1000),
            )
        )
    sessions, scan_truncated = sessions_in_period(store, _scope())
    assert len(sessions) == MAX_REVIEWS + 1 and (not scan_truncated)
    newest = sorted(sessions, key=lambda item: item.created_at, reverse=True)
    at_cap = analyze([RepositorySource("repo", store, newest[:MAX_REVIEWS])], scope=_scope(), now=NOW)
    assert at_cap.wire["bounds"] == {
        "max_reviews": MAX_REVIEWS,
        "candidates": MAX_REVIEWS,
        "analyzed": MAX_REVIEWS,
        "in_scope": MAX_REVIEWS,
        "truncated": False,
        "scan_truncated": False,
        "escapes_truncated": False,
    }
    past = analyze([RepositorySource("repo", store, sessions)], scope=_scope(), now=NOW)
    bounds = past.wire["bounds"]
    assert bounds["candidates"] == MAX_REVIEWS + 1 and bounds["analyzed"] == MAX_REVIEWS and bounds["truncated"]
    counted = {item["review_id"] for item in past.drill("volume.reviews", limit=10000)["records"]}
    assert newest[-1].id not in counted and len(counted) == MAX_REVIEWS


def test_session_scan_pages_until_the_period_starts_and_says_when_it_stops_at_its_cap(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    inside = [_change(store, title=f"in{index}", created=_at(index + 1))[0] for index in range(5)]
    for index in range(4):
        _change(store, title=f"old{index}", created=_at(60 + index))
    found, truncated = sessions_in_period(store, _scope(), page=2)
    assert {item.id for item in found} == {item.id for item in inside} and (not truncated)
    found, truncated = sessions_in_period(store, _scope(), page=2, max_scan=6)
    assert len(found) == 5 and (not truncated)
    found, truncated = sessions_in_period(store, _scope(), page=2, max_scan=4)
    assert len(found) == 4 and truncated
    report = analyze([RepositorySource("repo-a", store, found, scan_truncated=truncated)], scope=_scope(), now=NOW)
    assert report.wire["bounds"]["scan_truncated"] is True


def test_escape_matching_reads_a_bounded_ledger_window_and_says_only_when_it_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow.pro.capabilities.review.analytics import service
    from lemoncrow.pro.capabilities.review.outcomes import observation_outcomes

    monkeypatch.setattr(observation_outcomes, "SCAN_BATCH", 3)

    class _Ledger:

        def __init__(self, rows: int) -> None:
            self.rows, self.limits = (rows, [])

        def derived_results_by_kind(self, kind: str, *, limit: int) -> tuple[Any, ...]:
            self.limits.append(limit)
            return tuple(type("Row", (), {"review_id": f"{kind}-{index}"})() for index in range(min(self.rows, limit)))

    at_bound, past_bound = (_Ledger(3), _Ledger(4))
    found = service._escape_candidates(at_bound)
    assert not found.truncated and len(found.review_ids) == 6 and (at_bound.limits == [4, 4])
    found = service._escape_candidates(past_bound)
    assert found.truncated and len(found.review_ids) == 6


def _facts(review_id: str, *, repo_id: str = "repo-a", **fields: Any) -> ReviewFacts:
    base: dict[str, Any] = {
        "review_id": review_id,
        "repo_id": repo_id,
        "repo_name": repo_id,
        "title": review_id,
        "status": "open",
        "actor_type": "agent",
        "created_at": _at(3),
        "revision_ids": (f"{review_id}-r1",),
        "latest_revision_id": f"{review_id}-r1",
        "latest_revision_at": _at(3),
    }
    base.update(fields)
    return ReviewFacts(**base)


def _ai(record_id: str, outcome: str, *, basis: str = "", reason: str = "", path: str = "src/app.py") -> FindingFact:
    return FindingFact(
        source="ai",
        record_id=record_id,
        revision_id="r1",
        category="correctness",
        severity="p1",
        outcome=outcome,
        rules=(RULE,),
        check="ai_review",
        basis=basis or outcome,
        dismissal_reason=reason,
        path=path,
        created_at=_at(2),
    )


def _inbox_row(review_id: str, state: str, teams: tuple[str, ...], since: str, **extra: Any) -> InboxRow:
    return InboxRow(
        repo_id="repo-a",
        review_id=review_id,
        status=extra.get("status", "open"),
        views=extra.get("views", ()),
        workflow_state=state,
        priority="normal",
        sort_key=(),
        team_keys=frozenset(teams),
        wire={
            "id": review_id,
            "title": review_id,
            "attention": {"waiting_since": since},
            "workflow": {"rule": extra.get("rule", "")},
        },
    )


def test_findings_outcomes_come_from_human_judgment_and_drop_stale_and_retracted(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    # Created inside the real-now period the report below reads (a fixed date ages out of it).
    session, [first, second] = _change(store, revisions=2, created=_at(5, base=_real_now()))
    _finding(store, session, second)
    _finding(store, session, second, disposition="confirmed_by_reviewer")
    _finding(store, session, second, disposition="dismissed", reason="incorrect")
    _finding(store, session, first, disposition="superseded_stale")
    _finding(store, session, second, state="orphaned")
    _finding(store, session, second, source="lemoncrow", state="resolved")
    facts = review_facts(store, session, repo_id="repo-a")
    outcomes = sorted((item.source, item.outcome, item.basis) for item in facts.findings)
    assert outcomes == [
        ("ai", "acted_on", "confirmed_by_reviewer"),
        ("ai", "dismissed", "dismissed"),
        ("ai", "invalidated", "retracted"),
        ("ai", "invalidated", "stale"),
        ("ai", "open", "open"),
        ("deterministic", "acted_on", "resolved"),
    ]
    report = analyze([_source(store)], scope=_scope(now=_real_now()), now=_real_now())
    assert report.wire["findings"]["current"]["count"] == 4
    assert report.wire["catch_contribution"]["deterministic"]["count"] == 1
    assert report.wire["catch_contribution"]["ai"]["count"] == 1
    assert {row["rule"] for row in report.wire["rules"]} == {RULE, "lemoncrow:secrets"}
    _assert_provenance(report)


def _real_now() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=1)


def test_imported_static_findings_resolve_on_later_snapshots_and_stale_ones_never_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gateway import test_review_check_ingest as ingest

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    review = ingest._review(tmp_path)
    first = ingest._ingest_sarif(
        review,
        ingest._sarif(
            ingest._result("py.eval", level="error", line=2, message="eval of input"),
            ingest._result("py.sqli", level="error", line=1, message="sql built from input"),
        ),
    )
    assert first.status_code == 201, first.text
    old_tool = ingest._ingest_sarif(
        review, ingest._sarif(ingest._result("B101", line=1), tool="bandit"), title="bandit"
    )
    assert old_tool.status_code == 201, old_tool.text
    shown = {row["rule_id"]: row for row in ingest._get(review, "static-findings")["findings"]}
    judged = ingest._post(
        review,
        f"static-findings/{shown['py.eval']['id']}/disposition",
        {"disposition": "dismissed", "reason": "incorrect"},
    )
    assert judged.status_code == 200, judged.text
    ingest._push(review, "src/report.py", "\n" * 5 + ingest._CHANGED)
    later = ingest._ingest_sarif(
        review, ingest._sarif(ingest._result("py.eval", level="error", line=7, message="eval of input"))
    )
    assert later.status_code == 201, later.text
    session = review.store.get_session(review.review_id)
    facts = review_facts(review.store, session, repo_id="repo-a")
    outcomes = {item.rules[0]: (item.outcome, item.basis) for item in facts.findings if item.source == "deterministic"}
    assert outcomes == {
        "semgrep:py.eval": ("dismissed", "dismissed"),
        "semgrep:py.sqli": ("acted_on", "resolved_on_later_snapshot"),
        "bandit:B101": ("invalidated", "stale"),
    }
    now = _real_now()
    report = analyze([_source(review.store)], scope=_scope(now=now), now=now)
    rules = {row["rule"]: row for row in report.wire["rules"]}
    assert set(rules) == {"semgrep:py.eval", "semgrep:py.sqli"}
    assert (rules["semgrep:py.eval"]["dismissed"], rules["semgrep:py.eval"]["false_positive"]) == (1, 1)
    assert rules["semgrep:py.sqli"]["acted_on"] == 1 and rules["semgrep:py.eval"]["tool"] == "semgrep"
    usefulness = {row["rule_id"]: row for row in ingest._get(review, "checks/usefulness")["rules"]}
    assert usefulness["py.eval"]["dismissed"] == 1 and usefulness["py.eval"]["false_positive"] == 1
    assert {row["key"]: row["count"] for row in report.wire["findings"]["invalidated"]} == {"stale": 1, "retracted": 0}
    _assert_provenance(report)


def test_correction_loops_complete_only_when_a_human_reviews_the_fixed_snapshot(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.review_actions import (
        PROMPT_FALLBACK,
        ActionEvent,
        ActionSubject,
        ReviewAction,
        ReviewActionLedger,
    )

    store = ReviewStore(tmp_path / "store")
    now = _real_now()
    session, [source, fixed] = _change(store, created=_at(1, base=now), revisions=2)
    target = _finding(store, session, source)
    ledger = ReviewActionLedger(store)

    def action(action_id: str) -> None:
        ledger.create(
            ReviewAction(
                id=action_id,
                review_id=session.id,
                kind="fix",
                source_revision_id=source.id,
                route=PROMPT_FALLBACK,
                status="awaiting_snapshot",
                created_by="reviewer",
                created_at=_at(0.5, base=now),
                updated_at=_at(0.5, base=now),
                subjects=(ActionSubject(kind="ai_finding", id=target.id, title="t"),),
            )
        )

    for action_id in ("act-done", "act-waiting", "act-cancelled"):
        action(action_id)
    ledger.transition(
        source.id,
        "act-done",
        status="snapshot_recorded",
        allowed_from=frozenset({"awaiting_snapshot"}),
        event=ActionEvent("snapshot_recorded", _at(0.4, base=now), "system"),
        resulting_revision_id=fixed.id,
    )
    ledger.transition(
        source.id,
        "act-cancelled",
        status="cancelled",
        allowed_from=frozenset({"awaiting_snapshot"}),
        event=ActionEvent("cancelled", _at(0.4, base=now), "human", "reviewer"),
    )
    loops = analyze([_source(store)], scope=_scope(now=now), now=now).wire["correction_loops"]
    counts = {
        name: loops[name]["count"] for name in ("started", "snapshot_recorded", "re_reviewed", "in_flight", "cancelled")
    }
    assert counts == {"started": 3, "snapshot_recorded": 1, "re_reviewed": 0, "in_flight": 1, "cancelled": 1}
    assert loops["completion_rate"] == 0
    store.record_outcome(session.id, source.id, "alice", outcome="comment")
    loops = analyze([_source(store)], scope=_scope(now=now), now=now).wire["correction_loops"]
    assert loops["re_reviewed"]["count"] == 0
    store.record_outcome(session.id, fixed.id, "alice", outcome="lgtm")
    report = analyze([_source(store)], scope=_scope(now=now), now=now)
    assert report.wire["correction_loops"]["re_reviewed"]["count"] == 1
    assert report.wire["correction_loops"]["completion_rate"] == pytest.approx(0.5)
    assert report.wire["catch_contribution"]["ai"]["count"] == 1
    _assert_provenance(report)


def test_time_to_first_review_counts_only_human_judgment(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    now = _real_now()
    judged, [revision] = _change(store, title="judged", created=_at(2, base=now))
    _change(store, title="waiting", created=_at(1, base=now))
    policy, [policy_revision] = _change(store, title="policy", created=_at(1, base=now))
    store.record_outcome(judged.id, revision.id, "alice", outcome="lgtm")
    store.record_outcome(policy.id, policy_revision.id, "lemoncrow-policy", outcome="lgtm", actor_type="agent")
    report = analyze([_source(store)], scope=_scope(now=now), now=now)
    timing = report.wire["time_to_first_review"]
    assert timing["measured"]["count"] == 1 and timing["awaiting"]["count"] == 2
    [record] = report.drill("first_review.measured")["records"]
    assert record["review_id"] == judged.id and record["record_kind"] == "outcome"
    assert record["value"] == pytest.approx(48, abs=0.1)
    _assert_provenance(report)


def test_local_and_managed_execution_split_by_plan_executor_and_review_host(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    now = _real_now()
    local, [local_rev] = _change(store, title="local", created=_at(1, base=now))
    managed, [managed_rev] = _change(store, title="managed", created=_at(1, base=now))
    _plan(store, local, local_rev, run_on="local")
    _plan(store, managed, managed_rev, run_on="managed")
    _run(store, local, local_rev, calls=4, cost=0.4)
    _run(store, managed, managed_rev, calls=2, cost=0.1)
    _run(store, local, local_rev, capability="review_brief", calls=1, cost=None)
    ComputeLedger(store).record(
        review_id=local.id,
        revision_id=local_rev.id,
        entry_id="static.semgrep",
        executor_class="local",
        stage="run",
        seconds=120,
    )

    def split(report: AnalyticsReport) -> dict[tuple[str, str], float]:
        return {(row["cost_class"], row["locus"]): row["value"] for row in report.wire["cost"]["model_calls"]}

    on_local = analyze([_source(store)], scope=_scope(now=now), now=now)
    assert split(on_local) == {("customer", "local"): 4, ("managed", "managed"): 2, ("customer", "review_host"): 1}
    assert {row["cost_class"]: row["value"] for row in on_local.wire["cost"]["compute_minutes"]} == {"customer": 2.0}
    depth = {row["key"]: row["count"] for row in on_local.wire["depth"]["distribution"]}
    assert depth == {"thorough": 2}
    hosted = analyze(
        [
            _source(
                store,
                usage=lambda sessions: HostUsage(
                    review_host_cost_class="managed",
                    external={
                        local.id: [
                            {
                                "stage": "review_intelligence",
                                "locus": "customer",
                                "model_calls": 6,
                                "provider_cost": 0.6,
                                "compute_seconds": 30,
                            }
                        ]
                    },
                ),
            )
        ],
        scope=_scope(now=now),
        now=now,
        viewer=Viewer(review_host_cost_class="managed"),
    )
    assert split(hosted) == {("customer", "local"): 4, ("managed", "managed"): 2, ("managed", "review_host"): 1}
    jobs = {(row["cost_class"], row["executor"]): row["value"] for row in hosted.wire["cost"]["job_minutes"]}
    assert jobs == {("customer", "customer"): 0.5}
    assert {row["cost_class"]: row["value"] for row in hosted.wire["cost"]["compute_minutes"]} == {"customer": 2.0}
    _assert_provenance(hosted)


def test_side_effect_drift_links_each_change_to_its_review_and_human_judgment(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.behavior import BehaviorDelta
    from tests.gateway.test_review_observability import _inventory

    store = ReviewStore(tmp_path / "store")
    now = _real_now()
    session, [revision] = _change(store, created=_at(1, base=now))
    added = BehaviorDelta(
        kind="side_effect",
        producer="static_side_effects",
        subject="outbound_host:api.example.com",
        title="Outbound host added: api.example.com",
        summary="",
        change="added",
        paths=("src/client.py",),
    )
    removed = BehaviorDelta(
        kind="side_effect",
        producer="static_side_effects",
        subject="db_table:charges",
        title="Table removed: charges",
        summary="",
        change="removed",
        paths=("db/schema.sql",),
    )
    _inventory(store, session, added, removed)
    event_mark = store.set_mark(
        ReviewMark(
            review_id=session.id,
            unit_key=behavior_key(added),
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint=behavior_fingerprint(added),
            reviewer_id="alice",
        )
    )
    assert event_mark.state == "reviewed"
    report = analyze([_source(store)], scope=_scope(now=now), now=now)
    drift = report.wire["drift"]
    assert drift["changes"]["count"] == 2
    judged = {item["title"]: item["judgment"] for item in drift["items"]}
    assert judged["Outbound host added: api.example.com"]["state"] == "accepted"
    assert judged["Outbound host added: api.example.com"]["by"] == "alice"
    assert judged["Outbound host added: api.example.com"]["event_id"]
    assert judged["Table removed: charges"]["state"] == "not_judged"
    assert all(item["review_path"] == f"/r/{session.id}" for item in drift["items"])
    narrowed = analyze([_source(store)], scope=_scope(now=now, path="db"), now=now)
    assert narrowed.wire["drift"]["changes"]["count"] == 0
    _assert_provenance(report)


def test_escapes_come_from_production_problems_and_incident_links_never_count(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import due_plans, record_aggregates
    from tests.gateway.test_review_observability import _answers, _started
    from tests.gateway.test_review_observability import _at as _obs_at

    store, session = _started(tmp_path)
    [plan] = due_plans(store, now=_obs_at(13.5))
    scan = next(query for query in plan["queries"] if query["signal"] == "incident_scan")
    incident = {
        "source": "alert",
        "id": "p-1",
        "name": "HighLatency",
        "route": "POST /v1/charges",
        "started_at": _obs_at(4),
    }
    linked = record_aggregates(
        store,
        session.id,
        plan["scope"],
        [{"query_id": scan["id"], "status": "ok", "incidents": [incident]}],
        now=_obs_at(13.6),
    )
    assert linked.incidents
    now = _real_now()
    scope = _scope(now=now, days=MAX_PERIOD_DAYS)
    assert analyze([_source(store)], scope=scope, now=now).wire["escaped_defects"]["total"]["count"] == 0
    answers = _answers(plan, {"outbound_calls": {"status": "ok", "value": 7}})
    assert record_aggregates(store, session.id, plan["scope"], answers, now=_obs_at(13.7)).contradicted
    report = analyze([_source(store)], scope=scope, now=now)
    escapes = report.wire["escaped_defects"]
    assert escapes["total"]["count"] == 1 and [row["key"] for row in escapes["by_kind"]] == ["contradicted"]
    _assert_provenance(report)


def test_the_store_guard_exposes_only_reads(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    missing = sorted(name for name in READ_METHODS if not callable(getattr(store, name, None)))
    assert missing == []
    guarded = ReadOnlyStore(store)
    for writer in (
        "put_derived_results",
        "add_annotation",
        "record_outcome",
        "set_mark",
        "update_session",
        "_transaction",
    ):
        with pytest.raises(AnalyticsWriteRefused):
            getattr(guarded, writer)
    with pytest.raises(AnalyticsWriteRefused):
        guarded._store = store
    assert ReadOnlyStore(guarded).unwrap() is store
    assert all(not name.startswith(("put_", "add_", "set_", "record_", "update_", "delete_")) for name in READ_METHODS)


def test_a_full_analytics_read_leaves_the_review_store_unchanged(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    now = _real_now()
    session, [revision] = _change(store, created=_at(1, base=now))
    _finding(store, session, revision)
    _run(store, session, revision)

    def snapshot() -> list[tuple[str, int]]:
        conn = sqlite3.connect(store.db_path)
        try:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return [(name, conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]) for name in sorted(tables)]
        finally:
            conn.close()

    before = snapshot()
    report = analyze([_source(store)], scope=_scope(now=now), now=now)
    assert report.wire["gating"] == "never" and report.wire["individuals"] == "not_reported"
    assert snapshot() == before and before


READINESS_MODULES = (
    "readiness",
    "auto_approval",
    "landing",
    "landing_gates",
    "workflow_state",
    "blocking_reasons",
    "premerge",
    "review_policy",
    "inbox_views",
    "attention",
    "priority",
)
_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_ALLOWLIST = _ROOT / "server" / "review-engine-files.txt"
_SOURCE_ROOTS = {
    "lemoncrow": _ROOT / "src",
    "lemoncrow_client": _ROOT / "client" / "src",
    "lemoncrow_server_core": _ROOT / "server" / "src",
}
_ANALYTICS_FILES = (
    *sorted((_ROOT / "src" / "lemoncrow" / "pro" / "capabilities" / "review" / "analytics").glob("*.py")),
    _ROOT / "server" / "src" / "lemoncrow_server_core" / "local_review_analytics.py",
)


def _engine_files() -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in _ENGINE_ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and (not line.lstrip().startswith("#"))
    )


def _source_of(module: str) -> Path | None:
    root = _SOURCE_ROOTS.get(module.split(".", 1)[0])
    if root is None:
        return None
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _every_import(path: Path) -> list[str]:
    """First-party modules *path* imports anywhere -- module level, functions, guards."""
    root = next(root for root in _SOURCE_ROOTS.values() if path.is_relative_to(root))
    package = ".".join(path.relative_to(root).parts[:-1])
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parent = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{parent}.{node.module}" if node.module else parent
            else:
                base = node.module or ""
            modules.extend([base, *(f"{base}.{alias.name}" for alias in node.names)])
    return [module for module in modules if module.split(".", 1)[0] in _SOURCE_ROOTS]


_ENGINE_PROBE = '\nimport asyncio, json, sys\nfrom pathlib import Path\n\ntree, state = Path(sys.argv[1]), Path(sys.argv[2])\nimport lemoncrow, lemoncrow_server_core\nassert Path(lemoncrow.__file__).is_relative_to(tree), lemoncrow.__file__\nassert Path(lemoncrow_server_core.__file__).is_relative_to(tree), lemoncrow_server_core.__file__\n\nfrom aiohttp import ClientSession\nfrom lemoncrow_server_core.bootstrap import build_local_server\nfrom lemoncrow_server_core.config import LocalServerConfig\nfrom lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION\nfrom lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit\n\nREPO = "repo_0123456789abcdef0123456789abcdef"\n\n\nasync def main() -> None:\n    server, local = build_local_server(\n        LocalServerConfig(\n            listen_port=0,\n            state_root=state,\n            local_no_auth=True,\n            allow_local_fs=False,\n            products=frozenset({"review"}),\n        )\n    )\n    store = local.review_repository.store(REPO)\n    session = store.create_session(\n        ReviewSession(id="", subject_type="local_change", repo_root="/srv/engine", range_mode="working_tree", title="engine")\n    )\n    revision = store.add_revision(\n        ReviewRevision(\n            id="", review_id=session.id, revision_number=0, range_mode="working_tree",\n            tree_fingerprint="tree", packet_schema_version=SCHEMA_VERSION,\n        ),\n        [ReviewUnit(revision_id="", unit_key="file:a.py", kind="file", path="a.py", content_fingerprint="fp", fingerprint_method="blob_sha256")],\n    )\n    store.set_current_revision(session.id, revision.id)\n    url = await server.start()\n    out = {}\n    try:\n        async with ClientSession() as client:\n            async with client.get(f"{url}/api/review-analytics") as response:\n                out["status"], out["report"] = response.status, await response.json()\n            window = {"since": out["report"]["scope"]["since"], "until": out["report"]["scope"]["until"]}\n            async with client.get(f"{url}/api/review-analytics/drill", params={**window, "key": "volume.reviews"}) as response:\n                out["drill_status"], out["drill"] = response.status, await response.json()\n    finally:\n        await server.stop()\n    print("PROBE" + json.dumps(out))\n\n\nasyncio.run(main())\n'


def test_the_review_engine_serves_analytics_and_marks_what_it_does_not_ship(tmp_path: Path) -> None:
    """Run the analytics route from a tree holding exactly the engine allowlist, as the wheel does."""
    tree = tmp_path / "engine"
    for relative in _engine_files():
        stripped = next(
            relative[len(prefix) :] for prefix in ("server/src/", "client/src/", "src/") if relative.startswith(prefix)
        )
        target = tree / stripped
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((_ROOT / relative).read_bytes())
    probe = tmp_path / "probe.py"
    probe.write_text(_ENGINE_PROBE, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run(
        [sys.executable, "-s", str(probe), str(tree), str(tmp_path / "state")],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert run.returncode == 0, run.stderr[-4000:]
    out = json.loads(next(line for line in run.stdout.splitlines() if line.startswith("PROBE"))[len("PROBE") :])
    assert out["status"] == 200 and out["drill_status"] == 200, out
    report = out["report"]
    shipped = set(_engine_files())

    def ships(module: str) -> bool:
        source = _source_of(f"lemoncrow.pro.capabilities.review.{module}")
        return source is not None and str(source.relative_to(_ROOT)) in shipped

    expected = [
        name
        for name, src in OPTIONAL_SOURCES.items()
        if src.modules and (not all(ships(module) for module in src.modules))
    ]
    assert expected, "the engine ships every optional source; this test no longer proves degradation"
    named = [item["source"] for item in report["unavailable"]]
    assert [name for name in named if name != "reviewer_load"] == expected
    assert "reviewer_load" in named or report["reviewer_load"]["waiting_on_reviewers"]["count"] == 1
    assert all(item["sections"] == list(OPTIONAL_SOURCES[item["source"]].sections) for item in report["unavailable"])
    assert report["volume"]["reviews"]["count"] == 1
    assert out["drill"]["count"] == 1 and out["drill"]["records"][0]["review_path"].startswith("/r/")


def test_an_unavailable_source_is_never_read_and_is_named_in_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.analytics import facts as facts_module
    from lemoncrow.pro.capabilities.review.analytics import service as service_module

    store = ReviewStore(tmp_path / "store")
    _change(store, created=_at(1))
    sessions = tuple(store.list_sessions(status="", limit=None))
    full = analyze([RepositorySource("repo-a", store, sessions)], scope=_scope(), now=NOW)
    assert full.wire["unavailable"] == []
    for source in OPTIONAL_SOURCES.values():
        for module in source.modules:
            monkeypatch.setitem(sys.modules, f"lemoncrow.pro.capabilities.review.{module}", None)

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unavailable source was read")

    for reader in ("_plans", "_actions", "_static_findings", "_measured_compute", "_savings", "_auto_approval"):
        monkeypatch.setattr(facts_module, reader, never)
    for reader in ("_break_glass", "_register"):
        monkeypatch.setattr(facts_module, reader, never)
    monkeypatch.setattr(service_module, "_escape_candidates", never)
    optional = [name for name, source in OPTIONAL_SOURCES.items() if source.modules]
    assert facts_module.unavailable_sources() == frozenset(optional)

    def inbox_needs_absent_code(_sessions: Sequence[Any]) -> list[Any]:
        raise ModuleNotFoundError("absent", name="lemoncrow.pro.capabilities.review.sources.github")

    source = RepositorySource("repo-a", store, sessions, inbox_rows=inbox_needs_absent_code)
    report = analyze([source], scope=_scope(), now=NOW)
    assert [item["source"] for item in report.wire["unavailable"]] == list(OPTIONAL_SOURCES)
    assert report.wire["unavailable"][-1]["sections"] == ["waiting_by_owner", "reviewer_load"]

    def third_party_missing(_sessions: Sequence[Any]) -> list[Any]:
        raise ModuleNotFoundError("absent", name="yaml")

    with pytest.raises(ModuleNotFoundError):
        analyze([RepositorySource("repo-a", store, sessions, inbox_rows=third_party_missing)], scope=_scope(), now=NOW)
    assert report.wire["volume"]["reviews"]["count"] == 1
    _assert_provenance(report)
