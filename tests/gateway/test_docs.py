"""Docs and repo-governance checks for the live docs tree."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = ROOT / "docs"
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
CODE_FENCE_PATTERN = re.compile(r"```.*?```", re.DOTALL)

REQUIRED_DOCS = []


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
            if href.startswith(("http://", "https://", "mailto:")):
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


CLI_REFERENCE = DOCS_ROOT / "reference" / "cli.md"
TEXT_BLOCK_PATTERN = re.compile(r"```text\n(.*?)```", re.DOTALL)


def _usage_table_basis_cells(text: str) -> list[str]:
    """Return the BASIS cell of every data row in the ``lc usage`` samples.

    A data row is ``NAME  SESSIONS  ROWS  TOKENS  COST  BASIS`` -- six columns
    whose second and third are counts. That shape skips the header, the rule,
    and the prose footer without needing to know any of their wording.
    """

    cells: list[str] = []
    for block in TEXT_BLOCK_PATTERN.findall(text):
        if "BASIS" not in block:
            continue
        for line in block.splitlines():
            columns = line.split()
            if len(columns) != 6:
                continue
            if not all(part.replace(",", "").isdigit() for part in columns[1:3]):
                continue
            cells.append(columns[5])
    return cells


def test_cli_reference_shows_only_a_usage_basis_the_code_can_produce() -> None:
    """The reference must not print a BASIS word ``derive_cost`` cannot reach.

    ``derive_cost`` is the only writer of ``cost_provenance``, and a run
    ledger's recorded total is LemonCrow's own rate-card sum rather than a
    vendor figure, so it stamps ``api_estimated`` and never ``provider_billed``.
    The sample output kept showing ``billed`` -- the exact invoice claim, still
    shipping in the user-facing CLI reference after the code stopped making it.
    """

    import inspect

    from lemoncrow.pro.capabilities.usage.collect import derive_cost
    from lemoncrow.pro.capabilities.usage.models import COST_PROVENANCE_VALUES
    from lemoncrow.pro.capabilities.usage.render import _BASIS_LABELS

    source = inspect.getsource(derive_cost)
    reachable = {value for value in COST_PROVENANCE_VALUES if f'"{value}"' in source}
    assert reachable, "could not read any provenance out of derive_cost"
    producible = {_BASIS_LABELS[value] for value in reachable} | {"mixed"}

    cells = _usage_table_basis_cells(CLI_REFERENCE.read_text(encoding="utf-8"))
    assert cells, "no `lc usage` sample table found in the CLI reference"
    offenders = [cell for cell in cells if not set(cell.split("+")) <= producible]
    assert not offenders, "CLI reference shows a BASIS the code cannot produce: " + ", ".join(sorted(set(offenders)))


def test_cli_reference_does_not_call_a_usage_figure_a_vendor_invoice() -> None:
    """No dollar figure `lc usage` prints was ever charged by a vendor.

    Every total it shows is summed from LemonCrow's own rate card -- per
    recorded call when a run ledger has one, over the session aggregate
    otherwise. Telling a reader that one of them "is a figure the vendor
    charged" invites them to reconcile it against an invoice it is not.
    """

    text = CLI_REFERENCE.read_text(encoding="utf-8")
    offenders = [line.strip() for line in text.splitlines() if re.search(r"vendor (charged|billed)", line)]
    assert not offenders, "CLI reference claims a usage figure came from a vendor:\n" + "\n".join(offenders)


def test_live_docs_do_not_reference_removed_internal_path() -> None:
    offenders: list[str] = []
    for md_file in markdown_files():
        text = md_file.read_text(encoding="utf-8")
        if "docs/internal/" in text:
            offenders.append(str(md_file.relative_to(ROOT)))
    assert not offenders, "Live docs still reference removed docs/internal paths:\n" + "\n".join(offenders)
