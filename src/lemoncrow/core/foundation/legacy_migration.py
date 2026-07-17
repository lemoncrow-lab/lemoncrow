"""One-time local migrations for the maintenance-mode / account-free transition.

Removes commercial device-identity and savings-cap state left by older installs.
Idempotent and versioned via a marker file in the store root. Never transmits
anything, and never touches user repositories, lessons, memory, or the code
index. See docs/maintenance-mode-transition.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lemoncrow.core.foundation.paths import default_store_root

logger = logging.getLogger(__name__)

MIGRATION_VERSION = 1
_MARKER_FILENAME = ".migration_version"

# v1 removes: the legacy hardware-derived device id (regenerated as a fresh
# random-local id on next use) and commercial savings-cap state. The OPTIONAL
# hosted-account session (auth_token / auth_user.json / auth_base) is
# intentionally preserved — it is not commercial identity and gates nothing.
_V1_REMOVE = (
    "device_id",
    "cap_anon_token",
    "login_declined",
)


def _marker_path(root: Path) -> Path:
    return root / _MARKER_FILENAME


def _current_version(root: Path) -> int:
    try:
        return int(_marker_path(root).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def run_startup_migrations(root: Path | str | None = None) -> int:
    """Run pending one-time local migrations. Returns the resulting version.

    Idempotent: a no-op once the marker records the current version. Best-effort:
    filesystem errors are logged, never raised, so they cannot block startup.
    """
    store_root = Path(root).expanduser().resolve() if root is not None else default_store_root()
    if not store_root.exists():
        return 0
    version = _current_version(store_root)
    if version >= MIGRATION_VERSION:
        return version

    if version < 1:
        for name in _V1_REMOVE:
            target = store_root / name
            try:
                if target.exists():
                    target.unlink()
                    logger.info("legacy-migration: removed %s", name)
            except OSError:
                logger.warning("legacy-migration: could not remove %s", name, exc_info=True)

    try:
        _marker_path(store_root).write_text(f"{MIGRATION_VERSION}\n", encoding="utf-8")
    except OSError:
        logger.warning("legacy-migration: could not write marker", exc_info=True)
    return MIGRATION_VERSION
