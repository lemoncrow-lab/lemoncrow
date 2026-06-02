from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

# Must set dev mode before importing cli for @_dev_command registration
os.environ["ATELIER_DEV_MODE"] = "1"

from atelier.core.capabilities.plugin_runtime import update_session_stats
from atelier.core.foundation.models import ReasonBlock, Rubric
from atelier.core.foundation.store import ContextStore
from atelier.core.service.jobs import JOB_CONSOLIDATE_BLOCKS
from atelier.gateway.adapters import mcp_server
from atelier.gateway.cli import cli
from atelier.infra.internal_llm import OllamaUnavailable


def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    mcp_server._reset_runtime_cache_for_testing()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)


def _seed_state_change_rubric(root: Path) -> None:
    ContextStore(root).upsert_rubric(
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
    ContextStore(root).upsert_block(
        ReasonBlock(
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


def test_init_seeds_blocks_and_rubrics(tmp_path: Path) -> None:
    res = _invoke(tmp_path / "a", "init")
    assert res.exit_code == 0, res.output
    assert "seeded" in res.output
    assert "reasonblocks and" in res.output
    assert "rubrics" in res.output


def test_run_rubric_via_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
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
    _invoke(root, "init")
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
    root = tmp_path / "atelier"
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
    _invoke(root, "init")
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
    _invoke(root, "init")
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
    _invoke(root, "init")
    update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Search",
            "tool_input": {"content_regex": "needle", "file_glob_patterns": ["*.py"]},
        },
    )
    # Real measured savings come from live_savings_events.jsonl (written by
    # MCP tool handlers at result time, priced at the model in use that turn).
    (root / "live_savings_events.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "tool_name": "Read",
                "lever": "structure_map",
                "tokens_saved": 1200,
                "cost_saved_usd": 0.0036,
                "model": "claude-sonnet-4-5",
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
    token = json.dumps({"email": "dev@example.com", "userId": "u1", "refreshToken": "r1"})

    login = _invoke(root, "login", "--token", token, "--json")
    assert login.exit_code == 0, login.output
    login_payload = json.loads(login.output)
    assert login_payload["auth"]["email"] == "dev@example.com"

    status = _invoke(root, "status", "--auth", "--json")
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["authenticated"] is True
    assert status_payload["email"] == "dev@example.com"

    share = _invoke(root, "share", "--json")
    assert share.exit_code == 0, share.output
    assert json.loads(share.output)["code"].startswith("ATELIER-")

    set_result = _invoke(root, "settings", "set", "alwaysLoadTools", "off", "--json")
    assert set_result.exit_code == 0, set_result.output
    assert json.loads(set_result.output)["alwaysLoadTools"] is False

    show = _invoke(root, "settings", "show", "--json")
    assert show.exit_code == 0, show.output
    assert json.loads(show.output)["alwaysLoadTools"] is False


def test_logout_starts_anonymous_trial_by_default(tmp_path: Path) -> None:
    root = tmp_path / "a"
    res = _invoke(root, "logout", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["logged_out"] is True
    assert payload["anonymous"]["isAnonymous"] is True


def test_worker_runs_consolidation_job_on_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    store = ContextStore(root)
    store.upsert_block(
        ReasonBlock(
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
    store.upsert_block(
        ReasonBlock(
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

    monkeypatch.setattr("atelier.core.capabilities.consolidation.worker.chat", unavailable)

    enqueue = _invoke(root, "worker", "enqueue", JOB_CONSOLIDATE_BLOCKS, "--json")
    assert enqueue.exit_code == 0, enqueue.output
    payload = json.loads(enqueue.output)
    assert payload["status"] == "pending"

    run = _invoke(root, "worker", "run-once")
    assert run.exit_code == 0, run.output
    assert "processed job:" in run.output

    jobs = store.list_jobs(limit=10)
    assert jobs[0]["status"] == "succeeded"
    assert len(store.list_consolidation_candidates()) == 1


def test_stack_start_spawns_native_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **kwargs):  # type: ignore[no-untyped-def]
            spawned_calls.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
            self.pid = 2468

    monkeypatch.setattr("atelier.gateway.cli.commands.stack.subprocess.Popen", FakePopen)
    monkeypatch.setattr("atelier.gateway.cli.commands.stack._pid_is_running", lambda pid: pid == 2468)

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


def test_background_install_writes_native_stack_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    unit_dir = tmp_path / "systemd-user"
    commands: list[list[str]] = []

    monkeypatch.setattr("atelier.gateway.cli.commands.background._is_linux", lambda: True)
    monkeypatch.setattr("atelier.gateway.cli.commands.background._is_macos", lambda: False)
    monkeypatch.setattr("atelier.gateway.cli.commands.background.SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr(
        "atelier.gateway.cli.commands.background.shutil.which",
        lambda name: "/bin/systemctl" if name == "systemctl" else "/usr/bin/atelier",
    )
    monkeypatch.setattr(
        "atelier.gateway.cli.commands.background.subprocess.run",
        lambda args, **kwargs: commands.append([str(item) for item in args]),
    )

    res = _invoke(root, "background", "install", "--with-stack")

    assert res.exit_code == 0, res.output
    stack_unit = (unit_dir / "atelier-stack.service").read_text(encoding="utf-8")
    assert "docker compose" not in stack_unit
    assert "stack run" in stack_unit
    assert "stack stop" in stack_unit
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

    monkeypatch.setattr("atelier.gateway.cli.commands.background._is_linux", lambda: True)
    monkeypatch.setattr("atelier.gateway.cli.commands.background._is_macos", lambda: False)
    monkeypatch.setattr("atelier.gateway.cli.commands.background.SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr(
        "atelier.gateway.cli.commands.background.shutil.which",
        lambda name: "/bin/systemctl" if name == "systemctl" else "/usr/bin/atelier",
    )
    monkeypatch.setattr("atelier.gateway.cli.commands.background.subprocess.run", _run)

    res = _invoke(root, "background", "install", "--with-stack")

    assert res.exit_code == 0, res.output
    assert "systemd user bus is unavailable" in res.output
    assert (unit_dir / "atelier-controller.service").exists()
    assert (unit_dir / "atelier-stack.service").exists()
    assert not any(cmd[:3] == ["systemctl", "--user", "enable"] for cmd in commands)
    assert not any(cmd[:3] == ["systemctl", "--user", "restart"] for cmd in commands)


def test_background_install_writes_openmemory_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    unit_dir = tmp_path / "systemd-user"
    commands: list[list[str]] = []

    def _which(name: str) -> str | None:
        mapping = {
            "systemctl": "/bin/systemctl",
            "atelier": "/usr/bin/atelier",
            "git": "/usr/bin/git",
            "docker": "/usr/bin/docker",
            "make": "/usr/bin/make",
        }
        return mapping.get(name)

    monkeypatch.setattr("atelier.gateway.cli.commands.background._is_linux", lambda: True)
    monkeypatch.setattr("atelier.gateway.cli.commands.background._is_macos", lambda: False)
    monkeypatch.setattr("atelier.gateway.cli.commands.background.SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr("atelier.gateway.cli.commands.background.shutil.which", _which)
    monkeypatch.setattr(
        "atelier.gateway.cli.commands.background.subprocess.run",
        lambda args, **kwargs: commands.append([str(item) for item in args]),
    )

    res = _invoke(root, "background", "install", "--with-openmemory")

    assert res.exit_code == 0, res.output
    openmemory_unit = (unit_dir / "atelier-openmemory.service").read_text(encoding="utf-8")
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

    monkeypatch.setattr("atelier.infra.runtime.stack_lifecycle.os.getpgid", lambda pid: pid)

    def _mock_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))
        killed.add(pgid)

    monkeypatch.setattr(
        "atelier.infra.runtime.stack_lifecycle.os.killpg",
        _mock_killpg,
    )
    monkeypatch.setattr("atelier.infra.runtime.stack_lifecycle._pid_is_running", lambda pid: pid not in killed)

    from atelier.infra.runtime.stack_lifecycle import _stop_stack_processes

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
    _invoke(root, "init")

    store = ContextStore(root)
    store.upsert_block(
        ReasonBlock(
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
    store.upsert_block(
        ReasonBlock(
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

    monkeypatch.setattr("atelier.core.capabilities.consolidation.worker.chat", unavailable)

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
    assert len(store.list_consolidation_candidates()) == 1


def test_servicectl_start_writes_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    spawned: dict[str, object] = {}

    class FakePopen:
        def __init__(self, args, **kwargs):  # type: ignore[no-untyped-def]
            spawned["args"] = args
            spawned["kwargs"] = kwargs
            self.pid = 4321

    monkeypatch.setattr("atelier.gateway.cli.commands.servicectl.subprocess.Popen", FakePopen)
    monkeypatch.setattr("atelier.infra.runtime.servicectl_lifecycle._pid_is_running", lambda pid: pid == 4321)

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
    assert "atelier.gateway.cli" in " ".join(str(item) for item in args)
    assert (root / "servicectl" / "servicectl.pid").read_text(encoding="utf-8").strip() == "4321"


@pytest.mark.slow
def test_servicectl_tick_imports_only_new_or_updated_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

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
        "atelier.gateway.hosts.session_parsers.codex.find_codex_sessions",
        lambda root=None: [codex_file],
    )
    monkeypatch.setattr(
        "atelier.gateway.hosts.session_parsers.copilot.find_copilot_sessions",
        lambda root=None: iter(()),
    )
    monkeypatch.setattr(
        "atelier.gateway.hosts.session_parsers.claude.find_claude_sessions",
        lambda root=None: iter(()),
    )
    monkeypatch.setattr(
        "atelier.gateway.hosts.session_parsers.opencode.find_opencode_sessions",
        lambda db_path=None: iter(()),
    )
    monkeypatch.setattr(
        "atelier.gateway.hosts.session_parsers.gemini.find_gemini_sessions",
        lambda root=None: iter(()),
    )

    def unavailable(messages: object, json_schema: object | None = None) -> None:
        _ = (messages, json_schema)
        raise OllamaUnavailable("offline")

    monkeypatch.setattr("atelier.core.capabilities.consolidation.worker.chat", unavailable)

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
def test_servicectl_tick_collects_external_analytics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    monkeypatch.setattr(
        "atelier.gateway.integrations.external_analytics.run_external_reports",
        lambda tool="all", period="today", cwd=None, include_optimize=False: {
            "generated_at": "2026-05-11T12:00:00+00:00",
            "tool": tool,
            "period": period,
            "reports": [
                {
                    "tool": "tokscale",
                    "period": period,
                    "ok": True,
                    "returncode": 0,
                    "command_display": "tokscale --json --no-spinner --today",
                    "payload": {"summary": {"cost": 3.25, "input_tokens": 1200}},
                    "stdout": "{}",
                    "stderr": "",
                },
                {
                    "tool": "codeburn",
                    "period": period,
                    "ok": True,
                    "returncode": 0,
                    "command_display": "codeburn report --format json -p today",
                    "payload": {"overview": {"cost": 4.5, "calls": 9, "sessions": 2}},
                    "stdout": "{}",
                    "stderr": "",
                },
            ],
        },
    )

    def unavailable(messages: object, json_schema: object | None = None) -> None:
        _ = (messages, json_schema)
        raise OllamaUnavailable("offline")

    monkeypatch.setattr("atelier.core.capabilities.consolidation.worker.chat", unavailable)

    res = _invoke(
        root,
        "servicectl",
        "tick",
        "--maintenance-interval-seconds",
        "0",
        "--session-import-interval-seconds",
        "-1",
        "--external-analytics-interval-seconds",
        "0",
        "--external-analytics-period",
        "today",
        "--json",
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["external_analytics_ran"] is True
    assert {item["tool"] for item in payload["external_analytics_runs"]} == {"tokscale", "codeburn"}

    store = ContextStore(root)
    runs = store.list_external_analytics_runs(limit=10)
    assert {item["tool"] for item in runs} == {"tokscale", "codeburn"}
    assert all(item["source"] == "servicectl" for item in runs)


@pytest.mark.slow
def test_servicectl_surfaces_job_queue_health(
    tmp_path: Path,
) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    store = ContextStore(root)
    running_job_id = store.enqueue_job("consolidate", {"n": 1}, max_attempts=2)
    dead_job_id = store.enqueue_job("retry", {"n": 2}, max_attempts=1)

    running_job = store.claim_job()
    dead_job = store.claim_job()

    assert running_job is not None
    assert dead_job is not None
    assert running_job["id"] == running_job_id
    assert dead_job["id"] == dead_job_id
    assert store.fail_job(dead_job_id, "boom") is True

    stale_locked_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with store._connect() as conn:
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


@pytest.mark.slow
def test_servicectl_tick_collects_multiple_external_analytics_periods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    def fake_run_external_reports(
        tool: str = "all",
        period: str = "today",
        cwd: Path | None = None,
        include_optimize: bool = False,
    ) -> dict[str, object]:
        _ = (cwd, include_optimize)
        return {
            "generated_at": "2026-05-11T12:00:00+00:00",
            "tool": tool,
            "period": period,
            "reports": [
                {
                    "tool": "tokscale",
                    "period": period,
                    "ok": True,
                    "returncode": 0,
                    "command_display": f"tokscale --json --no-spinner --{period}",
                    "payload": {"summary": {"cost": 3.25, "input_tokens": 1200}},
                    "stdout": "{}",
                    "stderr": "",
                },
                {
                    "tool": "codeburn",
                    "period": period,
                    "ok": True,
                    "returncode": 0,
                    "command_display": f"codeburn report --format json -p {period}",
                    "payload": {"overview": {"cost": 4.5, "calls": 9, "sessions": 2}},
                    "stdout": "{}",
                    "stderr": "",
                },
            ],
        }

    monkeypatch.setattr(
        "atelier.gateway.integrations.external_analytics.run_external_reports",
        fake_run_external_reports,
    )

    def unavailable(messages: object, json_schema: object | None = None) -> None:
        _ = (messages, json_schema)
        raise OllamaUnavailable("offline")

    monkeypatch.setattr("atelier.core.capabilities.consolidation.worker.chat", unavailable)

    res = _invoke(
        root,
        "servicectl",
        "tick",
        "--maintenance-interval-seconds",
        "0",
        "--session-import-interval-seconds",
        "-1",
        "--external-analytics-interval-seconds",
        "0",
        "--external-analytics-period",
        "week",
        "--external-analytics-period",
        "month",
        "--json",
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["external_analytics_ran"] is True
    assert payload["external_analytics_periods"] == ["week", "month"]
    assert len(payload["external_analytics_runs"]) == 4
    assert {item["period"] for item in payload["external_analytics_runs"]} == {
        "week",
        "month",
    }

    store = ContextStore(root)
    runs = store.list_external_analytics_runs(limit=10)
    assert {item["period"] for item in runs} == {"week", "month"}
    assert {item["tool"] for item in runs} == {"tokscale", "codeburn"}


# `atelier task` command removed — cut in CLI consolidation.
