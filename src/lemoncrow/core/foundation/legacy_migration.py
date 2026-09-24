"""One-time cleanup of local state left by retired LemonCrow account/cap builds.

Idempotent and versioned via a marker file in the store root. Never transmits
anything and never touches user repositories, lessons, memory, or the code index.
Hosted Authward credentials use the separate client credential store.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lemoncrow.core.foundation.paths import default_store_root

logger = logging.getLogger(__name__)

MIGRATION_VERSION = 2
_MARKER_FILENAME = ".migration_version"

# v1 removed the legacy device/cap state.
_V1_REMOVE = (
    "device_id",
    "cap_anon_token",
    "login_declined",
)

# v2 removes the retired local LemonCrow-account cache. Hosted Authward sessions
# are stored by lemoncrow_client.credentials and are intentionally unrelated.
_V2_REMOVE = (
    "auth_token",
    "auth_user.json",
    "auth_base",
    "auth.json",
    "subscription.json",
    "login_pending.json",
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

    def _remove(names: tuple[str, ...]) -> None:
        for name in names:
            target = store_root / name
            try:
                if target.exists():
                    target.unlink()
                    logger.info("legacy-migration: removed %s", name)
            except OSError:
                logger.warning("legacy-migration: could not remove %s", name, exc_info=True)

    if version < 1:
        _remove(_V1_REMOVE)
    if version < 2:
        _remove(_V2_REMOVE)

    try:
        _marker_path(store_root).write_text(f"{MIGRATION_VERSION}\n", encoding="utf-8")
    except OSError:
        logger.warning("legacy-migration: could not write marker", exc_info=True)
    return MIGRATION_VERSION
