"""The Reader's friction counts: fixed names into the feature counts, nothing about the Review, off means off."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from lemoncrow_client import usage
from test_local_review_inbox_views import REPO, _Local, _ok, _refused, _review, _run

PATH = "/api/review-friction"


@pytest.fixture
def telemetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("DO_NOT_TRACK", "LEMONCROW_TELEMETRY", "CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_POLICY", str(tmp_path / "policy.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    usage.announce(io.StringIO())
    return tmp_path / "home" / "usage"


def _lines(directory: Path) -> list[dict[str, str]]:
    return [json.loads(line) for path in sorted(directory.glob("*.jsonl")) for line in path.read_text().splitlines()]


def test_each_event_is_one_fixed_count_and_a_wait_keeps_no_review_identity(telemetry: Path, tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        review_id, _revision = _review(local.store(REPO), "Secret title in src/private.py")
        for metric in ("map_open_with_delta", "map_open_without_delta", "blocked_reason"):
            await _ok(await local.send("POST", PATH, {"metric": metric}))
        await _ok(await local.send("POST", PATH, {"metric": "settings_before_first_review"}))
        await _ok(await local.send("POST", PATH, {"metric": "waiting", "review_id": review_id}))
        lines = _lines(telemetry)
        assert [line["f"] for line in lines] == [
            "review-friction-map-open-with-delta",
            "review-friction-map-open-without-delta",
            "review-friction-blocked-reason",
            "review-friction-settings-before-first-review",
            "review-friction-wait-reviewer",
        ]
        assert all(set(line) == {"f", "s", "o", "b"} and line["s"] == "reader" for line in lines)
        written = "".join(path.read_text() for path in telemetry.glob("*.jsonl"))
        assert review_id not in written and "private" not in written and "Secret" not in written

    _run(tmp_path, body)


def test_an_unknown_metric_or_extra_text_is_refused_and_counts_nothing(telemetry: Path, tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        await _refused(await local.send("POST", PATH, {"metric": "src/private.py"}), 400, "metric")
        await _refused(await local.send("POST", PATH, {"metric": "waiting"}), 400, "review_id")
        assert _lines(telemetry) == []

    _run(tmp_path, body)


@pytest.mark.parametrize(("name", "value"), [("DO_NOT_TRACK", "1"), ("LEMONCROW_TELEMETRY", "off")])
def test_the_off_switch_stops_every_count(
    telemetry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    async def body(local: _Local) -> None:
        review_id, _revision = _review(local.store(REPO), "Off")
        await _ok(await local.send("POST", PATH, {"metric": "blocked_reason"}))
        await _ok(await local.send("POST", PATH, {"metric": "waiting", "review_id": review_id}))
        assert _lines(telemetry) == []

    _run(tmp_path, body)
