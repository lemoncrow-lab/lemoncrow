from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.foundation.store import ContextStore
from atelier.gateway.integrations import external_analytics as ext


def test_external_status_reports_installed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext, "_find_executable", lambda spec: "/usr/bin/fake")

    payload = ext.external_status(cwd=Path("/tmp/work"))

    by_tool = {item["tool"]: item for item in payload}
    assert by_tool["tokscale"]["available"] is True
    assert by_tool["codeburn"]["available"] is True
    assert set(by_tool) == {"tokscale", "codeburn"}


def test_run_external_reports_collects_reportable_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(tool: str, *, period: str = "week", cwd: Path | None = None) -> dict[str, object]:
        return {"tool": tool, "period": period, "cwd": str(cwd), "ok": True, "payload": {"tool": tool}}

    monkeypatch.setattr(ext, "run_external_report", fake_run)

    payload = ext.run_external_reports(tool="all", period="week", cwd=Path("/tmp/work"))

    assert payload["tool"] == "all"
    assert [item["tool"] for item in payload["reports"]] == ["tokscale", "codeburn"]


def test_persist_external_reports_replaces_same_tool_period_snapshot(
    store: ContextStore,
) -> None:
    ext.persist_external_reports(
        store,
        {
            "generated_at": "2026-05-13T20:00:00+00:00",
            "period": "today",
            "reports": [
                {
                    "tool": "codeburn",
                    "period": "today",
                    "ok": True,
                    "payload": {"overview": {"calls": 23}},
                },
                {
                    "tool": "codeburn",
                    "period": "week",
                    "ok": True,
                    "payload": {"overview": {"calls": 140}},
                },
            ],
        },
        source="servicectl",
    )

    ext.persist_external_reports(
        store,
        {
            "generated_at": "2026-05-14T09:00:00+00:00",
            "period": "today",
            "reports": [
                {
                    "tool": "codeburn",
                    "period": "today",
                    "ok": True,
                    "payload": {"overview": {"calls": 48}},
                }
            ],
        },
        source="servicectl",
    )

    today_runs = store.list_external_analytics_runs(
        tool="codeburn",
        period="today",
        limit=10,
    )
    week_runs = store.list_external_analytics_runs(
        tool="codeburn",
        period="week",
        limit=10,
    )

    assert len(today_runs) == 1
    assert today_runs[0]["payload"]["overview"]["calls"] == 48
    assert len(week_runs) == 1
    assert week_runs[0]["payload"]["overview"]["calls"] == 140


def test_run_external_report_tokscale_combines_native_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ext,
        "_find_executable",
        lambda spec: "/usr/bin/tokscale" if spec.id == "tokscale" else None,
    )

    def fake_run_json_command(
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout_s: int = 120,
        parser: object | None = None,
    ) -> dict[str, object]:
        assert cwd == Path("/tmp/work")
        assert timeout_s == 120
        assert parser is None
        if command[1:2] == ["models"]:
            payload = {
                "entries": [{"model": "gpt-5.4", "cost": 4.2}],
                "groupBy": "client,model",
            }
        elif command[1:2] == ["monthly"]:
            payload = {
                "entries": [{"month": "2026-05", "cost": 4.2}],
                "totalCost": 4.2,
            }
        elif command[1:2] == ["hourly"]:
            payload = {
                "entries": [{"hour": "2026-05-12 19:00", "cost": 4.2}],
                "totalCost": 4.2,
            }
        elif command[1:2] == ["graph"]:
            payload = {
                "contributions": [{"date": "2026-05-12", "totals": {"cost": 4.2}}],
                "summary": {"totalCost": 4.2, "activeDays": 1},
                "meta": {"generatedAt": "2026-05-12T00:00:00Z"},
                "years": [{"year": "2026"}],
            }
        else:
            payload = {
                "entries": [{"model": "gpt-5.4", "cost": 4.2}],
                "groupBy": "client,model",
                "totalCost": 4.2,
                "totalInput": 100,
                "totalOutput": 50,
                "totalCacheRead": 25,
                "totalCacheWrite": 0,
                "totalMessages": 3,
                "processingTimeMs": 12,
            }
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "{}",
            "stderr": "",
            "payload": payload,
            "parse_error": None,
        }

    monkeypatch.setattr(ext, "_run_json_command", fake_run_json_command)

    report = ext.run_external_report("tokscale", period="week", cwd=Path("/tmp/work"))

    assert report["tool"] == "tokscale"
    assert report["ok"] is True
    assert "tokscale models --json --no-spinner --week" in str(report["command_display"])
    payload = report["payload"]
    assert isinstance(payload, dict)
    assert payload["reportKind"] == "tokscale_bundle"
    assert payload["totalCost"] == 4.2
    assert payload["modelEntries"] == [{"model": "gpt-5.4", "cost": 4.2}]
    assert payload["monthlyEntries"] == [{"month": "2026-05", "cost": 4.2}]
    assert payload["hourlyEntries"] == [{"hour": "2026-05-12 19:00", "cost": 4.2}]
    assert payload["dailyEntries"] == [{"date": "2026-05-12", "totals": {"cost": 4.2}}]
    assert payload["dailySummary"] == {"totalCost": 4.2, "activeDays": 1}
    assert set(payload["captures"]) == {"overview", "models", "monthly", "hourly", "graph"}
