"""Servicectl background-loop lifecycle (Phase 25-03, QBL-CLI-03).

PID/state files, status payloads, host-status refresh, host-session import,
external-analytics collection, git auto-update, and the periodic tick loop for
the ``atelier servicectl`` daemon. Moved verbatim from ``gateway/cli/app.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.infra.runtime.daemon_units import (
    LAUNCHD_USER_DIR,
    STACK_LABEL,
    STACK_UNIT,
    _is_macos,
)

logger = logging.getLogger(__name__)

# Prune stale workspace indexes once a day so they cannot silently pile up.
_WORKSPACE_PRUNE_INTERVAL_SECONDS = 86_400
_WORKSPACE_PRUNE_MAX_AGE_DAYS = 30

# Flush the Stop hook's locally-queued public rollup deltas into a single
# aggregated request once a day, so the public counters endpoint sees at
# most one POST per user per day instead of one per Stop-hook firing.
_PUBLIC_ROLLUP_INTERVAL_SECONDS = 86_400


def _servicectl_dir(root: Path) -> Path:
    return Path(root) / "servicectl"


def _servicectl_pid_path(root: Path) -> Path:
    return _servicectl_dir(root) / "servicectl.pid"


def _servicectl_log_path(root: Path) -> Path:
    return _servicectl_dir(root) / "servicectl.log"


def _servicectl_state_path(root: Path) -> Path:
    return _servicectl_dir(root) / "state.json"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_servicectl_state(root: Path) -> dict[str, Any]:
    path = _servicectl_state_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_servicectl_state(root: Path, payload: dict[str, Any]) -> None:
    state_path = _servicectl_state_path(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _read_servicectl_pid(root: Path) -> int | None:
    path = _servicectl_pid_path(root)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _clear_servicectl_pid(root: Path) -> None:
    path = _servicectl_pid_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _kill_orphan_servicectl_processes(current_root: Path) -> None:
    """Kill any servicectl run processes started with a root other than current_root.

    Prevents accumulation of stale daemons pointing at old/project-local stores
    when the canonical root changes (e.g. after moving from project/.atelier to
    ~/.atelier).  Only runs on Linux (requires /proc).
    """
    import glob as _glob

    my_pid = os.getpid()
    current_root_str = str(current_root.resolve())
    for cmdline_file in _glob.glob("/proc/*/cmdline"):
        try:
            pid = int(cmdline_file.split("/")[2])
        except (ValueError, IndexError):
            continue
        if pid == my_pid:
            continue
        try:
            raw = Path(cmdline_file).read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if (
            "atelier.gateway.cli" in cmdline
            and "servicectl" in cmdline
            and " run " in cmdline
            and current_root_str not in cmdline
        ):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)


def _servicectl_status_payload(root: Path) -> dict[str, Any]:
    state = _read_servicectl_state(root)
    pid = _read_servicectl_pid(root)
    running = bool(pid is not None and _pid_is_running(pid))
    from atelier.infra.storage.factory import create_store

    store = create_store(root)
    store.init()
    job_queue_health = store.job_queue_health()
    return {
        "running": running,
        "pid": pid,
        "pid_file": str(_servicectl_pid_path(root)),
        "log_file": str(_servicectl_log_path(root)),
        "state_file": str(_servicectl_state_path(root)),
        "last_tick_at": state.get("last_tick_at"),
        "last_processed_jobs": state.get("last_processed_jobs", []),
        "last_enqueued_jobs": state.get("last_enqueued_jobs", []),
        "last_imported_sessions": state.get("last_imported_sessions", {}),
        "last_session_import_at": state.get("last_session_import_at"),
        "last_exit_reason": state.get("last_exit_reason"),
        "started_at": state.get("started_at"),
        "job_queue_health": job_queue_health,
    }


def _servicectl_refresh_host_status(root: Path) -> dict[str, str]:
    """Detect host agent CLI tools and persist status for the Docker service.

    Writes to ``{root}/hosts/status.json`` in the same format as
    ``scripts/status.sh --write`` so the API running in Docker can
    consume it via ``_load_host_status_file()``.

    Also writes to the CWD's ``.atelier/hosts/status.json`` if different
    from *root* (handles the common case where Docker mounts the project's
    ``.atelier`` while servicectl uses ``~/.atelier``).

    Runs on the host (inside servicectl) so ``shutil.which()`` can
    find the actual CLI binaries.
    """
    import shutil

    hosts = [
        ("claude", "claude"),
        ("codex", "codex"),
        ("opencode", None),
        ("copilot", None),
        ("antigravity", "agy"),
    ]
    status: dict[str, str] = {}
    for hid, check in hosts:
        if check:
            installed = shutil.which(check) is not None
        elif hid == "opencode":
            installed = shutil.which("opencode") is not None
        elif hid == "copilot":
            installed = shutil.which("code") is not None
        elif hid == "antigravity":
            installed = shutil.which("agy") is not None or shutil.which("antigravity") is not None
        else:
            installed = False
        status[hid] = "installed" if installed else "not_installed"

    def _write_to(hosts_dir: Path) -> None:
        hosts_dir.mkdir(parents=True, exist_ok=True)
        (hosts_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

    # Primary: write to servicectl's root
    _write_to(Path(root) / "hosts")

    return status


def _run_tick_subprocess(cmd: list[str], *, timeout: int, what: str) -> subprocess.CompletedProcess[bytes] | None:
    """``subprocess.run`` guarded against timeout/launch failures.

    A tick subprocess (import, recall index, workspace prune) can legitimately
    run long under load. Letting ``TimeoutExpired`` propagate would abort
    ``_servicectl_tick`` before its periodic-job timestamp and state file are
    written, so the same subprocess would re-run and re-time-out on every
    subsequent tick forever, starving recall indexing / pruning / job
    processing behind it. Return ``None`` instead so callers can record the
    attempt (advance the periodic key) and let the tick continue.
    """
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.warning("%s subprocess timed out after %ds", what, timeout)
        return None
    except OSError:
        logging.exception("failed to launch %s subprocess", what)
        return None


def _servicectl_import_sessions(root: Path) -> dict[str, int]:
    """Import host sessions by delegating to the ``atelier import`` CLI subprocess.

    Running import out-of-process keeps JSON parsing, importer-level dedup, and
    the ``sync_usage`` upload out of the daemon's heap. ``sync_usage`` is called
    inside the subprocess, so there is no double-upload.
    """
    result = _run_tick_subprocess(
        [sys.executable, "-m", "atelier.gateway.cli", "--root", str(root), "import", "--json"],
        timeout=300,
        what="session import",
    )
    if result is None:
        return {}
    if result.returncode != 0:
        logging.warning(
            "session import subprocess failed (rc=%d): %s",
            result.returncode,
            result.stderr[-500:].decode("utf-8", errors="replace").strip(),
        )
        return {}
    try:
        data = json.loads(result.stdout)
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:
        logging.exception("failed to parse session import JSON output")
        return {}


def _servicectl_index_recall(root: Path) -> dict[str, int]:
    """Index recent session transcripts via the ``atelier session recall index`` CLI subprocess.

    Keeps embedding work and SQLite writes out of the daemon's heap. The subprocess
    is incremental by default (unchanged sessions are skipped).
    """
    result = _run_tick_subprocess(
        [sys.executable, "-m", "atelier.gateway.cli", "--root", str(root), "session", "recall", "index", "--json"],
        timeout=300,
        what="recall index",
    )
    if result is None:
        return {}
    if result.returncode != 0:
        logging.warning(
            "recall index subprocess failed (rc=%d): %s",
            result.returncode,
            result.stderr[-500:].decode("utf-8", errors="replace").strip(),
        )
        return {}
    try:
        data = json.loads(result.stdout)
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:
        logging.exception("failed to parse recall index JSON output")
        return {}


def _servicectl_prune_workspaces(root: Path, *, max_age_days: int = _WORKSPACE_PRUNE_MAX_AGE_DAYS) -> dict[str, Any]:
    """Remove orphaned / stale workspace indexes via the ``code prune`` CLI subprocess.

    Runs once a day.  Removes orphaned indexes (no ``session_state.json``),
    ``/tmp`` benchmark runs, indexes whose source repo is gone, and — via
    ``--max-age-days`` — indexes inactive for more than ``max_age_days`` days.
    Runs out-of-process to keep the rmtree/walk work off the daemon heap,
    matching the import/recall pattern.
    """
    result = _run_tick_subprocess(
        [
            sys.executable,
            "-m",
            "atelier.gateway.cli",
            "--root",
            str(root),
            "code",
            "prune",
            "--store-root",
            str(root),
            "--max-age-days",
            str(max_age_days),
            "--json",
        ],
        timeout=600,
        what="workspace prune",
    )
    if result is None:
        return {}
    if result.returncode != 0:
        logging.warning(
            "workspace prune subprocess failed (rc=%d): %s",
            result.returncode,
            result.stderr[-500:].decode("utf-8", errors="replace").strip(),
        )
        return {}
    try:
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("failed to parse workspace prune JSON output")
        return {}


def _atelier_version() -> str:
    """Return the installed Atelier version string."""
    try:
        from importlib.metadata import version

        return version("atelier")
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return "0.0.0"


def _git_project_root() -> Path | None:
    """Resolve the git project root from install record or file-path traversal."""
    record_path = Path.home() / ".atelier" / "install_dir"
    if record_path.exists():
        candidate = Path(record_path.read_text(encoding="utf-8").strip())
        if (candidate / ".git").exists():
            return candidate.resolve()
    # Fallback: traverse up from this file
    candidate = Path(__file__).resolve()
    for parent in candidate.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            try:
                content = (parent / "pyproject.toml").read_text("utf-8")
                if 'name = "atelier"' in content:
                    return parent
            except OSError:
                pass
    return None


# Distribution channel -- keep in lockstep with scripts/install.sh and
# src/atelier/gateway/cli/commands/update.py.
_GH_REPO = "atelier-ws/atelier"
_RELEASE_LATEST_URL = f"https://github.com/{_GH_REPO}/releases/latest/download"


def _github_latest_version() -> str | None:
    """Fetch the latest release tag from GitHub Releases (e.g. "0.3.5")."""
    import urllib.request

    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{_GH_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "atelier-update/1.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)  # nosec - pinned GitHub API URL
        data = json.loads(resp.read().decode())
        tag = data.get("tag_name", "")
        return tag.lstrip("v") or None
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return None


def _version_key(version: str) -> tuple[int, ...]:
    """Dotted version -> comparable int tuple (non-numeric chunks count as 0)."""
    import re

    parts: list[int] = []
    for chunk in version.split("."):
        match = re.match(r"\d+", chunk)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def _detect_auto_update_method() -> tuple[str, str | None]:
    """Detect the install method for auto-update.

    Returns ("git", project_root) for a source checkout, or ("release", None)
    for an end-user install, which updates through the GitHub release installer.
    """
    git_root = _git_project_root()
    if git_root is not None:
        return ("git", str(git_root))
    return ("release", None)


# Auto-update always tracks this remote branch, regardless of which local
# branch is currently checked out. Hardcoded to origin/main by request.
_AUTO_UPDATE_REMOTE = "origin"
_AUTO_UPDATE_BRANCH = "main"


def _update_via_git(project_root: str) -> bool:
    """Update from git: fetch origin/main, fast-forward, sync deps.

    Auto-update always tracks ``origin/main`` regardless of the currently
    checked-out local branch. Returns True only if an update was applied.
    """
    project_root_p = Path(project_root)
    remote_ref = f"{_AUTO_UPDATE_REMOTE}/{_AUTO_UPDATE_BRANCH}"

    subprocess.run(
        ["git", "fetch", "--quiet", _AUTO_UPDATE_REMOTE, _AUTO_UPDATE_BRANCH],
        cwd=project_root_p,
        check=True,
    )

    # Bail out cleanly if the tracking ref is missing (e.g. the remote has no
    # ``main``) instead of raising -- keeps the controller quiet on odd setups.
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", remote_ref],
        cwd=project_root_p,
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        logger.info(f"Auto-update: {remote_ref} not found; skipping git update.")
        return False

    show = subprocess.run(
        ["git", "show", f"{remote_ref}:pyproject.toml"],
        cwd=project_root_p,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        logger.info(f"Auto-update: could not read {remote_ref}:pyproject.toml; skipping git update.")
        return False

    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', show.stdout, re.MULTILINE)
    if not match:
        logger.info(f"Auto-update: could not parse version from {remote_ref}:pyproject.toml; skipping git update.")
        return False

    remote_version = match.group(1)
    current_version = _atelier_version()
    if _version_key(remote_version) <= _version_key(current_version):
        logger.info(
            f"Auto-update: remote version {remote_version} is not newer than "
            f"current version {current_version}; skipping git update."
        )
        return False

    res = subprocess.run(
        ["git", "rev-list", f"HEAD..{remote_ref}", "--count"],
        cwd=project_root_p,
        capture_output=True,
        text=True,
        check=True,
    )
    behind_count = int(res.stdout.strip())
    if behind_count == 0:
        return False

    logger.info(f"Auto-update: detected {behind_count} new commits on {remote_ref}. Updating...")

    # Fast-forward only: never clobber local commits. If the checked-out branch
    # has diverged from main it cannot fast-forward -- log and skip rather than
    # raising, so the controller keeps running without error spam.
    merge = subprocess.run(
        ["git", "merge", "--ff-only", "--quiet", remote_ref],
        cwd=project_root_p,
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        logger.warning(
            f"Auto-update: cannot fast-forward to {remote_ref} "
            f"(local branch has diverged); skipping. {merge.stderr.strip()}"
        )
        return False

    if (project_root_p / "uv.lock").exists() or (project_root_p / "pyproject.toml").exists():
        import shutil

        if shutil.which("uv"):
            logger.info("Auto-update: syncing dependencies with uv...")
            subprocess.run(["uv", "sync"], cwd=project_root_p, check=True)
    return True


def _update_via_release() -> bool:
    """Launch a detached installer to reinstall from the latest GitHub release.

    The daemon cannot reinstall itself inline: ``install.sh`` stops running
    atelier processes (this daemon included). So download the published
    ``install.sh`` and run it in a fully detached session -- it outlives this
    process, reinstalls the uv tool from ``atelier-distribution-*.tar.gz``, and
    its own ``run_setup`` restarts the stack on the new code.

    Returns True if an installer was launched (a newer release exists and the
    download succeeded), else False.
    """
    import shutil
    import tempfile
    import urllib.request

    # Enabled by default. Operators can set ATELIER_AUTO_UPDATE_RELEASE=0
    # (or false/no/off) to disable the release installer auto-update path.
    if os.environ.get("ATELIER_AUTO_UPDATE_RELEASE", "").strip().lower() in ("0", "false", "no", "off"):
        logger.info(
            "Auto-update: release auto-update is disabled by ATELIER_AUTO_UPDATE_RELEASE. "
            "Run 'atelier update' manually to update."
        )
        return False
    if not shutil.which("bash"):
        logger.error("Auto-update: bash unavailable; cannot apply release update")
        return False

    latest = _github_latest_version()
    if latest is None:
        logger.error("Auto-update: could not determine latest release version")
        return False
    current = _atelier_version()
    if _version_key(latest) <= _version_key(current):
        return False

    installer_url = f"{_RELEASE_LATEST_URL}/install.sh"
    try:
        fd, tmp_path = tempfile.mkstemp(suffix="-atelier-install.sh")
        with os.fdopen(fd, "wb") as fh, urllib.request.urlopen(installer_url, timeout=30) as resp:  # nosec
            shutil.copyfileobj(resp, fh)
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.error(f"Auto-update: failed to download installer ({installer_url}): {exc}")
        return False

    logger.info(f"Auto-update: launching detached installer ({current} -> {latest})")
    # Fully detached: new session so the installer survives this daemon being
    # stopped by the installer's own process-cleanup, plus its later restart.
    # The wrapper deletes the downloaded script once the installer finishes so it
    # does not accumulate in the temp dir across auto-update cycles.
    subprocess.Popen(
        ["bash", "-c", 'bash "$0"; rm -f "$0"', tmp_path],
        env={**os.environ, "ATELIER_NON_INTERACTIVE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True


def _stack_restart() -> None:
    """Trigger a restart of managed services (systemd or launchd)."""
    if os.environ.get("INVOCATION_ID"):
        logger.info("Auto-update: triggering systemd stack restart...")
        subprocess.run(["systemctl", "--user", "restart", STACK_UNIT], check=False)
    elif _is_macos() and (LAUNCHD_USER_DIR / f"{STACK_LABEL}.plist").exists():
        logger.info("Auto-update: triggering launchd stack restart...")
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{STACK_LABEL}"], check=False)


def _servicectl_check_and_apply_updates(root: Path) -> bool:
    """Check for updates and apply them if available.

    Two install topologies, two behaviours:

    - **git** -- pull + ``uv sync`` inline, write update-state, restart the stack,
      and return True so the caller exits for an immediate restart on new code.
    - **release** -- launch a *detached* installer (see ``_update_via_release``)
      and return False. The installer owns the reinstall and stack restart, so the
      caller must NOT exit here; returning False lets the tick record its check
      timestamp, preventing a relaunch on the next tick before the installer lands.

    Returns True only when the caller should exit for an immediate restart.
    """
    previous_version = _atelier_version()
    method, project_root = _detect_auto_update_method()
    logger.info(f"Auto-update: install method={method}, current version={previous_version}")

    try:
        if method == "git" and project_root:
            if not _update_via_git(project_root):
                logger.info("Auto-update: already up-to-date.")
                return False

            # In-process version is unchanged after a git pull; read the new
            # version from pyproject.toml for the SessionStart notification.
            try:
                import re

                from atelier.core.foundation.update_state import write_update_state

                pyproject = Path(project_root) / "pyproject.toml"
                match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text("utf-8"), re.MULTILINE)
                new_version = match.group(1) if match else previous_version
                write_update_state(
                    previous_version=previous_version,
                    current_version=new_version,
                    method=method,
                    root=root,
                )
            except Exception as exc:
                logging.exception("Recovered from broad exception handler")
                logger.warning(f"Auto-update: failed to write update state: {exc}")

            _stack_restart()
            logger.info("Auto-update: update applied successfully. Exiting for restart.")
            return True

        # release install: the detached installer reinstalls and restarts the
        # stack itself. Never exit the daemon here -- the installer stops it when
        # ready, and returning False records the check timestamp so we don't
        # launch a second installer on the next tick.
        if _update_via_release():
            logger.info("Auto-update: detached installer launched; it will restart the stack.")
        else:
            logger.info("Auto-update: already up-to-date.")
        return False

    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.error(f"Auto-update failed: {exc}")
        return False


def _servicectl_tick(
    root: Path,
    *,
    maintenance_interval_seconds: int,
    session_import_interval_seconds: int,
    auto_update: bool = False,
    auto_update_interval_seconds: int = 3600,
) -> dict[str, Any]:
    from atelier.core.capabilities.optimization import load_automation_config
    from atelier.core.service.jobs import JOB_CONSOLIDATE_BLOCKS, JOB_OPTIMIZE
    from atelier.infra.storage.factory import create_store

    SESSION_IMPORT_KEY = "import_host_sessions"

    store = create_store(root)
    store.init()

    # Refresh host agent detection status for the Docker service
    with suppress(Exception):
        _servicectl_refresh_host_status(root)

    now = datetime.now(UTC)
    state = _read_servicectl_state(root)
    periodic = state.setdefault("periodic_jobs", {})

    # 0. Check for auto-updates
    if auto_update:
        AUTO_UPDATE_KEY = "auto_update_check"
        last_update_raw = periodic.get(AUTO_UPDATE_KEY)
        last_update_at: datetime | None = None
        if isinstance(last_update_raw, str):
            try:
                last_update_at = datetime.fromisoformat(last_update_raw)
            except ValueError:
                last_update_at = None

        if last_update_at is None or (now - last_update_at).total_seconds() >= auto_update_interval_seconds:
            if _servicectl_check_and_apply_updates(root):
                # Exit 3 = restart-needed signal.  When running as a tick subprocess
                # the parent ``servicectl run`` loop detects code 3 and also exits,
                # letting systemd restart the whole controller with the new code.
                sys.exit(3)
            periodic[AUTO_UPDATE_KEY] = now.isoformat()

    def _periodic_timestamp(key: str) -> datetime | None:
        raw = periodic.get(key)
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    last_enqueue_at = _periodic_timestamp(JOB_CONSOLIDATE_BLOCKS)
    last_optimize_enqueue_at = _periodic_timestamp(JOB_OPTIMIZE)

    last_session_import_raw = periodic.get(SESSION_IMPORT_KEY)
    last_session_import_at: datetime | None = None
    if isinstance(last_session_import_raw, str):
        try:
            last_session_import_at = datetime.fromisoformat(last_session_import_raw)
        except ValueError:
            last_session_import_at = None

    if session_import_interval_seconds < 0:
        import_due = False
    elif session_import_interval_seconds == 0 or last_session_import_at is None:
        import_due = True
    else:
        import_due = (now - last_session_import_at).total_seconds() >= session_import_interval_seconds
    imported_sessions: dict[str, int] = {}
    if import_due:
        imported_sessions = _servicectl_import_sessions(root)
        periodic[SESSION_IMPORT_KEY] = now.isoformat()

    # Recall indexing (semantic past-session recall) runs on the maintenance cadence.
    RECALL_INDEX_KEY = "index_recall_sessions"
    last_recall_index_at = _periodic_timestamp(RECALL_INDEX_KEY)
    if maintenance_interval_seconds <= 0 or last_recall_index_at is None:
        recall_index_due = True
    else:
        recall_index_due = (now - last_recall_index_at).total_seconds() >= maintenance_interval_seconds
    indexed_recall: dict[str, int] = {}
    if recall_index_due:
        indexed_recall = _servicectl_index_recall(root)
        periodic[RECALL_INDEX_KEY] = now.isoformat()

    WORKSPACE_PRUNE_KEY = "prune_workspaces"
    last_workspace_prune_at = _periodic_timestamp(WORKSPACE_PRUNE_KEY)
    workspace_prune_due = (
        last_workspace_prune_at is None
        or (now - last_workspace_prune_at).total_seconds() >= _WORKSPACE_PRUNE_INTERVAL_SECONDS
    )
    pruned_workspaces: dict[str, Any] = {}
    if workspace_prune_due:
        pruned_workspaces = _servicectl_prune_workspaces(root)
        periodic[WORKSPACE_PRUNE_KEY] = now.isoformat()

    PUBLIC_ROLLUP_KEY = "public_rollup"
    last_public_rollup_at = _periodic_timestamp(PUBLIC_ROLLUP_KEY)
    public_rollup_due = (
        last_public_rollup_at is None
        or (now - last_public_rollup_at).total_seconds() >= _PUBLIC_ROLLUP_INTERVAL_SECONDS
    )
    public_rollup_checkpoint_day = state.get("public_rollup_checkpoint_day")
    if public_rollup_due:
        with suppress(Exception):
            from atelier.core.service.telemetry.public_rollup import flush_daily_public_rollup

            _, public_rollup_checkpoint_day = flush_daily_public_rollup(
                root, checkpoint_day=public_rollup_checkpoint_day
            )
        periodic[PUBLIC_ROLLUP_KEY] = now.isoformat()

    job_queue_health_before = store.job_queue_health()
    enqueued: list[str] = []
    if maintenance_interval_seconds <= 0 or last_enqueue_at is None:
        due = True
    else:
        due = (now - last_enqueue_at).total_seconds() >= maintenance_interval_seconds

    if due:
        active_jobs = [
            job
            for job in store.list_jobs(job_type=JOB_CONSOLIDATE_BLOCKS, limit=200)
            if job["status"] in {"pending", "running", "failed"}
        ]
        if not active_jobs:
            job_id = store.enqueue_job(
                JOB_CONSOLIDATE_BLOCKS,
                {"dry_run": False, "source": "servicectl"},
            )
            enqueued.append(job_id)
            periodic[JOB_CONSOLIDATE_BLOCKS] = now.isoformat()

    automation = load_automation_config(root)
    if automation.enabled:
        if maintenance_interval_seconds <= 0 or last_optimize_enqueue_at is None:
            optimize_due = True
        else:
            optimize_due = (now - last_optimize_enqueue_at).total_seconds() >= maintenance_interval_seconds
        if optimize_due:
            active_optimize_jobs = [
                job
                for job in store.list_jobs(job_type=JOB_OPTIMIZE, limit=200)
                if job["status"] in {"pending", "running", "failed"}
            ]
            if not active_optimize_jobs:
                job_id = store.enqueue_job(
                    JOB_OPTIMIZE,
                    {"days": 7, "host": None, "source": "servicectl"},
                )
                enqueued.append(job_id)
                periodic[JOB_OPTIMIZE] = now.isoformat()

    # Process queued jobs in subprocesses so heavy handlers (consolidation,
    # optimization) keep their LLM heap out of the daemon. Each subprocess
    # claims one job atomically, does the work, and exits.
    processed: list[str] = []
    while len(processed) < 20:
        try:
            job_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "atelier.gateway.cli",
                    "--root",
                    str(root),
                    "worker",
                    "run-once",
                    "--json",
                ],
                capture_output=True,
                timeout=600,
            )
            if job_result.returncode != 0:
                logging.warning(
                    "worker run-once subprocess failed (rc=%d): %s",
                    job_result.returncode,
                    job_result.stderr[-300:].decode("utf-8", errors="replace").strip(),
                )
                break
            data = json.loads(job_result.stdout)
            job_id = data.get("job_id")
            if not data.get("processed") or job_id is None:
                break  # queue empty
            processed.append(str(job_id))
        except Exception:
            logging.exception("worker run-once subprocess error")
            break

    payload = {
        "last_tick_at": now.isoformat(),
        "last_processed_jobs": processed,
        "last_enqueued_jobs": enqueued,
        "last_imported_sessions": imported_sessions if import_due else state.get("last_imported_sessions", {}),
        "last_session_import_at": periodic.get(SESSION_IMPORT_KEY),
        "last_indexed_recall": indexed_recall if recall_index_due else state.get("last_indexed_recall", {}),
        "last_recall_index_at": periodic.get(RECALL_INDEX_KEY),
        "last_pruned_workspaces": (
            pruned_workspaces if workspace_prune_due else state.get("last_pruned_workspaces", {})
        ),
        "last_workspace_prune_at": periodic.get(WORKSPACE_PRUNE_KEY),
        "public_rollup_checkpoint_day": public_rollup_checkpoint_day,
        "last_exit_reason": state.get("last_exit_reason"),
        "periodic_jobs": periodic,
        "started_at": state.get("started_at"),
        "job_queue_health": store.job_queue_health(),
    }
    _write_servicectl_state(root, payload)
    job_queue_health = payload["job_queue_health"]
    return {
        "enqueued_jobs": enqueued,
        "processed_jobs": processed,
        "imported_sessions": imported_sessions,
        "session_import_ran": import_due,
        "indexed_recall": indexed_recall,
        "recall_index_ran": recall_index_due,
        "pruned_workspaces": pruned_workspaces,
        "workspace_prune_ran": workspace_prune_due,
        "job_queue_health_before": job_queue_health_before,
        "job_queue_health": job_queue_health,
        "pending_jobs": job_queue_health["active"],
        "tick_at": now.isoformat(),
    }
