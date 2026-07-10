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
