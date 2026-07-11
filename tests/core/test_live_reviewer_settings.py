from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.core.capabilities.default_definitions import (
    DEFAULT_OWNED_MODEL,
    READONLY_OWNED_MODEL,
)
from lemoncrow.core.capabilities.live_reviewer.settings import (
    DEFAULT_DEEP_INTERVAL,
    MAX_DEEP_INTERVAL,
    MIN_DEEP_INTERVAL,
    ReviewerSettings,
    load_reviewer_settings,
    split_provider_model,
)


def _write_settings(root: Path, data: dict) -> None:
    (root / "plugin_settings.json").write_text(json.dumps(data), encoding="utf-8")


def test_defaults_off(tmp_path: Path) -> None:
    s = load_reviewer_settings(tmp_path)
    assert s.live_reviewer is False
    assert s.deep_edit_count_reviewer is False
    assert s.enabled is False
    assert s.deep_edit_count_interval == DEFAULT_DEEP_INTERVAL


def test_reads_values(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        {
            "liveReviewer": True,
            "deepEditCountReviewer": True,
            "deepEditCountInterval": 25,
            "reviewModel": "anthropic/claude-x",
        },
    )
    s = load_reviewer_settings(tmp_path)
    assert s.enabled is True
    assert s.deep_edit_count_interval == 25
    assert s.review_model == "anthropic/claude-x"


def test_interval_clamped_low(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"deepEditCountInterval": 3})
    assert load_reviewer_settings(tmp_path).deep_edit_count_interval == MIN_DEEP_INTERVAL


def test_interval_clamped_high(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"deepEditCountInterval": 5000})
    assert load_reviewer_settings(tmp_path).deep_edit_count_interval == MAX_DEEP_INTERVAL


def test_interval_garbage_falls_back(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"deepEditCountInterval": "nope"})
    assert load_reviewer_settings(tmp_path).deep_edit_count_interval == DEFAULT_DEEP_INTERVAL


def test_nested_lemoncrow_block(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"lemoncrow": {"liveReviewer": True}})
    assert load_reviewer_settings(tmp_path).live_reviewer is True


def test_model_for_defaults() -> None:
    s = ReviewerSettings()
    assert s.model_for("live") == READONLY_OWNED_MODEL
    assert s.model_for("deep") == DEFAULT_OWNED_MODEL


def test_model_for_pinned() -> None:
    s = ReviewerSettings(live_reviewer_model="x/y", review_model="a/b")
    assert s.model_for("live") == "x/y"
    assert s.model_for("deep") == "a/b"


def test_split_provider_model() -> None:
    assert split_provider_model("anthropic/claude-x") == ("anthropic", "claude-x")
    assert split_provider_model("claude-x") == ("", "claude-x")
    assert split_provider_model("") == ("", "")
