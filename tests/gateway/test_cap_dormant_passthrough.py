"""The MCP exposure helper evaluates the compiled cap authority live."""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_meter(root: Path, *, over: bool) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(root), {"plan": "free", "savingsOverCap": over})


@pytest.fixture(autouse=True)
def _local_meter_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setattr(licensing_gate, "_public_key_hex", lambda: "")
    # The dormancy snapshot is a module-global cache; reset it so a value cached
    # by one test never leaks into the next.
    monkeypatch.setattr(mcp_server, "_dormant_snapshot", None)


def test_dormant_true_when_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    _seed_meter(tmp_path, over=True)
    assert mcp_server._savings_dormant() is True


def test_dormant_false_when_under_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    _seed_meter(tmp_path, over=False)
    assert mcp_server._savings_dormant() is False


def test_authority_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    assert mcp_server._savings_dormant() is True


def test_dormant_tool_error_points_to_free_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setattr(mcp_server, "_savings_dormant", lambda: True)
    response = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read", "arguments": {}},
        }
    )
    assert response["error"]["code"] == -32601
    assert "lc account login" in response["error"]["message"]
    assert "uncapped Free" in response["error"]["message"]
    assert "upgrade" not in response["error"]["message"].lower()


# ── no_token self-heal ──────────────────────────────────────────────────────
# Dormancy with reason "no_token" means no verdict is persisted for the
# current identity (fresh container, offline login, expired token) — NOT that
# the server judged the cap exhausted. The server must force-mint one itself,
# throttled, so environments without a background reconciler converge.


def _verdict(*, dormant: bool, verified: bool, plan: str | None, reason: str) -> object:
    from lemoncrow.pro.capabilities.licensing_gate import CapVerdict

    return CapVerdict(dormant=dormant, verified=verified, plan=plan, reason=reason)


def test_no_token_dormancy_self_heals_by_forced_mint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.licensing import usage_report
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_tick_usage_report", lambda _root: None)
    monkeypatch.setattr(mcp_server, "_verdict_self_heal_at", 0.0)
    state = {"minted": False}
    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: (
            _verdict(dormant=False, verified=True, plan="pro", reason="signed")
            if state["minted"]
            else _verdict(dormant=True, verified=False, plan=None, reason="no_token")
        ),
    )
    forced: list[bool] = []

    def _mint(_root: object, *, force: bool = False, **_kw: object) -> bool:
        forced.append(force)
        state["minted"] = True
        return True

    monkeypatch.setattr(usage_report, "report_usage_once", _mint)
    assert mcp_server._refresh_dormant_snapshot() is False
    assert forced == [True]


def test_self_heal_attempts_are_throttled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import time

    from lemoncrow.core.capabilities.licensing import usage_report
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_tick_usage_report", lambda _root: None)
    monkeypatch.setattr(mcp_server, "_verdict_self_heal_at", 0.0)
    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: _verdict(dormant=True, verified=False, plan=None, reason="no_token"),
    )
    attempts: list[float] = []
    monkeypatch.setattr(
        usage_report,
        "report_usage_once",
        lambda _root, **_kw: attempts.append(time.monotonic()) or False,
    )
    assert mcp_server._refresh_dormant_snapshot() is True
    assert mcp_server._refresh_dormant_snapshot() is True  # within the interval — no second POST
    assert len(attempts) == 1


def test_verdict_ahead_of_local_ledger_triggers_throttled_reconcile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lemoncrow.core.capabilities import plugin_runtime
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_tick_usage_report", lambda _root: None)
    monkeypatch.setattr(mcp_server, "_ledger_reconcile_at", 0.0)
    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: _verdict(dormant=False, verified=True, plan="free", reason="signed")._replace(
            server_saved_usd=16.0
        ),
    )
    calls: list[tuple[object, float]] = []
    monkeypatch.setattr(
        plugin_runtime,
        "reconcile_local_savings_gap",
        lambda root, server_saved_usd: calls.append((root, server_saved_usd)) or True,
    )
    assert mcp_server._refresh_dormant_snapshot() is False
    assert calls == [(tmp_path, 16.0)]


def test_ledger_reconcile_is_throttled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities import plugin_runtime
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_tick_usage_report", lambda _root: None)
    monkeypatch.setattr(mcp_server, "_ledger_reconcile_at", 0.0)
    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: _verdict(dormant=False, verified=True, plan="free", reason="signed")._replace(
            server_saved_usd=16.0
        ),
    )
    calls: list[float] = []
    monkeypatch.setattr(
        plugin_runtime,
        "reconcile_local_savings_gap",
        lambda root, server_saved_usd: calls.append(server_saved_usd) or True,
    )
    assert mcp_server._refresh_dormant_snapshot() is False
    assert mcp_server._refresh_dormant_snapshot() is False  # within the throttle window -- no second check
    assert len(calls) == 1


def test_snapshot_ticks_an_opportunistic_usage_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Real reporting is otherwise host-stop-hook-driven with a 30-minute
    # on-disk throttle: a long session that never triggers a stop event would
    # never re-report, so local usage can run well past the cap while the
    # signed verdict -- and thus dormancy -- stays stale for the whole
    # session. _snapshot_dormant must tick an opportunistic report on every
    # request boundary so a long session still converges within ~30 minutes.
    from lemoncrow.core.capabilities.licensing import usage_report
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_usage_report_tick_at", 0.0)
    monkeypatch.setattr(mcp_server, "_verdict_self_heal_at", 120.0)
    monkeypatch.setattr(mcp_server, "_ledger_reconcile_at", 120.0)
    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: _verdict(dormant=False, verified=True, plan="free", reason="signed"),
    )
    calls: list[Path] = []
    monkeypatch.setattr(usage_report, "maybe_report_usage", lambda root, **_kw: calls.append(Path(root)) or True)
    assert mcp_server._refresh_dormant_snapshot() is False
    assert calls == [tmp_path]


def test_usage_report_tick_is_throttled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.licensing import usage_report
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_usage_report_tick_at", 0.0)
    monkeypatch.setattr(mcp_server, "_verdict_self_heal_at", 120.0)
    monkeypatch.setattr(mcp_server, "_ledger_reconcile_at", 120.0)
    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: _verdict(dormant=False, verified=True, plan="free", reason="signed"),
    )
    calls: list[Path] = []
    monkeypatch.setattr(usage_report, "maybe_report_usage", lambda root, **_kw: calls.append(Path(root)) or True)
    assert mcp_server._refresh_dormant_snapshot() is False
    assert mcp_server._refresh_dormant_snapshot() is False  # within the throttle window -- no second tick
    assert len(calls) == 1


def test_verified_dormancy_never_self_heals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A SIGNED "over cap" verdict is the server's final word — re-reporting
    # can't change it, so don't burn a network attempt on every tools/list.
    from lemoncrow.core.capabilities.licensing import usage_report
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_verdict_self_heal_at", 0.0)
    monkeypatch.setattr(
        licensing_gate,
        "resolve_cap_verdict",
        lambda _root: _verdict(dormant=True, verified=True, plan="free", reason="signed"),
    )
    attempts: list[bool] = []
    monkeypatch.setattr(usage_report, "report_usage_once", lambda _root, **_kw: attempts.append(True) or True)
    assert mcp_server._savings_dormant() is True
    assert attempts == []
