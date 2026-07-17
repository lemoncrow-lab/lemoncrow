from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
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
    monkeypatch.setenv("LEMONCROW_AUTH_TOKEN", "free-account-token")
    res = _invoke(tmp_path / "a", "init")
    assert res.exit_code == 0, res.output
    assert "store initialized" in res.output
    assert "Code index ready" in res.output
    assert "seeded" not in res.output


def test_init_requires_a_free_account(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)
    res = _invoke(tmp_path / "a", "init", "--no-seed", "--no-index")
    assert res.exit_code != 0
    assert "free LemonCrow account is required" in res.output


def test_init_login_ctrl_c_continues_local_setup(tmp_path: Path, monkeypatch) -> None:
    """Ctrl+C during the browser-login wait degrades to the --no-login path.

    The account-free steps (store init, seed, code index) must still run and
    the declined marker must be set; only project activation is skipped.
    """
    from lemoncrow.core.capabilities.licensing.store import is_login_declined
    from lemoncrow.gateway.cli.commands import admin, code

    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(admin, "_is_interactive_terminal", lambda: True)

    def _interrupted_login(root: Path, as_json: bool, dev_mode: bool = False) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(admin, "_oauth_login", _interrupted_login)
    index_calls: list[object] = []

    def _fake_index(engine: object, **_kw: object) -> dict[str, int]:
        index_calls.append(engine)
        return {"files_indexed": 1, "symbols_indexed": 2, "imports_indexed": 3}

    monkeypatch.setattr(code, "_code_context_engine", lambda repo_root: object())
    monkeypatch.setattr(code, "_index_repo_with_progress", _fake_index)

    res = _invoke(tmp_path / "a", "init")
    assert res.exit_code == 0, res.output
    assert "Aborted" not in res.output
    assert "Login skipped" in res.output
    assert "store initialized" in res.output
    assert index_calls, "code index must still run after an aborted login"
    assert is_login_declined()


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

    res = _invoke(root, "savings", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["session"]["session_count"] == 1
    assert payload["tokens_saved"] == 1200
    assert payload["saved_usd"] == 0.0036


def test_plugin_auth_status_share_and_settings_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    token = json.dumps(
        {
            "email": "dev@example.com",
            "userId": "u1",
            "refreshToken": "r1",
            "subscriptionStatus": {
                "plan": "lite",
                "monthlySavingsInUsd": 40.0,
                "monthlySavingsCapInUsd": 200.0,
                "savingsRemainingUsd": 160.0,
                "savingsOverCap": False,
            },
        }
    )

    # `--token` login was removed (it never wrote the token where the identity
    # resolver reads it); seed the same auth state directly via the primitives
    # that path used, then exercise the read-side status/subscription CLIs.
    from lemoncrow.core.capabilities.plugin_runtime import parse_login_token, write_auth_state

    login_auth = write_auth_state(root, parse_login_token(token))
    assert login_auth["email"] == "dev@example.com"

    status = _invoke(root, "account", "status", "--json")
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["authenticated"] is True
    assert status_payload["email"] == "dev@example.com"

    subscription = _invoke(root, "account", "subscription", "--json")
    assert subscription.exit_code == 0, subscription.output
    assert json.loads(subscription.output)["plan"] == "lite"

    cap = _invoke(root, "account", "cap", "--json")
    assert cap.exit_code == 0, cap.output
    # conftest.py strips the pinned Ed25519 key for the whole suite by default
    # (is_configured() == False), so compute_usage_meter's signed-verdict
    # override (licensing_gate.resolve_cap_verdict) stays inert here and the
    # local estimate applies unchanged -- verified/reason are the additive
    # fields that surface only once a key is pinned. See
    # tests/core/test_cap_verdict.py for the verified, signed-token case.
    assert json.loads(cap.output) == {
        "cap_usd": 200.0,
        "over_cap": False,
        "remaining_usd": 200.0,
        "saved_usd": 0.0,
        "verified": None,
        "reason": None,
    }

    share = _invoke(root, "share", "--json")
    assert share.exit_code == 0, share.output
    assert json.loads(share.output)["code"].startswith("LEMONCROW-")

    set_result = _invoke(root, "settings", "set", "alwaysLoadTools", "off", "--json")
    assert set_result.exit_code == 0, set_result.output
    assert json.loads(set_result.output)["alwaysLoadTools"] is False

    show = _invoke(root, "settings", "show", "--json")
    assert show.exit_code == 0, show.output
    assert json.loads(show.output)["alwaysLoadTools"] is False


def test_logout_starts_anonymous_trial_by_default(tmp_path: Path) -> None:
    root = tmp_path / "a"
    res = _invoke(root, "account", "logout", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["logged_out"] is True
    assert payload["anonymous"]["isAnonymous"] is True


def test_account_commands_have_no_top_level_compatibility_aliases(tmp_path: Path) -> None:
    root = tmp_path / "a"
    help_result = _invoke(root, "--help")

    assert help_result.exit_code == 0, help_result.output
    assert "  account " in help_result.output
    assert "  login " not in help_result.output
    assert "  logout " not in help_result.output

    assert _invoke(root, "login", "--help").exit_code != 0
    assert _invoke(root, "logout", "--help").exit_code != 0
    assert _invoke(root, "account", "login", "--status").exit_code != 0

    bare_account = _invoke(root, "account")
    assert bare_account.exit_code == 0, bare_account.output
    assert "Not logged in" in bare_account.output


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


def test_stack_start_spawns_native_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **kwargs):  # type: ignore[no-untyped-def]
            spawned_calls.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
            self.pid = 2468

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.stack.subprocess.Popen", FakePopen)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.stack._pid_is_running", lambda pid: pid == 2468)

    res = _invoke(tmp_path / "a", "stack", "start", "--with-docs")

    assert res.exit_code == 0, res.output
    # The stack-start command spawns one supervisor process ending in ["stack", "run"].
    # Filter to that call (other Popen calls may originate from background telemetry workers).
    stack_run_call = next(
        (call for call in spawned_calls if len(call) >= 2 and call[-2:] == ["stack", "run"]),
        None,
    )
    assert stack_run_call is not None, f"no `stack run` supervisor spawned; saw: {spawned_calls!r}"
    assert "http://localhost:3125" in res.output
    assert "docs are no longer part of the managed stack" in res.output


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


def test_background_install_writes_native_stack_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    unit_dir = tmp_path / "systemd-user"
    commands: list[list[str]] = []

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background._is_linux", lambda: True)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background._is_macos", lambda: False)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background.SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.background.shutil.which",
        lambda name: "/bin/systemctl" if name == "systemctl" else "/usr/bin/lemoncrow",
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.background.subprocess.run",
        lambda args, **kwargs: commands.append([str(item) for item in args]),
    )

    res = _invoke(root, "background", "install", "--with-stack")

    assert res.exit_code == 0, res.output
    stack_unit = (unit_dir / "lemoncrow-stack.service").read_text(encoding="utf-8")
    assert "docker compose" not in stack_unit
    assert "background service start" in stack_unit
    assert any(cmd[:3] == ["systemctl", "--user", "enable"] for cmd in commands)
    assert any(cmd[:3] == ["systemctl", "--user", "restart"] for cmd in commands)


def test_background_install_skips_activation_when_user_systemd_bus_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "a"
    unit_dir = tmp_path / "systemd-user"
    commands: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in args]
        commands.append(command)
        if command[:3] == ["systemctl", "--user", "daemon-reload"]:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="Failed to connect to user scope bus: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background._is_linux", lambda: True)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background._is_macos", lambda: False)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background.SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.background.shutil.which",
        lambda name: "/bin/systemctl" if name == "systemctl" else "/usr/bin/lemoncrow",
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background.subprocess.run", _run)

    res = _invoke(root, "background", "install", "--with-stack")

    assert res.exit_code == 0, res.output
    assert "systemd user bus is unavailable" in res.output
    assert (unit_dir / "lemoncrow-controller.service").exists()
    assert (unit_dir / "lemoncrow-stack.service").exists()
    assert not any(cmd[:3] == ["systemctl", "--user", "enable"] for cmd in commands)
    assert not any(cmd[:3] == ["systemctl", "--user", "restart"] for cmd in commands)


def test_background_install_writes_openmemory_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    unit_dir = tmp_path / "systemd-user"
    commands: list[list[str]] = []

    def _which(name: str) -> str | None:
        mapping = {
            "systemctl": "/bin/systemctl",
            "lemoncrow": "/usr/bin/lemoncrow",
            "git": "/usr/bin/git",
            "docker": "/usr/bin/docker",
            "make": "/usr/bin/make",
        }
        return mapping.get(name)

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background._is_linux", lambda: True)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background._is_macos", lambda: False)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background.SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.background.shutil.which", _which)
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.commands.background.subprocess.run",
        lambda args, **kwargs: commands.append([str(item) for item in args]),
    )

    res = _invoke(root, "background", "install", "--with-openmemory")

    assert res.exit_code == 0, res.output
    openmemory_unit = (unit_dir / "lemoncrow-openmemory.service").read_text(encoding="utf-8")
    assert "openmemory up" in openmemory_unit
    assert "openmemory down" in openmemory_unit
    assert any(cmd[:3] == ["systemctl", "--user", "enable"] for cmd in commands)
    assert any(cmd[:3] == ["systemctl", "--user", "restart"] for cmd in commands)


def test_stop_stack_processes_kills_process_groups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    stack_dir = root / "stack"
    stack_dir.mkdir(parents=True)
    (stack_dir / "stack.pid").write_text("101\n", encoding="utf-8")
    (stack_dir / "service.pid").write_text("202\n", encoding="utf-8")
    (stack_dir / "frontend.pid").write_text("303\n", encoding="utf-8")

    killed: set[int] = set()
    killpg_calls: list[tuple[int, int]] = []

    monkeypatch.setattr("lemoncrow.infra.runtime.stack_lifecycle.os.getpgid", lambda pid: pid)

    def _mock_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))
        killed.add(pgid)

    monkeypatch.setattr(
        "lemoncrow.infra.runtime.stack_lifecycle.os.killpg",
        _mock_killpg,
    )
    monkeypatch.setattr("lemoncrow.infra.runtime.stack_lifecycle._pid_is_running", lambda pid: pid not in killed)

    from lemoncrow.infra.runtime.stack_lifecycle import _stop_stack_processes

    payload = _stop_stack_processes(root, force=False)

    assert payload["running"] is False
    assert killpg_calls == [
        (303, 15),
        (202, 15),
        (101, 15),
    ]
    assert not (stack_dir / "stack.pid").exists()
    assert not (stack_dir / "service.pid").exists()
    assert not (stack_dir / "frontend.pid").exists()


@pytest.mark.slow
def test_servicectl_tick_enqueues_and_processes_periodic_consolidation(
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

    res = _invoke(
        root,
        "servicectl",
        "tick",
        "--maintenance-interval-seconds",
        "0",
        "--session-import-interval-seconds",
        "-1",
        "--json",
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert len(payload["enqueued_jobs"]) == 1
    assert len(payload["processed_jobs"]) == 1
    assert len(store.lessons.list_consolidation_candidates()) == 1


def test_servicectl_start_writes_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))

    spawned: dict[str, object] = {}

    class FakePopen:
        def __init__(self, args, **kwargs):  # type: ignore[no-untyped-def]
            spawned["args"] = args
            spawned["kwargs"] = kwargs
            self.pid = 4321

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.servicectl.subprocess.Popen", FakePopen)
    monkeypatch.setattr("lemoncrow.infra.runtime.servicectl_lifecycle._pid_is_running", lambda pid: pid == 4321)

    res = _invoke(
        root,
        "servicectl",
        "start",
        "--interval-seconds",
        "5",
        "--maintenance-interval-seconds",
        "60",
        "--json",
    )

    assert res.exit_code == 0, res.output
    # The output may contain a notice about systemd before the JSON payload
    json_start = res.output.index("{")
    payload = json.loads(res.output[json_start:])
    assert payload["running"] is True
    assert payload["pid"] == 4321
    args = spawned["args"]
    assert isinstance(args, list)
    assert "lemoncrow.gateway.cli" in " ".join(str(item) for item in args)
    assert (root / "servicectl" / "servicectl.pid").read_text(encoding="utf-8").strip() == "4321"


@pytest.mark.slow
def test_servicectl_tick_imports_only_new_or_updated_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))

    codex_file = tmp_path / "codex" / "rollout-2026-05-09T12-00-00-11111111-2222-3333-4444-555555555555.jsonl"
    codex_file.parent.mkdir(parents=True, exist_ok=True)
    codex_file.write_text(
        "\n".join(
            [
                '{"id":"meta","timestamp":"2026-05-09T12:00:00Z","instructions":"Test import"}',
                '{"type":"message","role":"user","content":[{"type":"input_text","text":"Do the task"}]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.codex.find_codex_sessions",
        lambda root=None: [codex_file],
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.copilot.find_copilot_sessions",
        lambda root=None: iter(()),
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.claude.find_claude_sessions",
        lambda root=None: iter(()),
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.opencode.find_opencode_sessions",
        lambda db_path=None: iter(()),
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.gemini.find_gemini_sessions",
        lambda root=None: iter(()),
    )

    def unavailable(messages: object, json_schema: object | None = None) -> None:
        _ = (messages, json_schema)
        raise OllamaUnavailable("offline")

    monkeypatch.setattr("lemoncrow.pro.capabilities.consolidation.worker.chat", unavailable)

    first = _invoke(
        root,
        "servicectl",
        "tick",
        "--maintenance-interval-seconds",
        "0",
        "--session-import-interval-seconds",
        "0",
        "--json",
    )
    assert first.exit_code == 0, first.output
    payload1 = json.loads(first.output)
    assert payload1["imported_sessions"]["codex"] == 1

    second = _invoke(
        root,
        "servicectl",
        "tick",
        "--maintenance-interval-seconds",
        "0",
        "--session-import-interval-seconds",
        "0",
        "--json",
    )
    assert second.exit_code == 0, second.output
    payload2 = json.loads(second.output)
    assert payload2["imported_sessions"]["codex"] == 0

    codex_file.write_text(
        codex_file.read_text(encoding="utf-8") + '{"type":"message","role":"assistant"}\n',
        encoding="utf-8",
    )
    bumped_mtime = codex_file.stat().st_mtime + 10
    os.utime(codex_file, (bumped_mtime, bumped_mtime))

    third = _invoke(
        root,
        "servicectl",
        "tick",
        "--maintenance-interval-seconds",
        "0",
        "--session-import-interval-seconds",
        "0",
        "--json",
    )
    assert third.exit_code == 0, third.output
    payload3 = json.loads(third.output)
    assert payload3["imported_sessions"]["codex"] == 1


@pytest.mark.slow
def test_servicectl_surfaces_job_queue_health(
    tmp_path: Path,
) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))

    store = build_sqlite_store_bundle(root)
    running_job_id = store.jobs.enqueue_job("consolidate", {"n": 1}, max_attempts=2)
    dead_job_id = store.jobs.enqueue_job("retry", {"n": 2}, max_attempts=1)

    running_job = store.jobs.claim_job()
    dead_job = store.jobs.claim_job()

    assert running_job is not None
    assert dead_job is not None
    assert running_job["id"] == running_job_id
    assert dead_job["id"] == dead_job_id
    assert store.jobs.fail_job(dead_job_id, "boom") is True

    stale_locked_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with store.jobs._connect() as conn:
        conn.execute(
            "UPDATE jobs SET locked_at = ?, updated_at = ? WHERE id = ?",
            (stale_locked_at, stale_locked_at, running_job_id),
        )

    expected_health = {
        "pending": 0,
        "running": 1,
        "failed": 0,
        "dead": 1,
        "stuck_running": 1,
        "active": 1,
    }

    status_before = _invoke(root, "servicectl", "status", "--json")
    assert status_before.exit_code == 0, status_before.output
    status_before_payload = json.loads(status_before.output)
    assert status_before_payload["job_queue_health"] == expected_health

    tick = _invoke(
        root,
        "servicectl",
        "tick",
        "--maintenance-interval-seconds",
        "-1",
        "--session-import-interval-seconds",
        "-1",
        "--json",
    )
    assert tick.exit_code == 0, tick.output
    tick_payload = json.loads(tick.output)
    assert tick_payload["job_queue_health_before"] == expected_health
    assert tick_payload["pending_jobs"] == tick_payload["job_queue_health"]["active"]

    status_after = _invoke(root, "servicectl", "status", "--json")
    assert status_after.exit_code == 0, status_after.output
    status_after_payload = json.loads(status_after.output)
    assert status_after_payload["job_queue_health"] == tick_payload["job_queue_health"]


# `lc task` command removed — cut in CLI consolidation.
