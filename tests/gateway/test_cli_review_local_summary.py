"""``lc review --ai`` prints the same structure as the pull request comment, in plain text: the confidence score, the impact
line, the verdict, the findings and the summary bullets.

Same lab as ``test_cli_review_local.py`` (a real repository, the real engine path, a fake model). The overview is one
more model call, made for a person at the command and not for a git hook, and reused while the tree is the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import local
from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, CACHE_BUGGY, TOKEN_BUGGY
from tests.gateway.test_cli_review_local import Lab, make_lab

#: Three changed files, so the change is not too small to summarise.
THREE = {"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY, "src/plain.py": "VALUE = 2\n"}


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return make_lab(tmp_path, monkeypatch, BASE)


def lines_of(text: str) -> list[str]:
    return text.split("\n")


def test_the_terminal_shows_the_score_the_impact_the_verdict_the_findings_and_the_summary_in_that_order(
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
    top = lines.index("Confidence Score: 3/5")
    assert top <= 2, "after the title and the line saying which configuration applied"
    assert lines[top + 1] == "[Medium impact] Guards authorize and bounds the cache"
    assert lines[top + 2].startswith("  security-sensitive code") and lines[top + 2].endswith(
        "(from the change itself, not a model)"
    )
    assert lines[top + 3] == "No must-fix findings · 1 should-fix · 1 optional"
    order = [
        out.index(part)
        for part in ("Findings\n", "1. P1 · should fix  src/auth.py:2-3", "Summary\n", "  - authorize rejects")
    ]
    assert order == sorted(order)
    assert "  The change touches the auth and cache modules.\n" in out
    assert "  - remember stores a list\n" in out
    assert "<" not in out.split("Summary")[1].split("Model:")[0], "plain text: no markup"
    assert lab.model.overviews and len(lab.model.overviews) == 1


def test_a_clean_review_is_a_five_and_has_no_findings_section(lab: Lab) -> None:
    lab.write({"src/plain.py": "VALUE = 2\n", "src/more.py": "A = 1\n" * 5, "src/third.py": "B = 2\n" * 5})
    out = lab.run().stdout
    assert "Confidence Score: 5/5" in out and "Findings\n" not in out
    assert "No must-fix findings" in out


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
    assert "Confidence Score: 3/5" in second and "Changes 3 files" in second
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
    top = lines.index("Confidence Score: 3/5")
    assert (
        lines[top + 1].startswith("[Medium impact]") and "Widgets" not in lines[top + 1]
    ), "a local change has no title"


def test_a_failed_overview_leaves_the_review_alone_and_says_so_in_a_note(lab: Lab) -> None:
    lab.model.fail_overview = "provider overloaded"
    lab.write(THREE)
    result = lab.run()
    assert "Confidence Score: 3/5" in result.stdout and "Summary\n" not in result.stdout
    assert "Note: overview skipped: the model call failed (RuntimeError)" in result.stdout
    assert "provider overloaded" not in result.stdout


def test_a_git_hook_asks_for_no_overview_because_it_must_stay_cheap(lab: Lab) -> None:
    lab.write(THREE)
    options = local.LocalOptions(repo_root=lab.repo, store_root=lab.store, fail_setup=False)
    report = local.run_local_review(options)
    assert report.state is not None and lab.model.overviews == [] and report.state.overview is None
    assert report.usage.calls == len(lab.model.calls), "its calls are the review's alone"


def test_the_overview_is_counted_in_the_usage_the_terminal_prints(lab: Lab) -> None:
    lab.write(THREE)
    out = lab.run().stdout
    investigated = len(lab.model.calls)
    assert f"{investigated + 1} model calls" in out
    assert TOKEN_BUGGY and local.NO_MODEL_ASKED not in out
