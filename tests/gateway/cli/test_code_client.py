from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.app import _argv_for_program
from lemoncrow.gateway.cli.update_footer import print_update_footer


def test_permanent_cli_entrypoints_map_to_canonical_cli() -> None:
    project_root = Path(__file__).parents[3]
    with (project_root / "pyproject.toml").open("rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]

    assert scripts["lemoncrow"] == "lemoncrow.gateway.cli:main"
    assert scripts["lc"] == scripts["lemoncrow"]
    assert "lemoncode" not in scripts


def test_bare_lc_remains_product_neutral() -> None:
    assert _argv_for_program("lc", []) == []
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert result.output.startswith("◆ LemonCrow ")
    assert "lc review" in result.output
    assert "lc code" in result.output
    assert "lc status" in result.output
    assert "lc rt" in result.output
    assert "lc with claude" in result.output
    assert "codex" in result.output
    assert "opencode" in result.output


def test_update_footer_only_targets_interactive_human_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from lemoncrow.core.foundation import update_notice
    from lemoncrow.gateway.cli import update_footer as cli_app

    tty = types.SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(cli_app.sys, "stdout", tty)
    monkeypatch.setattr(cli_app.sys, "stderr", tty)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(update_notice, "notice_for", lambda surface: "LemonCrow 9.9.9 is available")
    emitted: list[tuple[str, bool]] = []
    monkeypatch.setattr(cli_app.click, "echo", lambda message, err=False: emitted.append((str(message), bool(err))))

    print_update_footer("review", ["review"])
    assert emitted == [("\n◇ LemonCrow 9.9.9 is available", True)]

    emitted.clear()
    for command, argv in (("mcp", ["mcp", "check"]), ("review", ["review", "--json"]), ("code", ["code", "-p", "hi"])):
        print_update_footer(command, argv)
    assert emitted == []


def test_code_help_says_it_is_reserved_and_not_available_yet() -> None:
    for argv in (["code", "--help"], ["--help"]):
        result = CliRunner().invoke(cli, argv)
        assert result.exit_code == 0, result.output
        line = next(row for row in result.output.splitlines() if row.lstrip().startswith(("code ", "Reserved")))
        assert "Reserved" in line and "not available yet" in line, line

    result = CliRunner().invoke(cli, ["code", "-p", "hello"])
    assert result.exit_code == 1
    assert "reserved for the Hosted LemonCrow coding agent, which is not available yet" in result.output


def test_code_help_is_hosted_only() -> None:
    result = CliRunner().invoke(cli, ["runtime", "code", "--help"])
    assert result.exit_code == 0, result.output
    assert "Hosted coding agent" in result.output
    assert not any(line.lstrip().startswith("--pro ") for line in result.output.splitlines())
    assert "--internal-private" not in result.output
    assert "--provider" not in result.output
    assert "--model" not in result.output
    assert "--engine" not in result.output
    assert "index" not in result.output.lower()


def test_code_pro_compatibility_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.coding_engine.run_coding_engine",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("removed --pro path started private Runtime")),
    )
    result = CliRunner().invoke(cli, ["runtime", "code", "--pro", "-p", "hello"])
    assert result.exit_code == 2
    assert "No such option '--pro'" in result.output


def test_internal_private_runtime_is_guarded_and_preserved(monkeypatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities import code_intelligence_provider as provider

    blocked = CliRunner().invoke(cli, ["runtime", "code", "--internal-private", "-p", "hello"])
    assert blocked.exit_code == 1
    assert "internal LemonCrow development tooling" in blocked.output

    captured: dict[str, object] = {}
    monkeypatch.setenv("LEMONCROW_INTERNAL_PRIVATE_RUNTIME", "1")
    monkeypatch.setattr(provider, "require_private_code_intelligence", lambda: None)

    def fake_run_coding_engine(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("lemoncrow.gateway.cli.coding_engine.run_coding_engine", fake_run_coding_engine)
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path / "store"),
            "runtime",
            "code",
            "--internal-private",
            "--engine",
            "pi",
            "--project-root",
            str(tmp_path),
            "--no-mcp",
            "--optimization-mode",
            "enforce",
            "-p",
            "hello",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["engine"] == "pi"
    assert captured["prompt"] == "hello"
    assert captured["mcp_enabled"] is False
    assert captured["optimization_mode"] == "enforce"


def test_internal_private_runtime_fails_clearly_without_private_package(monkeypatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities import code_intelligence_provider as provider

    monkeypatch.setenv("LEMONCROW_INTERNAL_PRIVATE_RUNTIME", "1")

    def unavailable() -> object:
        raise provider.ProEngineUnavailable(provider.PRIVATE_CODE_INTELLIGENCE_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(provider, "_private_provider", unavailable)
    result = CliRunner().invoke(
        cli,
        ["runtime", "code", "--internal-private", "--project-root", str(tmp_path), "-p", "hello"],
    )
    assert result.exit_code == 1
    assert provider.PRIVATE_CODE_INTELLIGENCE_UNAVAILABLE_MESSAGE in result.output
    assert "Traceback" not in result.output


def test_code_no_longer_launches_local_lite(monkeypatch) -> None:
    monkeypatch.setattr(
        "lemoncrow_client.host_trial.run_trial_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("lc code launched a local Runtime Lite host")),
    )
    result = CliRunner().invoke(cli, ["runtime", "code", "-p", "hello"])
    assert result.exit_code == 1
    assert "Hosted LemonCrow coding agent" in result.output
    assert "lc with" in result.output
    assert "lc host" in result.output


def test_public_host_keeps_pi_available(monkeypatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.cli import pi_host

    monkeypatch.setattr(
        pi_host,
        "host_status",
        lambda _root: {
            "installed": True,
            "resolved_path": str(tmp_path / "pi-host"),
            "update_policy": "pinned",
            "pinned_tag": "v-test",
            "metadata": None,
        },
    )
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "host", "status", "--engine", "pi", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["installed"] is True

    help_result = CliRunner().invoke(cli, ["host", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "install" in help_result.output
    assert "update" in help_result.output
    assert "build" in help_result.output
    assert "remove" in help_result.output


def test_hosted_code_options_do_not_start_local_private_engine(monkeypatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.coding_engine.run_coding_engine",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("public Hosted command started local private engine")),
    )
    result = CliRunner().invoke(cli, ["runtime", "code", "--provider", "openai", "-p", "hi"])
    assert result.exit_code == 1
    assert "Hosted LemonCrow coding agent" in result.output


def test_runtime_index_defaults_to_runtime_lite(monkeypatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.cli.commands import code as code_commands

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("def lite_index_probe():\n    return 1\n", encoding="utf-8")

    def forbidden_heavy_engine(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default code index touched CodeContextEngine")

    monkeypatch.setattr(code_commands, "_code_context_engine", forbidden_heavy_engine)
    result = CliRunner().invoke(
        cli,
        ["runtime", "index", "--repo-root", str(repo), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["index_path"].endswith(".lemoncrow/workspace/runtime-lite-v3.sqlite3")
    assert payload["files_indexed"] == 1


def test_doctor_condenses_list_checks_into_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli.commands.admin import _doctor_check_lines, _doctor_summary

    monkeypatch.setenv("NO_COLOR", "1")

    assert _doctor_summary("code_index", ["Index version: 7", "Files indexed: 4567", "Symbols indexed: 1"]) == (
        "4,567 files · 1 symbol"
    )
    assert _doctor_summary("host_clis", ["claude: installed", "codex: not installed"]) == "claude  missing: codex"
    assert _doctor_summary(
        "optional_packages", ["openai: 2.44.0  [cloud]", "mcp: not installed  (uv pip install 'lemoncrow[mcp]')"]
    ) == ("openai 2.44.0  +1 available")
    assert _doctor_summary("core_libraries", ["click: 8.4.2", "rich: MISSING"]) == "missing: rich"

    info = {"ok": True, "version": "rtk 0.49.0", "hint": "set up later", "stats": ["a: 1", "b: 2"]}
    compact = _doctor_check_lines("ok", "rtk", info, verbose=False)
    assert [line.strip() for line in compact] == ["✓ rtk                0.49.0"]
    verbose = _doctor_check_lines("ok", "rtk", info, verbose=True)
    assert [line.strip() for line in verbose] == ["✓ rtk                0.49.0", "a: 1", "b: 2", "→ set up later"]
    failing = _doctor_check_lines("fail", "loopback_server", {"ok": False, "hint": "restart it"}, verbose=False)
    assert [line.strip() for line in failing] == ["✗ loopback server    restart it"]


def test_help_fills_a_wide_terminal() -> None:
    wide = CliRunner().invoke(cli, ["review", "--help"], terminal_width=200)
    narrow = CliRunner().invoke(cli, ["review", "--help"], terminal_width=80)

    assert wide.exit_code == 0 and narrow.exit_code == 0
    assert max(map(len, wide.output.splitlines())) > 120
    assert max(map(len, narrow.output.splitlines())) <= 80


def test_cli_errors_lead_with_the_problem_and_end_with_the_fix() -> None:
    result = CliRunner().invoke(cli, ["stauts"], prog_name="lc")

    assert result.exit_code == 2
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    assert lines[0].startswith("✗ Error: No such command 'stauts'.")
    assert "'status'" in lines[0]
    assert lines[-1] == "→ Try lc --help for help."


def test_a_bare_group_prints_its_help_not_an_error() -> None:
    result = CliRunner().invoke(cli, ["context"], prog_name="lc")

    assert result.output.startswith("Usage: lc context [OPTIONS] COMMAND [ARGS]...")
    assert "Error" not in result.output
    assert "Commands:" in result.output


def test_progress_indicators_only_open_for_slow_work() -> None:
    import time

    from lemoncrow.gateway.cli import style

    calls: list[str] = []
    with style.after_delay(lambda: calls.append("start"), lambda: calls.append("stop"), delay=0.5):
        pass
    assert calls == []

    with style.after_delay(lambda: calls.append("start"), lambda: calls.append("stop"), delay=0.01):
        time.sleep(0.2)
    assert calls == ["start", "stop"]


def test_host_probes_run_together_and_keep_their_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lemoncrow.gateway.cli.commands import runtime as runtime_commands

    barrier = threading.Barrier(3, timeout=5)

    def probe(host: str, workspace: object) -> dict[str, object]:
        barrier.wait()  # deadlocks (BrokenBarrierError) unless all three run at once
        return {"host": host}

    monkeypatch.setattr(runtime_commands, "_query_integration", probe)
    rows = runtime_commands.query_integrations(("claude", "codex", "opencode"), None)
    assert [row["host"] for row in rows] == ["claude", "codex", "opencode"]


def test_style_honors_no_color_and_dumb_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli import style

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert style.mark("ok") != "✓"
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.mark("ok") == "✓"
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert style.title("LemonCrow", "1.0") == "◆ LemonCrow  1.0"


def test_runtime_index_reports_what_it_did(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("def lite_index_probe():\n    return 1\n", encoding="utf-8")

    first = CliRunner().invoke(cli, ["runtime", "index", "--repo-root", str(repo)])
    assert first.exit_code == 0, first.output
    assert f"Updating Workspaces Lite index for {repo.resolve()}" in first.output
    assert "Workspaces Lite index ready: 1 file · 1 indexed · 0 deleted" in first.output
    assert "runtime-lite-v3.sqlite3" in first.output

    again = CliRunner().invoke(cli, ["runtime", "index", "--repo-root", str(repo)])
    assert again.exit_code == 0, again.output
    assert "Workspaces Lite index up to date: 1 file checked, nothing changed" in again.output


def test_private_maintenance_commands_are_not_public_runtime_lite() -> None:
    index = CliRunner().invoke(cli, ["runtime", "code", "index"])
    assert index.exit_code == 1
    assert "internal" in index.output.lower()

    prune = CliRunner().invoke(cli, ["runtime", "code", "prune", "--dry-run"])
    assert prune.exit_code == 1
    assert "internal" in prune.output.lower()

    train = CliRunner().invoke(
        cli,
        ["runtime", "code", "train", "--name", "embedding", "--dry-run"],
    )
    assert train.exit_code == 1
    assert "internal" in train.output.lower()


def test_runtime_index_remains_available() -> None:
    result = CliRunner().invoke(cli, ["runtime", "index", "--help"])
    assert result.exit_code == 0
    assert "--repo-root" in result.output
