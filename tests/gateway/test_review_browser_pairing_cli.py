"""Credential-free browser opening for local Review.

These tests isolate the browser-pairing handoff from the full Review capture
fixture so the machine credential can never accidentally become URL state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.gateway.cli.commands.review import _open_server_review


def _record_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import webbrowser

    opened: list[str] = []

    def _open(url: str, *args: Any, **kwargs: Any) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", _open)
    return opened


def test_local_open_pairs_exact_clean_path_before_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import hosted as review_server

    paired: list[str] = []
    monkeypatch.setattr(
        review_server,
        "pair_local_review_browser",
        lambda _config, path: paired.append(path) or {"state": "armed"},
    )
    opened = _record_browser(monkeypatch)
    config = SimpleNamespace(hosted=False, token="machine-secret-that-must-not-leak")

    _open_server_review(
        "http://127.0.0.1:7420/reviews/r-12345678",
        config=config,
        review_id="r-12345678",
        open_browser=True,
    )

    assert paired == ["/reviews/r-12345678"]
    assert opened == ["http://127.0.0.1:7420/reviews/r-12345678"]
    assert config.token not in opened[0]


def test_hosted_open_does_not_use_local_pairing(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import hosted as review_server

    def _unexpected(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("hosted Review must not use local browser pairing")

    monkeypatch.setattr(review_server, "pair_local_review_browser", _unexpected)
    opened = _record_browser(monkeypatch)
    config = SimpleNamespace(hosted=True, token="hosted-secret")

    _open_server_review(
        "https://lemoncrow.example/reviews/r-12345678",
        config=config,
        review_id="r-12345678",
        open_browser=True,
    )

    assert opened == ["https://lemoncrow.example/reviews/r-12345678"]
