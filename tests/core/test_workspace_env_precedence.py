"""Workspace identity: a pin beats every inherited variable, by rule not by name.

The same defect shipped three times under three names -- CLAUDE_WORKSPACE_ROOT
(setdefault), WORKSPACE_FOLDER_PATHS (never read), CURSOR_WORKSPACE_ROOT (ranked
below two inherited generics). Each fix addressed one name. These tests pin the
rule instead, parametrised over the variable tuple, so a newly added variable
cannot reintroduce it.

Observed consequences: a window on /tmp/ide-bench/L1 served reads from
/tmp/ide-bench/t3; 20 benchmark workspaces collapsed onto one scratch repo,
serialised on its index-write lock (41 min/prompt vs a 3 min baseline) and grew
it to 718 GB until the disk filled.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lemoncrow.core.foundation import paths as paths_mod
from lemoncrow.core.foundation.paths import host_workspace_root, pin_workspace_env

ALL_VARS = (paths_mod._HOST_WORKSPACE_FOLDERS_VAR, *paths_mod._HOST_WORKSPACE_ENV_VARS)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def _mk(tmp_path: Path, *names: str) -> list[Path]:
    out = []
    for n in names:
        d = tmp_path / n
        d.mkdir(parents=True, exist_ok=True)
        out.append(d)
    return out


@pytest.mark.parametrize("stale", paths_mod._HOST_WORKSPACE_ENV_VARS)
def test_pin_beats_each_inherited_var(stale: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Parametrised over EVERY var, so none can be the next one that wins."""
    _clear(monkeypatch)
    mine, theirs = _mk(tmp_path, "mine", "theirs")
    monkeypatch.setenv(stale, str(theirs))
    monkeypatch.setenv(paths_mod._HOST_WORKSPACE_FOLDERS_VAR, str(theirs))

    pin_workspace_env(mine)

    assert host_workspace_root() == mine


def test_pin_beats_a_fully_populated_stale_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    mine, theirs = _mk(tmp_path, "mine", "theirs")
    for var in ALL_VARS:
        monkeypatch.setenv(var, str(theirs))

    pin_workspace_env(mine)

    assert host_workspace_root() == mine
    # A single-workspace process has no second folder.
    assert paths_mod._HOST_WORKSPACE_FOLDERS_VAR not in os.environ


def test_pin_covers_every_var_the_lookup_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a var to the lookup without pinning it fails loudly here."""
    _clear(monkeypatch)
    (mine,) = _mk(tmp_path, "mine")

    pin_workspace_env(mine)

    missed = [v for v in paths_mod._HOST_WORKSPACE_ENV_VARS if os.environ.get(v) != str(mine)]
    assert not missed, f"pin_workspace_env does not cover: {missed}"


def test_host_specific_outranks_inherited_generic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpinned: the per-invocation host signal wins over a shell leftover."""
    _clear(monkeypatch)
    window, shell = _mk(tmp_path, "window", "shell")
    monkeypatch.setenv("VSCODE_CWD", str(shell))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(shell))
    monkeypatch.setenv("CURSOR_WORKSPACE_ROOT", str(window))

    assert host_workspace_root() == window


def test_copied_workspaces_never_collapse_onto_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The benchmark failure: N copies must stay N distinct roots."""
    _clear(monkeypatch)
    source, *copies = _mk(tmp_path, "scratch", "rep0", "rep1", "rep2")
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(source))  # inherited from the launcher

    resolved = []
    for copy in copies:
        pin_workspace_env(copy)
        resolved.append(host_workspace_root())

    assert resolved == copies
    assert len(set(resolved)) == len(copies), "workspaces collapsed onto one root"
