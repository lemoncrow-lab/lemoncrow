"""One-time legacy-identity migration: removes commercial device/cap state and
preserves the optional account session, without transmitting anything."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.foundation.legacy_migration import (
    MIGRATION_VERSION,
    run_startup_migrations,
)


def test_migration_removes_legacy_device_and_cap_state(tmp_path: Path) -> None:
    root = tmp_path
    (root / "device_id").write_text("old-hardware-derived-hash")
    (root / "cap_anon_token").write_text("tok")
    (root / "login_declined").write_text("")
    (root / "auth_token").write_text("keepme")

    assert run_startup_migrations(root) == MIGRATION_VERSION

    # Commercial device/cap state is gone (device id regenerates random-local).
    assert not (root / "device_id").exists()
    assert not (root / "cap_anon_token").exists()
    assert not (root / "login_declined").exists()
    # The optional hosted-account session is preserved.
    assert (root / "auth_token").read_text() == "keepme"
    # Marker records the version.
    assert (root / ".migration_version").read_text().strip() == str(MIGRATION_VERSION)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path
    (root / "device_id").write_text("old")
    assert run_startup_migrations(root) == MIGRATION_VERSION
    # A re-run does not re-delete or error, and does not touch a NEW device id.
    (root / "device_id").write_text("fresh-random-local")
    assert run_startup_migrations(root) == MIGRATION_VERSION
    assert (root / "device_id").read_text() == "fresh-random-local"


def test_migration_on_fresh_root_is_a_noop_marker(tmp_path: Path) -> None:
    root = tmp_path
    assert run_startup_migrations(root) == MIGRATION_VERSION
    assert not (root / "device_id").exists()
    assert (root / ".migration_version").read_text().strip() == str(MIGRATION_VERSION)
