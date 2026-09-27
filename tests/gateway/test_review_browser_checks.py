"""Browser/UI regression evidence: provider-neutral normalization and projection."""

from __future__ import annotations

import json
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.browser_checks import (
    BROWSER_CHECK_MEDIA_TYPE,
    BrowserPolicy,
    browser_source_ref,
    build_browser_projection,
    normalize_browser_run,
    parse_browser_policy,
    scenario_applicability,
    validate_browser_target,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision

_POLICY = """version: 1
browser_checks:
  trusted_origins: ["https://preview.example.test", "https://*.pr.example.test"]
  scenarios:
    - id: checkout
      title: Checkout regression
      route: /checkout
      paths: ["web/src/checkout/*"]
      required: true
      viewports: ["1280x800"]
      visual_threshold: 0.01
    - id: settings
      title: Settings smoke
      route: /settings
"""


def _revision(identifier: str, number: int) -> ReviewRevision:
    return ReviewRevision(
        id=identifier,
        review_id="r-review",
        revision_number=number,
        range_mode="working_tree",
        tree_fingerprint=f"tree-{number}",
        packet_schema_version=1,
        created_at=f"2026-09-25T00:0{number}:00+00:00",
    )


def _no_artifacts(_evidence_id: str) -> ReviewEvidence | None:
    return None


def _policy() -> BrowserPolicy:
    policy = parse_browser_policy(_POLICY, source="base Snapshot .lemoncrow/review.yaml")
    assert policy.error == ""
    return policy


def _stored(record: dict[str, Any], *, identifier: str, created_at: str) -> tuple[ReviewEvidence, dict[str, Any]]:
    item = ReviewEvidence(
        id=identifier,
        review_id="r-review",
        revision_id=str(record["snapshot_id"]),
        kind="verification",
        title=str(record["name"]),
        artifact_path=f"artifacts/{identifier}.json",
        content_hash=f"digest-{identifier}",
        mime_type=BROWSER_CHECK_MEDIA_TYPE,
        source="ci",
        source_ref=browser_source_ref(record),
        verification_status=str(record["status"]) if record["side"] == "new" else "",
        detail="summary",
        created_at=created_at,
    )
    return item, record


def _run(snapshot: ReviewRevision, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scenario": "checkout",
        "route": "/checkout",
        "viewport": {"width": 1280, "height": 800},
        "provider": "playwright",
        "completed": True,
        "navigation": {"status": 200},
        "assertions": [{"name": "total visible", "status": "PASS", "expected": "$10", "actual": "$10"}],
        "console": [],
    }
    body.update(overrides)
    return normalize_browser_run(body, policy=_policy(), snapshot=snapshot, resolve_artifact=_no_artifacts)


def test_policy_parses_required_scenarios_and_reports_invalid_configuration() -> None:
    policy = _policy()
    checkout = policy.scenario("checkout")
    assert checkout is not None and checkout.required is True and checkout.viewports == ("1280x800",)
    assert policy.scenario("settings") is not None and policy.scenario("settings").required is False  # type: ignore[union-attr]

    broken = parse_browser_policy("browser_checks:\n  - id: bad\n    route: https://evil.test/\n", source="base")
    assert broken.scenarios == ()
    assert "application-relative" in broken.error


def test_deterministic_checks_decide_the_verdict_not_the_reporter() -> None:
    snapshot = _revision("rr-one", 1)
    assert _run(snapshot)["status"] == "PASS"

    failed = _run(snapshot, assertions=[{"name": "total visible", "status": "FAIL"}], status="PASS")
    assert failed["status"] == "FAIL"
    assert failed["status_reasons"] == ["1 assertion(s) failed"]

    console = _run(snapshot, console=[{"level": "error", "text": "TypeError: cart is undefined"}])
    assert console["status"] == "FAIL"

    http = _run(snapshot, navigation={"status": 500})
    assert http["status"] == "FAIL"

    incomplete = _run(snapshot, completed=False)
    assert incomplete["status"] == "FAIL"

    over_threshold = _run(snapshot, visual={"diff_ratio": 0.2})
    assert over_threshold["status"] == "FAIL"
    assert "configured threshold" in over_threshold["status_reasons"][0]

    # A browser agent's statement is an observation, never a PASS.
    agent_claim = normalize_browser_run(
        {"scenario": "checkout", "status": "PASS", "observations": ["The checkout looks correct."]},
        policy=_policy(),
        snapshot=snapshot,
        resolve_artifact=_no_artifacts,
    )
    assert agent_claim["status"] == "UNKNOWN"
    assert agent_claim["observations"] == ["The checkout looks correct."]
    assert agent_claim["deterministic_checks"] == 0

    not_run = _run(snapshot, completed=False, status="NOT_RUN", not_run_reason="no preview environment")
    assert not_run["status"] == "NOT_RUN"


def test_playwright_json_result_maps_to_inspectable_checks() -> None:
    snapshot = _revision("rr-one", 1)
    passed = _run(
        snapshot, assertions=[], console=None, navigation=None, playwright={"status": "passed", "title": "checkout"}
    )
    assert passed["status"] == "PASS"
    failed = _run(
        snapshot,
        assertions=[],
        playwright={"status": "failed", "title": "checkout", "errors": [{"message": "expect(locator).toHaveText"}]},
    )
    assert failed["status"] == "FAIL"
    assert failed["assertions"][1]["detail"] == "expect(locator).toHaveText"
    timed_out = _run(snapshot, assertions=[], playwright={"status": "timedOut"})
    assert timed_out["status"] == "FAIL"
    skipped = _run(snapshot, assertions=[], console=None, navigation=None, playwright={"status": "skipped"})
    assert skipped["status"] == "UNKNOWN" or skipped["status"] == "NOT_RUN"


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://evil.example.test/checkout",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://user:secret@127.0.0.1:4173/",
        "https://pr.example.test/",
    ],
)
def test_untrusted_browser_targets_are_refused(target: str) -> None:
    with pytest.raises(ValueError):
        validate_browser_target(target, _policy().trusted_origins)


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:4173/checkout",
        "http://localhost:3000/checkout",
        "https://preview.example.test/checkout",
        "https://42.pr.example.test/checkout",
    ],
)
def test_loopback_and_configured_preview_targets_are_accepted(target: str) -> None:
    assert validate_browser_target(target, _policy().trusted_origins) == target


def test_route_and_artifact_identity_are_enforced() -> None:
    snapshot = _revision("rr-one", 1)
    with pytest.raises(ValueError, match="configured for route"):
        _run(snapshot, route="/admin")
    with pytest.raises(ValueError, match="application-relative"):
        normalize_browser_run(
            {"scenario": "adhoc", "route": "//evil.test/"},
            policy=_policy(),
            snapshot=snapshot,
            resolve_artifact=_no_artifacts,
        )

    foreign = ReviewEvidence(
        id="ev-shot",
        review_id="r-review",
        revision_id="rr-other",
        kind="screenshot",
        title="checkout",
        content_hash="shot",
        mime_type="image/png",
    )
    with pytest.raises(ValueError, match="same exact Snapshot"):
        normalize_browser_run(
            {"scenario": "checkout", "artifacts": {"screenshot": "ev-shot"}},
            policy=_policy(),
            snapshot=snapshot,
            resolve_artifact=lambda _identifier: foreign,
        )
    wrong_kind = ReviewEvidence(**{**foreign.__dict__, "revision_id": snapshot.id, "kind": "video"})
    with pytest.raises(ValueError, match="kind"):
        normalize_browser_run(
            {"scenario": "checkout", "artifacts": {"screenshot": "ev-shot"}},
            policy=_policy(),
            snapshot=snapshot,
            resolve_artifact=lambda _identifier: wrong_kind,
        )


def test_applicability_uses_only_configured_factual_signals() -> None:
    policy = _policy()
    checkout = policy.scenario("checkout")
    settings = policy.scenario("settings")
    assert checkout is not None and settings is not None
    assert scenario_applicability(checkout, ("web/src/checkout/Cart.tsx",), None)[0] == "applies"
    assert scenario_applicability(checkout, ("web/src/other.tsx",), None)[0] == "not_applicable"
    # Route-only scenario: unknown until the Web review surface is known.
    assert scenario_applicability(settings, ("web/src/settings.tsx",), None)[0] == "unknown"
    assert scenario_applicability(settings, ("web/src/settings.tsx",), ("/settings/",))[0] == "applies"
    assert scenario_applicability(settings, ("web/src/settings.tsx",), ("/home",))[0] == "not_applicable"


def test_projection_binds_current_baseline_stale_and_not_run_explicitly() -> None:
    first = _revision("rr-one", 1)
    second = _revision("rr-two", 2)
    shot = {"evidence_id": "ev-shot", "digest": "d-shot", "mime_type": "image/png", "bytes": 3, "observed_at": "t"}
    base_run = {**_run(second, side="old"), "artifacts": {"screenshot": {**shot, "evidence_id": "ev-base"}}}
    current_run = {
        **_run(second),
        "artifacts": {
            "screenshot": shot,
            "trace": {**shot, "evidence_id": "ev-trace", "mime_type": "application/zip"},
            "video": {**shot, "evidence_id": "ev-video", "mime_type": "video/webm"},
        },
    }
    old_pass = _run(first)
    rows = [
        _stored(current_run, identifier="ev-current", created_at="2026-09-25T00:02:30+00:00"),
        _stored(base_run, identifier="ev-baseline", created_at="2026-09-25T00:02:20+00:00"),
        _stored(old_pass, identifier="ev-old", created_at="2026-09-25T00:01:30+00:00"),
    ]
    records = {item.id: record for item, record in rows}
    checks, summary = build_browser_projection(
        evidence=[item for item, _ in rows],
        revision=second,
        revisions=(first, second),
        policy=_policy(),
        changed_paths=("web/src/checkout/Cart.tsx",),
        web_routes=(),
        read_record=lambda item: records.get(item.id),
    )

    current = [row for row in checks if row["state"] == "current"]
    assert len(current) == 1
    row = current[0]
    assert row["name"] == "Checkout regression · 1280x800"
    assert row["status"] == "PASS" and row["required"] is True
    assert row["snapshot_id"] == second.id
    assert row["browser"]["run"]["artifacts"]["trace"]["evidence_id"] == "ev-trace"
    assert row["browser"]["run"]["artifacts"]["video"]["evidence_id"] == "ev-video"
    baseline = row["browser"]["baseline"]
    assert baseline["relation"] == "base" and baseline["side"] == "old"
    assert baseline["snapshot_id"] == second.id
    assert baseline["artifacts"]["screenshot"]["evidence_id"] == "ev-base"
    # The Snapshot-1 PASS never blesses Snapshot 2; the baseline run is not a check.
    assert summary["pass"] == 1 and summary["stale"] == 0

    # A newer Snapshot with no result: the required scenario is NOT_RUN and the
    # older PASS is shown stale rather than carried forward.
    third = _revision("rr-three", 3)
    checks, summary = build_browser_projection(
        evidence=[item for item, _ in rows],
        revision=third,
        revisions=(first, second, third),
        policy=_policy(),
        changed_paths=("web/src/checkout/Cart.tsx",),
        web_routes=(),
        read_record=lambda item: records.get(item.id),
    )
    now = [row for row in checks if row["state"] == "current"]
    assert [(row["status"], row["required"]) for row in now] == [("NOT_RUN", True)]
    stale = [row for row in checks if row["state"] == "stale"]
    assert [(row["status"], row["snapshot_id"]) for row in stale] == [("PASS", second.id)]


def test_ui_change_without_configured_scenario_reports_not_run_not_coverage() -> None:
    revision = _revision("rr-one", 1)
    checks, _summary = build_browser_projection(
        evidence=(),
        revision=revision,
        revisions=(revision,),
        policy=BrowserPolicy(),
        changed_paths=("web/src/Home.tsx",),
        web_routes=("/home",),
        read_record=lambda _item: None,
    )
    assert [(row["name"], row["status"], row["required"]) for row in checks] == [
        ("Browser regression", "NOT_RUN", False)
    ]
    assert "Nothing was verified in a browser" in checks[0]["detail"]

    none, _summary = build_browser_projection(
        evidence=(),
        revision=revision,
        revisions=(revision,),
        policy=BrowserPolicy(),
        changed_paths=("src/server.py",),
        web_routes=(),
        read_record=lambda _item: None,
    )
    assert none == []


def test_record_round_trips_as_json() -> None:
    record = _run(_revision("rr-one", 1))
    assert json.loads(json.dumps(record))["status"] == "PASS"


def test_a_required_scenario_needs_a_current_result_at_every_configured_viewport() -> None:
    policy = parse_browser_policy(
        _POLICY.replace('viewports: ["1280x800"]', 'viewports: ["1280x800", "390x844"]'),
        source="base Snapshot .lemoncrow/review.yaml",
    )
    first = _revision("rr-one", 1)
    second = _revision("rr-two", 2)

    def run(snapshot: ReviewRevision, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "scenario": "checkout",
            "route": "/checkout",
            "viewport": {"width": 1280, "height": 800},
            "provider": "playwright",
            "completed": True,
            "navigation": {"status": 200},
            "assertions": [{"name": "total visible", "status": "PASS", "expected": "$10", "actual": "$10"}],
            "console": [],
        }
        body.update(overrides)
        return normalize_browser_run(body, policy=policy, snapshot=snapshot, resolve_artifact=_no_artifacts)

    mobile_fail = run(
        first,
        viewport={"width": 390, "height": 844},
        assertions=[{"name": "total visible", "status": "FAIL", "expected": "$10", "actual": ""}],
    )
    rows = [
        _stored(run(second), identifier="ev-desktop", created_at="2026-09-25T00:02:30+00:00"),
        _stored(mobile_fail, identifier="ev-mobile-old", created_at="2026-09-25T00:01:30+00:00"),
    ]
    records = {item.id: record for item, record in rows}
    checks, summary = build_browser_projection(
        evidence=[item for item, _ in rows],
        revision=second,
        revisions=(first, second),
        policy=policy,
        changed_paths=("web/src/checkout/Cart.tsx",),
        web_routes=(),
        read_record=lambda item: records.get(item.id),
    )

    # The desktop PASS says nothing about the phone layout: that viewport is
    # still owed on this Snapshot, and its earlier FAIL stays in view.
    current = {row["name"]: (row["status"], row["required"]) for row in checks if row["state"] == "current"}
    assert current == {
        "Checkout regression · 1280x800": ("PASS", True),
        "Checkout regression · 390x844": ("NOT_RUN", True),
    }
    stale = [(row["name"], row["status"]) for row in checks if row["state"] == "stale"]
    assert stale == [("Checkout regression · 390x844", "FAIL")]
    assert summary["pass"] == 1 and summary["not_run"] == 1
