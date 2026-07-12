"""Regression coverage for installer update-state version capture."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_lc(binary: Path, version: str) -> None:
    binary.write_text(f"#!/usr/bin/env bash\nprintf 'lc, version {version}\\n'\n", encoding="utf-8")
    binary.chmod(0o755)


def test_installer_records_version_before_replacing_cli(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "lc"
    _write_lc(binary, "2.3.4")

    script = """
set -euo pipefail
source "$1/scripts/lib/common.sh"
_capture_install_previous_version
printf '#!/usr/bin/env bash\\nprintf "lc, version 9.9.9\\\\n"\\n' > "$LEMONCROW_BIN_DIR/lc"
chmod +x "$LEMONCROW_BIN_DIR/lc"
_write_install_update_state
"""
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "LEMONCROW_BIN_DIR": str(bin_dir),
    }
    subprocess.run(["bash", "-c", script, "bash", str(_REPO_ROOT)], env=env, check=True)

    state = json.loads((tmp_path / ".lemoncrow" / "update_state.json").read_text(encoding="utf-8"))
    assert state["previous_version"] == "2.3.4"
    assert state["current_version"] == "9.9.9"
    assert state["notified"] is False
