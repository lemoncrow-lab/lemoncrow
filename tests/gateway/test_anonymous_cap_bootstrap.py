"""Anonymous lifecycle transitions bootstrap their signed cap authority."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import admin


def test_bootstrap_helper_is_best_effort(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.licensing import usage_report

    monkeypatch.setattr(usage_report, "maybe_report_usage", lambda root: root == tmp_path)
    assert admin._bootstrap_anonymous_verdict(tmp_path) is True

    monkeypatch.setattr(
        usage_report,
        "maybe_report_usage",
        lambda _root: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert admin._bootstrap_anonymous_verdict(tmp_path) is False


def test_anonymous_cli_transitions_bootstrap_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".lemoncrow"
    calls: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        admin,
        "_bootstrap_anonymous_verdict",
        lambda value: calls.append(Path(value)) or True,
    )
    runner = CliRunner()

    initialized = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "init",
            "--no-login",
            "--no-index",
            "--no-seed",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    assert calls == [root]

    calls.clear()
    logged_in = runner.invoke(
        cli,
        ["--root", str(root), "account", "login", "--anonymous", "--json"],
    )
    assert logged_in.exit_code == 0, logged_in.output
    assert calls == [root]

    calls.clear()
    logged_out = runner.invoke(
        cli,
        ["--root", str(root), "account", "logout", "--json"],
    )
    assert logged_out.exit_code == 0, logged_out.output
    assert calls == [root]

    calls.clear()
    no_trial = runner.invoke(
        cli,
        ["--root", str(root), "account", "logout", "--no-trial", "--json"],
    )
    assert no_trial.exit_code == 0, no_trial.output
    assert calls == []
