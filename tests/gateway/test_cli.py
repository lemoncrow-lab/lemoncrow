from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from atelier.core.capabilities.plugin_runtime import update_session_stats
from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.store import ReasoningStore
from atelier.core.service.jobs import JOB_CONSOLIDATE_BLOCKS
from atelier.gateway.adapters.cli import cli
from atelier.infra.internal_llm.ollama_client import OllamaUnavailable


def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)


def test_init_seeds_blocks_and_rubrics(tmp_path: Path) -> None:
    res = _invoke(tmp_path / "a", "init")
    assert res.exit_code == 0, res.output
    assert "seeded" in res.output
    # 10 blocks + 7 rubrics expected
    assert "10 reasonblocks" in res.output
    assert "7 rubrics" in res.output


def test_check_plan_blocks_resolving_target_from_slug(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(
        root,
        "lint",
        "--task",
        "Fix a live state change",
        "--domain",
        "state.change",
        "--step",
        "Resolve target from URL slug alone",
        "--step",
        "Apply the update",
        "--json",
    )
    assert res.exit_code == 2, res.output
    payload = json.loads(res.output)
    assert payload["status"] == "blocked"


def test_run_rubric_via_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    checks = json.dumps(
        {
            "canonical_identifier_used": True,
            "pre_change_state_captured": True,
            "read_after_write_completed": True,
            "observed_state_matches_intent": True,
            "rollback_plan_available": True,
            "user_visible_surface_checked": True,
        }
    )
    res = _invoke(root, "verify", "rubric_state_change_safety", "--json", input=checks)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["status"] == "pass"


def test_run_rubric_blocks_when_required_missing(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(root, "verify", "rubric_state_change_safety", "--json", input="{}")
    assert res.exit_code == 2
    payload = json.loads(res.output)
    assert payload["status"] == "blocked"


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
    res = _invoke(root, "trace", "record", input=trace)
    assert res.exit_code == 0
    trace_id = res.output.strip()

    res2 = _invoke(root, "block", "extract", trace_id, "--json")
    assert res2.exit_code == 0
    payload = json.loads(res2.output)
    assert payload["confidence"] >= 0.4


def test_rescue_returns_procedure(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(
        root,
        "rescue",
        "--task",
        "Update external state",
        "--error",
        "wrong target updated",
        "--domain",
        "state.change",
        "--json",
    )
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert "rescue" in payload
    assert payload["matched_blocks"]


def test_savings_cli_reports_session_stats(tmp_path: Path) -> None:
    root = tmp_path / "a"
    root.mkdir(parents=True)
    (root / "smart_state.json").write_text(
        json.dumps({"savings": {"calls_avoided": 1, "tokens_saved": 500}}),
        encoding="utf-8",
    )
    update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "Search",
            "tool_input": {"content_regex": "needle", "file_glob_patterns": ["*.py"]},
        },
    )

    res = _invoke(root, "savings", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["session"]["session_count"] == 1
    assert payload["calls_avoided"] >= 2
    assert payload["tokens_saved"] >= 500
    assert "local estimates" in payload["local_note"]


def test_plugin_auth_status_share_and_settings_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    token = json.dumps({"email": "dev@example.com", "userId": "u1", "refreshToken": "r1"})

    login = _invoke(root, "login", "--token", token, "--json")
    assert login.exit_code == 0, login.output
    login_payload = json.loads(login.output)
    assert login_payload["auth"]["email"] == "dev@example.com"

    status = _invoke(root, "status", "--json")
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

    store = ReasoningStore(root)
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


def test_stack_start_uses_compose_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "atelier.gateway.adapters.cli._run_stack_compose",
        lambda args: calls.append(args),
    )

    res = _invoke(tmp_path / "a", "stack", "start", "--with-docs")

    assert res.exit_code == 0, res.output
    assert calls == [["up", "--build", "-d", "service", "frontend", "otel-collector", "docs"]]
    assert "http://localhost:3125" in res.output


def test_servicectl_tick_enqueues_and_processes_periodic_consolidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    store = ReasoningStore(root)
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

    res = _invoke(root, "servicectl", "tick", "--maintenance-interval-seconds", "0", "--json")

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

    monkeypatch.setattr("atelier.gateway.adapters.cli.subprocess.Popen", FakePopen)
    monkeypatch.setattr("atelier.gateway.adapters.cli._pid_is_running", lambda pid: pid == 4321)

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
    payload = json.loads(res.output)
    assert payload["running"] is True
    assert payload["pid"] == 4321
    args = spawned["args"]
    assert isinstance(args, list)
    assert "atelier.gateway.adapters.cli" in " ".join(str(item) for item in args)
    assert (root / "servicectl" / "servicectl.pid").read_text(encoding="utf-8").strip() == "4321"


# `atelier task` command removed — cut in CLI consolidation.
