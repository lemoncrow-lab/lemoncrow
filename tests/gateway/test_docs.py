"""Docs and repo-governance checks for the live docs tree."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = ROOT / "docs"
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
CODE_FENCE_PATTERN = re.compile(r"```.*?```", re.DOTALL)

REQUIRED_DOCS = [
    DOCS_ROOT / "README.md",
    DOCS_ROOT / "agent-os/README.md",
    DOCS_ROOT / "architecture/README.md",
    DOCS_ROOT / "design/index.md",
    DOCS_ROOT / "frontend/README.md",
    DOCS_ROOT / "reliability/README.md",
    DOCS_ROOT / "security/README.md",
    DOCS_ROOT / "quality/scorecard.md",
    DOCS_ROOT / "plans/README.md",
    DOCS_ROOT / "decisions/README.md",
    DOCS_ROOT / "references/README.md",
]


def markdown_files() -> list[Path]:
    return sorted(DOCS_ROOT.rglob("*.md"))


def test_docs_directory_exists() -> None:
    assert DOCS_ROOT.exists()
    assert DOCS_ROOT.is_dir()


def test_required_live_docs_exist() -> None:
    for path in REQUIRED_DOCS:
        assert path.exists(), f"Missing required live doc: {path.relative_to(ROOT)}"


def test_markdown_files_are_non_empty() -> None:
    files = markdown_files()
    assert files, "No markdown files found in docs/"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"{path.relative_to(ROOT)} is empty"


def test_internal_links_resolve() -> None:
    broken: list[str] = []
    for md_file in markdown_files():
        content = CODE_FENCE_PATTERN.sub("", md_file.read_text(encoding="utf-8"))
        for label, href in LINK_PATTERN.findall(content):
            if href.startswith(("http://", "https://")):
                continue
            href_path = href.split("#", 1)[0]
            if not href_path:
                continue
            # Resolve relative to the parent dir of the doc, as Markdown does
            target = (md_file.parent / href_path).resolve()
            if not target.exists():
                broken.append(f"{md_file.relative_to(ROOT)} -> [{label}]({href})")
    # Docs/plans/ and docs/quality/ may reference files not yet created
    known_forward_refs = {
        "docs/plans/README.md",
        "docs/plans/quality-and-benchmark-lift/index.md",
    }
    actual_broken = [b for b in broken if b.split(" ->")[0] not in known_forward_refs]
    assert not actual_broken, "Broken internal links:\n" + "\n".join(actual_broken)


def test_live_docs_do_not_reference_removed_internal_path() -> None:
    offenders: list[str] = []
    for md_file in markdown_files():
        text = md_file.read_text(encoding="utf-8")
        if "docs/internal/" in text:
            offenders.append(str(md_file.relative_to(ROOT)))
    assert not offenders, "Live docs still reference removed docs/internal paths:\n" + "\n".join(offenders)


def test_docs_tree_contains_expected_directories() -> None:
    assert DOCS_ROOT.is_dir()
    assert (DOCS_ROOT / "plans").is_dir()
    assert (DOCS_ROOT / "decisions").is_dir()
