from __future__ import annotations

from atelier.gateway.hosts.session_parsers.registry import (
    SUPPORTED_SESSION_IMPORT_HOSTS,
    iter_importer_classes,
)


def test_supported_session_import_hosts_match_codeburn_inventory() -> None:
    assert SUPPORTED_SESSION_IMPORT_HOSTS == (
        "antigravity",
        "claude",
        "codex",
        "copilot",
        "cursor",
        "opencode",
    )


def test_registry_resolves_all_importer_classes() -> None:
    resolved = iter_importer_classes()
    assert [host for host, _ in resolved] == list(SUPPORTED_SESSION_IMPORT_HOSTS)
    assert all(cls.__name__.endswith("Importer") for _, cls in resolved)
