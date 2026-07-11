"""Recall candidate-window saturation warning is throttled to once per process (RF#6)."""

from __future__ import annotations

import logging

import pytest

from lemoncrow.core.capabilities.archival_recall import capability as cap_mod


def test_window_saturation_warns_once(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap_mod, "_window_saturation_warned", False)
    with caplog.at_level(logging.WARNING, logger=cap_mod._log.name):
        cap_mod._warn_window_saturated()
        cap_mod._warn_window_saturated()
        cap_mod._warn_window_saturated()
    hits = [r for r in caplog.records if "candidate window saturated" in r.getMessage()]
    assert len(hits) == 1
