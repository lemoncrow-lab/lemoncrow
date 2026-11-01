"""Web QA (``lc review qa run``): scenario format, outcomes, and Review / GitHub binding without a browser."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import web_qa
from lemoncrow.pro.capabilities.review.browser_checks import (
    BrowserScenario,
    normalize_browser_run,
    parse_browser_policy,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision

SHA = "a" * 40

POLICY = """version: 1
browser_checks:
  trusted_origins: ["https://*.preview.example.test"]
  scenarios:
    - id: signup
      title: Sign up
      route: /signup
      outcome: A new visitor can create an account and lands on the welcome page.
      steps:
        - goto: /
        - click: {role: link, name: Sign up}
        - fill: {label: Email, value: ada@example.test}
        - fill: {label: Password, value: "${secrets.QA_PASSWORD}"}
        - click: {role: button, name: Create account}
        - expect_url: /welcome
        - expect_text: "Welcome, ada@example.test"
        - expect_visible: {role: heading, name: Account ready}
        - screenshot: welcome
    - id: reported-elsewhere
      title: Reported by CI
      route: /checkout
"""


def _scenario() -> BrowserScenario:
    policy = parse_browser_policy(POLICY, source="test")
    assert policy.error == ""
    scenario = policy.scenario("signup")
    assert scenario is not None
    return scenario


def test_saved_steps_parse_describe_and_name_their_secrets() -> None:
    policy = parse_browser_policy(POLICY, source="test")
    scenario = _scenario()
    assert scenario.outcome.startswith("A new visitor")
    assert [step.describe() for step in scenario.steps] == [
        "goto /",
        'click link "Sign up"',
        'fill label "Email" with "ada@example.test"',
        'fill label "Password" with ${secrets.QA_PASSWORD}',
        'click button "Create account"',
        "expect URL /welcome",
        'expect text "Welcome, ada@example.test"',
        'expect visible heading "Account ready"',
        "screenshot welcome",
    ]
    assert scenario.secrets() == ("QA_PASSWORD",)
    # A scenario without steps is unchanged: another runner reports it.
    other = policy.scenario("reported-elsewhere")
    assert other is not None and other.steps == () and other.outcome == ""


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ("[{click: Sign up}]", "must start with goto"),
        ("[{goto: /}, {hover: x}]", "unknown action"),
        ("[{goto: /, click: x}]", "exactly one action"),
        ("[{goto: /}, {expect_text: '${secrets.X}'}]", "only in a fill value"),
        ("[{goto: /}, {click: {name: Go}}]", "exactly one of role, text or label"),
        ("[{goto: /}, {click: {text: Go, name: Go}}]", "accessible name of a role"),
        ("[{goto: /}, {fill: {label: P, value: '${secrets.bad-name}'}}]", "malformed secret reference"),
        ("[{goto: /}, {fill: Email}]", "needs {role, name}"),
        ("[{goto: 'https://evil.example/'}]", "application-relative path"),
        ("[{goto: /}, {screenshot: ../x}]", "screenshot name"),
    ],
)
def test_invalid_steps_are_a_visible_policy_error(steps: str, message: str) -> None:
    text = f"browser_checks:\n  - id: s\n    route: /\n    steps: {steps}\n"
    policy = parse_browser_policy(text, source="test")
    assert message in policy.error
    assert policy.scenarios == ()


def _raw(**overrides: Any) -> dict[str, Any]:
    scenario = _scenario()
    raw: dict[str, Any] = {
        "setup_error": None,
        "steps": [
            {"index": i + 1, "status": "passed", "duration_ms": 20, "error": ""} for i in range(len(scenario.steps))
        ],
        "console": [],
        "page_errors": [],
    }
    raw.update(overrides)
    return raw


def test_outcome_is_exactly_one_of_four_and_could_not_run_wins_over_nothing() -> None:
    scenario = _scenario()
    assert web_qa.classify_run(_raw(), scenario)[0] == "pass"

    failed = _raw()
    failed["steps"][6] = {"index": 7, "status": "failed", "error": 'text "Welcome" is not on the page'}
    for row in failed["steps"][7:]:
        row["status"] = "skipped"
    outcome, reasons, _ = web_qa.classify_run(failed, scenario)
    assert outcome == "fail" and reasons[0].startswith('step 7 (expect text "Welcome, ada@example.test") failed')

    noisy = _raw(console=[{"level": "error", "text": "boom"}])
    outcome, reasons, _ = web_qa.classify_run(noisy, scenario)
    assert outcome == "fail" and reasons[0].startswith("1 console error(s), first: boom")
    slow = _raw()
    slow["steps"][0]["duration_ms"] = 9000
    outcome, reasons, _ = web_qa.classify_run(slow, scenario, slow_step_ms=5000)
    assert outcome == "warning" and "9000 ms, past the 5000 ms threshold" in reasons[0]

    walled = _raw()
    walled["steps"][0] = {"index": 1, "status": "could_not_run", "category": "bot_wall", "error": "challenge"}
    assert web_qa.classify_run(walled, scenario)[::2] == ("could_not_run", "bot_wall")
    setup = _raw(setup_error={"category": "browser", "message": "Chromium could not start"})
    assert web_qa.classify_run(setup, scenario)[::2] == ("could_not_run", "browser")
    stopped = _raw(steps=_raw()["steps"][:3])
    assert web_qa.classify_run(stopped, scenario)[0] == "could_not_run"


def _run(tmp_path: Path, scenario: BrowserScenario, **kwargs: Any) -> dict[str, Any]:
    policy = parse_browser_policy(POLICY, source="test")
    defaults: dict[str, Any] = {
        "policy": policy,
        "base_url": "http://127.0.0.1:9",
        "head_sha": SHA,
        "out_dir": tmp_path / "run",
        "playwright_dir": None,
        "env": {"PATH": "/usr/bin:/bin", "QA_PASSWORD": "s3cr3t-pw"},
    }
    defaults.update(kwargs)
    return web_qa.run_scenario(scenario, **defaults)


def test_untrusted_target_could_not_run_before_any_browser_and_has_no_verdict(tmp_path: Path) -> None:
    report = _run(tmp_path, _scenario(), base_url="https://evil.example.test")
    assert report["outcome"] == "could_not_run" and report["verdict"] is None
    assert report["could_not_run_category"] == "untrusted_origin"
    assert all(step["status"] == "skipped" for step in report["steps"])
    path = tmp_path / "run" / "report.json"
    assert json.loads(path.read_text()) == report
    assert report["digest"].startswith("sha256:")
    # The sealed report is read-only, and a second run cannot overwrite it.
    assert not path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    with pytest.raises(ValueError, match="written once"):
        _run(tmp_path, _scenario())
    # A trusted wildcard preview origin passes the target check.
    trusted = _run(tmp_path, _scenario(), base_url="https://pr-7.preview.example.test", out_dir=tmp_path / "trusted")
    assert trusted["target_trusted"] is True and trusted["could_not_run_category"] == "setup"


def test_missing_secret_could_not_run_names_it_and_never_a_value(tmp_path: Path) -> None:
    report = _run(tmp_path, _scenario(), env={"PATH": "/usr/bin", "UNRELATED_TOKEN": "tok-123"})
    assert report["outcome"] == "could_not_run" and report["verdict"] is None
    assert report["reasons"] == ["missing secret(s): QA_PASSWORD"]
    assert "tok-123" not in (tmp_path / "run" / "report.json").read_text()


def test_malformed_head_sha_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40- or 64-character"):
        _run(tmp_path, _scenario(), head_sha="abc1234")


def _report(outcome: str, *, console: bool = False) -> dict[str, Any]:
    scenario = _scenario()
    steps = [
        {"index": i + 1, "description": step.describe(), "status": "passed", "duration_ms": 10, "error": ""}
        for i, step in enumerate(scenario.steps)
    ]
    if outcome == "fail":
        steps[6].update(status="failed", error='text "Welcome, ada@example.test" is not on the page')
        for row in steps[7:]:
            row["status"] = "skipped"
    if outcome == "could_not_run":
        for row in steps:
            row["status"] = "skipped"
    return {
        "run_id": "r1",
        "digest": "sha256:x",
        "scenario": "signup",
        "title": "Sign up",
        "outcome": outcome,
        "reasons": ["the preview could not be reached"] if outcome == "could_not_run" else ["ok"],
        "head_sha": SHA,
        "base_url": "http://127.0.0.1:4173",
        "target_trusted": True,
        "viewport": {"width": 1280, "height": 720, "configured": False},
        "browser": {"version": "140.0", "playwright": "1.62.1"},
        "navigation": {"status": 200} if outcome != "could_not_run" else {},
        "page_url": "http://127.0.0.1:4173/welcome",
        "steps": steps,
        "console_errors": [{"text": "boom", "location": ""}] if console else [],
        "page_errors": [],
    }


def _snapshot() -> ReviewRevision:
    return ReviewRevision(
        id="rev-1",
        review_id="r-1",
        revision_number=1,
        range_mode="commit_range",
        tree_fingerprint="t",
        packet_schema_version=1,
    )


def _no_artifacts(_evidence_id: str) -> ReviewEvidence | None:
    return None


@pytest.mark.parametrize(
    ("outcome", "review_status"), [("pass", "PASS"), ("fail", "FAIL"), ("could_not_run", "NOT_RUN")]
)
def test_review_body_is_recomputed_by_review_never_trusted(outcome: str, review_status: str) -> None:
    policy = parse_browser_policy(POLICY, source="test")
    body = web_qa.review_run_body(_report(outcome), _scenario(), revision_id="rev-1")
    record = normalize_browser_run(body, policy=policy, snapshot=_snapshot(), resolve_artifact=_no_artifacts)
    assert record["status"] == review_status
    assert record["provider"] == web_qa.QA_PROVIDER and record["environment"]["head_sha"] == SHA
    # No configured viewport: the run fills the scenario's default slot.
    assert record["viewport_key"] == ""
    if outcome == "could_not_run":
        # No checks travel with it, so nothing can read as a verdict.
        assert record["assertions"] == [] and record["status_reasons"][0].startswith("could not run:")


def test_console_errors_read_the_same_in_qa_and_in_review() -> None:
    policy = parse_browser_policy(POLICY, source="test")
    body = web_qa.review_run_body(_report("fail", console=True), _scenario(), revision_id="rev-1")
    record = normalize_browser_run(body, policy=policy, snapshot=_snapshot(), resolve_artifact=_no_artifacts)
    assert record["environment"]["qa_outcome"] == "fail"
    assert record["status"] == "FAIL" and "1 console error(s)" in record["status_reasons"]


def test_github_check_could_not_run_is_neutral_never_success() -> None:
    unreachable = web_qa.github_check_payload([_report("could_not_run")], head_sha=SHA)
    assert unreachable["name"] == "LemonCrow QA" and unreachable["head_sha"] == SHA
    assert unreachable["conclusion"] == "neutral"
    assert "COULD NOT RUN" in unreachable["output"]["summary"]
    assert "the preview could not be reached" in unreachable["output"]["summary"]
    assert web_qa.github_check_payload([_report("pass")], head_sha=SHA)["conclusion"] == "success"
    mixed = web_qa.github_check_payload([_report("pass"), _report("fail"), _report("could_not_run")], head_sha=SHA)
    assert mixed["conclusion"] == "failure"
    assert mixed["output"]["title"] == "1 pass, 1 fail, 1 could not run"
    assert web_qa.exit_code("could_not_run") == 2 and web_qa.exit_code("warning") == 0


def test_snapshot_for_sha_binds_only_an_exact_clean_snapshot() -> None:
    rows = [
        {"id": "1", "revision_number": 1, "head_sha": SHA},
        {"id": "2", "revision_number": 2, "head_sha": "b" * 40},
        {"id": "3", "revision_number": 3, "head_sha": SHA, "dirty": True},
    ]
    found = web_qa.snapshot_for_sha(rows, SHA)
    assert found is not None and found["id"] == "1"
    assert web_qa.snapshot_for_sha(rows, "c" * 40) is None


def test_prune_runs_keeps_the_newest(tmp_path: Path) -> None:
    for name in ("20260101T000000Z-a", "20260102T000000Z-a", "20260103T000000Z-a"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text("{}")
        (tmp_path / name / "report.json").chmod(0o444)
    web_qa.prune_runs(tmp_path, keep=2)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["20260102T000000Z-a", "20260103T000000Z-a"]
