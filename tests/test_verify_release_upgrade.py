"""The release upgrade drill (``scripts/verify_release_upgrade.sh``) is isolated from its host.

An isolated ``HOME`` does not isolate the host's service managers: ``systemctl --user``
reaches the login's real user manager, and a candidate install restarted the developer's
own ``lemoncrow-local-server.service`` from inside the drill. Nor does it move the XDG base
directories, which still name the developer's own config, cache, data and state. A fake
previous-release installer probes what every installer sees, then fails so nothing real is
installed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_release_upgrade.sh"
_MANAGERS = ("systemctl", "systemd-run", "launchctl", "loginctl")
_XDG_HOMES = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
_SESSION = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _run_drill(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    calls = tmp_path / "host-manager-calls"
    for tool in _MANAGERS:
        _executable(host_bin / tool, f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\n')
    probe = tmp_path / "probe"
    helper_log = tmp_path / "server-helper"
    installer = tmp_path / "previous-install.sh"
    installer.write_text(f"""#!/usr/bin/env bash
{{
  echo "pwd=$PWD"
  echo "home=$HOME"
  echo "supervisor=${{LEMONCROW_LOCAL_SERVER_SUPERVISOR:-}}"
  echo "port=${{LEMONCROW_LOCAL_SERVER_PORT:-}}"
  for name in {" ".join((*_XDG_HOMES, *_SESSION))}; do
    echo "$name=${{!name-<unset>}}"
  done
  for tool in {" ".join(_MANAGERS)}; do
    "$tool" --user show-environment >/dev/null 2>&1 && echo "$tool=reached" || echo "$tool=refused"
  done
}} > "{probe}"
mkdir -p "$LEMONCROW_INSTALL_DIR/scripts"
printf '#!/usr/bin/env bash\\necho "$1" >> "{helper_log}"\\n' > "$LEMONCROW_INSTALL_DIR/scripts/local_server.sh"
exit 7
""")
    # `curl -fsSL URL -o FILE` hands the drill the fake installer; nothing leaves the box.
    _executable(
        host_bin / "curl",
        f'#!/usr/bin/env bash\nwhile (( $# )); do [[ "$1" == -o ]] && cp "{installer}" "$2"; shift; done\n',
    )
    archive = tmp_path / "candidate.tar.gz"
    archive.write_bytes(b"")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    # The developer's own login: every XDG home and the session bus point at the host.
    developer = tmp_path / "developer"
    env = {
        **os.environ,
        "PATH": f"{host_bin}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(scratch),
        **{name: str(developer / name.lower()) for name in _XDG_HOMES},
        "XDG_RUNTIME_DIR": str(developer / "run"),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={developer / 'run' / 'bus'}",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "v0.0.1", str(archive)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    seen = dict(line.split("=", 1) for line in probe.read_text().splitlines()) if probe.exists() else {}
    return result, seen


def test_no_installer_reaches_the_host_service_manager_or_its_default_port(tmp_path: Path) -> None:
    result, seen = _run_drill(tmp_path)
    assert result.returncode == 7, result.stderr
    assert {tool: seen.get(tool) for tool in _MANAGERS} == dict.fromkeys(_MANAGERS, "refused")
    assert not (tmp_path / "host-manager-calls").exists()
    assert seen["supervisor"] == "none"
    assert seen["port"].isdigit() and int(seen["port"]) != 7420
    work = Path(seen["home"]).parent
    assert work.parent == tmp_path / "tmp" and Path(seen["pwd"]) == work


def test_no_installer_sees_the_developers_xdg_directories_or_session_bus(tmp_path: Path) -> None:
    result, seen = _run_drill(tmp_path)
    assert result.returncode == 7, result.stderr
    work = Path(seen["home"]).parent
    for name in _XDG_HOMES:
        assert Path(seen[name]).is_relative_to(work), (name, seen[name])
    assert len({seen[name] for name in _XDG_HOMES}) == len(_XDG_HOMES)
    assert {name: seen[name] for name in _SESSION} == dict.fromkeys(_SESSION, "<unset>")
    assert not (tmp_path / "developer").exists()


def test_the_drill_stops_the_server_it_installed_and_leaves_nothing_behind(tmp_path: Path) -> None:
    result, _ = _run_drill(tmp_path)
    assert result.returncode == 7, result.stderr
    assert (tmp_path / "server-helper").read_text() == "stop\n"
    assert list((tmp_path / "tmp").iterdir()) == []


def test_candidate_verifier_path_survives_isolated_working_directory() -> None:
    """The drill sources a helper that changes cwd before post-upgrade checks."""
    text = SCRIPT.read_text(encoding="utf-8")
    resolve = 'UPGRADE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    change_cwd = 'source "$UPGRADE_SCRIPT_DIR/release_drill_env.sh"'
    verify = 'python "$UPGRADE_SCRIPT_DIR/verify_release_review_bootstrap.py"'
    assert text.index(resolve) < text.index(change_cwd) < text.index(verify)
