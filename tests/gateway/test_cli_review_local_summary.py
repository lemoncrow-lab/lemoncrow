"""``lc review --ai`` prints the same structure as the pull request comment, in plain text: the verdict in words, the
coverage and human-review line, the Reader's Risk line, the findings and the summary bullets (the score is in ``--json`` only).

Same lab as ``test_cli_review_local.py`` (a real repository, the real engine path, a fake model). The overview is one
more model call, made for a person at the command and not for a git hook, and reused while the tree is the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import local
from tests.gateway.pr_bot_noise_support import review_yaml
from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, CACHE_BUGGY, TOKEN_BUGGY
from tests.gateway.test_cli_review_local import Lab, make_lab

#: Three changed files, so the change is not too small to summarise.
THREE = {"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY, "src/plain.py": "VALUE = 2\n"}


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return make_lab(tmp_path, monkeypatch, BASE)


def lines_of(text: str) -> list[str]:
    return text.split("\n")


def test_the_terminal_shows_the_verdict_the_coverage_the_impact_the_findings_and_the_summary_in_that_order(
    lab: Lab,
) -> None:
    lab.model.overview = {
        "one_line": "Guards authorize and bounds the cache",
        "intro": "The change touches the auth and cache modules.",
        "bullets": ["authorize rejects a missing user", "remember stores a list"],
        "diagram": None,
    }
    lab.write(THREE)
    out = lab.run().stdout
    lines = lines_of(out)
    assert lines[0] == "LemonCrow review · working tree against HEAD"
    top = lines.index("No must-fix findings · 1 should-fix · 1 optional")
    assert top <= 2, "after the title and the line saying which configuration applied"
    assert lines[top + 1] == "All 3 changed files reviewed · Human review required: touches security-sensitive code"
    assert lines[top + 2] == "Risk: High · authorize is access-control code"
    assert lines[top + 3] == "Guards authorize and bounds the cache"
    # No code index: callers were measured by searching the working tree, so nothing is listed as not measured.
    assert lines[top + 4] == "  3 files, 6 changed lines · measured from the change and the run record, not a model"
    assert "impact]" not in out and "from the change itself" not in out
    assert lab.wire().get("risk") is None, "--json is unchanged: the impact stays in the hidden state for tools"
    assert "Confidence Score" not in out
    order = [
        out.index(part)
        for part in ("Findings\n", "1. P1 · should fix  src/auth.py:2-3", "Summary\n", "  - authorize rejects")
    ]
    assert order == sorted(order)
    assert "  The change touches the auth and cache modules.\n" in out
    assert "  - remember stores a list\n" in out
    assert "<" not in out.split("Summary")[1].split("Model:")[0], "plain text: no markup"
    assert lab.model.overviews and len(lab.model.overviews) == 1


def test_the_footer_names_the_effort_auto_resolved_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quick = make_lab(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": review_yaml(effort="quick")})
    quick.write({"src/plain.py": "VALUE = 2\n"})
    assert " · depth auto (quick) · " in quick.run().stdout
    assert quick.wire("--fresh")["effort"] == "quick"


def test_the_footer_stays_plain_when_auto_runs_auto(lab: Lab) -> None:
    lab.write({"src/plain.py": "VALUE = 2\n"})
    assert " · depth auto · " in lab.run().stdout


def test_a_clean_review_reads_calmly_and_has_no_findings_section(lab: Lab) -> None:
    lab.write({"src/plain.py": "VALUE = 2\n", "src/more.py": "A = 1\n" * 5, "src/third.py": "B = 2\n" * 5})
    out = lab.run().stdout
    assert "No issues found\n" in out and "Findings\n" not in out
    assert "All 3 changed files reviewed · No extra human review flagged" in out
    assert lab.wire()["verdict"]["confidence"] == 5


def test_the_json_result_carries_the_score_and_the_overview(lab: Lab) -> None:
    lab.write(THREE)
    wire = lab.wire()
    assert wire["verdict"]["confidence"] == 3 and wire["verdict"]["p1"] == 1
    assert wire["overview"]["one_line"] == "Changes 3 files" and wire["overview"]["bullets"]


def test_a_second_run_on_the_same_tree_reuses_the_overview_and_asks_nothing(lab: Lab) -> None:
    lab.write(THREE)
    first = lab.run().stdout
    lab.forget_calls()
    asked = len(lab.model.overviews)
    second = lab.run().stdout
    assert len(lab.model.overviews) == asked and lab.model.calls == [], "no call of any kind"
    assert "No must-fix findings · 1 should-fix · 1 optional" in second and "Changes 3 files" in second
    assert "reused, with no model call" in second
    assert first.split("Model:")[0].count("Summary") == 1


def test_a_changed_tree_asks_again(lab: Lab) -> None:
    lab.write(THREE)
    lab.run()
    lab.write({"src/plain.py": "VALUE = 3\nOTHER = 4\n"})
    lab.run()
    assert len(lab.model.overviews) == 2


def test_a_small_change_is_not_summarised_and_its_terminal_output_has_no_summary_section(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    out = lab.run().stdout
    assert lab.model.overviews == [] and "Summary\n" not in out
    lines = lines_of(out)
    top = lines.index("No must-fix findings · 1 should-fix") + 1
    assert lines[top + 1] == "Risk: High · authorize is access-control code"
    assert lines[top + 2].startswith("  1 file, ") and "Widgets" not in out, "a local change has no title"


def test_the_terminal_and_the_reader_give_the_same_risk_for_the_same_revision(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from lemoncrow.pro.capabilities.review.pr_bot import engine

    real = local._recorded_checks
    seen: dict[str, Any] = {}

    def spy(captured: Any) -> tuple[dict[str, str], str]:
        # While the revision's store exists: record a check on it, then read its Risk as the Reader's Brief states it.
        with TestClient(engine._mount(captured)) as client:
            review = f"/api/reviews/{captured.session.id}"
            posted = client.post(
                f"{review}/verification", json={"revision_id": captured.revision.id, "name": "unit", "status": "PASS"}
            )
            assert posted.status_code == 201, posted.text
            seen["reader"] = client.get(f"{review}/projection").json()["summary"]["risk"]
        seen["recorded"] = real(captured)
        return seen["recorded"]

    monkeypatch.setattr(local, "_recorded_checks", spy)
    # This Reader has no code index to ask about callers, so the terminal must not search the checkout for them either.
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.pr_bot.callers.checkout_callers", lambda *_a, **_k: None)
    lab.write(THREE)
    report = local.run_local_review(local.LocalOptions(repo_root=lab.repo, store_root=lab.store, fail_setup=False))
    assert seen["recorded"] == ({"unit": "PASS"}, ""), "the check recorded for the revision, read as the Brief reads it"
    reader, risk = seen["reader"], report.risk
    assert risk is not None
    assert (risk.level, risk.label, risk.reason, list(risk.not_measured)) == (
        reader["level"],
        reader["label"],
        reader["reason"],
        list(reader["not_measured"]),
    )
    assert f"Risk: {reader['label']} · {reader['reason']}" in local.render_text(report)


def test_check_records_that_cannot_be_read_are_not_measured_never_none_ran(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot import engine

    def refused(_captured: Any, _submit: Any = None) -> Any:
        raise RuntimeError("no API")

    real_mount = engine._mount

    def mount(captured: Any, submit: Any = None) -> Any:
        # The AI stage mounts the API first; the record read after it is the one refused.
        return real_mount(captured, submit) if submit is not None else refused(captured)

    monkeypatch.setattr(engine, "_mount", mount)
    lab.write(THREE)
    report = local.run_local_review(local.LocalOptions(repo_root=lab.repo, store_root=lab.store, fail_setup=False))
    assert report.risk is not None and local.CHECKS_UNREAD in report.risk.not_measured
    assert "no check ran" not in report.risk.reason
    assert report.complete, "an unreadable record is a gap the Risk line names, never a failed review"


def test_a_failed_overview_leaves_the_review_alone_and_says_so_in_a_note(lab: Lab) -> None:
    lab.model.fail_overview = "provider overloaded"
    lab.write(THREE)
    result = lab.run()
    assert "No must-fix findings · 1 should-fix · 1 optional" in result.stdout and "Summary\n" not in result.stdout
    assert "Note: overview skipped: the model call failed (RuntimeError)" in result.stdout
    assert "provider overloaded" not in result.stdout


def test_a_git_hook_asks_for_no_overview_because_it_must_stay_cheap(lab: Lab) -> None:
    lab.write(THREE)
    options = local.LocalOptions(repo_root=lab.repo, store_root=lab.store, fail_setup=False)
    report = local.run_local_review(options)
    assert report.state is not None and lab.model.overviews == [] and report.state.overview is None
    asked = len(lab.model.calls) + len(lab.model.dedupes)  # the investigations and the repeat check of their findings
    assert report.usage.calls == asked, "its calls are the review's alone"


def test_a_gap_the_risk_line_gives_as_its_reason_is_not_said_again_under_it() -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.impact import ChangeRisk

    gap = "callers of 3 names: a, b, c (each is also defined in another file)"
    risk = ChangeRisk(
        level="not_measured",
        label="Not measured",
        reason=f"could not measure {gap}",
        files=2,
        lines=9,
        not_measured=(gap, "which tests exercise a, b, c"),
    )
    assert local._risk_lines(local.LocalReport(risk=risk), lambda text, **_: text) == [
        f"Risk: Not measured · could not measure {gap}",
        f"  2 files, 9 changed lines · {local.RISK_MEASURED} · not measured: which tests exercise a, b, c",
    ]


def _snapshot_cannot_be_rebuilt(lab: Lab, limit: int) -> None:
    """The model reads the frozen Snapshot (as an agent CLI does), and changed files over *limit* bytes are not captured."""

    from lemoncrow.pro.capabilities.review import ai_review, gitdiff

    lab.monkeypatch.setattr(ai_review, "_CLI_DRIVERS", frozenset({*ai_review._CLI_DRIVERS, "openai"}))
    lab.monkeypatch.setattr(gitdiff, "MAX_CAPTURE_BLOB_BYTES", limit)


def test_a_snapshot_that_cannot_be_rebuilt_is_named_as_one_and_nothing_reads_as_reviewed(lab: Lab) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.render import NO_HUMAN_REVIEW
    from lemoncrow.pro.capabilities.review.snapshot import NOT_CAPTURED

    _snapshot_cannot_be_rebuilt(lab, 40)
    lab.write(THREE)
    result = lab.run()
    out = result.stdout
    lines = lines_of(out)
    top = next(at for at, line in enumerate(lines) if line.startswith("Review incomplete: "))
    assert lines[top] == (
        f"Review incomplete: {NOT_CAPTURED}: src/auth.py (128 bytes, over the 40 bytes capture limit), src/cache.py "
        f"(113 bytes, over the 40 bytes capture limit) were not captured; {local.SNAPSHOT_REMEDY}"
    )
    assert lines[top + 1] == (
        "Nothing was reviewed: no model call ran, so none of the 3 changed areas was examined · Human review required: "
        "touches security-sensitive code"
    )
    assert "model calls failed" not in out and "Partially reviewed" not in out and "Reviewed " not in out
    assert lines_of(out.split("Model: ")[1])[0].endswith(" · no model calls"), "no call reached a model"
    assert lab.model.calls == [] and result.exit_code == 0, "exit codes are unchanged: incomplete is 2 under --strict"
    assert lab.run("--strict").exit_code == 2
    wire = lab.wire()
    assert wire["incomplete_reason"].startswith(NOT_CAPTURED) and wire["usage"]["calls"] == 0
    # The remedy it gives works: a commit's Snapshot is read from git, not from the capture.
    lab.commit("the change")
    remedied = lab.wire("HEAD~1..HEAD")
    assert remedied["complete"] and lab.model.calls and remedied["usage"]["calls"] > 0
    # With no human review flagged, a review that examined nothing never says none is needed.
    lab.write({"src/cache.py": CACHE_BUGGY + "\n\ndef forget(key):\n    CACHE.pop(key, None)\n"})
    calm = lab.run("--fresh").stdout
    assert "\nNothing was reviewed: no model call ran, so the one risk signal was not examined\n" in calm
    assert NO_HUMAN_REVIEW not in calm


def test_the_overview_is_counted_in_the_usage_the_terminal_prints(lab: Lab) -> None:
    lab.write(THREE)
    out = lab.run().stdout
    asked = len(lab.model.calls) + len(lab.model.dedupes)  # the investigations and the repeat check of their findings
    assert f"{asked + 1} model calls" in out
    assert TOKEN_BUGGY and local.NO_MODEL_ASKED not in out
