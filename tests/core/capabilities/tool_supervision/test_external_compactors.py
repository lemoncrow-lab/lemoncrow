"""Soft-detection and safe-allowlist matching for external compactors."""

from __future__ import annotations

import subprocess

import pytest

from lemoncrow.pro.capabilities.tool_supervision import external_compactors as ec


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    ec.reset()
    monkeypatch.delenv(ec._ENV_ENABLED, raising=False)
    yield
    ec.reset()


def test_enabled_by_default() -> None:
    assert ec.external_compactors_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "False", "OFF"])
def test_opt_out_via_env(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ec._ENV_ENABLED, value)
    assert ec.external_compactors_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything-else"])
def test_explicit_enable_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ec._ENV_ENABLED, value)
    assert ec.external_compactors_enabled() is True


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["git", "status"], "rtk"),
        (["git", "log", "-n", "5"], "rtk"),
        (["pytest", "-q"], "rtk"),
        (["cargo", "test"], "rtk"),
        (["ruff", "check", "."], "rtk"),
        (["docker", "ps"], "rtk"),
        (["docker", "logs", "web"], "rtk"),
        (["docker", "compose", "ps"], "rtk"),
        (["kubectl", "get", "pods"], "rtk"),
        (["pnpm", "list"], "rtk"),
        (["pip", "list"], "rtk"),
        (["aws", "s3", "ls"], "rtk"),
        (["docker", "rm", "web"], None),
        (["kubectl", "delete", "pod", "x"], None),
        (["aws", "s3", "cp", "a", "b"], None),
        (["pip", "install", "requests"], None),
        (["git", "branch", "foo"], None),  # mutating -- not on the allowlist
        (["git", "commit", "-m", "x"], None),
        (["docker", "run", "x"], None),
        (["prettier", "."], None),  # missing --check -> mutates in place
        ([], None),
    ],
)
def test_compactor_for_command(tokens: list[str], expected: str | None) -> None:
    compactor = ec.compactor_for_command(tokens)
    assert (compactor.name if compactor else None) == expected


def test_resolve_compactor_caches_result(monkeypatch) -> None:
    calls: list[object] = []

    def fake_which(name: str) -> str:
        calls.append(name)
        return "/usr/local/bin/rtk"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout="rtk 1.2.3\n", stderr="")

    monkeypatch.setattr(ec.shutil, "which", fake_which)
    monkeypatch.setattr(ec.subprocess, "run", fake_run)

    first = ec.resolve_compactor("rtk")
    second = ec.resolve_compactor("rtk")

    assert first.available is True
    assert first.version == "rtk 1.2.3"
    assert first == second
    # Probed exactly once -- the second call is served from the process-local cache.
    assert calls.count("rtk") == 1


def test_resolve_compactor_absent_binary(monkeypatch) -> None:
    monkeypatch.setattr(ec.shutil, "which", lambda name: None)
    resolution = ec.resolve_compactor("rtk")
    assert resolution.available is False
    assert "not found" in (resolution.reason or "")


def test_resolve_compactor_version_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(ec.shutil, "which", lambda name: "/usr/local/bin/rtk")
    monkeypatch.setattr(
        ec.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="boom"),
    )
    resolution = ec.resolve_compactor("rtk")
    assert resolution.available is False


def test_resolve_compactor_unknown_name() -> None:
    resolution = ec.resolve_compactor("nonexistent")
    assert resolution.available is False


def test_registered_compactors_include_rtk_with_install_hint() -> None:
    compactors = ec.registered_compactors()
    assert any(c.name == "rtk" and "cargo install" in c.install_hint for c in compactors)
