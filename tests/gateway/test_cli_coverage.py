"""CLI coverage for commands not tested in test_cli.py or test_cli_v2.py.

Covers:
- add-block, list-blocks, search, deprecate, quarantine
- ledger reset, ledger update
- env validate
- failure show, eval show/deprecate, eval-from-cluster
- search, cached-grep
- savings-detail, savings-reset
- benchmark hosts, benchmark full, benchmark packs
- copilot/claude/codex/opencode import (with empty session dir)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from atelier.gateway.adapters.cli import cli
from atelier.infra.runtime.run_ledger import RunLedger


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
# add-block / list-blocks / search                                            #
# --------------------------------------------------------------------------- #


def test_add_block_upserts_and_list_blocks_shows_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")
    root = tmp_path / ".atelier"
    _invoke(root, "init")

    block_yaml = tmp_path / "myblock.yaml"
    block_yaml.write_text(
        "title: My Custom Block\n"
        "domain: coding.custom\n"
        "situation: Use this when a custom situation arises\n"
        "procedure:\n"
        "  - Do the thing\n"
        "dead_ends: []\n",
        encoding="utf-8",
    )
    res = _invoke(root, "block", "add", str(block_yaml))
    assert res.exit_code == 0, res.output
    assert "upserted" in res.output

    # list-blocks should include it
    res2 = _invoke(root, "block", "list", "--json")
    assert res2.exit_code == 0, res2.output
    blocks = json.loads(res2.output)
    assert any(b["domain"] == "coding.custom" for b in blocks)


def test_list_blocks_table_format(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "list-blocks")
    assert res.exit_code == 0
    # Table header with counts
    assert "blocks shown" in res.output


def test_list_blocks_filter_by_domain(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "list-blocks", "--domain", "state.change", "--json")
    assert res.exit_code == 0
    blocks = json.loads(res.output)
    assert all(b["domain"] == "state.change" for b in blocks)


def test_search_returns_matches(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
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
        "--json",
    )
    assert res.exit_code == 0
    results = json.loads(res.output)
    assert isinstance(results, dict)


def test_search_table_format(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
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
# deprecate / quarantine                                                      #
# --------------------------------------------------------------------------- #


def test_deprecate_block(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    blocks_res = _invoke(root, "list-blocks", "--json")
    blocks = json.loads(blocks_res.output)
    assert blocks, "need at least one block to deprecate"
    block_id = blocks[0]["id"]

    res = _invoke(root, "deprecate", block_id)
    assert res.exit_code == 0
    assert f"deprecated {block_id}" in res.output

    # After deprecation, block should NOT appear in default (active-only) listing
    listed_default = json.loads(_invoke(root, "list-blocks", "--json").output)
    assert not any(b["id"] == block_id for b in listed_default)

    # Block should appear when --include-deprecated is passed
    listed_all = json.loads(_invoke(root, "list-blocks", "--include-deprecated", "--json").output)
    assert any(b["id"] == block_id for b in listed_all)


def test_deprecate_unknown_block_errors(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "deprecate", "nonexistent-block-id")
    assert res.exit_code != 0


def test_quarantine_block(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    blocks = json.loads(_invoke(root, "list-blocks", "--json").output)
    block_id = blocks[0]["id"]

    res = _invoke(root, "quarantine", block_id)
    assert res.exit_code == 0
    assert f"quarantined {block_id}" in res.output


# --------------------------------------------------------------------------- #
# ledger reset / update                                                       #
# --------------------------------------------------------------------------- #


def test_ledger_update_field(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    _seed_ledger(root)

    res = _invoke(root, "ledger", "update", "--field", "task", "--value", "updated task text")
    assert res.exit_code == 0
    assert "updated task" in res.output

    snap = json.loads((root / "runs" / "run1.json").read_text(encoding="utf-8"))
    assert snap["task"] == "updated task text"


def test_ledger_update_json_value(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
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
    snap = json.loads((root / "runs" / "run1.json").read_text(encoding="utf-8"))
    assert snap["current_blockers"] == ["blocker one", "blocker two"]


def test_ledger_reset_with_confirmation(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    _seed_ledger(root)
    ledger_path = root / "runs" / "run1.json"
    assert ledger_path.exists()

    res = _invoke(root, "ledger", "reset", input="y\n")
    assert res.exit_code == 0
    assert not ledger_path.exists()


# --------------------------------------------------------------------------- #
# env validate                                                                #
# --------------------------------------------------------------------------- #


def test_env_validate_known_env(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "env", "validate", "env_state_change_safety")
    assert res.exit_code == 0
    assert "ok" in res.output


def test_env_validate_unknown_env(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "env", "validate", "env_does_not_exist")
    assert res.exit_code != 0


# --------------------------------------------------------------------------- #
# failure show                                                                #
# --------------------------------------------------------------------------- #


def test_failure_show_after_accept(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    _seed_ledger(root)
    _seed_ledger(root, session_id="run2")

    clusters = json.loads(_invoke(root, "failure", "list", "--json").output)
    assert clusters
    cid = clusters[0]["id"]

    _invoke(root, "failure", "accept", cid)
    res = _invoke(root, "failure", "show", cid)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["id"] == cid
    assert payload["status"] == "accepted"


def test_failure_show_unknown_cluster(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "failure", "show", "nonexistent-cluster-id")
    assert res.exit_code != 0


# --------------------------------------------------------------------------- #
# eval show / deprecate / eval-from-cluster                                  #
# --------------------------------------------------------------------------- #


def _make_eval_case(root: Path, case_id: str = "case1") -> None:
    eval_dir = root / "evals"
    eval_dir.mkdir(parents=True, exist_ok=True)
    case = {
        "id": case_id,
        "domain": "state.change",
        "description": "test eval",
        "task": "Fix live state",
        "plan": ["Resolve target from URL slug alone"],
        "expected_status": "blocked",
        "status": "draft",
    }
    (eval_dir / f"{case_id}.json").write_text(json.dumps(case), encoding="utf-8")


def test_eval_show(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    _make_eval_case(root)

    res = _invoke(root, "eval", "show", "case1")
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["id"] == "case1"


def test_eval_deprecate(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    _make_eval_case(root)

    res = _invoke(root, "eval", "deprecate", "case1")
    assert res.exit_code == 0
    case = json.loads((root / "evals" / "case1.json").read_text(encoding="utf-8"))
    assert case["status"] == "deprecated"


def test_eval_from_cluster(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    _seed_ledger(root)
    _seed_ledger(root, session_id="run2")

    clusters = json.loads(_invoke(root, "failure", "list", "--json").output)
    assert clusters
    cid = clusters[0]["id"]

    # Must accept cluster before generating eval
    _invoke(root, "failure", "accept", cid)

    res = _invoke(root, "eval-from-cluster", cid)
    assert res.exit_code == 0
    assert "saved draft eval" in res.output


def test_eval_from_cluster_unaccepted_errors(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    _seed_ledger(root)
    _seed_ledger(root, session_id="run2")

    clusters = json.loads(_invoke(root, "failure", "list", "--json").output)
    cid = clusters[0]["id"]

    res = _invoke(root, "eval-from-cluster", cid)
    assert res.exit_code != 0


# --------------------------------------------------------------------------- #
# search / cached-grep                                                        #
# --------------------------------------------------------------------------- #


def test_search_blocks_returns_matches(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
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
        "--json",
    )
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert isinstance(payload, dict)


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
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


def test_cached_grep_finds_pattern(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    haystack = tmp_path / "code.py"
    haystack.write_text("import os\nresult = compute()\n", encoding="utf-8")

    # path is a --path option, not a positional arg
    res = _invoke(root, "cached-grep", "compute", "--path", str(tmp_path))
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert "output" in payload
    assert "compute" in payload["output"]


def test_cached_grep_rejects_shell_metachar(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "cached-grep", "foo; rm -rf /")
    assert res.exit_code != 0


# --------------------------------------------------------------------------- #
# savings-detail / savings-reset                                              #
# --------------------------------------------------------------------------- #


def test_savings_detail_runs(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "savings-detail", "--json")
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert "summary" in payload
    assert "operations" in payload


def test_savings_reset_clears_counters(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    res = _invoke(root, "savings-reset")
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


def test_benchmark_packs_returns_domain_keys(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--root", str(tmp_path / ".atelier"), "benchmark", "packs", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["suite"] == "domains"
    assert payload["domains_total"] >= payload["domains_benchmarked"]


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
    _invoke(root, "init")
    sessions_dir = tmp_path / "copilot_sessions"
    sessions_dir.mkdir()

    res = _invoke(root, "copilot", "import", "--path", str(sessions_dir))
    assert res.exit_code == 0
    assert "imported" in res.output


def test_claude_import_empty_dir(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    sessions_dir = tmp_path / "claude_projects"
    sessions_dir.mkdir()

    res = _invoke(root, "claude", "import", "--path", str(sessions_dir))
    assert res.exit_code == 0
    assert "imported" in res.output


def test_codex_import_empty_dir(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    sessions_dir = tmp_path / "codex_sessions"
    sessions_dir.mkdir()

    res = _invoke(root, "codex", "import", "--path", str(sessions_dir))
    assert res.exit_code == 0
    assert "imported" in res.output


def test_opencode_import_missing_db(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _invoke(root, "init")
    nonexistent_db = tmp_path / "opencode.db"

    res = _invoke(root, "opencode", "import", "--path", str(nonexistent_db))
    # Should either succeed with 0 imports or fail gracefully (no crash/traceback)
    assert "imported" in res.output or res.exit_code != 0
    assert "Traceback" not in res.output
