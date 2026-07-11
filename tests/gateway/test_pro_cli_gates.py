"""Pro entitlement gates on CLI control surfaces (recall, router, zoekt).

Free installs (not signed in) must block these commands with an upsell; a
signed-in account on a Pro plan opens the gate.
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
    ("session", "recall", "search", "hello"),
    ("router", "start"),
    ("zoekt", "index"),
    ("knowledge", "extract"),
    ("swarm", "start"),
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


def test_pro_install_opens_recall_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grant_oauth_pro(monkeypatch)

    root = tmp_path / "a"
    init_store_at(str(root))
    res = _invoke(root, "session", "recall", "search", "hello")
    # Gate opened: the command ran (no matches in an empty index) instead of the upsell.
    assert "LemonCrow Pro feature" not in res.output
    assert res.exit_code == 0, res.output
