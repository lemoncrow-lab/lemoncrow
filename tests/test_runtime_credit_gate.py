"""Paired Runtime Lite credit gate: real usage inputs must fail closed."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.runtime_credit_gate import CreditGateError, evaluate


def _run(name: str, *, cost: float = 1.0, fresh: int = 1200) -> dict:
    return {
        "schema_version": 1,
        "run_id": name,
        "runtime": "local_lite",
        "commit": ("a" if name == "baseline" else "b") * 40,
        "source": "provider_usage",
        "cases": [
            {
                "case_id": f"task_{n}",
                "task_digest": f"{n:064x}",
                "fixture_digest": "e" * 64,
                "model": "claude-opus-5-5",
                "cost_basis": "priced_tokens",
                "pricing_revision": "fixed-anthropic-2026-10-01",
                "cost_usd": cost,
                "verified": True,
                "passed": True,
                "verification_ref": f"verification/{name}/{n}",
                "usage": {
                    "input_tokens": 200,
                    "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": fresh - 200,
                    "output_tokens": 150,
                },
            }
            for n in range(5)
        ],
    }


def _write(tmp_path: Path, baseline: dict, current: dict) -> tuple[Path, Path]:
    old, new = tmp_path / "baseline.json", tmp_path / "current.json"
    old.write_text(json.dumps(baseline), encoding="utf-8")
    new.write_text(json.dumps(current), encoding="utf-8")
    return old, new


def test_paired_quality_and_token_cost_stay_within_budget(tmp_path: Path) -> None:
    before, after = _write(tmp_path, _run("baseline"), _run("current", cost=1.08, fresh=1350))
    report = evaluate(before, after)
    assert report["status"] == "pass"
    assert report["paired_cases"] == 5
    assert report["successful_baseline_cases"] == 5
    assert report["baseline"]["cache_read_input_tokens"] == 45000
    assert report["current"]["fresh_input_tokens"] == 6750


def test_fails_on_fresh_token_regression_even_if_cost_is_flat(tmp_path: Path) -> None:
    paths = _write(tmp_path, _run("baseline"), _run("current", fresh=1750))
    report = evaluate(*paths)
    assert report["status"] == "fail"
    assert any("fresh input rose" in problem for problem in report["reason"])


def test_fails_on_cost_regression_and_quality_failure(tmp_path: Path) -> None:
    current = _run("current", cost=1.3)
    current["cases"][0]["passed"] = False
    report = evaluate(*_write(tmp_path, _run("baseline"), current))
    assert report["status"] == "fail"
    assert any("cost rose" in problem for problem in report["reason"])
    assert any("failed verification" in problem for problem in report["reason"])


@pytest.mark.parametrize(
    "mutation",
    [
        "model",
        "digest",
        "missing",
        "extra",
        "duplicate",
        "same_run",
        "same_commit",
        "unverified",
        "unknown_cost",
        "negative_tokens",
        "unpriced",
        "reused_evidence",
        "fixture",
        "price_card",
    ],
)
def test_refuses_unpaired_or_untrustworthy_observations(tmp_path: Path, mutation: str) -> None:
    baseline, current = _run("baseline"), _run("current")
    if mutation == "model":
        current["cases"][0]["model"] = "claude-sonnet-5-5"
    elif mutation == "digest":
        current["cases"][0]["task_digest"] = "f" * 64
    elif mutation == "missing":
        current["cases"].pop()
    elif mutation == "extra":
        current["cases"].append({**copy.deepcopy(current["cases"][0]), "case_id": "extra"})
    elif mutation == "duplicate":
        current["cases"].append(copy.deepcopy(current["cases"][0]))
    elif mutation == "same_run":
        current["run_id"] = baseline["run_id"]
    elif mutation == "same_commit":
        current["commit"] = baseline["commit"]
    elif mutation == "unverified":
        current["cases"][0]["verified"] = False
    elif mutation == "unknown_cost":
        current["cases"][0].pop("cost_usd")
    elif mutation == "negative_tokens":
        current["cases"][0]["usage"]["input_tokens"] = -4
    elif mutation == "unpriced":
        current["cases"][0]["cost_basis"] = "free"
    elif mutation == "reused_evidence":
        current["cases"][1]["verification_ref"] = current["cases"][0]["verification_ref"]
    elif mutation == "fixture":
        current["cases"][0]["fixture_digest"] = "d" * 64
    elif mutation == "price_card":
        current["cases"][0]["pricing_revision"] = "changed-prices"
    try:
        report = evaluate(*_write(tmp_path, baseline, current))
    except CreditGateError:
        return
    assert report["status"] == "fail"


def test_cli_fails_when_no_recorded_evidence(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.runtime_credit_gate",
            "--baseline",
            str(tmp_path / "missing-baseline.json"),
            "--current",
            str(tmp_path / "missing-current.json"),
            "--out",
            str(tmp_path / "result.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["status"] == "fail"
    assert "cannot read" in report["reason"][0]
