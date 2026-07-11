"""Unit tests for the pure search-verdict core (Phase 1 keystone)."""

from __future__ import annotations

import pytest

from lemoncrow.core.capabilities.code_context.search_verdict import (
    ChannelHealth,
    SearchHistory,
    breaker_threshold,
    compute_verdict,
    normalize_query,
    reformulation_threshold,
)


def test_found_when_hits_present() -> None:
    res = compute_verdict(hit_count=3, query="timezone conversion")
    assert res.verdict == "found"
    assert res.next == ""
    assert res.as_payload() == {"verdict": "found"}


def test_found_short_circuits_even_with_dark_channel() -> None:
    # A hit is a hit regardless of channel health.
    res = compute_verdict(hit_count=1, query="foo", channels=ChannelHealth(semantic=False, zoekt=False))
    assert res.verdict == "found"


@pytest.mark.parametrize(
    "channels,expected_label",
    [
        (ChannelHealth(semantic=False), "semantic"),
        (ChannelHealth(zoekt=False), "zoekt"),
        (ChannelHealth(semantic=False, zoekt=False), "semantic + zoekt"),
    ],
)
def test_dark_when_empty_and_wanted_channel_off(channels: ChannelHealth, expected_label: str) -> None:
    res = compute_verdict(hit_count=0, query="foo bar", channels=channels)
    assert res.verdict == "dark"
    assert expected_label in res.next
    assert res.as_payload()["next"]


def test_channel_none_is_not_dark() -> None:
    # None = not applicable to this query/mode -> empty is honest, not dark.
    res = compute_verdict(hit_count=0, query="foo", channels=ChannelHealth(semantic=None, zoekt=None))
    assert res.verdict == "missed"


def test_first_empty_is_missed_not_absent() -> None:
    res = compute_verdict(hit_count=0, query="render template tag")
    assert res.verdict == "missed"
    assert res.next == ""  # plain empty, no reformulate nudge


def test_second_distinct_same_area_empty_is_absent() -> None:
    prior = (normalize_query("timezone convert backend"),)
    res = compute_verdict(hit_count=0, query="timezone offset handling", prior_empties=prior)
    assert res.verdict == "absent"
    assert res.next == ""  # plain empty, no broaden-scope nudge


def test_second_empty_different_area_stays_missed() -> None:
    prior = (normalize_query("database migration squash"),)
    res = compute_verdict(hit_count=0, query="timezone offset handling", prior_empties=prior)
    assert res.verdict == "missed"


def test_exact_rerun_does_not_escalate_to_absent() -> None:
    # Re-running the identical empty query is not a reformulation.
    q = "timezone convert"
    res = compute_verdict(hit_count=0, query=q, prior_empties=(normalize_query(q),))
    assert res.verdict == "missed"


def test_near_identical_rerun_does_not_escalate() -> None:
    # High token overlap (Jaccard >= cap) counts as a re-run, not a new phrasing.
    prior = (normalize_query("timezone convert backend utc"),)
    res = compute_verdict(hit_count=0, query="timezone convert backend", prior_empties=prior)
    assert res.verdict == "missed"


def test_threshold_override_param() -> None:
    prior = (normalize_query("alpha beta"),)
    # threshold=3 needs two prior distinct same-area empties; one is not enough.
    res = compute_verdict(hit_count=0, query="alpha gamma delta", prior_empties=prior, threshold=3)
    assert res.verdict == "missed"


def test_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_SEARCH_REFORMULATION_THRESHOLD", "4")
    assert reformulation_threshold() == 4
    monkeypatch.setenv("LEMONCROW_SEARCH_REFORMULATION_THRESHOLD", "garbage")
    assert reformulation_threshold() == 2
    monkeypatch.setenv("LEMONCROW_SEARCH_REFORMULATION_THRESHOLD", "0")
    assert reformulation_threshold() == 2


def test_normalize_query_is_token_set() -> None:
    assert normalize_query("Foo, bar FOO!") == frozenset({"foo", "bar"})
    assert normalize_query("") == frozenset()


class TestSearchHistory:
    def test_records_only_empties(self) -> None:
        h = SearchHistory()
        h.record("alpha beta", found=False)
        h.record("gamma delta", found=True)
        assert h.prior_empties() == (normalize_query("alpha beta"),)

    def test_blank_query_ignored(self) -> None:
        h = SearchHistory()
        h.record("   ", found=False)
        assert h.prior_empties() == ()

    def test_found_prunes_same_area_streak(self) -> None:
        h = SearchHistory()
        h.record("timezone convert backend", found=False)
        h.record("database migration", found=False)
        # A hit in the timezone area clears that streak, leaving the unrelated one.
        h.record("timezone offset utc", found=True)
        assert h.prior_empties() == (normalize_query("database migration"),)

    def test_window_is_bounded(self) -> None:
        h = SearchHistory()
        for i in range(50):
            h.record(f"unique-term-{i}", found=False)
        assert len(h.prior_empties()) <= 24

    def test_breaker_counts_consecutive_unproductive(self) -> None:
        h = SearchHistory()
        for i in range(6):
            h.record(f"miss-term-{i}", found=False)
        assert h.unproductive_streak == 6
        assert h.breaker_tripped(threshold=6) is True
        assert h.breaker_tripped(threshold=10) is False

    def test_found_resets_breaker_streak(self) -> None:
        h = SearchHistory()
        h.record("miss one", found=False)
        h.record("miss two", found=False)
        assert h.unproductive_streak == 2
        h.record("hit here", found=True)
        assert h.unproductive_streak == 0
        assert h.breaker_tripped(threshold=2) is False

    def test_breaker_disabled_when_threshold_non_positive(self) -> None:
        h = SearchHistory()
        for _ in range(20):
            h.record("miss", found=False)
        assert h.breaker_tripped(threshold=0) is False

    def test_breaker_threshold_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEMONCROW_SEARCH_BREAKER_THRESHOLD", "3")
        assert breaker_threshold() == 3
        monkeypatch.setenv("LEMONCROW_SEARCH_BREAKER_THRESHOLD", "junk")
        assert breaker_threshold() == 6

    def test_drives_absent_across_calls(self) -> None:
        h = SearchHistory()
        q1 = "timezone convert backend"
        v1 = compute_verdict(hit_count=0, query=q1, prior_empties=h.prior_empties())
        h.record(q1, found=False)
        assert v1.verdict == "missed"

        q2 = "timezone offset handling"
        v2 = compute_verdict(hit_count=0, query=q2, prior_empties=h.prior_empties())
        assert v2.verdict == "absent"
