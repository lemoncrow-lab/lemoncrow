"""Auto covers every risk signal of a small change, within the hard call cap; a large one stays honestly partial.

The default depth used to investigate two signals and call a change with three "Partially reviewed", which is what the
very first pull request of a team read. Up to ``AUTO_FULL_COVERAGE_SIGNALS`` signals each get an investigation of their
own (no call is spent choosing among them), the hard cap still applies, and above the limit the summary says how many
signals it reached.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.ai_review import (
    AUTO_FULL_COVERAGE_SIGNALS,
    ReviewCandidate,
    _auto_cover,
)
from tests.gateway.pr_bot_support import BASE, World, build_world

SIGNAL_OLD = "def check(token):\n    return True\n"
SIGNAL_NEW = "def check(token):\n    return token is not None\n"


def signal_files(count: int, text: str) -> dict[str, str]:
    return {f"src/token/m{index}.py": text for index in range(count)}  # a "token" path is a security signal


def world_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: int) -> World:
    return build_world(tmp_path, monkeypatch, {**BASE, **signal_files(signals, SIGNAL_OLD)})


def candidate(category: str, *targets: str, score: int = 100) -> ReviewCandidate:
    return ReviewCandidate(category, targets, score, ("a reason",), ", ".join(targets))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# the whole pull request, end to end
# --------------------------------------------------------------------------- #


def test_a_small_change_has_every_risk_signal_examined_and_reads_clean(world: World) -> None:
    """The case from the audit: three risk signals, nothing found. It is no longer "Partially reviewed: 2 of 3"."""

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


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, {**BASE, "src/token.py": "def verify(token):\n    return True\n"})


@pytest.mark.parametrize("signals", [3, AUTO_FULL_COVERAGE_SIGNALS])
def test_up_to_the_limit_every_signal_gets_an_investigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: int
) -> None:
    world = world_with(tmp_path, monkeypatch, signals)
    world.origin.push(signal_files(signals, SIGNAL_NEW))
    report = world.review()
    assert len(world.model.paths()) == signals and "(synthesis)" not in world.model.paths()
    assert report.verdict is not None and report.verdict.state == "clean"


def test_above_the_limit_auto_keeps_its_two_investigations_and_says_partially_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signals = AUTO_FULL_COVERAGE_SIGNALS + 1
    world = world_with(tmp_path, monkeypatch, signals)
    world.origin.push(signal_files(signals, SIGNAL_NEW))
    report = world.review()
    assert world.model.paths()[0] == "(synthesis)" and len(world.model.paths()) == 3
    headline = f"Partially reviewed: 2 of {signals} risk signals examined"
    assert report.verdict is not None and (report.verdict.state, report.verdict.headline) == ("partial", headline)
    assert report.status == ("success", headline), "partial stays a pass that says what it covered"
    assert f"{signals - 2} risk signals not investigated yet" in world.github.summary_comments()[0]["body"]


def test_the_hard_cap_on_model_calls_limits_how_far_a_small_change_is_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "4")
    world = world_with(tmp_path, monkeypatch, 6)
    world.origin.push(signal_files(6, SIGNAL_NEW))
    report = world.review()
    assert len(world.model.paths()) == 4
    assert report.verdict is not None and report.verdict.headline == "Partially reviewed: 4 of 6 risk signals examined"
    # The next push competes for the cap again: the two signals left are examined, and then nothing is owed.
    reviewed = set(world.model.paths())
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "touch an unrelated file")
    done = world.review()
    left = {path for path in world.model.paths() if path != "(synthesis)"}
    assert len(left) == 2 and not left & reviewed
    assert done.verdict is not None and done.verdict.state == "clean"


def test_an_explicit_depth_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = world_with(tmp_path, monkeypatch, 3)
    world.origin.push(signal_files(3, SIGNAL_NEW))
    world.review(depth="thorough")
    assert world.model.paths()[0] == "(synthesis)"


# --------------------------------------------------------------------------- #
# the selection itself
# --------------------------------------------------------------------------- #


def test_one_candidate_per_target_in_rank_order_and_none_outside_the_window() -> None:
    ranked = [candidate("security", f"t{i}", score=100 - i) for i in range(4)]
    cover = _auto_cover(ranked, paths={}, calls_left=20)
    assert cover is not None and [c.target_ids for c in cover] == [("t0",), ("t1",), ("t2",), ("t3",)]
    assert _auto_cover(ranked[:2], paths={}, calls_left=20) is None, "two signals are the normal Auto"
    too_many = [candidate("security", f"t{i}") for i in range(AUTO_FULL_COVERAGE_SIGNALS + 1)]
    assert _auto_cover(too_many, paths={}, calls_left=20) is None
    assert _auto_cover(ranked, paths={}, calls_left=0) is None


def test_a_target_with_two_categories_costs_one_call_and_a_rule_covers_its_targets_with_one() -> None:
    ranked = [
        candidate("security", "a", score=100),
        candidate("correctness", "a", score=80),
        candidate("critical_rule", "b", "c", score=90),
        candidate("performance", "d", score=72),
    ]
    cover = _auto_cover(ranked, paths={}, calls_left=20)
    assert cover is not None and [(c.category, c.target_ids) for c in cover] == [
        ("security", ("a",)),
        ("critical_rule", ("b", "c")),
        ("performance", ("d",)),
    ]


def test_targets_of_one_file_that_share_a_signal_are_asked_about_together() -> None:
    """A function is often several targets (its signature, its body, a hunk); one question each yields one finding each."""

    ranked = [candidate("security", "z", score=95)] + [
        candidate("api_compatibility", f"t{i}", score=90) for i in range(3)
    ]
    paths = {"t0": "pricing.py", "t1": "pricing.py", "t2": "pricing.py", "z": "auth.py"}
    cover = _auto_cover(ranked, paths=paths, calls_left=20)
    assert cover is not None and [(c.category, c.target_ids) for c in cover] == [
        ("security", ("z",)),
        ("api_compatibility", ("t0", "t1", "t2")),
    ]


def test_the_cap_keeps_the_highest_ranked() -> None:
    ranked = [candidate("security", f"t{i}", score=100 - i) for i in range(5)]
    cover = _auto_cover(ranked, paths={}, calls_left=3)
    assert cover is not None and [c.target_ids for c in cover] == [("t0",), ("t1",), ("t2",)]
