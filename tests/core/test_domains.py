"""Tests for Atelier internal domain bundle system."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from atelier.core.domains import DomainManager
from atelier.core.domains.loader import DomainLoader
from atelier.core.domains.models import DomainBundle
from atelier.gateway.cli import cli

# ---------------------------------------------------------------------------
# DomainManager: basic loading
# ---------------------------------------------------------------------------


def test_domain_manager_lists_builtins(tmp_path: Path) -> None:
    """DomainManager should return built-in bundles when no user bundles exist."""
    manager = DomainManager(tmp_path / ".atelier")
    refs = manager.list_bundles()
    assert isinstance(refs, list)
    # swe.general is the only builtin right now
    ids = {r.bundle_id for r in refs}
    assert "swe.general" in ids


def test_domain_manager_loads_user_bundle(tmp_path: Path) -> None:
    """A bundle.yaml placed under <root>/domains/<id>/ is picked up as a user bundle."""
    root = tmp_path / ".atelier"
    bundle_dir = root / "domains" / "custom.test"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "bundle_id": "custom.test",
                "domain": "custom.test",
                "description": "Custom test bundle",
                "author": "tester",
                "playbooks": [],
            }
        ),
        encoding="utf-8",
    )

    manager = DomainManager(root)
    ids = {r.bundle_id for r in manager.list_bundles()}
    assert "custom.test" in ids


def test_domain_manager_user_bundle_overrides_builtin(tmp_path: Path) -> None:
    """A user bundle with the same id as a builtin should shadow it."""
    root = tmp_path / ".atelier"
    bundle_dir = root / "domains" / "swe.general"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "bundle_id": "swe.general",
                "domain": "swe.general",
                "description": "Custom override",
                "author": "tester",
                "playbooks": [],
            }
        ),
        encoding="utf-8",
    )

    manager = DomainManager(root)
    info = manager.info("swe.general")
    assert info is not None
    assert info["description"] == "Custom override"


def test_domain_manager_info_returns_none_for_unknown(tmp_path: Path) -> None:
    manager = DomainManager(tmp_path / ".atelier")
    assert manager.info("does.not.exist") is None


def test_domain_manager_all_playbooks_returns_list(tmp_path: Path) -> None:
    manager = DomainManager(tmp_path / ".atelier")
    blocks = manager.all_playbooks()
    assert isinstance(blocks, list)
    assert blocks == []


def test_domain_manager_load_playbooks_for_bundle(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    bundle_dir = root / "domains" / "custom.test"
    playbooks_dir = bundle_dir / "playbooks"
    playbooks_dir.mkdir(parents=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "bundle_id": "custom.test",
                "domain": "custom.test",
                "description": "Custom test bundle",
                "author": "tester",
                "playbooks": ["playbooks/example.yaml"],
            }
        ),
        encoding="utf-8",
    )
    (playbooks_dir / "example.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "example-block",
                "title": "Example Block",
                "domain": "custom.test",
                "situation": "A test bundle needs a playbook.",
                "procedure": ["Do the thing"],
            }
        ),
        encoding="utf-8",
    )

    manager = DomainManager(root)
    blocks = manager.load_playbooks("custom.test")
    assert isinstance(blocks, list)
    ids = {b.id for b in blocks}
    assert "example-block" in ids


# ---------------------------------------------------------------------------
# DomainLoader: builtin discovery
# ---------------------------------------------------------------------------


def test_domain_loader_lists_builtins() -> None:
    loader = DomainLoader()
    builtins = loader.list_builtin()
    assert isinstance(builtins, list)
    assert any(b.bundle_id == "swe.general" for b in builtins)


def test_domain_loader_load_builtin_swe_general() -> None:
    loader = DomainLoader()
    bundle = loader.load_builtin("swe.general")
    assert isinstance(bundle, DomainBundle)
    assert bundle.bundle_id == "swe.general"
    assert bundle.domain == "swe.general"


# ---------------------------------------------------------------------------
# domain CLI commands
# ---------------------------------------------------------------------------


def test_domain_cli_list(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "domain", "list"])
    assert result.exit_code == 0, result.output
    assert "swe.general" in result.output


def test_domain_cli_list_json(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "domain", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    ids = {item["bundle_id"] for item in payload}
    assert "swe.general" in ids


def test_domain_cli_info(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "domain", "info", "swe.general"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["bundle_id"] == "swe.general"


def test_domain_cli_info_unknown_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "domain", "info", "does.not.exist"])
    assert result.exit_code != 0


def test_domain_cli_no_pack_commands() -> None:
    """The old 'pack' group must not be present in the CLI."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert "pack" not in result.output or "domain" in result.output
    # Specifically the old 'pack' group should be gone
    result2 = runner.invoke(cli, ["pack", "--help"])
    assert result2.exit_code != 0
