from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from lemoncrow.gateway import mcp_connectors as connectors


def test_connector_binding_roundtrip_is_private(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(connectors, "default_connector_dir", lambda: tmp_path / "connectors")
    path = connectors.save_connector(connectors.ConnectorBinding("LC-Test.Example.com", str(root)))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert connectors.load_connector_for_hostname("lc-test.example.com") == connectors.ConnectorBinding(
        hostname="lc-test.example.com", workspace=str(root.resolve())
    )


def test_connector_binding_refuses_missing_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(connectors, "default_connector_dir", lambda: tmp_path / "connectors")
    try:
        connectors.save_connector(connectors.ConnectorBinding("lc-test.example.com", str(tmp_path / "missing")))
    except ValueError as exc:
        assert "workspace" in str(exc)
    else:
        raise AssertionError("missing workspace accepted")


def test_hosted_connector_owner_roundtrips_with_binding(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "workspace-owned"
    root.mkdir()
    monkeypatch.setattr(connectors, "default_connector_dir", lambda: tmp_path / "connectors")
    expected = connectors.ConnectorBinding(
        "owned.example.com",
        str(root),
        org_id="org_acme",
        subject="usr_0123456789abcdef",
    )
    connectors.save_connector(expected)
    loaded = connectors.load_connector_for_hostname("owned.example.com")
    assert loaded == connectors.ConnectorBinding(
        hostname="owned.example.com",
        workspace=str(root.resolve()),
        org_id="org_acme",
        subject="usr_0123456789abcdef",
    )


def test_connector_binding_refuses_half_an_owner(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "workspace-half"
    root.mkdir()
    directory = tmp_path / "connectors"
    monkeypatch.setattr(connectors, "default_connector_dir", lambda: directory)
    path = connectors.connector_path("half.example.com")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hostname": "half.example.com",
                "workspace": str(root),
                "org_id": "org_acme",
            }
        ),
        encoding="utf-8",
    )
    assert connectors.load_connector(path) is None
