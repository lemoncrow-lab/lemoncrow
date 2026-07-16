"""Pro entitlement gates on paid CLI control surfaces.

Recall and swarm are local Free capabilities; hosted/advanced controls remain
gated behind a signed-in Pro account.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lemoncrow.core.capabilities.licensing import entitlements
from lemoncrow.gateway.cli import cli
from tests.helpers import deny_oauth, grant_oauth_pro, init_store_at


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), *args])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # Isolate the auth store away from any real ~/.lemoncrow and force signed-out.
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "lic"))
    deny_oauth(monkeypatch)
    yield
    entitlements.reload()


GATED = [
    ("zoekt", "index"),
    ("knowledge", "extract"),
    ("memory", "find", "hello"),
    ("savings", "detail"),
]


@pytest.mark.parametrize("args", GATED)
def test_free_install_blocks_pro_cli(tmp_path: Path, args: tuple[str, ...]) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    res = _invoke(root, *args)
    assert res.exit_code != 0
    assert "LemonCrow Pro feature" in res.output


def test_free_install_opens_recall(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    res = _invoke(root, "session", "recall", "search", "hello")
    assert "LemonCrow Pro feature" not in res.output
    assert res.exit_code == 0, res.output


def test_free_install_reaches_swarm_validation(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))
    res = _invoke(root, "swarm", "start")
    assert "LemonCrow Pro feature" not in res.output


def test_pro_install_keeps_recall_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grant_oauth_pro(monkeypatch)

    root = tmp_path / "a"
    init_store_at(str(root))
    res = _invoke(root, "session", "recall", "search", "hello")
    assert "LemonCrow Pro feature" not in res.output
    assert res.exit_code == 0, res.output


@pytest.mark.parametrize("action", ["start", "restart"])
def test_routing_daemon_is_not_sold_as_a_pro_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    grant_oauth_pro(monkeypatch)
    root = tmp_path / "a"
    init_store_at(str(root))

    res = _invoke(root, "router", action)

    assert res.exit_code != 0
    assert "not available in this release" in res.output
    assert "LemonCrow Pro feature" not in res.output


def test_unshipped_routing_commands_are_hidden_from_main_help(tmp_path: Path) -> None:
    root = tmp_path / "a"
    init_store_at(str(root))

    res = _invoke(root, "--help")

    assert res.exit_code == 0, res.output
    assert "  route " not in res.output
    assert "  router " not in res.output
