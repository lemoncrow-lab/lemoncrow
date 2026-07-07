"""Reporting capabilities for leadership-facing governance summaries."""

from atelier.core.capabilities.reporting.weekly_report import (
    Report,
    generate_report,
    render_markdown,
)

__all__ = ["Report", "generate_report", "render_markdown"]
