"""Release upgrades must never compare a candidate with itself."""

import pytest

from scripts.select_previous_release import previous_release


def record(tag: str, **extra: object) -> dict[str, object]:
    return {"tag_name": tag, "draft": False, "prerelease": False, **extra}


def test_picks_latest_stable_predecessor_for_already_published_candidate() -> None:
    releases = [record("v0.8.2"), record("v0.8.0"), record("v0.7.4"), record("v0.8.1-rc1")]
    assert previous_release("v0.8.2", releases) == "v0.8.0"
    assert previous_release("v0.8.3", releases) == "v0.8.2"
    assert previous_release("v0.8.3-rc1", releases) == "v0.8.2"


def test_draft_prerelease_future_and_invalid_rows_are_not_selected() -> None:
    releases = [
        record("v1.0.0", draft=True),
        record("v0.9.0", prerelease=True),
        record("v0.8.1", draft=True),
        record("v0.8.0"),
        record("garbage"),
        record("v0.9.2"),
    ]
    assert previous_release("v0.8.2", releases) == "v0.8.0"


def test_no_safe_public_predecessor_fails_closed() -> None:
    with pytest.raises(ValueError, match="no published stable predecessor"):
        previous_release("v0.8.0", [record("v0.8.0"), record("v0.9.0")])
    with pytest.raises(ValueError, match="invalid candidate"):
        previous_release("unknown", [record("v0.8.0")])
