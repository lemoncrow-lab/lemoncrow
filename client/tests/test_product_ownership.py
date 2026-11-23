from __future__ import annotations

from lemoncrow_client.product_ownership import COMMAND_OWNERSHIP, ProductOwner


def test_thin_client_commands_have_explicit_product_owners() -> None:
    assert COMMAND_OWNERSHIP == {
        "auth": ProductOwner.CORE,
        "status": ProductOwner.CORE,
        "uninstall": ProductOwner.CORE,
        "update": ProductOwner.CORE,
        "target": ProductOwner.CORE,
        "review": ProductOwner.REVIEW,
        "code": ProductOwner.RUNTIME,
        "run": ProductOwner.RUNTIME,
        "session": ProductOwner.RUNTIME,
        "context": ProductOwner.RUNTIME,
        "with": ProductOwner.RUNTIME,
        "runtime": ProductOwner.RUNTIME,
        "rt": ProductOwner.RUNTIME,
        "audit": ProductOwner.CORE,
    }


def test_review_is_not_owned_by_runtime() -> None:
    assert COMMAND_OWNERSHIP["review"] is ProductOwner.REVIEW
    assert COMMAND_OWNERSHIP["runtime"] is ProductOwner.RUNTIME


def test_primary_product_detects_isolated_and_runtime_owned_combined_artifacts(monkeypatch) -> None:
    import lemoncrow_client.cli as cli

    available: set[str] = set()
    monkeypatch.setattr(cli, "_module_available", lambda name: name in available)
    assert cli._primary_product() == "core"

    available.add("lemoncrow_client.review")
    assert cli._primary_product() == "review"

    available.clear()
    available.add("lemoncrow_client.mcpserver")
    assert cli._primary_product() == "runtime"

    available.add("lemoncrow_client.review")
    assert cli._primary_product() == "runtime"


def test_combined_client_target_writes_runtime_state_not_legacy_shared_env(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    available = {"lemoncrow_client.review", "lemoncrow_client.mcpserver"}
    monkeypatch.setattr(cli, "_module_available", lambda name: name in available)
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    sink = io.StringIO()

    assert cli.main(["target", "https://runtime.example"], sink) == 0
    assert (tmp_path / "runtime" / "env").read_text() == "LEMONCROW_URL=https://runtime.example\n"
    assert not (tmp_path / "env").exists()
    assert "runtime LEMONCROW_URL" in sink.getvalue()


def test_rt_delegates_to_runtime_namespace(monkeypatch) -> None:
    import io

    import lemoncrow_client.cli as cli

    calls: list[tuple[list[str], object]] = []
    monkeypatch.setattr(cli, "_runtime", lambda args, sink: calls.append((list(args), sink)) or 0)
    sink = io.StringIO()
    assert cli.main(["rt", "status"], sink) == 0
    assert calls == [(["status"], sink)]


def test_thin_code_help_is_hosted_boundary(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)
    sink = io.StringIO()

    assert cli.main(["code", "--help"], sink) == 0
    output = sink.getvalue()
    assert "Hosted coding agent" in output
    assert "not the open-source local Workspaces Lite launcher" in output
    assert "lc with claude" in output
    assert "lc rt host" in output


def test_managed_only_runtime_command_does_not_recommend_local_server(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)
    sink = io.StringIO()

    assert cli.main(["session", "list"], sink) == 2
    assert "not part of serverless Workspaces Lite" in sink.getvalue()
    assert "--product runtime --local" not in sink.getvalue()


def test_namespaced_promoted_runtime_command_delegates_to_full_cli(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    executable = tmp_path / "lemoncrow"
    executable.write_text("#!/bin/sh\nprintf 'full:%s\\n' \"$*\"\n")
    executable.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_FULL_RUNTIME_CLI", str(executable))
    sink = io.StringIO()

    assert cli.main(["rt", "context", "doctor"], sink) == 0
    assert sink.getvalue() == "full:runtime context doctor\n"


def test_thin_status_reports_review_only(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_module_available", lambda name: name == "lemoncrow_client.review")
    monkeypatch.setattr(cli, "_external_runtime_executable", lambda: None)
    monkeypatch.setattr(cli, "_full_runtime_executable", lambda: None)
    sink = io.StringIO()
    assert cli.main(["status"], sink) == 0
    output = sink.getvalue()
    assert "Review\n  Installed      yes" in output
    assert "Workspaces\n  Installed      no" in output
    assert "lemoncrow.com → Workspaces" in output


def test_thin_uninstall_delegates_to_persisted_product_wrapper(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    script = tmp_path / "install" / "scripts" / "uninstall_product.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nprintf 'uninstall:%s\\n' \"$*\"\n")
    script.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    sink = io.StringIO()
    assert cli.main(["uninstall", "runtime", "--dry-run"], sink) == 0
    assert sink.getvalue() == "uninstall:runtime --dry-run\n"


def test_thin_root_short_prompt_is_removed_in_favor_of_hosted_code(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)

    root = io.StringIO()
    assert cli.main(["-p", "fix this"], root) == 2
    assert "runtime COMMAND" in root.getvalue()

    hosted = io.StringIO()
    assert cli.main(["code", "-p", "fix this"], hosted) == 2
    assert "Hosted coding agent" in hosted.getvalue()
    assert "does not contain the Hosted owned-agent execution loop" in hosted.getvalue()
    assert "lc rt enable <host> --hosted" in hosted.getvalue()
    assert "https://docs.lemoncrow.com/setup/pro-hosted-runtime" in hosted.getvalue()


def test_thin_code_pro_compatibility_is_removed(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)
    sink = io.StringIO()
    assert cli.main(["code", "--pro", "-p", "fix this"], sink) == 2
    assert "unknown Hosted code option: --pro" in sink.getvalue()


def test_thin_with_host_runs_from_runtime_client(monkeypatch) -> None:
    import io

    import lemoncrow_client.cli as cli
    import lemoncrow_client.host_trial as trial

    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(trial, "run_trial_host", lambda host, args: calls.append((host, list(args))) or 0)
    sink = io.StringIO()
    assert cli.main(["with", "claude", "-p", "fix this"], sink) == 0
    assert calls == [("claude", ["-p", "fix this"])]


def test_retired_root_commands_do_not_delegate_to_runtime(monkeypatch) -> None:
    import io

    import lemoncrow_client.cli as cli

    def refuse_delegation(arguments, sink):
        raise AssertionError("retired commands must not delegate")

    monkeypatch.setattr(cli, "_delegate_full_runtime", refuse_delegation)
    for command in ("share", "proof", "optimize", "savings", "add-playbook", "list-playbooks", "letta", "reembed"):
        sink = io.StringIO()
        assert cli.main([command, "--help"], sink) == 2
        assert f"unknown command: {command}\n" in sink.getvalue()
        assert "Run `lc --help`" in sink.getvalue()


def test_local_savings_runtime_commands_delegate_or_fail_cleanly(monkeypatch, tmp_path) -> None:
    import io

    import lemoncrow_client.cli as cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)
    for command in (["rt", "savings", "status"], ["runtime", "model", "list"], ["rt", "work", "report"]):
        sink = io.StringIO()
        assert cli.main(command, sink) == 2
        output = sink.getvalue()
        assert "not part of serverless Workspaces Lite" in output
        assert "optional managed/full CLI" in output
        assert "unknown runtime command" not in output
        assert f"lc runtime {command[1]}" in output

    executable = tmp_path / "full-lemoncrow"
    executable.write_text("#!/bin/sh\nprintf 'full:%s\\n' \"$*\"\n")
    executable.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_FULL_RUNTIME_CLI", str(executable))

    sink = io.StringIO()
    assert cli.main(["rt", "savings", "status"], sink) == 0
    assert sink.getvalue() == "full:runtime savings status\n"


def test_thin_runtime_index_is_local_lite_and_not_code_subcommand(monkeypatch, tmp_path) -> None:
    import io
    import json
    import subprocess

    import lemoncrow_client.cli as cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "probe.py").write_text("def thin_lite_index_probe():\n    return 1\n", encoding="utf-8")

    sink = io.StringIO()
    assert cli.main(["runtime", "index", "--repo-root", str(repo), "--json"], sink) == 0
    payload = json.loads(sink.getvalue())
    assert payload["index_path"].endswith(".lemoncrow/workspace/runtime-lite-v3.sqlite3")
    assert payload["files_indexed"] == 1

    legacy = io.StringIO()
    assert cli.main(["code", "index"], legacy) == 2
    assert "unknown Hosted code option: index" in legacy.getvalue()
