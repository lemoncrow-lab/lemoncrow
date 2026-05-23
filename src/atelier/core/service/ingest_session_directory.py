"""Service for watching and ingesting session directories."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

from atelier.core.foundation.paths import default_store_root
from atelier.gateway.integrations.ledger_reconstructor import LedgerReconstructor
from atelier.infra.runtime.run_ledger import RunLedger
from atelier.infra.storage.factory import make_memory_store

logger = logging.getLogger(__name__)


def _hash_content(content: str) -> str:
    """Generate a short hash for content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _get_session_id(file_path: Path, content: str) -> str:
    """Extract or generate a session ID for a file."""
    # Try to get session ID from filename (without extension)
    stem = file_path.stem
    if stem and len(stem) >= 16 and all(c in "0123456789abcdefABCDEF" for c in stem):
        return stem

    # Fall back to content hash
    return f"session_{_hash_content(content)}"


def ingest_session_file(file_path: str, store: Any = None) -> dict[str, Any]:
    """Ingest a single session file."""
    if store is None:
        store_root = default_store_root()
        store = make_memory_store(store_root)
    else:
        store_root = Path(getattr(store, "root", default_store_root())).resolve()

    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    try:
        raw_content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "message": f"Failed to read file: {exc}"}

    session_id = _get_session_id(path, raw_content)
    reconstructor = LedgerReconstructor(root=store_root)
    try:
        ledger: RunLedger = reconstructor.reconstruct(
            source="unknown",
            session_id=session_id,
            raw_content=raw_content,
        )
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "status": "error",
            "message": f"Failed to reconstruct ledger from session file: {exc}",
        }

    # TODO: Store the ledger events as traces in the store.
    # For now, we just return a success message with metadata.
    return {
        "status": "success",
        "session_id": session_id,
        "event_count": len(ledger.events),
        "ledger": ledger,  # For debugging; remove in production.
    }


class SessionDirectoryWatcher:
    """Watches a directory for new or modified session files and ingests them."""

    def __init__(self, directory_path: str, store: Any = None, poll_interval: float = 5.0):
        self.directory_path = directory_path
        self.store = store
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen_files: dict[Path, float] = {}

        if self.store is None:
            store_root = default_store_root()
            self.store = make_memory_store(store_root)

        self.directory = Path(directory_path)
        if not self.directory.exists() or not self.directory.is_dir():
            raise ValueError(f"Directory does not exist or is not a directory: {directory_path}")

    def start(self) -> None:
        """Start the directory watcher in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Directory watcher is already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(
            "Started directory watcher for: %s (poll interval: %ss)",
            self.directory_path,
            self.poll_interval,
        )

    def stop(self) -> None:
        """Stop the directory watcher."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        logger.info("Stopped directory watcher for: %s", self.directory_path)

    def _run(self) -> None:
        """Main watcher loop."""
        logger.info("Directory watcher loop started for: %s", self.directory_path)

        try:
            while not self._stop_event.is_set():
                try:
                    # Scan for .jsonl files in the directory
                    for file_path in self.directory.glob("*.jsonl"):
                        try:
                            mtime = file_path.stat().st_mtime
                            # If we haven't seen this file or it's been modified
                            if file_path not in self._seen_files or self._seen_files[file_path] < mtime:
                                logger.info("Detected new or modified session file: %s", file_path)
                                result = ingest_session_file(str(file_path), self.store)
                                if result.get("status") == "success":
                                    logger.info(
                                        "Successfully ingested session file: %s (session_id: %s, events: %d)",
                                        file_path,
                                        result.get("session_id"),
                                        result.get("event_count", 0),
                                    )
                                else:
                                    logger.error(
                                        "Failed to ingest session file %s: %s",
                                        file_path,
                                        result.get("message", "Unknown error"),
                                    )
                                self._seen_files[file_path] = mtime
                        except OSError as exc:
                            logger.error("Error accessing file %s: %s", file_path, exc)

                    # Remove entries for files that no longer exist
                    self._seen_files = {
                        path: mtime for path, mtime in self._seen_files.items() if path.exists()
                    }

                    # Wait for the next poll interval or until stopped
                    self._stop_event.wait(self.poll_interval)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.error("Unexpected error in directory watcher loop: %s", exc)
                    time.sleep(self.poll_interval)  # Wait before retrying
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Directory watcher failed: %s", exc)
        finally:
            logger.info("Directory watcher loop ended for: %s", self.directory_path)


def ingest_session_directory(directory_path: str, store: Any = None, poll_interval: float = 5.0) -> dict[str, Any]:
    """Start watching a directory for new or modified session files and ingest them.

    This function returns immediately after starting the watcher in a background thread.

    Args:
        directory_path: Path to the directory to watch for session files.
        store: Optional store instance. If not provided, the default store is used.
        poll_interval: Seconds to wait between directory scans.

    Returns:
        A dictionary with the result of starting the watcher.
    """
    try:
        watcher = SessionDirectoryWatcher(directory_path, store, poll_interval)
        watcher.start()
        return {
            "status": "success",
            "message": f"Directory watcher started for {directory_path}",
            "watcher": watcher,  # Return the watcher so caller can stop it later if needed
        }
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to start directory watcher: %s", exc)
        return {
            "status": "error",
            "message": f"Failed to start directory watcher: {exc}",
        }


def ingest_session_directory_blocking(directory_path: str, store: Any = None, poll_interval: float = 5.0) -> None:
    """Watch a directory for new or modified session files and ingest them (blocking).

    This function runs indefinitely, polling the directory at the specified interval.
    Intended for use in a dedicated worker process.

    Args:
        directory_path: Path to the directory to watch for session files.
        store: Optional store instance. If not provided, the default store is used.
        poll_interval: Seconds to wait between directory scans.
    """
    watcher = SessionDirectoryWatcher(directory_path, store, poll_interval)
    try:
        watcher.start()
        # Wait forever until interrupted
        while not watcher._stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Directory watcher stopped by user")
    finally:
        watcher.stop()