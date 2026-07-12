from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install_codex.sh"
DEFAULT_AGENT_INSTRUCTIONS = ROOT / "integrations" / "AGENTS.lemoncrow.md"


def _run_without_codex(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    dirname = shutil.which("dirname")
    assert bash is not None
    assert dirname is not None

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    os.symlink(dirname, fake_bin / "dirname")

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": str(fake_bin)})
    return subprocess.run(
        [bash, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_codex_print_only_is_side_effect_free_without_cli(tmp_path: Path) -> None:
    result = _run_without_codex(tmp_path, "--print-only")

    assert result.returncode == 0, f"stdout: {result.stdout}, stderr: {result.stderr}"
    assert "Manual Install Steps" in result.stdout
    assert not any((tmp_path / "home").iterdir())


def test_codex_missing_cli_skips_before_staging(tmp_path: Path) -> None:
    result = _run_without_codex(tmp_path)

    assert result.returncode == 0, f"stdout: {result.stdout}, stderr: {result.stderr}"
    assert "=== SKIPPED (codex CLI absent) ===" in result.stdout
    assert not any((tmp_path / "home").iterdir())


def test_codex_installer_avoids_gnu_only_readlink_flag() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "readlink -f" not in content
    assert "resolve_real_path" in content


def test_workspace_codex_commands_keep_user_codex_home() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert '(cd "$WORKSPACE" && codex "$@")' in content
    assert 'CODEX_HOME="$CODEX_HOME" codex' not in content
    assert 'CODEX_HOME="$CODEX_DIR" codex' not in content
    assert 'USER_CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"' in content


def test_codex_installer_uses_current_agent_discovery_and_restart_safe_plugins() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "write_codex_agents" in content
    assert "config = write_workspace_codex_agent_config(" not in content
    assert "config = write_codex_agent_config(" not in content
    assert '"installation": "INSTALLED_BY_DEFAULT"' in content
    assert "Codex config missing plugin entry" not in content
    assert "plugin activation will complete after Codex restart" in content


def test_codex_default_session_uses_lemoncrow_code_instructions() -> None:
    content = DEFAULT_AGENT_INSTRUCTIONS.read_text(encoding="utf-8")

    assert "lemoncrow:code" in content
    installer = SCRIPT.read_text(encoding="utf-8")
    assert "integrations/AGENTS.lemoncrow.md" in installer
    assert 'merge_agents_file "${LEMONCROW_REPO}/integrations/AGENTS.lemoncrow.md" "$AGENTS_FILE"' in installer
