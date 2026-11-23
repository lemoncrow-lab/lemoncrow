from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from lemoncrow_client.errors import ClientError, ErrorCode

from lemoncrow.gateway.cli.commands import hosted_telemetry as push
from lemoncrow.gateway.cli.commands.telemetry import telemetry_group
from lemoncrow.infra.runtime.session_report import build_report

SECRET = "customer-secret-prompt /home/me/private/repo.py"


def _snapshot(session: str = "sess-1", status: str = "complete") -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "session_id": session,
        "status": status,
        "created_at": (now - timedelta(minutes=5)).isoformat(),
        "updated_at": now.isoformat(),
        "task": SECRET,
        "cwd": SECRET,
        "events": [{"ts": now.isoformat(), "type": "tool", "summary": SECRET}],
        "cost": {"total_cost_usd": 0.25, "calls": []},
    }


def _write(root: Path, snapshot: dict[str, Any]) -> None:
    folder = root / "sessions" / "2026-11-19" / str(snapshot["session_id"])
    folder.mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps(snapshot), encoding="utf-8")


class _Transport:
    def __init__(self, answers: list[Any] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.answers = answers or []
        self.closed = False

    def request(self, method: str, path: str, *, body: dict[str, Any], headers: dict[str, str]) -> Any:
        assert (method, path) == ("POST", push.ENDPOINT)
        self.bodies.append(body)
        self.headers.append(headers)
        answer = self.answers.pop(0) if self.answers else {"accepted": len(body["runs"])}
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(require=lambda: answer)

    def close(self) -> None:
        self.closed = True


def _invoke(root: Path, *args: str) -> Any:
    return CliRunner().invoke(telemetry_group, ["push", *args], obj={"root": root})


def _connect(
    monkeypatch: pytest.MonkeyPatch, transport: _Transport, *, hosted: bool = True, token: str = "tok"
) -> None:
    config = SimpleNamespace(hosted=hosted, authenticated=bool(token), token=token)
    monkeypatch.setattr(push, "load_config", lambda cwd=None: config)
    monkeypatch.setattr(push, "_transport", lambda _config: transport)


def test_a_real_run_record_becomes_counters_with_none_of_its_content(tmp_path: Path) -> None:
    _write(tmp_path, _snapshot())
    result = _invoke(tmp_path, "--dry-run")
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output and "private" not in result.output
    run = json.loads(result.output)["runs"][0]
    assert run["run_id"] == "sess-1" and run["status"] == "success" and run["cost_micros"] == 250_000


def test_unknown_cost_is_null_and_a_later_end_has_a_later_sequence(tmp_path: Path) -> None:
    report = build_report(_snapshot(), tmp_path)
    assert push.run_payload(replace(report, actual_cost_source="unavailable"), "complete")["cost_micros"] is None
    assert push.run_payload(report, "complete")["savings_micros"] is None
    later = replace(report, ended_at=(report.ended_at or report.started_at) + timedelta(minutes=1))
    assert push.run_payload(later, "complete")["sequence"] > push.run_payload(report, "complete")["sequence"]


def test_a_push_is_split_at_the_server_batch_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [{"run_id": f"r{i}"} for i in range(push.BATCH_RUNS + 1)]
    monkeypatch.setattr(push, "collect_runs", lambda _root, days: runs)
    transport = _Transport()
    _connect(monkeypatch, transport)
    result = _invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert [len(body["runs"]) for body in transport.bodies] == [push.BATCH_RUNS, 1]
    assert transport.headers[0]["Authorization"] == "Bearer tok" and transport.closed
    assert f"{push.BATCH_RUNS + 1} new" in result.output


def test_refused_runs_and_server_errors_fail_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push, "collect_runs", lambda _root, days: [{"run_id": "r"}])
    _connect(monkeypatch, _Transport([{"accepted": 0, "refused_capacity": 1}]))
    refused = _invoke(tmp_path)
    assert refused.exit_code != 0 and "were not stored" in refused.output
    _connect(monkeypatch, _Transport([ClientError(ErrorCode.INTERNAL, "boom")]))
    failed = _invoke(tmp_path)
    assert failed.exit_code != 0 and "boom" in failed.output


def test_it_sends_nothing_unless_connected_and_signed_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _Transport()
    _connect(monkeypatch, transport, hosted=False)
    assert "not connected" in _invoke(tmp_path).output
    _connect(monkeypatch, transport, token="")
    assert "lc auth login" in _invoke(tmp_path).output
    assert transport.bodies == []
