"""One-time cleanup for state left by retired LemonCrow account/cap builds."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.foundation.legacy_migration import MIGRATION_VERSION, run_startup_migrations


def test_migration_removes_all_retired_account_and_cap_state(tmp_path: Path) -> None:
    retired = (
        "device_id",
        "cap_anon_token",
        "login_declined",
        "auth_token",
        "auth_user.json",
        "auth_base",
        "auth.json",
        "subscription.json",
        "login_pending.json",
    )
    for name in retired:
        (tmp_path / name).write_text("legacy", encoding="utf-8")
    unrelated = tmp_path / "memory.sqlite"
    unrelated.write_text("keep", encoding="utf-8")

    assert run_startup_migrations(tmp_path) == MIGRATION_VERSION
    assert all(not (tmp_path / name).exists() for name in retired)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / ".migration_version").read_text().strip() == str(MIGRATION_VERSION)


def test_v1_install_upgrades_by_removing_only_v2_state(tmp_path: Path) -> None:
    (tmp_path / ".migration_version").write_text("1\n", encoding="utf-8")
    (tmp_path / "auth_token").write_text("legacy", encoding="utf-8")
    (tmp_path / "subscription.json").write_text("{}", encoding="utf-8")
    # A v1 marker means v1 already ran; do not reinterpret a newly-created file
    # with a v1-only legacy name as pending migration state.
    (tmp_path / "device_id").write_text("new-unrelated-file", encoding="utf-8")

    assert run_startup_migrations(tmp_path) == MIGRATION_VERSION
    assert not (tmp_path / "auth_token").exists()
    assert not (tmp_path / "subscription.json").exists()
    assert (tmp_path / "device_id").read_text(encoding="utf-8") == "new-unrelated-file"


def test_migration_is_idempotent_after_current_marker(tmp_path: Path) -> None:
    assert run_startup_migrations(tmp_path) == MIGRATION_VERSION
    later = tmp_path / "auth_token"
    later.write_text("created-after-migration", encoding="utf-8")
    assert run_startup_migrations(tmp_path) == MIGRATION_VERSION
    assert later.read_text(encoding="utf-8") == "created-after-migration"


def test_migration_on_fresh_root_writes_current_marker(tmp_path: Path) -> None:
    assert run_startup_migrations(tmp_path) == MIGRATION_VERSION
    assert (tmp_path / ".migration_version").read_text().strip() == str(MIGRATION_VERSION)
