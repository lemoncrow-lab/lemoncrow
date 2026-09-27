from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local_server.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_process_start_does_not_accept_health_from_an_unrelated_server(tmp_path: Path) -> None:
    """A stale 200 on the target port cannot bless a failed fallback child."""
    home = tmp_path / "home"
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("ok", encoding="utf-8")
    (frontend / "assets").mkdir()

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  -c) exit 0 ;;
  -) exit 0 ;; # Simulate an unrelated healthy process already answering /healthz.
  -m) sleep 0.2; exit 1 ;;
  *) exit 0 ;;
esac
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
            "LEMONCROW_LOCAL_SERVER_START_TIMEOUT": "1",
            "LEMONCROW_LOCAL_SERVER_PORT": "17499",
            "LEMONCROW_FRONTEND_DIR": str(frontend),
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "did not become healthy" in result.stderr


def test_stop_keeps_fallback_pid_until_process_cleanup(tmp_path: Path) -> None:
    """Supervisor cleanup must not erase the fallback PID before it is stopped."""
    home = tmp_path / "home"
    state = home / ".lemoncrow" / "server"
    state.mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")

    process = subprocess.Popen(["bash", "-c", "exec -a lemoncrow_server_core sleep 60"])
    try:
        (state / "server.pid").write_text(f"{process.pid}\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "LEMONCROW_HOME": str(home / ".lemoncrow"),
                "LEMONCROW_LOCAL_SERVER_DIR": str(state),
                "LEMONCROW_SERVER_PYTHON": "/bin/true",
                "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT), "stop"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        process.wait(timeout=2)
        assert not (state / "server.pid").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_systemd_restart_is_handed_off_without_stopping_own_unit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
set -euo pipefail
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        f"""#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> {str(log)!r}
if [[ "$*" == *"show-environment"* ]]; then exit 0; fi
if [[ "$*" == *"MainPID"* ]]; then echo 4242; exit 0; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemd-run",
        f"""#!/usr/bin/env bash
printf 'systemd-run %s\\n' "$*" >> {str(log)!r}
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "systemd",
            "LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF": "0",
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "restart"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    calls = log.read_text(encoding="utf-8")
    assert "systemd-run " in calls
    assert "_restart-worker systemd" in calls
    assert "systemctl --user stop " not in calls
    assert "systemctl --user disable " not in calls
    assert "restart handed off" in result.stdout

    status = home / ".lemoncrow" / "server" / "restart.status"
    assert status.read_text(encoding="utf-8").startswith("scheduled\t")


def test_launchd_start_reconciles_gateway_and_uses_machine_auth(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    python_log = tmp_path / "python.log"

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        f"""#!/usr/bin/env bash
set -euo pipefail
case "${{1:-}}" in
  -c) exit 0 ;;
  -)
    if [[ "$#" == "1" ]]; then
      printf 'repair url=%s mode=%s home=%s\\n' "${{LEMONCROW_URL:-}}" "${{LEMONCROW_INSTALL_MODE:-}}" "${{LEMONCROW_HOME:-}}" >> {str(python_log)!r}
    fi
    exit 0
    ;;
  *) exit 0 ;;
esac
""",
    )
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n")
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        fake_bin / "launchctl",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'launchctl %s\\n' "$*" >> {str(launchctl_log)!r}
if [[ "${{1:-}}" == "list" ]]; then
  printf '{{\\n    "PID" = 4242;\\n}}\\n'
fi
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "launchd",
            "LEMONCROW_LOCAL_SERVER_PORT": "7438",
            "LEMONCROW_URL": "http://127.0.0.1:7999",
        }
    )

    started = subprocess.run(
        ["bash", str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "ready at http://127.0.0.1:7438" in started.stdout
    plist = home / "Library" / "LaunchAgents" / "com.lemoncrow.local-server.plist"
    content = plist.read_text(encoding="utf-8")
    assert "<string>--token-file</string>" in content
    assert f"<string>{home / '.lemoncrow' / 'token'}</string>" in content
    assert "<string>7438</string>" in content
    assert "$(seq " not in SCRIPT.read_text(encoding="utf-8")
    assert python_log.read_text(encoding="utf-8").strip() == (
        f"repair url=http://127.0.0.1:7438 mode=local home={home / '.lemoncrow'}"
    )
    calls = launchctl_log.read_text(encoding="utf-8")
    assert f"launchctl load {plist}" in calls

    status = subprocess.run(
        ["bash", str(SCRIPT), "status"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "supervisor=launchd" in status.stdout
    assert "pid=4242" in status.stdout

    subprocess.run(
        ["bash", str(SCRIPT), "stop"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not plist.exists()


def test_start_reconciles_gateway_to_the_backend_it_actually_started(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "python.log"

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "-" && "$#" == "1" ]]; then
  printf 'repair url=%s mode=%s home=%s\\n' "${{LEMONCROW_URL:-}}" "${{LEMONCROW_INSTALL_MODE:-}}" "${{LEMONCROW_HOME:-}}" >> {str(calls)!r}
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"show-environment"* ]]; then exit 0; fi
if [[ "$*" == *"MainPID"* ]]; then echo 4242; exit 0; fi
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "systemd",
            "LEMONCROW_LOCAL_SERVER_PORT": "7437",
            # This is the regression: a caller's stale target must not win over
            # the backend this lifecycle invocation just started.
            "LEMONCROW_URL": "http://127.0.0.1:7999",
        }
    )

    subprocess.run(
        ["bash", str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert calls.read_text(encoding="utf-8").strip() == (
        f"repair url=http://127.0.0.1:7437 mode=local home={home / '.lemoncrow'}"
    )
    target = (home / ".lemoncrow" / "env").read_text(encoding="utf-8")
    assert "LEMONCROW_URL=http://127.0.0.1:7437" in target
