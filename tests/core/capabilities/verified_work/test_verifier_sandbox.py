from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.verified_work import workspace as workspace_mod


@pytest.mark.skipif(not Path("/proc").exists(), reason="the bubblewrap verifier is chosen only where /proc exists")
def test_verifier_uses_bubblewrap_network_namespace_and_review_limits(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setattr(workspace_mod.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

    from lemoncrow.pro.environment import sandbox_limits

    def fake_limit_prefix(*, memory_mb: int, timeout: float, tasks: int):
        captured["limits"] = (memory_mb, timeout, tasks)
        return ["limit-wrapper"]

    monkeypatch.setattr(sandbox_limits, "limit_prefix_for", fake_limit_prefix)

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(workspace_mod.subprocess, "run", fake_run)

    result = workspace_mod.run_exact_verifier(
        "python -m py_compile x.py",
        cwd=root,
        root=root,
        timeout_s=17,
        memory_mb=1536,
    )

    argv = captured["argv"]
    assert argv[0] == "limit-wrapper"
    assert "--unshare-all" in argv
    bind_index = argv.index("--bind")
    assert ["--bind", str(root), str(root)] == argv[bind_index : bind_index + 3]
    # The verifier must not receive a read-only view of the whole host.
    assert ["--ro-bind", "/", "/"] not in [argv[i : i + 3] for i in range(len(argv) - 2)]
    assert captured["limits"] == (1536, 17.0, 128)
    assert "AWS_SECRET_ACCESS_KEY" not in captured["env"]
    assert result.exit_code == 0
    assert result.network_isolated is True
    assert result.resource_limited is True


def test_verifier_reports_when_network_namespace_is_unavailable(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(workspace_mod.shutil, "which", lambda name: None)
    from lemoncrow.pro.environment import sandbox_limits

    monkeypatch.setattr(sandbox_limits, "limit_prefix_for", lambda **kwargs: [])
    monkeypatch.setattr(
        workspace_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "ok", ""),
    )

    result = workspace_mod.run_exact_verifier("true", cwd=root, root=root, timeout_s=5)

    assert result.network_isolated is False
    assert result.resource_limited is False
