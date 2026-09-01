"""Regression tests for _claude_additional_dirs (issue #37).

Claude Code writes its allow-listed extra directories under the nested
``permissions.additionalDirectories`` key, not (only) the legacy top-level
``additionalDirectories`` key, and its ``/permissions`` UI persists them to
``settings.local.json`` rather than ``settings.json``. All four files (home and
workspace x settings.json and settings.local.json) and both keys must be
honored, live (no restart), without ever widening the boundary beyond what was
listed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from lemoncrow.gateway.adapters.mcp import fs_access
from lemoncrow.gateway.adapters.mcp.fs_access import _claude_additional_dirs


def _reset_cache() -> None:
    fs_access._CLAUDE_ADDITIONAL_DIRS_CACHE.clear()
    fs_access._MALFORMED_SETTINGS_WARNED.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake home + workspace with the env var cleared and caches reset."""

    class _Env:
        home = tmp_path / "home"
        workspace = tmp_path / "ws"

        @staticmethod
        def settings(scope: str, name: str = "settings.json") -> Path:
            base = _Env.home if scope == "home" else _Env.workspace
            return base / ".claude" / name

        @staticmethod
        def write(scope: str, payload: dict, name: str = "settings.json") -> Path:
            p = _Env.settings(scope, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload), encoding="utf-8")
            return p

        @staticmethod
        def dir(name: str) -> Path:
            d = tmp_path / name
            d.mkdir(parents=True, exist_ok=True)
            return d

        @staticmethod
        def resolve() -> list[Path]:
            _reset_cache()
            return _claude_additional_dirs(_Env.workspace)

    (_Env.home / ".claude").mkdir(parents=True)
    _Env.workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: _Env.home))
    # ``Path.expanduser`` consults $HOME, not ``Path.home``; keep the two agreeing
    # so `~` behaves the same for expansion and for the home-grant check.
    monkeypatch.setenv("HOME", str(_Env.home))
    monkeypatch.delenv("LEMONCROW_ADDITIONAL_DIRS", raising=False)
    _reset_cache()
    yield _Env
    _reset_cache()


def test_nested_permissions_additional_directories_honored(env):
    extra = env.dir("plans")
    env.write("home", {"permissions": {"additionalDirectories": [str(extra)]}})

    assert extra.resolve() in env.resolve()


def test_top_level_and_nested_keys_both_merge(env):
    top_level_dir = env.dir("top")
    nested_dir = env.dir("nested")
    env.write(
        "home",
        {
            "additionalDirectories": [str(top_level_dir)],
            "permissions": {"additionalDirectories": [str(nested_dir)]},
        },
    )

    dirs = env.resolve()

    assert top_level_dir.resolve() in dirs
    assert nested_dir.resolve() in dirs


def test_workspace_settings_json_honored(env):
    extra = env.dir("ws-extra")
    env.write("workspace", {"permissions": {"additionalDirectories": [str(extra)]}})

    assert extra.resolve() in env.resolve()


@pytest.mark.parametrize("scope", ["home", "workspace"])
@pytest.mark.parametrize("key", ["additionalDirectories", "permissions"])
def test_settings_local_json_honored(env, scope, key):
    """Claude Code's /permissions UI writes settings.local.json, not settings.json."""
    extra = env.dir(f"local-{scope}-{key}")
    payload = (
        {"additionalDirectories": [str(extra)]}
        if key == "additionalDirectories"
        else {"permissions": {"additionalDirectories": [str(extra)]}}
    )
    env.write(scope, payload, name="settings.local.json")

    assert extra.resolve() in env.resolve()


def test_all_four_settings_files_merge(env):
    dirs_by_file = {
        ("home", "settings.json"): env.dir("a"),
        ("home", "settings.local.json"): env.dir("b"),
        ("workspace", "settings.json"): env.dir("c"),
        ("workspace", "settings.local.json"): env.dir("d"),
    }
    for (scope, name), d in dirs_by_file.items():
        env.write(scope, {"permissions": {"additionalDirectories": [str(d)]}}, name=name)

    resolved = env.resolve()

    for d in dirs_by_file.values():
        assert d.resolve() in resolved


def test_live_reload_on_settings_local_create_and_delete(env):
    """Creating/deleting settings.local.json must invalidate the mtime-keyed cache."""
    extra = env.dir("late")
    path = env.settings("workspace", "settings.local.json")

    # No cache reset between calls: the cache key itself must notice the file.
    assert extra.resolve() not in _claude_additional_dirs(env.workspace)

    env.write("workspace", {"permissions": {"additionalDirectories": [str(extra)]}}, name="settings.local.json")
    assert extra.resolve() in _claude_additional_dirs(env.workspace)

    path.unlink()
    assert extra.resolve() not in _claude_additional_dirs(env.workspace)


def test_env_var_accepts_pathsep_separated_entries(env, monkeypatch):
    """``os.pathsep`` is the separator, and multi-entry values still work."""
    first = env.dir("env-one")
    second = env.dir("env-two")
    third = env.dir("env-three")
    monkeypatch.setenv("LEMONCROW_ADDITIONAL_DIRS", f" {first}{os.pathsep}{second}{os.pathsep}{third} ")

    dirs = env.resolve()

    assert first.resolve() in dirs
    assert second.resolve() in dirs
    assert third.resolve() in dirs


def test_env_var_ignores_empty_and_whitespace_entries(env, monkeypatch):
    only = env.dir("env-only")
    monkeypatch.setenv("LEMONCROW_ADDITIONAL_DIRS", f"{os.pathsep}  {os.pathsep} {only} {os.pathsep}")

    assert env.resolve() == [only.resolve()]


def test_directory_name_containing_a_comma_is_one_path(env, monkeypatch):
    """A comma is a legal directory-name character, not a separator.

    Splitting on it made `/x/data,backup` unrepresentable *and* silently granted
    `/x/data` -- a directory the user never named.
    """
    comma_dir = env.dir("data,backup")
    never_named = env.dir("data")
    monkeypatch.setenv("LEMONCROW_ADDITIONAL_DIRS", str(comma_dir))

    dirs = env.resolve()

    assert dirs == [comma_dir.resolve()]
    assert never_named.resolve() not in dirs


def test_relative_entry_is_rejected_not_resolved_against_cwd(env, monkeypatch, caplog):
    """A bare fragment must never resolve against the MCP server's CWD."""
    monkeypatch.setenv("LEMONCROW_ADDITIONAL_DIRS", "backup")

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        dirs = env.resolve()

    assert dirs == []
    assert (Path.cwd() / "backup").resolve() not in dirs
    assert "backup" in caplog.text


def test_relative_entry_in_settings_file_is_rejected(env, caplog):
    path = env.write("workspace", {"permissions": {"additionalDirectories": ["notes"]}})

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        dirs = env.resolve()

    assert dirs == []
    assert str(path) in caplog.text


@pytest.mark.parametrize("raw", [".", "..", "./sub", "../sibling"])
def test_dot_and_dotdot_entries_are_rejected(env, monkeypatch, caplog, raw):
    monkeypatch.setenv("LEMONCROW_ADDITIONAL_DIRS", raw)

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        dirs = env.resolve()

    assert dirs == []
    assert caplog.records


@pytest.mark.parametrize("raw", ["/", "~"])
def test_filesystem_root_and_bare_home_are_rejected(env, monkeypatch, caplog, raw):
    """`/` and `~` are blanket grants, not directories worth allow-listing."""
    monkeypatch.setenv("LEMONCROW_ADDITIONAL_DIRS", raw)

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        dirs = env.resolve()

    assert dirs == []
    assert caplog.records


@pytest.mark.parametrize("entry", ["/", "~", "home"])
def test_workspace_settings_local_cannot_grant_the_whole_filesystem(env, caplog, entry):
    """A cloned repo's .claude/settings.local.json must not hand over / or ~."""
    raw = str(env.home) if entry == "home" else entry
    path = env.write(
        "workspace",
        {"permissions": {"additionalDirectories": [raw]}},
        name="settings.local.json",
    )

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        dirs = env.resolve()

    assert dirs == []
    assert str(path) in caplog.text


def test_blanket_grant_does_not_discard_the_other_entries(env, monkeypatch):
    keep = env.dir("keep")
    monkeypatch.setenv("LEMONCROW_ADDITIONAL_DIRS", f"/{os.pathsep}{keep}")

    assert env.resolve() == [keep.resolve()]


def test_subdirectory_of_home_is_still_allowed(env):
    notes = env.home / "notes"
    notes.mkdir(parents=True)
    env.write("home", {"permissions": {"additionalDirectories": [str(notes)]}})

    assert notes.resolve() in env.resolve()


def test_tilde_prefixed_subdirectory_is_expanded_and_allowed(env):
    """`~` expansion stays supported for entries that are absolute once expanded."""
    notes = env.home / "notes"
    notes.mkdir(parents=True)
    env.write("home", {"permissions": {"additionalDirectories": ["~/notes"]}})

    assert notes.resolve() in env.resolve()


def test_sibling_dir_sharing_name_prefix_stays_blocked(env):
    """`/x/plans` must not authorize `/x/plans-secret` (component-wise containment)."""
    allowed = env.dir("plans")
    sibling = env.dir("plans-secret")
    env.write("home", {"permissions": {"additionalDirectories": [str(allowed)]}})

    roots = env.resolve()

    assert allowed.resolve() in roots
    assert sibling.resolve() not in roots
    target = (sibling / "loot.txt").resolve()
    # Same containment predicate the edit gate applies to each touched path.
    assert not any(target == r or target.is_relative_to(r) for r in roots)
    assert any((allowed / "ok.txt").resolve() == r or (allowed / "ok.txt").resolve().is_relative_to(r) for r in roots)


def test_retrieval_env_var_does_not_widen_the_edit_gate(env, monkeypatch):
    """A retrieval/indexing knob must never grant write access (issue #37)."""
    retrieval_only = env.dir("index-me")
    monkeypatch.setenv("LEMONCROW_RETRIEVAL_ADDITIONAL_DIRS", str(retrieval_only))

    assert env.resolve() == []


def test_malformed_settings_logs_warning_naming_the_file(env, caplog):
    path = env.settings("workspace", "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"permissions": {"additionalDirectories": ["/tmp/x",]}}', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        assert env.resolve() == []

    assert str(path) in caplog.text


def test_malformed_settings_warns_once_per_revision(env, caplog):
    path = env.settings("home", "settings.json")
    path.write_text("not json at all", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        env.resolve()
        # Cache-busting repeats must not re-log for the same file revision.
        fs_access._CLAUDE_ADDITIONAL_DIRS_CACHE.clear()
        _claude_additional_dirs(env.workspace)
        fs_access._CLAUDE_ADDITIONAL_DIRS_CACHE.clear()
        _claude_additional_dirs(env.workspace)

    assert len([r for r in caplog.records if str(path) in r.getMessage()]) == 1


def test_malformed_home_settings_does_not_block_workspace_settings(env, caplog):
    extra = env.dir("still-allowed")
    env.settings("home", "settings.json").write_text("{oops", encoding="utf-8")
    env.write("workspace", {"permissions": {"additionalDirectories": [str(extra)]}})

    with caplog.at_level(logging.WARNING, logger=fs_access.__name__):
        dirs = env.resolve()

    assert extra.resolve() in dirs
