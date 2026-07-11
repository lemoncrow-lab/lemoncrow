"""Shared fixtures for gateway / MCP-surface tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _disable_code_autosync() -> None:
    """Disable autosync for gateway tests to make them deterministic.

    MCP read tools never block on a cold index: a read returns immediately and
    the background autosync worker builds the index. That background build races
    the assertions and makes MCP-surface tests non-deterministic. With autosync
    disabled there is no worker, so tests that need a populated index build it
    explicitly (via index_repo) and observe a fully built index deterministically.
    """
    from lemoncrow.core.capabilities.code_context import CodeContextEngine

    original_init = CodeContextEngine.__init__

    def patched_init(self, repo_root, *, db_path=None, autosync_enabled=None):
        # Force autosync_enabled=False for deterministic tests
        original_init(self, repo_root, db_path=db_path, autosync_enabled=False)

    with patch.object(CodeContextEngine, "__init__", patched_init):
        yield
