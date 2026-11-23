from __future__ import annotations

import io
from unittest import mock

from lemoncrow_client import cli


def test_runtime_delegation_fetches_once_then_runs(tmp_path, monkeypatch):
    exe = tmp_path / "lemoncrow-client"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    calls = []

    def fake_fetch(product, sink):
        calls.append(product)
        monkeypatch.setenv("LEMONCROW_RUNTIME_CLIENT", str(exe))
        return True

    monkeypatch.delenv("LEMONCROW_RUNTIME_CLIENT", raising=False)
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "empty"))
    sink = io.StringIO()
    with mock.patch.object(cli, "_lazy_install_product", fake_fetch):
        assert cli._delegate_external_runtime(["x"], sink) == 0
    assert calls == ["runtime"]


def test_lazy_install_can_be_disabled(monkeypatch):
    monkeypatch.setenv("LEMONCROW_NO_LAZY_INSTALL", "1")
    assert cli._lazy_install_product("runtime", io.StringIO()) is False


def test_review_verb_fetches_then_reexecs(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_review(name, globals=None, locals=None, fromlist=(), level=0):
        if (level == 1 and name == "review") or name == "lemoncrow_client.review":
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    fetched = []
    reexec = []
    monkeypatch.setattr(cli, "_lazy_install_product", lambda product, sink: fetched.append(product) or True)
    monkeypatch.setattr(cli, "_delegate_review_after_install", lambda args, sink: reexec.append(list(args)) or 0)
    monkeypatch.setattr(builtins, "__import__", no_review)
    assert cli.main(["review", "--help"], io.StringIO()) == 0
    assert fetched == ["review"]
    assert reexec == [["--help"]]


def test_review_verb_reports_install_command_when_fetch_disabled(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_review(name, globals=None, locals=None, fromlist=(), level=0):
        if (level == 1 and name == "review") or name == "lemoncrow_client.review":
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_review)
    monkeypatch.setenv("LEMONCROW_NO_LAZY_INSTALL", "1")
    sink = io.StringIO()
    assert cli.main(["review"], sink) == 2
    assert "--product review" in sink.getvalue()


def test_reexec_prefers_installed_bin_lc(tmp_path, monkeypatch):
    lc = tmp_path / "bin" / "lc"
    lc.parent.mkdir()
    lc.write_text("#!/bin/sh\n")
    lc.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_BIN_DIR", str(lc.parent))
    seen = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check=False: seen.append(cmd) or mock.Mock(returncode=0))
    assert cli._delegate_review_after_install(["--x"], io.StringIO()) == 0
    assert seen == [[str(lc), "review", "--x"]]

    lc.unlink()
    monkeypatch.setattr(cli.sys, "argv", ["/fallback/lc"])
    cli._delegate_review_after_install([], io.StringIO())
    assert seen[-1] == ["/fallback/lc", "review"]


def test_review_only_client_delegates_pi_and_index(monkeypatch):
    monkeypatch.setattr(cli, "_module_available", lambda _: False)
    calls = []
    monkeypatch.setattr(cli, "_delegate_external_runtime", lambda args, sink: calls.append(list(args)) or 0)
    assert cli.main(["rt", "host", "status", "--engine", "pi"], io.StringIO()) == 0
    assert cli.main(["rt", "index"], io.StringIO()) == 0
    assert calls == [["runtime", "host", "status", "--engine", "pi"], ["runtime", "index"]]


def _without_workspace_cli(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_workspace(name, globals=None, locals=None, fromlist=(), level=0):
        if (level == 1 and name == "workspace_cli") or name == "lemoncrow_client.workspace_cli":
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_workspace)


def test_review_client_hands_workspace_to_the_installed_runtime(tmp_path, monkeypatch):
    # Both products installed: `lc` is the Review client, and `lc workspace` must run, not ask to install Runtime.
    _without_workspace_cli(monkeypatch)
    monkeypatch.setattr(cli, "_external_runtime_executable", lambda: tmp_path / "lemoncrow-client")
    calls = []
    monkeypatch.setattr(cli, "_delegate_external_runtime", lambda args, sink: calls.append(list(args)) or 0)
    sink = io.StringIO()
    assert cli.main(["workspace", "status"], sink) == 0
    assert calls == [["workspace", "status"]]
    assert sink.getvalue() == ""


def test_review_only_workspace_names_the_add_command_and_fetches_nothing(monkeypatch):
    _without_workspace_cli(monkeypatch)
    monkeypatch.setattr(cli, "_external_runtime_executable", lambda: None)
    monkeypatch.setattr(
        cli, "_lazy_install_product", lambda product, sink: (_ for _ in ()).throw(AssertionError("fetched"))
    )
    sink = io.StringIO()
    assert cli.main(["workspace", "status"], sink) == 2
    assert sink.getvalue().startswith("lc workspace is part of LemonCrow Workspaces. Add it: ")
    assert "--product runtime" in sink.getvalue()


def test_mcp_lazy_install_progress_uses_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_RUNTIME_CLIENT", raising=False)
    monkeypatch.setattr(cli, "_external_runtime_executable", lambda: None)
    sinks = []
    monkeypatch.setattr(cli, "_lazy_install_product", lambda product, sink: sinks.append(sink) or False)
    assert cli._delegate_external_runtime(["runtime", "mcp"], io.StringIO()) == 2
    assert sinks == [cli.sys.stderr]


def test_incomplete_runtime_does_not_delegate_to_itself(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "bin" / "lemoncrow-client"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_RUNTIME_CLIENT", str(runtime))
    monkeypatch.setattr(cli.sys, "executable", str(runtime.parent / "python"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("recursive child")))
    sink = io.StringIO()
    assert cli._delegate_external_runtime(["runtime", "lite-mcp"], sink) == 2
    assert sink.getvalue() == ""
    assert "incomplete" in capsys.readouterr().err


def test_review_only_lite_mcp_delegates_with_host_and_keeps_stdout_clean(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def without_lite(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and name == "lite.mcp":
            raise ModuleNotFoundError("No module named 'lemoncrow_client.lite'")
        return real_import(name, globals, locals, fromlist, level)

    calls = []
    monkeypatch.setattr(builtins, "__import__", without_lite)
    monkeypatch.setattr(cli, "_delegate_external_runtime", lambda args, sink: calls.append(list(args)) or 0)
    sink = io.StringIO()
    assert cli.main(["runtime", "lite-mcp", "--host", "pi"], sink) == 0
    assert calls == [["runtime", "lite-mcp", "--host", "pi"]]
    assert sink.getvalue() == ""
