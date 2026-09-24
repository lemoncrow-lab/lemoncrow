from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

# Must set dev mode before importing cli for @_dev_command registration
from lemoncrow.core.capabilities.plugin_runtime import update_session_stats
from lemoncrow.core.foundation.models import Playbook, Rubric
from lemoncrow.core.service.jobs import JOB_CONSOLIDATE_BLOCKS
from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.cli import cli
from lemoncrow.infra.internal_llm import OllamaUnavailable
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle
from tests.helpers import init_store_at


def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    mcp_server._reset_runtime_cache_for_testing()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)


def _seed_state_change_rubric(root: Path) -> None:
    build_sqlite_store_bundle(root).knowledge.upsert_rubric(
        Rubric(
            id="rubric_state_change_safety",
            domain="state.change",
            required_checks=[
                "canonical_identifier_used",
                "pre_change_state_captured",
                "read_after_write_completed",
                "observed_state_matches_intent",
                "rollback_plan_available",
                "user_visible_surface_checked",
            ],
            block_if_missing=[
                "canonical_identifier_used",
                "pre_change_state_captured",
                "read_after_write_completed",
                "observed_state_matches_intent",
                "rollback_plan_available",
                "user_visible_surface_checked",
            ],
        )
    )


def _seed_rescue_block(root: Path) -> None:
    build_sqlite_store_bundle(root).knowledge.upsert_block(
        Playbook(
            id="state-change-rescue",
            title="Recover from wrong target update",
            domain="state.change",
            triggers=["wrong target updated", "Update external state"],
            failure_signals=["wrong target updated"],
            situation="When an external state change was applied to the wrong target.",
            procedure=[
                "Stop retrying the write path.",
                "Confirm the intended target before any further state changes.",
            ],
            verification=["Verify the target identifier against the original request."],
            dead_ends=["Do not repeat the mutation without checking the target."],
        )
    )


def test_init_handles_empty_bundled_seed_resources(tmp_path: Path, monkeypatch) -> None:
    res = _invoke(tmp_path / "a", "init")
    assert res.exit_code == 0, res.output
    assert "store initialized" in res.output
    assert "Code index ready" in res.output
    assert "seeded" not in res.output


def test_init_runs_locally_without_an_account(tmp_path: Path, monkeypatch) -> None:
    # Open-source runtime: init is fully local and never requires an account.
    res = _invoke(tmp_path / "a", "init", "--no-seed", "--no-index")
    assert res.exit_code == 0, res.output
    assert "store initialized" in res.output
    assert "account is required" not in res.output


def test_init_runs_locally_with_no_login_prompt(tmp_path: Path, monkeypatch) -> None:
    """Open-source `lc init` is fully local: it never attempts a browser login,
    and the account-free steps (store init, code index) run normally.
    """
    from lemoncrow.gateway.cli.commands import code

    index_calls: list[object] = []

    def _fake_index(engine: object, **_kw: object) -> dict[str, int]:
        index_calls.append(engine)
        return {"files_indexed": 1, "symbols_indexed": 2, "imports_indexed": 3}

    monkeypatch.setattr(code, "_code_context_engine", lambda repo_root: object())
    monkeypatch.setattr(code, "_index_repo_with_progress", _fake_index)

    res = _invoke(tmp_path / "a", "init")
    assert res.exit_code == 0, res.output
    assert "Aborted" not in res.output
    assert "store initialized" in res.output
    assert "account is required" not in res.output
    assert index_calls, "code index must still run"


def test_run_rubric_via_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    _seed_state_change_rubric(root)
    checks = {
        "canonical_identifier_used": True,
        "pre_change_state_captured": True,
        "read_after_write_completed": True,
        "observed_state_matches_intent": True,
        "rollback_plan_available": True,
        "user_visible_surface_checked": True,
    }
    res = _invoke(
        root,
        "tools",
        "call",
        "verify",
        "--dev",
        "--args",
        json.dumps({"rubric_id": "rubric_state_change_safety", "checks": checks}),
        "--json",
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["status"] == "pass"


def test_run_rubric_blocks_when_required_missing(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    _seed_state_change_rubric(root)
    res = _invoke(
        root,
        "tools",
        "call",
        "verify",
        "--dev",
        "--args",
        json.dumps({"rubric_id": "rubric_state_change_safety", "checks": {}}),
        "--json",
    )
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["status"] == "blocked"


def test_code_context_cli_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "lemoncrow"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.py").write_text(
        "def alpha() -> int:\n    return 1\n\ndef beta() -> int:\n    return alpha()\n",
        encoding="utf-8",
    )
    _invoke(root, "init", "--no-seed")

    indexed = _invoke(root, "code", "index", "--repo-root", str(repo), "--json")
    assert indexed.exit_code == 0, indexed.output
    assert json.loads(indexed.output)["symbols_indexed"] >= 2


def test_record_trace_and_extract_block(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    trace = json.dumps(
        {
            "agent": "codex",
            "domain": "coding",
            "task": "Test trace ingest",
            "status": "success",
            "files_touched": ["src/foo.py"],
            "commands_run": ["pytest"],
            "validation_results": [{"name": "unit", "passed": True, "detail": ""}],
        }
    )
    res = _invoke(root, "runs", "record", input=trace)
    assert res.exit_code == 0, res.output
    trace_id = res.output.strip()
    assert len(trace_id) > 0


def test_rescue_returns_procedure(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    _seed_rescue_block(root)
    res = _invoke(
        root,
        "tools",
        "call",
        "rescue",
        "--dev",
        "--args",
        json.dumps(
            {
                "task": "Update external state",
                "error": "wrong target updated",
                "domain": "state.change",
            }
        ),
        "--json",
    )
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert "rescue" in payload
    assert payload["rescue"]


def test_savings_cli_reports_session_stats(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Search",
            "tool_input": {"content_regex": "needle", "file_glob_patterns": ["*.py"]},
        },
    )
    # Real measured savings come from the canonical session dir's savings.jsonl
    # (written by the stop hook at session end, priced at the model in use
    # that turn).
    from lemoncrow.core.foundation.paths import session_dir

    savings_file = session_dir(root, "claude", "s1") / "savings.jsonl"
    savings_file.parent.mkdir(parents=True, exist_ok=True)
    import datetime

    savings_file.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "tool_name": "Read",
                "lever": "structure_map",
                "tokens_saved": 1200,
                "cost_saved_usd": 0.0036,
                "model": "claude-sonnet-4-5",
                "ts": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # `savings` is now a hidden back-compat alias (spec §5.1) -- installed
    # statusline scripts still shell out to it, so this exercises the alias
    # exactly as they do. The promoted spelling is `lc usage optimize`.
    res = _invoke(root, "savings", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["session"]["session_count"] == 1
    assert payload["tokens_saved"] == 1200
    assert payload["saved_usd"] == 0.0036


def test_legacy_account_cli_is_absent_and_local_share_settings_remain(tmp_path: Path) -> None:
    root = tmp_path / "a"
    help_result = _invoke(root, "--help")
    assert help_result.exit_code == 0, help_result.output
    assert "  account " not in help_result.output
    assert "  auth " in help_result.output
    assert _invoke(root, "account", "--help").exit_code != 0

    # Hosted authentication is the distinct Authward-backed `lc auth` surface.
    auth_help = _invoke(root, "auth", "--help")
    assert auth_help.exit_code == 0, auth_help.output
    assert "login" in auth_help.output
    assert "logout" in auth_help.output

    # `lc init` is local-only; the retired account-link flag must not reappear.
    init_help = _invoke(root, "init", "--help")
    assert init_help.exit_code == 0, init_help.output
    assert "--login" not in init_help.output

    share = _invoke(root, "share", "--json")
    assert share.exit_code == 0, share.output
    assert json.loads(share.output) == {
        "url": "https://lemoncrow.com",
        "text": "LemonCrow: https://lemoncrow.com",
    }

    set_result = _invoke(root, "settings", "set", "alwaysLoadTools", "off", "--json")
    assert set_result.exit_code == 0, set_result.output
    assert json.loads(set_result.output)["alwaysLoadTools"] is False

    show = _invoke(root, "settings", "show", "--json")
    assert show.exit_code == 0, show.output
    assert json.loads(show.output)["alwaysLoadTools"] is False


def test_worker_runs_consolidation_job_on_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))

    store = build_sqlite_store_bundle(root)
    store.knowledge.upsert_block(
        Playbook(
            id="rb-one",
            title="Checkout retry timeout",
            domain="testing",
            situation="When checkout retries fail with timeout during webhook delivery",
            triggers=["checkout", "retry", "timeout"],
            procedure=["Inspect retry budget", "Verify idempotency key", "Run webhook tests"],
            failure_signals=["timeout", "duplicate delivery"],
        ),
        write_markdown=False,
    )
    store.knowledge.upsert_block(
        Playbook(
            id="rb-two",
            title="Checkout retry webhook timeout",
            domain="testing",
            situation="When checkout retries fail with timeout during webhook delivery",
            triggers=["checkout", "retry", "timeout"],
            procedure=["Inspect retry budget", "Verify idempotency key", "Run webhook tests"],
            failure_signals=["timeout", "duplicate delivery"],
        ),
        write_markdown=False,
    )

    def unavailable(messages: object, json_schema: object | None = None) -> None:
        _ = (messages, json_schema)
        raise OllamaUnavailable("offline")

    monkeypatch.setattr("lemoncrow.pro.capabilities.consolidation.worker.chat", unavailable)

    enqueue = _invoke(root, "worker", "enqueue", JOB_CONSOLIDATE_BLOCKS, "--json")
    assert enqueue.exit_code == 0, enqueue.output
    payload = json.loads(enqueue.output)
    assert payload["status"] == "pending"

    run = _invoke(root, "worker", "run-once")
    assert run.exit_code == 0, run.output
    assert "processed job:" in run.output

    jobs = store.jobs.list_jobs(limit=10)
    assert jobs[0]["status"] == "succeeded"
    assert len(store.lessons.list_consolidation_candidates()) == 1


def test_map_prints_current_workspace_url_without_opening_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo with spaces"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.map.discover_dashboard_url",
        lambda root: "http://127.0.0.1:3225",
    )
    opened: list[str] = []
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.map.webbrowser.open", opened.append)

    res = _invoke(tmp_path / "runtime", "map", "--no-open")

    assert res.exit_code == 0, res.output
    assert "http://127.0.0.1:3225/map?repo=" in res.output
    assert "repo+with+spaces" in res.output
    assert opened == []


def test_dashboard_open_discovers_the_existing_frontend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.infra.runtime.dashboard_url.discover_dashboard_url",
        lambda root, requested_port=None: "http://127.0.0.1:3225",
    )
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    res = _invoke(tmp_path / "runtime", "dashboard", "open")

    assert res.exit_code == 0, res.output
    assert "http://127.0.0.1:3225/" in res.output
    assert opened == ["http://127.0.0.1:3225/"]


# `lc task` command removed — cut in CLI consolidation.
