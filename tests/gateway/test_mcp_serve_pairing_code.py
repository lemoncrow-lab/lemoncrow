"""``lc mcp serve`` pairing code is stable across restarts.

A fresh random code per start meant re-typing it into the client every time the
server was restarted, even though the tokens the client already held were still
valid. The code is now persisted next to the OAuth store (same scope key), with
``--new-pairing-code`` as the deliberate rotation path and ``--pairing-code`` as
a one-off override that must NOT overwrite the stored value.

No network, no cloudflared: ``--no-tunnel`` plus a monkeypatched
``uvicorn.Server.run`` means ``serve`` builds the app, prints the banner and
returns.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
import uvicorn
from click.testing import CliRunner

from lemoncrow.gateway.adapters.mcp_oauth import (
    default_pairing_path,
    load_or_create_pairing_code,
    reset_pairing_code,
)
from lemoncrow.gateway.cli.commands.mcp_serve import mcp_serve_cmd

_CODE_RE = re.compile(r"Pairing code:\s+(\S+)")


def _serve(*args: str) -> str:
    """Run ``serve --no-tunnel`` and return the pairing code it printed."""
    result = CliRunner().invoke(mcp_serve_cmd, ["--no-tunnel", *args])
    assert result.exit_code == 0, result.output
    match = _CODE_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)


# ── store helpers ─────────────────────────────────────────────────────────
def test_pairing_file_is_scoped_like_the_oauth_store(tmp_path: Path) -> None:
    base = tmp_path / ".lemoncrow" / "chatgpt"
    assert default_pairing_path() == base / "pairing.json"
    assert default_pairing_path("mcp-example-com") == base / "pairing-mcp-example-com.json"


def test_load_or_create_is_idempotent_and_0600(tmp_path: Path) -> None:
    path = default_pairing_path()
    first = load_or_create_pairing_code(path)
    assert first
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_or_create_pairing_code(path) == first


def test_rotate_replaces_the_stored_code(tmp_path: Path) -> None:
    path = default_pairing_path()
    first = load_or_create_pairing_code(path)
    rotated = load_or_create_pairing_code(path, rotate=True)
    assert rotated != first
    assert load_or_create_pairing_code(path) == rotated


def test_corrupt_pairing_file_mints_a_new_code_instead_of_wedging(tmp_path: Path) -> None:
    """An unreadable store must not lock the operator out of their own server."""
    path = default_pairing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    code = load_or_create_pairing_code(path)
    assert code
    assert load_or_create_pairing_code(path) == code


def test_reset_pairing_code_reports_whether_it_removed_anything(tmp_path: Path) -> None:
    path = default_pairing_path()
    assert reset_pairing_code(path) is False
    load_or_create_pairing_code(path)
    assert reset_pairing_code(path) is True


# ── CLI behaviour ────────────────────────────────────────────────────
def test_restarting_serve_keeps_the_same_pairing_code() -> None:
    first = _serve()
    assert first == _serve()


def test_banner_says_the_code_is_stable() -> None:
    result = CliRunner().invoke(mcp_serve_cmd, ["--no-tunnel"])
    assert "stays the same across restarts" in result.output


def test_explicit_pairing_code_is_a_one_off_and_does_not_overwrite_the_store() -> None:
    stored = _serve()
    assert _serve("--pairing-code", "one-off") == "one-off"
    assert _serve() == stored  # store untouched by the override


def test_explicit_pairing_code_prints_no_stability_promise() -> None:
    result = CliRunner().invoke(mcp_serve_cmd, ["--no-tunnel", "--pairing-code", "x"])
    assert "stays the same across restarts" not in result.output


def test_new_pairing_code_rotates_and_says_so() -> None:
    first = _serve()
    result = CliRunner().invoke(mcp_serve_cmd, ["--no-tunnel", "--new-pairing-code"])
    assert result.exit_code == 0, result.output
    rotated = _CODE_RE.search(result.output)
    assert rotated is not None
    assert rotated.group(1) != first
    assert "Rotated the stored pairing code." in result.output
    assert _serve() == rotated.group(1)  # the rotation persisted


def test_reset_clears_the_stored_pairing_code_too() -> None:
    """``--reset`` revokes every token; leaving the code behind would imply the
    old pairing still means something."""
    first = _serve()
    assert _serve("--reset") != first


@pytest.mark.parametrize(
    "args",
    [
        ["--no-auth", "--new-pairing-code"],
        ["--new-pairing-code", "--pairing-code", "x"],
    ],
)
def test_conflicting_flags_are_usage_errors(args: list[str]) -> None:
    result = CliRunner().invoke(mcp_serve_cmd, ["--no-tunnel", *args])
    assert result.exit_code != 0
    assert "cannot be combined" in result.output
