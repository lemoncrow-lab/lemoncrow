from __future__ import annotations

from pathlib import Path

from lemoncrow.infra.runtime import dashboard_url


def test_discover_dashboard_url_finds_non_default_listening_port(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.infra.runtime.stack_lifecycle._stack_status_payload",
        lambda root: {"frontend_url": "http://localhost:3125"},
    )
    monkeypatch.setattr(dashboard_url, "_listening_loopback_ports", lambda: {8787, 3225})
    monkeypatch.setattr(
        dashboard_url,
        "_dashboard_responds",
        lambda url: url == "http://127.0.0.1:3225",
    )

    assert dashboard_url.discover_dashboard_url(tmp_path) == "http://127.0.0.1:3225"


def test_requested_dashboard_port_does_not_fall_back(tmp_path: Path, monkeypatch) -> None:
    checked: list[str] = []
    monkeypatch.setattr(
        dashboard_url,
        "_dashboard_responds",
        lambda url: checked.append(url) or False,
    )

    assert dashboard_url.discover_dashboard_url(tmp_path, requested_port=3999) is None
    assert checked == ["http://127.0.0.1:3999"]
