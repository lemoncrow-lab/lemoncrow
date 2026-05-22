"""Encrypted cross-machine sync primitives."""

from .config import (
    SyncConfig,
    default_machine_id,
    load_sync_config,
    load_sync_status,
    save_sync_config,
    save_sync_status,
)
from .encryption import InvalidPassphraseError, decrypt_bytes, encrypt_bytes
from .sync_engine import (
    CloudAccessError,
    SyncApplyError,
    SyncError,
    SyncNotConfiguredError,
    init_sync,
    render_sync_status_text,
    sync_bidirectional,
    sync_down,
    sync_status,
    sync_up,
)

__all__ = [
    "CloudAccessError",
    "InvalidPassphraseError",
    "SyncApplyError",
    "SyncConfig",
    "SyncError",
    "SyncNotConfiguredError",
    "decrypt_bytes",
    "default_machine_id",
    "encrypt_bytes",
    "init_sync",
    "load_sync_config",
    "load_sync_status",
    "render_sync_status_text",
    "save_sync_config",
    "save_sync_status",
    "sync_bidirectional",
    "sync_down",
    "sync_status",
    "sync_up",
]
