"""Typed models for historical symbol ingestion."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraveyardEntry:
    symbol_name: str
    qualified_name: str | None
    file_path: str
    language: str | None
    deleted_at_sha: str
    deleted_at_ts: int
    last_author: str | None
    last_commit_msg: str | None
    rename_target: str | None = None
    signature_hash: str | None = None
