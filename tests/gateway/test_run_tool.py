"""Tests for the run (bash exec) MCP tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.tool_supervision.bash_exec import classify_command, run_command


def test_run_simple_command(tmp_path: Path) -> None:
    result = run_command("echo hello", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.truncated is False


def test_run_exit_code(tmp_path: Path) -> None:
    result = run_command("exit 42", cwd=str(tmp_path))
    assert result.exit_code == 42


def test_run_stderr_captured(tmp_path: Path) -> None:
    result = run_command("echo err >&2", cwd=str(tmp_path))
    assert "err" in result.stderr


def test_run_ansi_stripped(tmp_path: Path) -> None:
    result = run_command("printf '\\033[31mred\\033[0m'", cwd=str(tmp_path))
    assert "\x1b" not in result.stdout
    assert "red" in result.stdout


def test_run_truncation(tmp_path: Path) -> None:
    result = run_command("seq 1 500", cwd=str(tmp_path), max_lines=50)
    assert result.truncated is True
    assert result.lines_omitted > 0
    assert "lines omitted" in result.stdout
    # head and tail present
    assert "1\n" in result.stdout
    assert "500" in result.stdout


def test_run_timeout(tmp_path: Path) -> None:
    result = run_command("sleep 10", cwd=str(tmp_path), timeout=1)
    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()


def test_run_duration_recorded(tmp_path: Path) -> None:
    result = run_command("true", cwd=str(tmp_path))
    assert result.duration_ms >= 0


def test_run_non_utf8_output_survives_with_replacement_chars(tmp_path: Path) -> None:
    # Regression: text=True with default errors='strict' raised inside the
    # reader thread's suppress() on the first invalid byte, silently returning
    # EMPTY stdout for exit 0 (bench evidence: doom's boot log -> "" -> agents
    # burned 7 turns on cat/head/wc/strings archaeology).
    result = run_command("printf 'ok\\xff\\xfe end'", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert "ok" in result.stdout
    assert "end" in result.stdout


def test_classify_rewrite_cat() -> None:
    decision = classify_command("cat README.md")
    assert decision.action == "rewrite"
    assert decision.rewrite_target == "read"


def test_classify_rewrite_rg() -> None:
    decision = classify_command("rg -i hello src")
    assert decision.action == "rewrite"
    assert decision.rewrite_target == "grep"
    payload = decision.rewrite_payload
    assert payload["file_path"] == "src"
    assert payload["content_regex"] == "hello"
    assert payload["ignore_case"] is True
    assert payload["output_mode"] in ("content", "file_paths_with_content")


def test_run_allows_destructive_rm(tmp_path: Path) -> None:
    # The hard rm -rf block was removed (benchmark transcripts showed it
    # costing recovery turns while protecting a disposable tree); rm now runs.
    target = tmp_path / "scratch-dir"
    target.mkdir()
    result = run_command(f"rm -rf {target}", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert result.policy_action != "block"
    assert not target.exists()


def test_run_allows_inline_shell(tmp_path: Path) -> None:
    # The inline bash -c / sh -c block was removed; the payload still gets
    # block-checked (see test_classify_inline_shell_payload_still_scanned).
    result = run_command("bash -c 'echo no'", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert result.policy_action != "block"
    assert "no" in result.stdout


def test_classify_allows_shell_noexec_syntax_check() -> None:
    for cmd in ("bash -n script.sh", "sh -n script.sh", "bash -o noexec script.sh", "bash -nx script.sh"):
        decision = classify_command(cmd)
        assert decision.action != "block", cmd
        assert decision.category != "shell-interpreter", cmd


def test_classify_allows_inline_and_stdin_shell(tmp_path: Path) -> None:
    script = tmp_path / "real.sh"
    script.write_text("echo ok\n")
    for cmd in (
        "bash -c 'echo hi'",
        "bash -lc 'echo hi'",
        "sh -s",
        "sh nonexistent-script.sh",  # missing file: bash reports it at runtime
        f"bash -c 'echo hi' {script}",
    ):
        decision = classify_command(cmd)
        assert decision.action != "block", cmd


def test_classify_inline_shell_payload_still_scanned() -> None:
    # Allowing bash -c must not launder the remaining destructive-git guards.
    for cmd in (
        "bash -c 'git reset --hard'",
        "sh -c 'echo hi && git clean -fd'",
        'bash -lc "git reset --hard HEAD~1"',
    ):
        decision = classify_command(cmd)
        assert decision.action == "block", cmd
        assert decision.category == "destructive", cmd


def test_classify_allows_existing_script_file(tmp_path: Path) -> None:
    script = tmp_path / "install.sh"
    script.write_text("echo ok\n")
    for cmd in (f"bash {script}", f"sh {script} --flag arg", f"bash -x {script}", f"bash -- {script}"):
        decision = classify_command(cmd)
        assert decision.action != "block", cmd


def test_classify_blocks_script_with_destructive_content(tmp_path: Path) -> None:
    script = tmp_path / "cleanup.sh"
    script.write_text("#!/bin/bash\necho starting\ngit reset --hard\n")
    decision = classify_command(f"bash {script}")
    assert decision.action == "block"
    assert str(script) in decision.reason
    assert "git reset --hard" in decision.reason


def test_classify_allows_script_with_comment_only_mentions(tmp_path: Path) -> None:
    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/bash\n# this used to git reset --hard the build dir\necho ok\n")
    decision = classify_command(f"bash {script}")
    assert decision.action != "block"


def test_classify_allows_script_with_rm_rf_content(tmp_path: Path) -> None:
    # rm -rf is no longer on the blocklist, in scripts either.
    script = tmp_path / "cleanup.sh"
    script.write_text("#!/bin/bash\nrm -rf /important-data\n")
    decision = classify_command(f"bash {script}")
    assert decision.action != "block"


def test_classify_script_scan_survives_mutual_recursion(tmp_path: Path) -> None:
    a = tmp_path / "a.sh"
    b = tmp_path / "b.sh"
    a.write_text(f"bash {b}\necho a\n")
    b.write_text(f"bash {a}\necho b\n")
    decision = classify_command(f"bash {a}")
    assert decision.action != "block"


def test_classify_scans_script_run_through_chaining(tmp_path: Path) -> None:
    script = tmp_path / "evil.sh"
    script.write_text("git reset --hard\n")
    decision = classify_command(f"echo hi && bash {script}")
    assert decision.action == "block"


def test_classify_blocks_cat_write_outside_roots(tmp_path: Path) -> None:
    decision = classify_command("cat > /etc/evil.conf", allowed_write_roots=[tmp_path])
    assert decision.action == "block"
    assert decision.category == "file-write"
    assert "edit tool" in decision.reason


def test_classify_allows_cat_write_inside_roots(tmp_path: Path) -> None:
    decision = classify_command(f"cat > {tmp_path}/notes.txt", allowed_write_roots=[tmp_path])
    assert decision.action != "block"


def test_classify_blocks_inline_python_write_outside_roots(tmp_path: Path) -> None:
    decision = classify_command("python3 -c \"open('/etc/evil.conf','w').write('x')\"", allowed_write_roots=[tmp_path])
    assert decision.action == "block"
    assert decision.category == "file-write"


def test_classify_write_guard_fails_open_on_opaque_targets(tmp_path: Path) -> None:
    # A .write_text receiver / variable path cannot be resolved statically:
    # the guard must fail open (run) rather than block what it cannot parse.
    for cmd in (
        "python3 -c \"from pathlib import Path; Path(p).write_text('x')\"",
        'cat > "$OUT_FILE"',
    ):
        decision = classify_command(cmd, allowed_write_roots=[tmp_path])
        assert decision.action != "block", cmd


def test_classify_write_guard_inert_without_roots() -> None:
    assert classify_command("cat > /etc/evil.conf").action != "block"


def test_classify_rewrites_plain_url_fetch() -> None:
    decision = classify_command("curl -sL https://example.com/page")
    assert decision.action == "rewrite"
    assert decision.rewrite_target == "web_fetch"


def test_classify_keeps_fetch_with_request_flags_as_is() -> None:
    # Headers/method/auth/body flags would be silently dropped by the
    # web_fetch rewrite -- these commands must run unmodified.
    for cmd in (
        "curl -H 'Authorization: Bearer x' https://api.example.com/v1",
        "curl -X POST https://api.example.com/v1",
        "curl -d 'a=b' https://api.example.com/v1",
        "curl --header 'Accept: application/json' https://api.example.com",
        "curl -u user:pass https://api.example.com",
        "curl -F 'file=@notes.txt' https://api.example.com/upload",
        "wget --header='X-Api-Key: k' https://example.com/data",
    ):
        decision = classify_command(cmd)
        assert decision.action == "allow", cmd
        assert decision.rewrite_target is None, cmd


def test_run_via_mcp_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    from lemoncrow.gateway.adapters.mcp_server import _handle

    resp = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run", "arguments": {"command": "echo mcp_ok"}},
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    assert "mcp_ok" in text
    assert "exit_code=" not in text


def test_run_via_mcp_rewrites_cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    from lemoncrow.gateway.adapters.mcp_server import _handle

    f = tmp_path / "sample.txt"
    f.write_text("rewritten\n", encoding="utf-8")
    resp = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "cat sample.txt"}},
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    assert "rewritten" in text
    assert "exit_code=" not in text


def test_run_via_mcp_rewrites_rg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    from lemoncrow.gateway.adapters.mcp_server import _handle

    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.py").write_text("def needle():\n    return 1\n", encoding="utf-8")
    resp = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "rg needle src"}},
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    assert "needle" in text
    assert "exit_code=" not in text
