"""P5-4: per-user Review settings: validation and the store. The HTTP routes are tested in server/tests/test_local_review_settings.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.user_settings import (
    RESERVED_KEYS,
    UserSettingsError,
    apply_patch,
    effective,
)


def test_reserved_keys_match_the_frontend_keymap() -> None:
    source = (Path(__file__).resolve().parents[2] / "frontend/src/review/flowKeymap.ts").read_text(encoding="utf-8")
    match = re.search(r"export const RESERVED_KEYS: readonly string\[\] = \[(.*?)\];", source)
    assert match is not None
    assert tuple(re.findall(r'"([^"]*)"', match.group(1))) == RESERVED_KEYS


def test_a_patch_sets_and_forgets_keys() -> None:
    stored = apply_patch({}, {"default_view": "all", "skip_overview_single_decision": False})
    assert stored == {"default_view": "all", "skip_overview_single_decision": False}
    assert apply_patch(stored, {"default_view": None}) == {"skip_overview_single_decision": False}
    assert effective({})["default_view"] == "one"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"default_view": "grid"}, "default_view must be one of one, all."),
        ({"theme": "sepia"}, "theme must be one of light, dark, auto."),
        ({"skip_overview_single_decision": "yes"}, "skip_overview_single_decision must be true or false."),
        ({"review_mode": "focus"}, "Unknown setting: review_mode."),
        ({"key_looks_good": "ab"}, "Pick a single letter, number or symbol."),
        ({"key_looks_good": "A"}, "Use the lowercase key."),
        ({"key_looks_good": "c"}, "C is already used for Needs a change. Pick another key."),
        ({"key_looks_good": "j"}, "J is already used for next part. Pick another key."),
        ({"key_needs_change": "a"}, "A is already used for Looks good. Pick another key."),
    ],
)
def test_a_bad_patch_is_refused_with_a_sentence(patch: dict[str, object], message: str) -> None:
    with pytest.raises(UserSettingsError, match=re.escape(message)):
        apply_patch({}, patch)


def test_the_store_keeps_only_set_keys_and_a_full_reset_leaves_no_row(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    assert store.get_user_settings("local") == {}
    store.patch_user_settings("local", {"key_looks_good": "y"})
    store.patch_user_settings("local", {"theme": "dark"})
    assert store.get_user_settings("local") == {"key_looks_good": "y", "theme": "dark"}
    store.patch_user_settings("local", {"key_looks_good": None, "theme": None})
    assert store.get_user_settings("local") == {}
    store.patch_user_settings("other", {"theme": "light"})
    assert store.get_user_settings("local") == {}
    store.reset_user_settings("other")
    with store._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM review_user_settings").fetchone()["n"] == 0


def test_two_keys_can_be_swapped_in_one_patch() -> None:
    stored = apply_patch({}, {"key_looks_good": "c", "key_needs_change": "a"})
    assert (stored["key_looks_good"], stored["key_needs_change"]) == ("c", "a")


def test_forgetting_a_key_that_would_clash_with_another_is_refused() -> None:
    with pytest.raises(UserSettingsError, match="A is already used for Looks good"):
        apply_patch(
            {"key_looks_good": "a", "key_needs_change": "y"}, {"key_needs_change": None, "key_decide_later": "a"}
        )


def test_forgetting_a_single_key_that_clashes_is_refused() -> None:
    # needs_change forgotten returns to its default C, which looks_good now holds.
    with pytest.raises(UserSettingsError, match="C is already used for Looks good"):
        apply_patch({"key_looks_good": "c", "key_needs_change": "y"}, {"key_needs_change": None})
