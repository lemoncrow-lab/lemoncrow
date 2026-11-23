from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
HOST_MATRIX = REPO_ROOT / "docs" / "hosts" / "host-capability-matrix.md"

MAIN_HOSTS = [
    "Claude Code",
    "Codex",
    "OpenCode",
    "Cursor",
    "Pi",
]

ADDITIONAL_HOSTS = ["Copilot", "Hermes Agent", "Antigravity"]

REQUIRED_MAIN_COLUMNS = [
    "Host",
    "User-facing model",
    "Temporary launch",
    "Persistent integration",
    "Lifecycle/evidence",
    "Owned-loop frontend",
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


def test_host_capability_matrix_has_current_product_contract() -> None:
    content = _read(HOST_MATRIX)
    table = _extract_table_lines(content, "## Main product integrations")
    header = table[0]

    for col in REQUIRED_MAIN_COLUMNS:
        assert col in header, f"host-capability-matrix missing required column: {col}"

    for host in MAIN_HOSTS:
        assert host in content, f"host-capability-matrix missing main integration: {host}"

    for host in ADDITIONAL_HOSTS:
        assert host in content, f"host-capability-matrix missing additional integration: {host}"


def test_host_capability_matrix_preserves_host_distinctions() -> None:
    content = _read(HOST_MATRIX)
    assert "`lc with claude`" in content
    assert "`lc with codex`" in content
    assert "`lc with opencode`" in content
    assert "Installed IDE + `cursor-agent` integration" in content
    assert "`lc with pi`" in content
    assert "Public managed coding-agent host" in content
    assert "Pi" in content
    assert "lc with cursor" not in content
