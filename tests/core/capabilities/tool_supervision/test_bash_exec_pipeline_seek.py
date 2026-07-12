"""Pipeline-aware rewrite: `od <bigfile> | tail` -> an in-place `od -j <offset>`.

The single-command `tokens[0]` dispatch in classify_command can't see into a
`producer | consumer` pipeline, so `od gpt2.ckpt | tail -60` used to hex-format
the whole 497MB file (tail can't SIGPIPE-abort od early the way head does) just
to show the end. _rewrite_pipeline detects the narrow, safely-rewritable shape
and seeks instead -- preserving od's absolute byte addresses, unlike the naive
`tail -c N file | od` whose addresses restart at 0. Everything outside that
narrow shape must run untouched (no silent rewrite we can't prove equivalent).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

import lemoncrow.core.capabilities.tool_supervision.bash_exec as bx

_BIG_BYTES = 9 * 1024 * 1024  # just over _PIPELINE_SEEK_MIN_BYTES (8 MiB)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    big = tmp_path / "gpt2.ckpt"
    big.write_bytes(os.urandom(_BIG_BYTES))
    small = tmp_path / "small.bin"
    small.write_bytes(os.urandom(1024))
    return {"dir": tmp_path, "big": big, "small": small}


def _rewrote(cmd: str, cwd: Path) -> bx.CommandPolicyDecision:
    return bx.classify_command(cmd, cwd=cwd)


def _is_seek(policy: bx.CommandPolicyDecision) -> bool:
    return policy.action == "rewrite" and policy.rewrite_target == "pipeline_seek"


def test_target_shape_rewrites_to_od_seek(files: dict[str, Path]) -> None:
    p = _rewrote(f"od -A d -t x1 {files['big']} | tail -60", files["dir"])
    assert _is_seek(p)
    rw = str((p.rewrite_payload or {}).get("command"))
    # last 60 lines -> seek (60+1)*16 bytes back from EOF; original od flags kept.
    expected_off = _BIG_BYTES - 61 * 16
    assert rw == f"od -A d -t x1 -j {expected_off} -- {files['big']}"
    assert "od -j" in str(p.reason) or "od -j" in str((p.rewrite_payload or {}).get("note"))


def test_bytes_mode_offset_is_byte_exact(files: dict[str, Path]) -> None:
    p = _rewrote(f"od -A d -t x1 {files['big']} | tail -c 320", files["dir"])
    assert _is_seek(p)
    rw = str((p.rewrite_payload or {}).get("command"))
    assert f"-j {_BIG_BYTES - 320} " in rw


def test_bare_tail_rewrites_with_default_ten_lines(files: dict[str, Path]) -> None:
    p = _rewrote(f"od -A d -t x1 {files['big']} | tail", files["dir"])
    assert _is_seek(p)
    assert f"-j {_BIG_BYTES - 11 * 16} " in str((p.rewrite_payload or {}).get("command"))


def test_short_dash_count_form(files: dict[str, Path]) -> None:
    # `tail -60` (== `tail -n 60`) must parse the same as the explicit form.
    a = _rewrote(f"od -A d -t x1 {files['big']} | tail -60", files["dir"])
    b = _rewrote(f"od -A d -t x1 {files['big']} | tail -n 60", files["dir"])
    assert (a.rewrite_payload or {}).get("command") == (b.rewrite_payload or {}).get("command")


@pytest.mark.parametrize(
    "shape",
    [
        "od -A d -t x1 {big} {small} | tail -60",  # multi-file: od concatenates, seek can't
        "od -A d -t x1 {small} | tail -60",  # below size threshold
        "od -A d -t x1 {big} | wc -l",  # consumer needs the whole stream
        "od -A d -t x1 {big} | head -60",  # head already SIGPIPE-aborts od
        "od -A d -w32 -t x1 {big} | tail -60",  # row geometry override
        "od -A d -t x1 -j 100 {big} | tail -60",  # already seeking
        "od -A d -t x1 -N 500 {big} | tail -60",  # already length-limited
        "od -A d -t x1 {big} | grep 00 | tail -60",  # 3-stage pipeline
        "od -A d -t x1 {big} | tail -f",  # follow -- not a bounded tail
        "od -A d -t x1 {big} | tail -c 1k",  # suffixed count -- geometry unclear
        "od -A d -t x1 {big} > /tmp/pv_out",  # redirect, not a pipe
        "od -A d -t x1 {big} | tail -60 ; echo hi",  # chained
        "od -A d -t x1 {big} && tail -60 {big}",  # && not a pipe
        "od -A d -t x1 {big}",  # not a pipeline at all
        "cat {big} | tail -60",  # producer isn't od
        "hexdump -C {big} | tail -60",  # hexdump not handled (only od)
    ],
)
def test_bail_cases_run_untouched(files: dict[str, Path], shape: str) -> None:
    cmd = shape.format(big=files["big"], small=files["small"])
    assert not _is_seek(_rewrote(cmd, files["dir"]))


def test_glob_operand_not_rewritten(files: dict[str, Path]) -> None:
    # A glob can expand to multiple files at runtime; classify sees the literal
    # `*.ckpt` which isn't a single stat-able file -> bail.
    p = _rewrote(f"od -A d -t x1 {files['dir']}/*.ckpt | tail -60", files["dir"])
    assert not _is_seek(p)


def test_quoted_pipe_in_arg_not_misparsed(files: dict[str, Path]) -> None:
    # The `|` inside quotes is a literal, not a pipeline operator; must not
    # rewrite (and must not crash).
    p = _rewrote(f"grep '|' {files['big']} | tail -60", files["dir"])
    assert not _is_seek(p)


def test_relative_path_resolved_against_cwd(files: dict[str, Path]) -> None:
    p = bx.classify_command("od -A d -t x1 gpt2.ckpt | tail -60", cwd=files["dir"])
    assert _is_seek(p)
    assert "gpt2.ckpt" in str((p.rewrite_payload or {}).get("command"))


@pytest.mark.slow
def test_rewrite_is_byte_faithful_to_the_original_tail(files: dict[str, Path]) -> None:
    """The rewritten seek must show the SAME final rows -- identical addresses and
    bytes -- as the original `od | tail`, only far faster."""
    original = f"od -A d -t x1 {files['big']} | tail -60"
    p = _rewrote(original, files["dir"])
    assert _is_seek(p)
    rewritten = str((p.rewrite_payload or {}).get("command"))

    orig_out = subprocess.run(original, shell=True, capture_output=True, text=True, timeout=60).stdout.splitlines()
    t0 = time.monotonic()
    rw_out = subprocess.run(rewritten, shell=True, capture_output=True, text=True, timeout=60).stdout.splitlines()
    rw_elapsed = time.monotonic() - t0

    # Rewrite emits a safe superset (>= the requested rows); its last 60 rows are
    # byte-identical to the original tail, addresses preserved.
    assert rw_out[-60:] == orig_out[-60:]
    assert len(rw_out) >= 60
    # Seek reads a few hundred bytes; formatting the whole file it is not.
    assert rw_elapsed < 1.0


@pytest.mark.slow
def test_rewritten_command_surfaces_the_transform_note(files: dict[str, Path]) -> None:
    """start_managed_command(note=...) prepends the note so the model sees that a
    rewrite happened (the plumbing _run_bash_tool uses for pipeline_seek)."""
    p = _rewrote(f"od -A d -t x1 {files['big']} | tail -60", files["dir"])
    payload = p.rewrite_payload or {}
    started = bx.start_managed_command(str(payload.get("command")), timeout=30, note=str(payload.get("note")))
    sid = str(started["session_id"])
    deadline = time.time() + 30
    result = None
    while time.time() < deadline:
        result = bx.poll_managed_command(sid)
        if result["status"] != "running":
            break
        time.sleep(0.02)
    assert result is not None and result["status"] != "running"
    assert "hex-format the whole file" in str(result["stdout"])
