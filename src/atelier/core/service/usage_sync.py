"""Compatibility shim for the historical usage_sync module path."""

from __future__ import annotations

from atelier.core.service.sync import sync_usage

__all__ = ["sync_usage"]
