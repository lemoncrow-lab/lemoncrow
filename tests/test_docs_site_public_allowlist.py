"""docs-site publishes an explicit allowlist of docs/, never the internal folders."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "docs-site" / "sync-docs.mjs"

INTERNAL_DIRS = (
    "planning",
    "superpowers",
    "usability",
    "operations",
    "marketing",
    "reports",
    "status",
    "runbooks",
    "performance",
    "guides",
)
# The only internal-folder pages docs.lemoncrow.com publishes.
PUBLIC_EXCEPTIONS = {
    "planning/roadmap.md",
    "planning/savings-optimization-roadmap.md",
    "operations/production-readiness.md",
}


@pytest.fixture(scope="module")
def synced(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = tmp_path_factory.mktemp("docs-site-docs")
    subprocess.run([node, str(SYNC), "--out", str(out)], check=True, capture_output=True)
    return {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}


def test_no_internal_folder_is_copied(synced: set[str]) -> None:
    leaked = sorted(rel for rel in synced if rel.split("/")[0] in INTERNAL_DIRS and rel not in PUBLIC_EXCEPTIONS)
    assert leaked == []


def test_the_public_set_is_copied(synced: set[str]) -> None:
    for page in ("README.md", "setup/installation.md", "reference/cli.md", "hosts/claude-code-install.md"):
        assert page in synced
    assert not any(rel.startswith(("superpowers/", "usability/")) for rel in synced)
    assert "planning/2026-09-07-agent-runtime-competitive-plan.md" not in synced
