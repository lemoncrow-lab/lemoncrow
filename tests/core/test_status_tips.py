from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.core.capabilities.savings_summary import (
    _STATUS_TIPS,
    _resolve_status_text,
    _status_tip,
)


def _authed(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")


def test_tip_shown_when_no_higher_status(tmp_path: Path) -> None:
    _authed(tmp_path)
    assert _resolve_status_text(tmp_path) in _STATUS_TIPS


def test_tips_can_be_disabled(tmp_path: Path) -> None:
    _authed(tmp_path)
    (tmp_path / "plugin_settings.json").write_text(json.dumps({"statusLineTips": False}), encoding="utf-8")
    assert _resolve_status_text(tmp_path) == ""


def test_tips_disabled_via_nested_lemoncrow_block(tmp_path: Path) -> None:
    _authed(tmp_path)
    (tmp_path / "plugin_settings.json").write_text(json.dumps({"lemoncrow": {"statusLineTips": False}}), encoding="utf-8")
    assert _resolve_status_text(tmp_path) == ""


def test_login_takes_priority_over_tip(tmp_path: Path) -> None:
    # No auth.json -> login status wins over any tip.
    assert _resolve_status_text(tmp_path) == "login"


def test_status_tip_is_known() -> None:
    assert _status_tip() in _STATUS_TIPS
