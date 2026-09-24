from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local_server.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


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
