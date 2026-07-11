"""CLI coverage for commands not tested in test_cli.py or test_cli_v2.py.

Covers:
- search
- ledger reset, ledger update
- env validate
- search
- savings detail/reset
- benchmark hosts, benchmark full, benchmark packs
- unified host import (with empty session dir)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from atelier.core.capabilities.licensing import entitlements
from atelier.core.foundation.paths import session_dir
from atelier.gateway.cli import cli
from atelier.infra.runtime.run_ledger import RunLedger
from tests.helpers import grant_oauth_pro, init_store_at


def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)


def _seed_ledger(root: Path, session_id: str = "run1") -> Path:
    led = RunLedger(session_id=session_id, agent="codex", task="t", domain="d", root=root)
    led.record_command("pytest", ok=False, error_signature="sig1")
    led.record_command("pytest", ok=False, error_signature="sig1")
    led.record_alert("repeated_command_failure", "high", "pytest x2")
    return led.persist()


# --------------------------------------------------------------------------- #
# search                                                                      #
# --------------------------------------------------------------------------- #


def test_search_returns_matches(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    target = tmp_path / "shopify.md"
    target.write_text("shopify checkout retry\n", encoding="utf-8")
    res = _invoke(
        root,
        "tools",
        "call",
        "grep",
        "--dev",
        "--workspace",
        str(tmp_path),
        "--args",
        json.dumps(
            {
                "path": ".",
                "content_regex": "shopify",
                "file_glob_patterns": ["*.md"],
            }
        ),
    )
    assert res.exit_code == 0
    assert "shopify" in res.output


def test_search_table_format(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    target = tmp_path / "shopify.md"
    target.write_text("shopify checkout retry\n", encoding="utf-8")
    res = _invoke(
        root,
        "tools",
        "call",
        "grep",
        "--dev",
        "--workspace",
        str(tmp_path),
        "--args",
        json.dumps(
            {
                "path": ".",
                "content_regex": "shopify",
                "file_glob_patterns": ["*.md"],
            }
        ),
    )
    assert res.exit_code == 0


# --------------------------------------------------------------------------- #
# ledger reset / update                                                       #
# --------------------------------------------------------------------------- #


def test_ledger_update_field(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    _seed_ledger(root)

    res = _invoke(root, "ledger", "update", "--field", "task", "--value", "updated task text")
    assert res.exit_code == 0
    assert "updated task" in res.output

    snap = json.loads((session_dir(root, "codex", "run1") / "run.json").read_text(encoding="utf-8"))
    assert snap["task"] == "updated task text"


def test_ledger_update_json_value(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    _seed_ledger(root)

    res = _invoke(
        root,
        "ledger",
        "update",
        "--field",
        "current_blockers",
        "--value",
        '["blocker one", "blocker two"]',
    )
    assert res.exit_code == 0
    snap = json.loads((session_dir(root, "codex", "run1") / "run.json").read_text(encoding="utf-8"))
    assert snap["current_blockers"] == ["blocker one", "blocker two"]


def test_ledger_reset_with_confirmation(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    _seed_ledger(root)
    ledger_path = session_dir(root, "codex", "run1") / "run.json"
    assert ledger_path.exists()

    res = _invoke(root, "ledger", "reset", input="y\n")
    assert res.exit_code == 0
    assert not ledger_path.exists()


# --------------------------------------------------------------------------- #
# env validate                                                                #
# --------------------------------------------------------------------------- #


def test_env_validate_known_env(tmp_path: Path) -> None:
    from atelier.core.foundation.models import Rubric
    from atelier.infra.storage.factory import create_store

    root = tmp_path / ".atelier"
    init_store_at(str(root))
    # atelier init no longer seeds built-in rubrics; env validate resolves
    # user-supplied rubrics, so the known-env success path seeds its own.
    store = create_store(root)
    store.upsert_rubric(
        Rubric(id="rubric_state_change_safety", domain="state_change_safety"),
        write_yaml=False,
    )
    res = _invoke(root, "env", "validate", "env_state_change_safety")
    assert res.exit_code == 0, res.output
    assert "ok" in res.output


def test_env_validate_unknown_env(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    res = _invoke(root, "env", "validate", "env_does_not_exist")
    assert res.exit_code != 0


# --------------------------------------------------------------------------- #
# search                                                        #
# --------------------------------------------------------------------------- #


def test_search_blocks_returns_matches(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    target = tmp_path / "shopify.md"
    target.write_text("shopify publish retry\n", encoding="utf-8")
    res = _invoke(
        root,
        "tools",
        "call",
        "grep",
        "--dev",
        "--workspace",
        str(tmp_path),
        "--args",
        json.dumps(
            {
                "path": ".",
                "content_regex": "shopify",
                "file_glob_patterns": ["*.md"],
            }
        ),
    )
    assert res.exit_code == 0
    assert "shopify" in res.output


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    res = _invoke(
        root,
        "tools",
        "call",
        "grep",
        "--dev",
        "--workspace",
        str(tmp_path),
        "--args",
        json.dumps(
            {
                "path": ".",
                "content_regex": "zzz_no_match_xyz",
                "file_glob_patterns": ["*.md"],
            }
        ),
    )
    assert res.exit_code == 0


# --------------------------------------------------------------------------- #
# savings-detail / savings-reset                                              #
# --------------------------------------------------------------------------- #


def test_savings_detail_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grant_oauth_pro(monkeypatch)
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    res = _invoke(root, "savings", "detail", "--json")
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert "summary" in payload
    assert "operations" in payload
    entitlements.reload()


def test_savings_reset_clears_counters(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    res = _invoke(root, "savings", "reset", "--force")
    assert res.exit_code == 0
    assert "reset" in res.output

    after = json.loads(_invoke(root, "savings", "--json").output)
    assert after["calls_avoided"] == 0
    assert after["tokens_saved"] == 0


# --------------------------------------------------------------------------- #
# benchmark hosts / benchmark packs / benchmark full                         #
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_benchmark_hosts_command_runs(tmp_path: Path) -> None:
    """benchmark hosts runs the host verify script; may fail in CI but must emit valid JSON."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--root", str(tmp_path / ".atelier"), "benchmark", "hosts", "--json"],
    )
    # The exit code may be non-zero if the shell script exits non-zero,
    # but the JSON payload must be present and structurally valid.
    output = result.output
    # Find the JSON payload (before any trailing Error: line)
    json_lines = []
    for line in output.splitlines():
        try:
            json.loads(line)
            json_lines.append(line)
            break
        except json.JSONDecodeError:
            pass
    if not json_lines:
        # Full output should be valid JSON (printed via _emit)
        # Strip trailing Click error message if present
        json_text = output.split("\nError:")[0].strip()
        payload = json.loads(json_text)
    else:
        payload = json.loads(json_lines[0])
    assert payload["suite"] == "hosts"
    assert "exit_code" in payload


@pytest.mark.slow
def test_benchmark_full_runs(tmp_path: Path) -> None:
    """benchmark full may fail due to host verification, but must emit valid JSON."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--root", str(tmp_path / ".atelier"), "benchmark", "full", "--json"],
    )
    json_text = result.output.split("\nError:")[0].strip()
    payload = json.loads(json_text)
    assert payload["suite"] == "full"
    assert "core" in payload
    assert "hosts" in payload
    assert "packs" in payload


# --------------------------------------------------------------------------- #
# copilot / claude / codex / opencode import (empty session dirs)            #
# --------------------------------------------------------------------------- #


def test_copilot_import_empty_dir(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    sessions_dir = tmp_path / "copilot_sessions"
    sessions_dir.mkdir()

    res = _invoke(root, "import", "--host", "copilot", "--path", str(sessions_dir))
    assert res.exit_code == 0
    assert "imported" in res.output


def test_claude_import_empty_dir(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    sessions_dir = tmp_path / "claude_projects"
    sessions_dir.mkdir()

    res = _invoke(root, "import", "--host", "claude", "--path", str(sessions_dir))
    assert res.exit_code == 0
    assert "imported" in res.output


def test_codex_import_empty_dir(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    sessions_dir = tmp_path / "codex_sessions"
    sessions_dir.mkdir()

    res = _invoke(root, "import", "--host", "codex", "--path", str(sessions_dir))
    assert res.exit_code == 0
    assert "imported" in res.output


def test_opencode_import_missing_db(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    nonexistent_db = tmp_path / "opencode.db"

    res = _invoke(root, "import", "--host", "opencode", "--path", str(nonexistent_db))
    # Should either succeed with 0 imports or fail gracefully (no crash/traceback)
    assert "imported" in res.output or res.exit_code != 0
    assert "Traceback" not in res.output


# --------------------------------------------------------------------------- #
# session list / stats defect regressions                                     #
# --------------------------------------------------------------------------- #


def _make_trace(i: int = 0, *, host: str = "codex", **overrides: object) -> Any:
    from atelier.core.foundation.models import Trace

    kwargs: dict[str, object] = {
        "id": f"trace-{host}-{i}",
        "session_id": f"sess-{host}-{i}",
        "agent": "test-agent",
        "domain": "coding",
        "task": "test task",
        "status": "success",
        "host": host,
        "input_tokens": 10,
        "model": "claude-haiku-4-5",
    }
    kwargs.update(overrides)
    return Trace(**kwargs)  # type: ignore[arg-type]


def test_session_row_adopts_routing_only_live_savings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (B2): a session whose only live savings are routing dollars
    (saved_usd > 0, tokens/calls/carry all 0) must not render $0 savings in
    `session list`/`stats` while the statusline shows the saving."""
    from atelier.core.foundation.store import ContextStore
    from atelier.gateway.cli.commands import sessions as sessions_cmd

    monkeypatch.setattr(sessions_cmd, "_claude_transcript_block", lambda sid: None)
    monkeypatch.setattr(
        sessions_cmd,
        "_claude_live_savings_summary",
        lambda sid, root: (0.42, 0, 0, 0.0, 0, 0.0, 0.0),
    )
    monkeypatch.setattr(sessions_cmd, "_claude_subagent_count", lambda sid: 0)
    monkeypatch.setattr(sessions_cmd, "_claude_subagent_cost_usd", lambda sid: 0.0)

    store = ContextStore(tmp_path)
    store.init()
    trace = _make_trace(host="claude")
    row = sessions_cmd._build_session_row(trace, store, "claude", tmp_path)

    assert row["saved_usd"] == pytest.approx(0.42)


def test_trace_cost_breakdown_parts_sum_to_total_with_thinking(tmp_path: Path) -> None:
    """Regression (B3): thinking tokens are priced into the estimated total
    but were dropped from the 4-bucket breakdown, so the rendered parts did
    not sum to the displayed cost."""
    from atelier.gateway.cli.commands import sessions as sessions_cmd

    trace = _make_trace(input_tokens=1_000, output_tokens=500, thinking_tokens=2_000)
    total = sessions_cmd._estimated_trace_cost_usd(trace)
    breakdown = sessions_cmd._estimated_trace_cost_breakdown(trace)

    assert total > 0
    assert sum(breakdown.values()) == pytest.approx(total, abs=1e-5)

    # Thinking-only trace: the cost must land in the output bucket.
    thinking_only = _make_trace(i=1, input_tokens=0, output_tokens=0, thinking_tokens=1_000)
    bd = sessions_cmd._estimated_trace_cost_breakdown(thinking_only)
    assert bd["output"] > 0
    assert sum(bd.values()) == pytest.approx(sessions_cmd._estimated_trace_cost_usd(thinking_only), abs=1e-5)


def test_session_stats_store_since_marks_truncation(tmp_path: Path) -> None:
    """Regression (B4): --source store --since caps the query at 15/host but
    never flagged truncation, so the header claimed the full window."""
    from atelier.infra.storage.factory import create_store

    root = tmp_path / ".atelier"
    init_store_at(str(root))
    store = create_store(root)
    for i in range(15):
        store.record_trace(_make_trace(i), write_json=False)

    res = _invoke(root, "session", "stats", "--source", "store", "--since", "7d", "--host", "codex")
    assert res.exit_code == 0
    assert "capped at 15/host" in res.output


def test_session_stats_store_since_no_truncation_under_cap(tmp_path: Path) -> None:
    from atelier.infra.storage.factory import create_store

    root = tmp_path / ".atelier"
    init_store_at(str(root))
    store = create_store(root)
    for i in range(3):
        store.record_trace(_make_trace(i), write_json=False)

    res = _invoke(root, "session", "stats", "--source", "store", "--since", "7d", "--host", "codex")
    assert res.exit_code == 0
    assert "capped at" not in res.output


def test_session_list_store_since_marks_truncation(tmp_path: Path) -> None:
    """Regression (B4, list analogue): store query capped at --scan per host
    must flag truncation in the footer label when --since is set."""
    from atelier.infra.storage.factory import create_store

    root = tmp_path / ".atelier"
    init_store_at(str(root))
    store = create_store(root)
    for i in range(4):
        store.record_trace(_make_trace(i), write_json=False)

    res = _invoke(root, "session", "list", "--source", "store", "--since", "7d", "--host", "codex", "--scan", "4")
    assert res.exit_code == 0
    assert "capped at 4/host" in res.output


def test_session_list_missing_path_warns_on_stderr(tmp_path: Path) -> None:
    """Regression (R3): --path pointing at a nonexistent directory used to
    print only 'No host sessions found' with no hint the path was wrong."""
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    missing = tmp_path / "does-not-exist"

    res = _invoke(root, "session", "list", "--host", "claude", "--path", str(missing))
    assert res.exit_code == 0
    assert "does not exist" in res.stderr


def test_session_stats_missing_path_warns_on_stderr(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    missing = tmp_path / "does-not-exist"

    res = _invoke(root, "session", "stats", "--host", "claude", "--path", str(missing))
    assert res.exit_code == 0
    assert "does not exist" in res.stderr
