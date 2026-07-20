"""Non-UTF8 output handling: exact lossy detection, integrity banner, and
raw-byte recovery (see _LossyStreamReader). Covers both the pre-fix failure
mode -- a strict decode raising in the reader thread and returning an *empty*
stream -- and the hazard errors="replace" traded it for: silent U+FFFD
substitution the model would otherwise read as byte-exact output."""

from __future__ import annotations

import re
import time
from pathlib import Path

import lemoncrow.pro.capabilities.tool_supervision.bash_exec as bx


def _poll_until_done(session_id: str, timeout_s: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = bx.poll_managed_command(session_id)
        if payload["status"] != "running":
            return payload
        time.sleep(0.05)
    raise AssertionError(f"command {session_id} still running after {timeout_s}s")


def test_non_utf8_stream_is_flagged_bannered_and_raw_recoverable() -> None:
    started = bx.start_managed_command("printf 'ok\\xff\\xfebad\\n'", timeout=30)
    payload = _poll_until_done(started["session_id"])
    stdout = str(payload["stdout"])
    assert payload["exit_code"] == 0
    assert payload["output_lossy"] is True
    assert "non-UTF8" in stdout  # banner present
    assert "�" in stdout  # replaced, not dropped: the stream must not come back empty
    assert "ok" in stdout and "bad" in stdout
    raw_path = payload["raw_log_file"]
    assert isinstance(raw_path, str) and raw_path
    assert Path(raw_path).read_bytes() == b"ok\xff\xfebad\n"  # byte-exact recovery


def test_clean_output_gets_no_banner_and_leaves_no_raw_spill() -> None:
    started = bx.start_managed_command("printf 'plain\\n'", timeout=30)
    session_id = str(started["session_id"])
    payload = _poll_until_done(session_id)
    stdout = str(payload["stdout"])
    assert "plain" in stdout
    assert "non-UTF8" not in stdout
    assert "output_lossy" not in payload
    assert "raw_log_file" not in payload
    # The clean stream's raw tee is unlinked at EOF, not left behind.
    assert not (bx._BASH_LOG_DIR / f"{session_id}.stdout.bin").exists()


def test_genuine_ufffd_in_valid_utf8_is_not_flagged() -> None:
    # U+FFFD legitimately present in *valid* utf-8 must not trip the detector
    # (a count-U+FFFD-in-the-decoded-text heuristic would).
    started = bx.start_managed_command("printf '\\xef\\xbf\\xbdreal\\n'", timeout=30)
    payload = _poll_until_done(started["session_id"])
    assert "output_lossy" not in payload
    assert "�" in str(payload["stdout"])
    assert "non-UTF8" not in str(payload["stdout"])


def test_sync_run_lane_banners_and_recovers_raw_bytes() -> None:
    result = bx.run_command("printf 'a\\xffb\\n'", timeout=30)
    assert result.exit_code == 0
    assert "non-UTF8" in result.stdout
    assert "�" in result.stdout
    match = re.search(r"exact bytes: (\S+)\]", result.stdout)
    assert match, result.stdout
    assert Path(match.group(1)).read_bytes() == b"a\xffb\n"


def test_sync_run_lane_clean_output_unchanged() -> None:
    result = bx.run_command("printf 'hello\\n'", timeout=30)
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert "non-UTF8" not in result.stdout
