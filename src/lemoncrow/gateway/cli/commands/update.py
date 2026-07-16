"""``lc update`` — manually check for and apply LemonCrow updates.

LemonCrow is distributed in exactly two ways, so updates have exactly two paths:

1. **Git checkout** — ``git pull --ff-only`` + ``uv sync``. The checkout's
   ``.venv`` is the runtime, so syncing it is the update.
2. **Release install** — end users install via the GitHub-release ``install.sh``
   (which downloads ``lemoncrow-distribution-<os>-<arch>.tar.gz`` and reinstalls
   the uv tool). Updating re-runs that same installer for the latest release, so
   the update channel can never drift from the install channel.

There is deliberately no PyPI path: LemonCrow is not published to PyPI. The sole
distribution channel is GitHub Releases.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import click

from lemoncrow import __version__ as current_version
from lemoncrow.core.foundation.update_state import write_update_state

# Single source of truth for the distribution channel. Keep these in lockstep
# with scripts/install.sh and .github/workflows/release.yml.
_GH_REPO = "lemoncrow-lab/lemoncrow"
_RELEASE_LATEST_URL = f"https://github.com/{_GH_REPO}/releases/latest/download"
_INSTALLER_ASSET = "install.sh"

# ---------------------------------------------------------------------------
# Install-method detection
# ---------------------------------------------------------------------------


def _git_project_root() -> Path | None:
    """Resolve the git project root, if installed from a git checkout."""
    # 1. Check install record written by local.sh
    record = Path.home() / ".lemoncrow" / "install_dir"
    if record.exists():
        candidate = Path(record.read_text("utf-8").strip())
        if (candidate / ".git").exists():
            return candidate.resolve()
    # 2. Walk up from this file (dev install without record)
    candidate = Path(__file__).resolve()
    for parent in candidate.parents:
        if (parent / ".git").exists():
            # Verify it's the lemoncrow repo, not an unrelated project
            if (parent / "pyproject.toml").exists():
                try:
                    content = (parent / "pyproject.toml").read_text("utf-8")
                    if 'name = "lemoncrow"' in content:
                        return parent
                except OSError:
                    pass
    return None


def _detect_method() -> tuple[str, str | None]:
    """Detect the install method.

    Returns ``("git", project_root)`` for a source checkout, or
    ``("release", None)`` for any end-user install (uv tool / binary), which all
    update through the GitHub release installer.
    """
    git_root = _git_project_root()
    if git_root is not None:
        return ("git", str(git_root))
    return ("release", None)


# ---------------------------------------------------------------------------
# Remote-version lookup
# ---------------------------------------------------------------------------


def _github_latest_version() -> str | None:
    """Fetch the latest release tag from GitHub Releases (e.g. "0.3.5")."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{_GH_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "lemoncrow-update/1.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)  # nosec - pinned GitHub API URL
        data = json.loads(resp.read().decode())
        tag = data.get("tag_name", "")
        return tag.lstrip("v") or None
    except Exception:  # noqa: BLE001
        return None


def _git_remote_version(project_root: str) -> str | None:
    """Read the upstream package version from ``origin/main:pyproject.toml``."""
    try:
        fetch = subprocess.run(
            ["git", "fetch", "--quiet", "origin"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if fetch.returncode != 0:
            return None
        show = subprocess.run(
            ["git", "show", "origin/main:pyproject.toml"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        match = re.search(r'^version\s*=\s*"([^"]+)"', show.stdout, re.MULTILINE)
        return match.group(1) if match else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _version_key(version: str) -> tuple[int, ...] | None:
    """Dotted version → comparable int tuple, or None when nothing numeric parses."""
    parts: list[int] = []
    any_numeric = False
    for chunk in version.split("."):
        match = re.match(r"\d+", chunk)
        if match:
            any_numeric = True
            parts.append(int(match.group()))
        else:
            parts.append(0)
    return tuple(parts) if any_numeric else None


# ---------------------------------------------------------------------------
# Update application per method
# ---------------------------------------------------------------------------


def _update_git(project_root: str) -> bool:
    """Update from git: fetch, pull, sync."""
    click.echo("  ◆ Git checkout detected — pulling latest...")
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin"],
            cwd=project_root,
            check=True,
            timeout=30,
        )
        result = subprocess.run(
            ["git", "rev-list", "HEAD..@{u}", "--count"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        behind = int(result.stdout.strip())
        if behind == 0:
            click.echo("  ✓ Already up-to-date.")
            return False

        click.echo(f"  ◇ {behind} new commits behind. Pulling...")
        subprocess.run(
            ["git", "pull", "--ff-only", "--quiet", "origin"],
            cwd=project_root,
            check=True,
            timeout=60,
        )

        # Sync dependencies
        if shutil.which("uv"):
            click.echo("  ◇ Syncing dependencies with uv...")
            subprocess.run(
                ["uv", "sync"],
                cwd=project_root,
                check=True,
                timeout=120,
            )
        else:
            click.echo("  ◇ Reinstalling package...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", project_root],
                cwd=project_root,
                check=True,
                timeout=120,
            )

        click.echo("  ✓ Update applied successfully.")
        return True
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"git update failed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException(f"git update timed out: {exc}") from exc


def _update_via_release() -> bool:
    """Re-run the published GitHub-release ``install.sh`` for the latest release.

    This is the exact flow end users installed with, so the binary the MCP
    server runs is rebuilt from the same artifact — no channel drift, no stale
    uv-tool snapshot left behind.
    """
    if not shutil.which("bash"):
        raise click.ClickException("bash is required to apply the update.")

    installer_url = f"{_RELEASE_LATEST_URL}/{_INSTALLER_ASSET}"
    click.echo("  ◆ Release install detected — re-running the published installer...")

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", suffix="-lemoncrow-install.sh", delete=False) as tmp:
            tmp_path = tmp.name
            with urllib.request.urlopen(installer_url, timeout=30) as resp:  # nosec - pinned GitHub release URL
                shutil.copyfileobj(resp, tmp)
    except urllib.error.URLError as exc:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        raise click.ClickException(f"Could not download installer ({installer_url}): {exc}") from exc

    try:
        # Non-interactive so the update refreshes the binary and host
        # integrations without re-prompting the install wizard.
        env = {**os.environ, "LEMONCROW_NON_INTERACTIVE": "1"}
        result = subprocess.run(["bash", tmp_path], env=env, timeout=1800)
        if result.returncode != 0:
            raise click.ClickException(f"Installer exited with status {result.returncode}.")
        click.echo("  ✓ Update applied successfully.")
        return True
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException(f"Update timed out: {exc}") from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _reconcile_companions(project_root: str) -> None:
    """Reconcile companion binaries (Node/Go/Zoekt) to the release pins.

    Reuses the installer's reconcile (``scripts/reconcile.sh``) so the drift
    logic lives in one place: a binary is rebuilt only when its pin in
    ``scripts/lib/versions.sh`` changed from what was last installed. The Zoekt
    build is detached by the installer, so this returns quickly. Best-effort —
    a failure here never fails the update.
    """
    script = Path(project_root) / "scripts" / "reconcile.sh"
    if not script.is_file() or shutil.which("bash") is None:
        return
    click.echo("  ◇ Reconciling companion binaries (Node/Go/Zoekt) to release pins...")
    try:
        subprocess.run(
            ["bash", str(script)],
            cwd=project_root,
            env={**os.environ, "LEMONCROW_NON_INTERACTIVE": "1"},
            timeout=300,
        )
    except (subprocess.SubprocessError, OSError):
        pass


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("update")
@click.option("--check", "check_only", is_flag=True, help="Only check for updates, do not apply.")
@click.option("--force", "force_update", is_flag=True, help="Reinstall even if same version.")
@click.option("--json", "as_json", is_flag=True, help="Output JSON (requires --check).")
@click.pass_context
def update_cmd(ctx: click.Context, check_only: bool, force_update: bool, as_json: bool) -> None:
    """Check for and apply LemonCrow updates.

    Detects your install method (git checkout or GitHub-release install) and
    runs the matching upgrade.

    With --check: exits 0 when up-to-date, 1 when an update is available.
    """
    if as_json and not check_only:
        raise click.UsageError("--json requires --check")
    root: Path = ctx.obj.get("root", Path.home() / ".lemoncrow")

    # 1. Detect install method
    method, project_root = _detect_method()
    if not as_json:
        click.echo(f"  Current version: {current_version}")
        click.echo(f"  Install method:  {method}")

    # 2. Check remote version
    if method == "git" and project_root:
        remote_version = _git_remote_version(project_root)
    else:
        remote_version = _github_latest_version()

    if remote_version is None:
        raise click.ClickException("Could not determine latest available version. Check your internet connection.")

    if not as_json:
        click.echo(f"  Remote version:  {remote_version}")

    remote_key = _version_key(remote_version)
    if remote_key is None:
        # An unparseable remote tag must never compare as 0.0 (→ false "up-to-date").
        raise click.ClickException(f"Could not parse remote version {remote_version!r}.")

    # 3. Compare (ordered — a local checkout ahead of the remote is not an update)
    local_key = _version_key(current_version) or ()
    update_available = remote_key > local_key

    if check_only:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "current_version": current_version,
                        "remote_version": remote_version,
                        "method": method,
                        "update_available": update_available,
                    }
                )
            )
        elif update_available:
            click.echo(f"\n  ◇ Update available: {current_version} → {remote_version}")
            click.echo("  ◇ Run `lc update` to apply.")
        else:
            click.echo("\n  ✓ Already up-to-date.")
        if update_available:
            ctx.exit(1)
        return

    if not update_available and not force_update:
        click.echo("\n  ✓ Already up-to-date.")
        return

    # 4. Apply
    click.echo(f"\n  ◆ Updating {current_version} → {remote_version} ({method} install)...")
    previous = current_version

    if method == "git":
        assert project_root is not None, "git method requires project_root"
        applied = _update_git(project_root)
    else:
        applied = _update_via_release()

    if applied:
        # The running process still holds the old code; report the target
        # version (a restart picks up the new one).
        write_update_state(
            previous_version=previous,
            current_version=remote_version,
            method=method,
            root=root,
        )
        if method == "git" and project_root:
            _reconcile_companions(project_root)
        click.echo(f"\n  ◆ Updated from {previous} → {remote_version}")
        click.echo("  ◆ Restart the MCP server or hooks to pick up changes.")
    else:
        click.echo("\n  ✓ Already up-to-date.")
