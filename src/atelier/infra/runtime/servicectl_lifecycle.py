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
from contextlib import redirect_stderr, redirect_stdout, suppress
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

from atelier.core.foundation.store import ContextStore
from atelier.infra.runtime.daemon_units import (
    DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS,
    LAUNCHD_USER_DIR,
    STACK_LABEL,
    STACK_UNIT,
    SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS,
    _is_macos,
)

logger = logging.getLogger(__name__)


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
        "last_external_analytics_runs": state.get("last_external_analytics_runs", []),
        "last_external_analytics_at": state.get("last_external_analytics_at"),
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


def _servicectl_import_sessions(store: ContextStore) -> dict[str, int]:
    """Import host sessions with importer-level timestamp dedup.

    Each importer already skips unchanged sessions by comparing source timestamp
    against the previously imported RawArtifact timestamp.
    """
    from atelier.gateway.hosts.session_parsers.registry import iter_importer_classes

    counts: dict[str, int] = {}
    importers: list[tuple[str, Any]] = [(host, importer_cls(store)) for host, importer_cls in iter_importer_classes()]
    all_imported_ids = []
    for host, importer in importers:
        try:
            # Keep servicectl output machine-readable (`--json`) by swallowing
            # importer progress prints.
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                imported_ids = importer.import_all(force=False)
            counts[host] = len(imported_ids)
            all_imported_ids.extend(imported_ids)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            counts[host] = 0

    # Report aggregated session counts to atelier.beseam.com
    try:
        from atelier.core.service.sync import sync_usage

        sync_usage(store.root, session_ids=all_imported_ids)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        logger.warning(
            "Suppressed exception at cli.py:534",
            exc_info=True,
        )

    return counts


def _normalize_external_analytics_periods(
    periods: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    requested = periods or DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS
    normalized: list[str] = []
    for period in requested:
        if period not in SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS:
            raise ValueError(
                "Unsupported external analytics period "
                f"'{period}'. Choose from: {', '.join(SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS)}"
            )
        if period not in normalized:
            normalized.append(period)
    return tuple(normalized)


def _servicectl_collect_external_analytics(
    store: Any,
    *,
    periods: tuple[str, ...] | list[str],
) -> list[dict[str, Any]]:
    from atelier.gateway.integrations.external_analytics import (
        persist_external_reports,
        run_external_reports,
    )

    persisted: list[dict[str, Any]] = []
    for period in _normalize_external_analytics_periods(periods):
        batch = run_external_reports(tool="all", period=period, cwd=Path.cwd(), include_optimize=True)
        persisted.extend(persist_external_reports(store, batch, source="servicectl"))
    return persisted


def _servicectl_check_and_apply_updates(root: Path) -> bool:
    """Check for git updates and apply them if available.

    Returns True if an update was applied and the process should restart.
    """
    try:
        # 1. Identify project root (where .git is)
        # We look for the install record or traverse up from this file.
        record_path = Path.home() / ".atelier" / "install_dir"
        if record_path.exists():
            project_root = Path(record_path.read_text(encoding="utf-8").strip())
        else:
            # Fallback: traverse up from src/atelier/gateway/cli/app.py
            project_root = Path(__file__).parents[4]

        if not (project_root / ".git").exists():
            return False

        # 2. git fetch
        subprocess.run(["git", "fetch", "--quiet"], cwd=project_root, check=True)

        # 3. Check if behind
        res = subprocess.run(
            ["git", "rev-list", "HEAD..@{u}", "--count"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
        behind_count = int(res.stdout.strip())

        if behind_count == 0:
            return False

        logger.info(f"Auto-update: detected {behind_count} new commits. Pulling...")

        # 4. Pull
        subprocess.run(["git", "pull", "--ff-only", "--quiet"], cwd=project_root, check=True)

        # 5. Check if dependencies changed
        if (project_root / "uv.lock").exists() or (project_root / "pyproject.toml").exists():
            import shutil

            if shutil.which("uv"):
                logger.info("Auto-update: syncing dependencies with uv...")
                subprocess.run(["uv", "sync"], cwd=project_root, check=True)

        # 6. Check if we should restart systemd/launchd managed services
        # If we are running under systemd, we can trigger a restart of the whole stack
        if os.environ.get("INVOCATION_ID"):
            logger.info("Auto-update: update applied (systemd). Triggering stack restart...")
            subprocess.run(["systemctl", "--user", "restart", STACK_UNIT], check=False)
        elif _is_macos() and (LAUNCHD_USER_DIR / f"{STACK_LABEL}.plist").exists():
            logger.info("Auto-update: update applied (launchd). Triggering stack restart...")
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{STACK_LABEL}"], check=False)

        logger.info("Auto-update: update applied successfully. Exiting for restart.")
        return True

    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.error(f"Auto-update failed: {exc}")
        return False


def _servicectl_tick(
    root: Path,
    *,
    maintenance_interval_seconds: int,
    session_import_interval_seconds: int,
    external_analytics_interval_seconds: int,
    external_analytics_periods: tuple[str, ...] | list[str],
    auto_update: bool = False,
    auto_update_interval_seconds: int = 3600,
) -> dict[str, Any]:
    from atelier.core.capabilities.optimization import load_automation_config
    from atelier.core.service.jobs import JOB_CONSOLIDATE_BLOCKS, JOB_OPTIMIZE
    from atelier.core.service.worker import Worker
    from atelier.infra.storage.factory import create_store

    SESSION_IMPORT_KEY = "import_host_sessions"
    EXTERNAL_ANALYTICS_KEY = "external_analytics_reports"

    store = create_store(root)
    store.init()
    worker = Worker(store=store)
    normalized_external_analytics_periods = _normalize_external_analytics_periods(external_analytics_periods)

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
                # Process will exit if update was applied (Restart=always will pick it up)
                sys.exit(0)
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

    last_external_analytics_raw = periodic.get(EXTERNAL_ANALYTICS_KEY)
    last_external_analytics_at: datetime | None = None
    if isinstance(last_external_analytics_raw, str):
        try:
            last_external_analytics_at = datetime.fromisoformat(last_external_analytics_raw)
        except ValueError:
            last_external_analytics_at = None

    if session_import_interval_seconds < 0:
        import_due = False
    elif session_import_interval_seconds == 0 or last_session_import_at is None:
        import_due = True
    else:
        import_due = (now - last_session_import_at).total_seconds() >= session_import_interval_seconds
    imported_sessions: dict[str, int] = {}
    if import_due:
        imported_sessions = _servicectl_import_sessions(store)
        periodic[SESSION_IMPORT_KEY] = now.isoformat()

    if external_analytics_interval_seconds < 0:
        external_analytics_due = False
    elif external_analytics_interval_seconds == 0 or last_external_analytics_at is None:
        external_analytics_due = True
    else:
        external_analytics_due = (
            now - last_external_analytics_at
        ).total_seconds() >= external_analytics_interval_seconds
    external_analytics_runs: list[dict[str, Any]] = []
    if external_analytics_due:
        with suppress(Exception):
            external_analytics_runs = _servicectl_collect_external_analytics(
                store,
                periods=normalized_external_analytics_periods,
            )
        periodic[EXTERNAL_ANALYTICS_KEY] = now.isoformat()

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

    processed: list[str] = []
    while len(processed) < 20:
        job_id = worker.run_once()
        if job_id is None:
            break
        processed.append(job_id)

    payload = {
        "last_tick_at": now.isoformat(),
        "last_processed_jobs": processed,
        "last_enqueued_jobs": enqueued,
        "last_imported_sessions": imported_sessions if import_due else state.get("last_imported_sessions", {}),
        "last_session_import_at": periodic.get(SESSION_IMPORT_KEY),
        "last_external_analytics_runs": (
            external_analytics_runs if external_analytics_due else state.get("last_external_analytics_runs", [])
        ),
        "last_external_analytics_periods": list(normalized_external_analytics_periods),
        "last_external_analytics_at": periodic.get(EXTERNAL_ANALYTICS_KEY),
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
        "external_analytics_runs": external_analytics_runs,
        "external_analytics_periods": list(normalized_external_analytics_periods),
        "external_analytics_ran": external_analytics_due,
        "job_queue_health_before": job_queue_health_before,
        "job_queue_health": job_queue_health,
        "pending_jobs": job_queue_health["active"],
        "tick_at": now.isoformat(),
    }
