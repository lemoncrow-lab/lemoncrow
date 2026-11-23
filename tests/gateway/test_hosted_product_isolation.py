from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEW_INSTALLER = ROOT / "scripts" / "hosted.sh"
RUNTIME_INSTALLER = ROOT / "scripts" / "runtime.sh"


def _dry_run(tmp_path: Path, installer: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "LEMONCROW_HOME": str(tmp_path / ".lemoncrow"),
            "LEMONCROW_BIN_DIR": str(tmp_path / ".lemoncrow" / "bin"),
            "LEMONCROW_DRY_RUN": "1",
        }
    )
    return subprocess.run(
        ["bash", str(installer), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_review_install_is_runtime_neutral_by_default(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, REVIEW_INSTALLER)
    assert result.returncode == 0, result.stderr
    assert "Product: LemonCrow Review" in result.stdout
    assert "lemoncrow-review-client.tar.gz" in result.stdout
    assert "Workspaces/agent configuration: none" in result.stdout
    assert "/review/client" in result.stdout


def test_review_install_rejects_runtime_agent_configuration(tmp_path: Path) -> None:
    for option in ("--runtime-agent", "--agent"):
        result = _dry_run(tmp_path, REVIEW_INSTALLER, option, "claude")
        assert result.returncode == 2
        assert "Workspaces is a separate product" in result.stderr
        assert "runtime.sh" in result.stderr


def test_legacy_no_hosts_is_a_review_compatibility_noop(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, REVIEW_INSTALLER, "--no-hosts")
    assert result.returncode == 0, result.stderr
    assert "Workspaces/agent configuration: none" in result.stdout


def test_runtime_install_changes_no_host_configuration_by_default(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, RUNTIME_INSTALLER)
    assert result.returncode == 0, result.stderr
    assert "Product: LemonCrow Workspaces" in result.stdout
    assert "lemoncrow-runtime-client.tar.gz" in result.stdout
    assert "Agent registration: none" in result.stdout
    assert "/runtime/client" in result.stdout


def test_runtime_lite_dry_run_has_no_server_or_endpoint(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, RUNTIME_INSTALLER, "--lite")
    assert result.returncode == 0, result.stderr
    assert "Workspaces mode: lite" in result.stdout
    assert "Workspaces server: none (session-scoped stdio only)" in result.stdout
    assert "Workspaces endpoint:" not in result.stdout


def test_runtime_install_requires_explicit_agent_opt_in(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, RUNTIME_INSTALLER, "--agent", "codex")
    assert result.returncode == 0, result.stderr
    assert "Agent registration: codex" in result.stdout


def test_runtime_install_rejects_unknown_host(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, RUNTIME_INSTALLER, "--agent", "unknown")
    assert result.returncode == 2
    assert "unsupported --agent" in result.stderr


def test_review_and_runtime_installers_persist_separate_product_settings(tmp_path: Path) -> None:
    review = REVIEW_INSTALLER.read_text(encoding="utf-8")
    runtime = RUNTIME_INSTALLER.read_text(encoding="utf-8")

    assert 'target="$STATE_DIR/review/env"' in review
    assert 'target="$STATE_DIR/runtime/env"' in runtime
    assert 'target="$STATE_DIR/env"' not in review
    assert 'target="$STATE_DIR/env"' not in runtime


def test_runtime_loopback_endpoint_installs_runtime_lite(tmp_path: Path) -> None:
    # The local server executes no Runtime tools, so a loopback Runtime
    # endpoint is the pre-Lite way of asking for local Runtime.
    result = _dry_run(tmp_path, RUNTIME_INSTALLER, "--url", "http://127.0.0.1:7420")
    assert result.returncode == 0, result.stderr
    assert "Workspaces mode: lite" in result.stdout


def test_runtime_https_endpoint_resolves_hosted_mode(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, RUNTIME_INSTALLER, "--url", "https://runtime.example")
    assert result.returncode == 0, result.stderr
    assert "Workspaces mode: hosted" in result.stdout


def test_review_loopback_endpoint_resolves_local_mode(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, REVIEW_INSTALLER, "--url", "http://127.0.0.1:7420")
    assert result.returncode == 0, result.stderr
    assert "Review mode: local" in result.stdout


def test_review_https_endpoint_resolves_hosted_mode(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, REVIEW_INSTALLER, "--url", "https://review.example")
    assert result.returncode == 0, result.stderr
    assert "Review mode: hosted" in result.stdout
