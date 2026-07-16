"""CLI tests for ``lc session backfill``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.core.capabilities import savings_summary
from lemoncrow.core.capabilities import session_backfill as session_backfill_mod
from lemoncrow.core.capabilities.session_backfill import BackfilledSession, BackfillResult
from lemoncrow.gateway.cli import cli


def _result(*, saved_usd: float = 0.0, n: int = 0, scanned: int = 5) -> BackfillResult:
    r = BackfillResult(scanned=scanned, already_tracked=scanned - n, below_threshold=0)
    for i in range(n):
        r.backfilled.append(
            BackfilledSession(
                host="claude",
                session_id=f"sid-{i}",
                saved_usd=saved_usd,
                calls_saved=2,
                when=__import__("datetime").datetime.now(),
            )
        )
    return r


def test_backfill_defaults_to_claude_codex_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen_hosts: list[str] = []

    def _fake(root: Path, host: str, **kw: object) -> BackfillResult:
        seen_hosts.append(host)
        return _result()

    monkeypatch.setattr(session_backfill_mod, "backfill_host_savings", _fake)
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".lemoncrow"), "session", "backfill"])
    assert result.exit_code == 0, result.output
    assert seen_hosts == ["claude", "codex", "opencode"]


def test_backfill_host_filter_is_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen_hosts: list[str] = []
    monkeypatch.setattr(
        session_backfill_mod,
        "backfill_host_savings",
        lambda root, host, **kw: seen_hosts.append(host) or _result(),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".lemoncrow"), "session", "backfill", "--host", "codex"])
    assert result.exit_code == 0, result.output
    assert seen_hosts == ["codex"]


def test_backfill_reconciles_only_when_something_was_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reconciled: list[Path] = []
    monkeypatch.setattr(session_backfill_mod, "backfill_host_savings", lambda root, host, **kw: _result())
    monkeypatch.setattr(savings_summary, "reconcile_savings_aggregate", lambda root: reconciled.append(root) or {})
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(tmp_path / ".lemoncrow"), "session", "backfill", "--host", "claude"])
    assert result.exit_code == 0, result.output
    assert reconciled == []  # nothing backfilled -> no reconcile

    monkeypatch.setattr(
        session_backfill_mod, "backfill_host_savings", lambda root, host, **kw: _result(saved_usd=1.0, n=1)
    )
    result = runner.invoke(cli, ["--root", str(tmp_path / ".lemoncrow"), "session", "backfill", "--host", "claude"])
    assert result.exit_code == 0, result.output
    assert len(reconciled) == 1


def test_backfill_dry_run_never_reconciles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reconciled: list[object] = []
    monkeypatch.setattr(
        session_backfill_mod, "backfill_host_savings", lambda root, host, **kw: _result(saved_usd=1.0, n=1)
    )
    monkeypatch.setattr(savings_summary, "reconcile_savings_aggregate", lambda root: reconciled.append(root) or {})
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--root", str(tmp_path / ".lemoncrow"), "session", "backfill", "--host", "claude", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert reconciled == []
    assert "Would backfill" in result.output


def test_backfill_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        session_backfill_mod, "backfill_host_savings", lambda root, host, **kw: _result(saved_usd=0.5, n=2)
    )
    monkeypatch.setattr(savings_summary, "reconcile_savings_aggregate", lambda root: {})
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--root", str(tmp_path / ".lemoncrow"), "session", "backfill", "--host", "claude", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sessions_backfilled"] == 2
    assert payload["saved_usd"] == 1.0
    assert payload["hosts"]["claude"]["backfilled"] == 2
