from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_STOP = Path("integrations/claude/plugin/hooks/stop.py")


def _load_stop() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lemoncrow_stop_hook", _STOP)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_format_review_findings_surfaces_and_consumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    from lemoncrow.core.capabilities.live_reviewer.sink import (
        append_verdict,
        latest_unconsumed,
    )

    append_verdict(tmp_path, "sid1", {"verdict": "NEEDS_FIX", "paths": ["a.py"], "missing": "- bug"})
    stop = _load_stop()
    out = stop._format_review_findings("sid1")
    assert "NEEDS_FIX" in out
    assert "a.py" in out
    assert "bug" in out
    assert latest_unconsumed(tmp_path, "sid1") == []


def test_format_review_findings_empty_when_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    stop = _load_stop()
    assert stop._format_review_findings("sid1") == ""


def test_format_review_findings_skips_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    from lemoncrow.core.capabilities.live_reviewer.sink import append_verdict

    append_verdict(tmp_path, "sid1", {"verdict": "DONE"})
    stop = _load_stop()
    assert stop._format_review_findings("sid1") == ""
