"""Thin helper: list persisted sessions as ``SessionReport`` objects.

Used by ``insights.py`` to build multi-session aggregates.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from atelier.infra.runtime.session_report import SessionReport, build_report, list_run_files


def list_sessions(
    root: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[SessionReport]:
    """Return ``SessionReport`` objects for all runs in the time window.

    Results are sorted newest-first (same order as ``list_run_files``).
    Files that cannot be parsed are silently skipped.
    """
    files = list_run_files(root, since=since)
    reports: list[SessionReport] = []
    for f in files:
        try:
            snap: dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            report = build_report(snap, root)
        except Exception:
            continue
        if until is not None and report.started_at > until:
            continue
        reports.append(report)
        if limit is not None and len(reports) >= limit:
            break
    return reports
