from __future__ import annotations

from pathlib import Path

DOCS_ROOT = Path(__file__).parent.parent.parent / "docs"
HOST_MATRIX = DOCS_ROOT / "hosts" / "host-capability-matrix.md"
INTEGRATIONS_MATRIX = DOCS_ROOT / "integrations" / "host-matrix.md"

SUPPORTED_HOSTS = [
    "Claude Code",
    "Codex CLI",
    "Copilot",
    "opencode",
    "Gemini CLI",
]

REQUIRED_HOST_MATRIX_COLUMNS = [
    "Host",
    "Native surfaces Atelier uses",
    "MCP",
    "Hooks / events",
    "Wrapper",
    "Routing enforcement",
    "Trace confidence",
    "Unsupported controls",
    "Fallback",
]

REQUIRED_INTEGRATIONS_COLUMNS = [
    "Host",
    "Install path",
    "Interface",
    "Safe default",
    "Enforcement contract",
    "Trace coverage",
    "Unsupported controls",
    "Fallback",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_table_lines(content: str, header_start: str) -> list[str]:
    lines = content.splitlines()
    start = -1
    for i, line in enumerate(lines):
        if line.strip() == header_start:
            start = i + 1
            break
    assert start != -1, f"table heading not found: {header_start}"

    table: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if not table:
                continue
            break
        if not line.strip().startswith("|"):
            if table:
                break
            continue
        table.append(line)
    assert table, f"no table rows found under heading: {header_start}"
    return table


def test_host_contract_docs_exist() -> None:
    assert HOST_MATRIX.exists(), f"missing {HOST_MATRIX}"
    assert INTEGRATIONS_MATRIX.exists(), f"missing {INTEGRATIONS_MATRIX}"


def test_host_capability_matrix_has_required_columns_and_hosts() -> None:
    content = _read(HOST_MATRIX)
    table = _extract_table_lines(content, "## Capability Matrix")
    header = table[0]

    for col in REQUIRED_HOST_MATRIX_COLUMNS:
        assert col in header, f"host-capability-matrix missing required column: {col}"

    for host in SUPPORTED_HOSTS:
        assert host in content, f"host-capability-matrix missing host row: {host}"


def test_integrations_host_matrix_has_required_columns_and_hosts() -> None:
    content = _read(INTEGRATIONS_MATRIX)
    table = _extract_table_lines(content, "## Supported Hosts")
    header = table[0]

    for col in REQUIRED_INTEGRATIONS_COLUMNS:
        assert col in header, f"integrations host-matrix missing required column: {col}"

    for host in SUPPORTED_HOSTS:
        assert host in content, f"integrations host-matrix missing host row: {host}"
