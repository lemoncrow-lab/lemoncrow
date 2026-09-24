"""Opt-in daily update notice: gating, once-a-day claims, badge, detached refresh/auto-update."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation import update_notice as un

ROOT_DIR = Path(__file__).resolve().parents[2]
DAY = 24 * 60 * 60


class _Spawns:
    """Records detached child launches instead of running them."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> None:
        self.argv.append(argv[1:])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (un.ENV_CHECK, un.ENV_AUTO):
        monkeypatch.delenv(name, raising=False)


def _opt_in(root: Path, **settings: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin_settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_everything_is_off_until_the_user_opts_in(tmp_path: Path) -> None:
    un._write_cache(tmp_path, {"latest": "9.9.9", "checked_at": 0})
    spawns = _Spawns()
    assert not un.notice_enabled(tmp_path)
    assert un.pending(tmp_path, "1.0.0") is None
    assert un.notice_for("hook", root=tmp_path, installed="1.0.0", popen=spawns) is None
    assert un.refresh(tmp_path, fetch=lambda: pytest.fail("must not touch the network")) is None
    assert spawns.argv == []


def test_env_beats_saved_setting_and_dev_installs_never_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True})
    assert un.notice_enabled(tmp_path)
    monkeypatch.setenv(un.ENV_CHECK, "0")
    assert not un.notice_enabled(tmp_path)
    monkeypatch.delenv(un.ENV_CHECK)
    (tmp_path / ".dev_mode").touch()
    assert not un.notice_enabled(tmp_path)


def test_refresh_caches_latest_and_badge_tracks_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True})
    monkeypatch.setattr(un, "installed_version", lambda: "1.0.0")
    assert un.refresh(tmp_path, fetch=lambda: "1.2.0", now=100.0) == "1.2.0"
    assert (tmp_path / "update_badge").read_text() == "1.2.0"
    assert un.read_cache(tmp_path)["checked_at"] == 100.0

    monkeypatch.setattr(un, "installed_version", lambda: "1.2.0")  # user updated
    un.sync_badge(tmp_path)
    assert not (tmp_path / "update_badge").exists()


def test_refresh_survives_offline_and_keeps_the_last_known_latest(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True})
    un._write_cache(tmp_path, {"latest": "1.2.0", "checked_at": 1})

    def _offline() -> str | None:
        raise OSError("no network")

    assert un.refresh(tmp_path, fetch=_offline, now=500.0) is None
    cache = un.read_cache(tmp_path)
    assert cache["latest"] == "1.2.0"
    assert cache["checked_at"] == 500.0  # back off a day rather than retrying every session


def test_notice_shows_once_per_day_per_surface(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True})
    un._write_cache(tmp_path, {"latest": "1.2.0", "checked_at": 10**12})
    spawns = _Spawns()

    def call(surface: str, today: str) -> str | None:
        return un.notice_for(surface, root=tmp_path, installed="1.0.0", now=10**12, today=today, popen=spawns)

    first = call("hook", "2026-09-19")
    assert first and "1.2.0" in first and "lc update" in first
    assert call("hook", "2026-09-19") is None  # same day, same surface
    assert call("cli", "2026-09-19") is not None  # other surface has its own daily slot
    assert call("hook", "2026-09-20") is not None  # next day
    assert spawns.argv == []  # cache is fresh: no refresh, no auto-update


def test_opt_out_removes_a_stale_statusline_badge(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: False, un.KEY_AUTO: False})
    (tmp_path / "update_badge").write_text("9.9.9", encoding="utf-8")
    assert un.notice_for("hook", root=tmp_path, installed="1.0.0", popen=_Spawns()) is None
    assert not (tmp_path / "update_badge").exists()


def test_daily_claim_is_atomic_across_concurrent_callers(tmp_path: Path) -> None:
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: un._claim_today(tmp_path, "refresh", "2026-09-19"), range(32)))
    assert claimed.count(True) == 1
    assert claimed.count(False) == 31


def test_up_to_date_shows_nothing(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True})
    un._write_cache(tmp_path, {"latest": "1.0.0", "checked_at": 10**12})
    assert un.notice_for("hook", root=tmp_path, installed="1.0.0", now=10**12, popen=_Spawns()) is None


def test_stale_cache_kicks_one_detached_refresh_not_an_inline_fetch(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True})
    un._write_cache(tmp_path, {"latest": "1.0.0", "checked_at": 0})
    spawns = _Spawns()
    for _ in range(3):
        un.notice_for(
            "hook",
            root=tmp_path,
            installed="1.0.0",
            now=DAY + 1,
            today="2026-09-19",
            popen=spawns,
        )
    assert spawns.argv == [["-m", "lemoncrow.core.foundation.update_notice"]]


def test_auto_update_launches_lc_update_once_a_day(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True, un.KEY_AUTO: True})
    un._write_cache(tmp_path, {"latest": "1.2.0", "checked_at": 10**12})
    spawns = _Spawns()

    def call() -> str | None:
        return un.notice_for("hook", root=tmp_path, installed="1.0.0", now=10**12, today="2026-09-19", popen=spawns)

    message = call()
    assert message and "updating in the background" in message
    assert spawns.argv == [["-m", "lemoncrow.gateway.cli", "update"]]
    call()  # a failing update is not retried in a loop
    assert spawns.argv.count(["-m", "lemoncrow.gateway.cli", "update"]) == 1


def test_auto_update_without_the_check_is_off(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: False, un.KEY_AUTO: False})
    assert not un.auto_update_enabled(tmp_path)


def test_clear_after_a_successful_update_drops_badge_and_announcement(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{un.KEY_CHECK: True})
    un._write_cache(tmp_path, {"latest": "1.2.0", "checked_at": 1})
    (tmp_path / "update_badge").write_text("1.2.0")
    un.clear(tmp_path)
    assert not (tmp_path / "update_badge").exists()
    assert "latest" not in un.read_cache(tmp_path)


def test_claude_hook_emits_exactly_one_json_object_with_the_notice(tmp_path: Path) -> None:
    hook = ROOT_DIR / "integrations/claude/plugin/hooks/session_start.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("lc_session_start_hook", hook)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        module._emit_env_context(str(tmp_path), "LemonCrow 1.2.0 is available")
    out = json.loads(buffer.getvalue())  # a hook must print exactly one JSON document
    assert out["systemMessage"] == "LemonCrow 1.2.0 is available"
    assert "additionalContext" in out["hookSpecificOutput"]


def _installer_functions(*names: str) -> str:
    common = (ROOT_DIR / "scripts/lib/common.sh").read_text()
    return "\n".join(
        m.group(0) for n in names if (m := re.search(rf"^{n}\(\) \{{.*?^\}}", common, re.S | re.M)) is not None
    )


def _run_installer_snippet(tmp_path: Path, body: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    funcs = _installer_functions("_plugin_setting_value", "prompt_update_selection", "persist_update_selection")
    return subprocess.run(
        ["bash", "-c", f"{funcs}\n{body}"],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "LEMONCROW_ROOT": str(tmp_path), "LEMONCROW_DRY_RUN": "0", **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_persists_choices_and_leaves_unset_ones_alone(tmp_path: Path) -> None:
    _opt_in(tmp_path, **{"cli.telegraphic": True})  # unrelated key must survive
    result = _run_installer_snippet(
        tmp_path, "persist_update_selection", {"LEMONCROW_UPDATE_CHECK": "1", "LEMONCROW_AUTO_UPDATE": "0"}
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads((tmp_path / "plugin_settings.json").read_text())
    assert saved == {"cli.telegraphic": True, "cli.update_check": True, "cli.auto_update": False}

    # Nothing chosen (non-interactive update run): file is untouched, so a saved opt-in survives `lc update`.
    before = (tmp_path / "plugin_settings.json").read_text()
    assert _run_installer_snippet(tmp_path, "persist_update_selection", {}).returncode == 0
    assert (tmp_path / "plugin_settings.json").read_text() == before


def _prompt_body(answer: int) -> str:
    stub = (
        f'interactive_single_select() {{ echo "ASKED: $1"; printf -v "$2" "%s" {answer}; }}\n'
        "has_interactive_input() { return 0; }; supports_interactive_selector() { return 0; }\n"
    )
    return (
        stub + 'prompt_update_selection; echo "check=[${LEMONCROW_UPDATE_CHECK:-}] auto=[${LEMONCROW_AUTO_UPDATE:-}]"'
    )


def _prompt(tmp_path: Path, answer: int, env: dict[str, str] | None = None) -> str:
    result = _run_installer_snippet(tmp_path, _prompt_body(answer), {"LEMONCROW_NON_INTERACTIVE": "0", **(env or {})})
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_installer_asks_once_and_offers_auto_notify_or_no_check(tmp_path: Path) -> None:
    auto = _prompt(tmp_path, 0)
    assert auto.count("ASKED") == 1 and "How should LemonCrow handle updates?" in auto
    assert "check=[1] auto=[1]" in auto

    notify = _prompt(tmp_path, 1)
    assert notify.count("ASKED") == 1
    assert "check=[1] auto=[0]" in notify

    disabled = _prompt(tmp_path, 2)
    assert disabled.count("ASKED") == 1
    assert "check=[0] auto=[0]" in disabled


@pytest.mark.parametrize(
    "env",
    [
        {"LEMONCROW_NON_INTERACTIVE": "1"},
        {"LEMONCROW_AUTO_UPDATE": "1"},
        {"LEMONCROW_UPDATE_CHECK": "1"},
        {"LEMONCROW_UPDATE_CHECK": "0"},
    ],
)
def test_installer_stays_quiet_when_already_decided_or_non_interactive(tmp_path: Path, env: dict[str, str]) -> None:
    result = _run_installer_snippet(tmp_path, _prompt_body(0), env)
    assert result.returncode == 0, result.stderr
    assert "ASKED" not in result.stdout


@pytest.mark.parametrize(
    "saved",
    [{un.KEY_AUTO: False}, {un.KEY_AUTO: True}, {un.KEY_CHECK: False}, {un.KEY_CHECK: True}],
)
def test_installer_never_re_asks_a_saved_answer(tmp_path: Path, saved: dict[str, bool]) -> None:
    _opt_in(tmp_path, **saved)
    assert "ASKED" not in _prompt(tmp_path, 0)
