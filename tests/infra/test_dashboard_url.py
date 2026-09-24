from __future__ import annotations

from pathlib import Path

from lemoncrow.infra.runtime import dashboard_url


def test_discover_dashboard_url_uses_configured_loopback_endpoint(tmp_path: Path, monkeypatch) -> None:
    checked: list[str] = []
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7555")
    monkeypatch.setattr(
        dashboard_url,
        "_server_healthy",
        lambda url: checked.append(url) or url == "http://127.0.0.1:7555",
    )

    assert dashboard_url.discover_dashboard_url(tmp_path) == "http://127.0.0.1:7555"
    assert checked == ["http://127.0.0.1:7555"]


def test_requested_dashboard_port_does_not_fall_back(tmp_path: Path, monkeypatch) -> None:
    checked: list[str] = []
    monkeypatch.setattr(dashboard_url, "_server_healthy", lambda url: checked.append(url) or False)
    monkeypatch.setattr(dashboard_url, "_dashboard_responds", lambda url: False)

    assert dashboard_url.discover_dashboard_url(tmp_path, requested_port=3999) is None
    assert checked == ["http://127.0.0.1:3999"]
