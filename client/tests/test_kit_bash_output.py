"""The shared bash output pipeline's pieces the thin client relies on."""

from __future__ import annotations

from lemoncrow_client.kit.bash_output import cap_output_bytes, unchanged_marker
from lemoncrow_client.kit.output_delta import RunHistory

_TOKEN = "ghp_abcdefghijklmnopqrstuvwx"


def test_cap_output_bytes_cuts_on_a_character_boundary() -> None:
    text = "a" * 9 + "é" * 4  # 9 + 8 bytes
    kept, truncated = cap_output_bytes(text, cap=10)
    assert truncated
    assert kept == "a" * 9  # the straddling two-byte character is dropped whole
    assert "�" not in kept


def test_cap_output_bytes_leaves_text_under_the_cap_alone() -> None:
    assert cap_output_bytes("short", cap=64) == ("short", False)


def test_unchanged_marker_redacts_the_first_line() -> None:
    marker = unchanged_marker(f"{_TOKEN}\n" + "x\n" * 600, "")
    assert _TOKEN not in marker
    assert "<redacted-github-token>" in marker


def test_run_histories_do_not_share_what_they_saw() -> None:
    output = "line\n" * 200
    first, second = RunHistory(), RunHistory()
    assert not first.observe("seq", cwd="/repo", stdout=output, stderr="", exit_code=0)
    assert first.observe("seq", cwd="/repo", stdout=output, stderr="", exit_code=0)
    assert not second.observe("seq", cwd="/repo", stdout=output, stderr="", exit_code=0)
