"""Regression tests for _claude_additional_dirs (issue #37).

Claude Code writes its allow-listed extra directories under the nested
``permissions.additionalDirectories`` key, not (only) the legacy top-level
``additionalDirectories`` key. Both must be honored, from both the home and
workspace settings.json files.
"""

from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.gateway.adapters.mcp import fs_access
from lemoncrow.gateway.adapters.mcp.fs_access import _claude_additional_dirs


def _reset_cache() -> None:
    fs_access._CLAUDE_ADDITIONAL_DIRS_CACHE.clear()


def test_nested_permissions_additional_directories_honored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    extra = tmp_path / "plans"
    (home / ".claude").mkdir(parents=True)
    workspace.mkdir()
    extra.mkdir()

    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"additionalDirectories": [str(extra)]}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("LEMONCROW_ADDITIONAL_DIRS", raising=False)
    _reset_cache()

    dirs = _claude_additional_dirs(workspace)

    assert extra.resolve() in dirs


def test_top_level_and_nested_keys_both_merge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    top_level_dir = tmp_path / "top"
    nested_dir = tmp_path / "nested"
    (home / ".claude").mkdir(parents=True)
    workspace.mkdir()
    top_level_dir.mkdir()
    nested_dir.mkdir()

    (home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "additionalDirectories": [str(top_level_dir)],
                "permissions": {"additionalDirectories": [str(nested_dir)]},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("LEMONCROW_ADDITIONAL_DIRS", raising=False)
    _reset_cache()

    dirs = _claude_additional_dirs(workspace)

    assert top_level_dir.resolve() in dirs
    assert nested_dir.resolve() in dirs
