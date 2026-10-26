"""Auto looks at every changed code target within the hard call cap; a change too big for the cap stays honestly partial.

The model used to be asked only about a keyword "risk signal", two (later up to six) at a time, so ordinary changed code
was never looked at and a change with a few signals read "Partially reviewed". Now every risk signal gets its own
investigation, all the other changed code worth a look goes in batches that fit a prompt, no call is spent choosing among
them, and only a change whose calls do not fit the hard cap on model calls is left partly pending (the summary says how
much, and the next push competes for the cap again).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.gateway.pr_bot_support import BASE, World, build_world, one_call_per_file

SIGNAL_OLD = "def check(token):\n    return True\n"
SIGNAL_NEW = "def check(token):\n    return token is not None\n"


def signal_files(count: int, text: str) -> dict[str, str]:
    return {f"src/token/m{index}.py": text for index in range(count)}  # a "token" path is a security signal


def world_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: int) -> World:
    one_call_per_file(monkeypatch)  # these tests count calls against the cap; the batching has tests of its own
    return build_world(tmp_path, monkeypatch, {**BASE, **signal_files(signals, SIGNAL_OLD)})


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    one_call_per_file(monkeypatch)
    return build_world(tmp_path, monkeypatch, {**BASE, "src/token.py": "def verify(token):\n    return True\n"})


def test_a_small_change_has_every_risk_signal_examined_and_reads_clean(world: World) -> None:
    """The case from the audit: three risk signals, nothing found. It is never "Partially reviewed: 2 of 3"."""

    world.origin.push(
        {
            "src/auth.py": "def authorize(user):\n    return bool(user) and user.is_admin\n",
            "src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = (value,)\n",
            "src/token.py": "def verify(token):\n    return token is not None\n",
        }
    )
    report = world.review()
    assert sorted(world.model.paths()) == ["src/auth.py", "src/cache.py", "src/token.py"]
    assert "(synthesis)" not in world.model.paths(), "no call is spent choosing among signals that all get a call"
    assert report.verdict is not None and (report.verdict.state, report.verdict.headline) == (
        "clean",
        "No must-fix findings",
    )
    assert report.status == ("success", "No must-fix findings")
    assert "not investigated yet" not in world.github.summary_comments()[0]["body"]


@pytest.mark.parametrize("signals", [3, 6])
def test_every_signal_gets_an_investigation_while_the_calls_fit_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: int
) -> None:
    world = world_with(tmp_path, monkeypatch, signals)
    world.origin.push(signal_files(signals, SIGNAL_NEW))
    report = world.review()
    assert len(world.model.paths()) == signals and "(synthesis)" not in world.model.paths()
    assert report.verdict is not None and report.verdict.state == "clean"


def test_a_change_whose_calls_do_not_fit_the_hard_cap_keeps_the_highest_ranked_and_says_partially_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signals = 25  # the hard cap on model calls is 20
    monkeypatch.setenv("LEMONCROW_REVIEW_TAIL_STOP", "off")  # this test is about the cap, not about a clean tail
    world = world_with(tmp_path, monkeypatch, signals)
    world.origin.push(signal_files(signals, SIGNAL_NEW))
    report = world.review()
    assert len(world.model.paths()) == 20 and "(synthesis)" not in world.model.paths()
    headline = f"Partially reviewed: 20 of {signals} risk signals examined"
    assert report.verdict is not None and (report.verdict.state, report.verdict.headline) == ("partial", headline)
    assert report.status == ("success", headline), "partial stays a pass that says what it covered"
    assert f"{signals - 20} risk signals not investigated yet" in world.github.summary_comments()[0]["body"]


# The batches are ranked by how likely they are to hold a defect. The first six run together; past them the change is
# covered in rounds of three, and the first round to come back clean ends the run: what is ranked below it is pending.
# The path sort puts the twelve files in the order m0 m1 m10 m11 m2 m3 | m4 m5 m6 | m7 m8 m9.


def test_a_clean_tail_ends_the_review_after_the_first_wave_and_one_clean_round_and_says_what_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signals = 12
    world = world_with(tmp_path, monkeypatch, signals)
    world.origin.push(signal_files(signals, SIGNAL_NEW))
    report = world.review()
    assert len(world.model.paths()) == 9 and "(synthesis)" not in world.model.paths()
    headline = f"Partially reviewed: 9 of {signals} risk signals examined"
    assert report.verdict is not None and (report.verdict.state, report.verdict.headline) == ("partial", headline)
    assert report.status == ("success", headline), "a partial review is a pass that says what it covered"
    assert f"{signals - 9} risk signals not investigated yet" in world.github.summary_comments()[0]["body"]
    # The three left compete for the next run, which is small enough to cover them all.
    reviewed = set(world.model.files_seen())
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "touch an unrelated file")
    done = world.review()
    left = {path for path in world.model.files_seen() if path != "src/plain.py"}
    assert len(left) == 3 and not left & reviewed
    assert done.verdict is not None and done.verdict.state == "clean"


def test_a_round_that_finds_something_keeps_the_review_going_to_the_end_of_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signals = 12
    world = world_with(tmp_path, monkeypatch, signals)
    files = signal_files(signals, SIGNAL_NEW)
    files["src/token/m5.py"] = SIGNAL_NEW + "# BUG: accepts an empty token\n"  # in the second round
    world.origin.push(files)
    report = world.review()
    assert len(world.model.paths()) == signals, "the second round was not clean, so the third ran"
    assert [finding.title for finding in report.findings] == ["accepts an empty token"]


def test_a_finding_in_the_first_wave_does_not_keep_a_clean_tail_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first wave is run whole whatever it finds, and says nothing of the tail: only the round just run decides."""

    world = world_with(tmp_path, monkeypatch, 12)
    files = signal_files(12, SIGNAL_NEW)
    files["src/token/m0.py"] = SIGNAL_NEW + "# BUG: accepts an empty token\n"
    world.origin.push(files)
    world.review()
    assert len(world.model.paths()) == 9


@pytest.mark.parametrize("how", ["switch", "deep"])
def test_the_tail_is_covered_in_full_when_the_switch_is_off_and_at_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    if how == "switch":
        monkeypatch.setenv("LEMONCROW_REVIEW_TAIL_STOP", "off")
    world = world_with(tmp_path, monkeypatch, 12)
    world.origin.push(signal_files(12, SIGNAL_NEW))
    world.review(depth="deep" if how == "deep" else "auto")
    assert set(world.model.files_seen()) - {"(synthesis)"} == {f"src/token/m{index}.py" for index in range(12)}


def test_thorough_stops_a_clean_tail_too_and_still_runs_what_the_model_proposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, 12)
    world.origin.push(signal_files(12, SIGNAL_NEW))
    world.review(depth="thorough")
    paths = world.model.paths()
    assert paths[0] == "(synthesis)"
    assert (
        len(paths) - 1 == 9 + 4
    ), "nine batches the clean tail allowed, and the four hypotheses the synthesis proposed"


def test_the_hard_cap_on_model_calls_limits_how_far_a_small_change_is_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "4")
    world = world_with(tmp_path, monkeypatch, 6)
    world.origin.push(signal_files(6, SIGNAL_NEW))
    report = world.review()
    assert len(world.model.paths()) == 4
    assert report.verdict is not None and report.verdict.headline == "Partially reviewed: 4 of 6 risk signals examined"
    # The next push competes for the cap again: the two signals left are examined (and the file touched, which is
    # changed code of its own), and then nothing is owed.
    reviewed = set(world.model.paths())
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "touch an unrelated file")
    done = world.review()
    left = {path for path in world.model.files_seen() if path != "src/plain.py"}
    assert len(left) == 2 and not left & reviewed
    assert "src/plain.py" in world.model.files_seen()
    assert done.verdict is not None and done.verdict.state == "clean"


def test_changed_code_with_no_risk_signal_makes_the_partial_headline_count_changed_areas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the review also looks at plain code, the count is of everything it set out to look at, and says so."""

    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "2")
    world = world_with(tmp_path, monkeypatch, 2)
    world.origin.push(
        {
            **signal_files(2, SIGNAL_NEW),
            "src/plain.py": "VALUE = 3\n",
            "src/other.py": "def other():\n    return 2\n",
        }
    )
    report = world.review()
    # The two risk signals rank first; the two plain files are pending.
    assert sorted(world.model.paths()) == ["src/token/m0.py", "src/token/m1.py"]
    assert report.verdict is not None and report.verdict.state == "partial"
    assert report.verdict.headline == "Partially reviewed: 2 of 4 changed areas examined"
    assert "2 changed areas not investigated yet" in world.github.summary_comments()[0]["body"]


def test_an_explicit_depth_goes_beyond_auto_and_never_below_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Thorough covers the change exactly as Auto does, then spends what is left of the cap on hypotheses the model
    proposes (one synthesis call first); it does not look at less."""

    world = world_with(tmp_path, monkeypatch, 3)
    world.origin.push(signal_files(3, SIGNAL_NEW))
    world.review(depth="thorough")
    paths = world.model.paths()
    assert paths[0] == "(synthesis)"
    assert {"src/token/m0.py", "src/token/m1.py", "src/token/m2.py"} <= set(world.model.files_seen())
    assert len(paths) > 3 + 1, "the proposed hypotheses are investigated on top of the coverage"


def test_small_files_with_a_risk_signal_of_one_category_share_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twelve tiny files that all carry a security signal are one question, not twelve; the order is the same."""

    world = build_world(tmp_path, monkeypatch, {**BASE, **signal_files(12, SIGNAL_OLD)})
    world.origin.push(signal_files(12, SIGNAL_NEW))
    report = world.review()
    assert len(world.model.calls) <= 2, "twelve small files, one category"
    assert len(world.model.files_seen()) == 12 and {call["category"] for call in world.model.calls} == {"security"}
    assert report.verdict is not None and report.verdict.headline == "No must-fix findings"
    body = world.github.summary_comments()[0]["body"]
    assert "Risks examined: 12 of 12" in body and "not investigated yet" not in body
