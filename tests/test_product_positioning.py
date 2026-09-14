"""Pins the review-first product story on the three surfaces that carry it.

``docs/planning/2026-09-08-review-first-developer-workspace.md`` §1 states the
user problem as *"I have agents writing a lot more code. Help me understand what
they did."* and §23 demands that cost/benchmark numbers become **supporting
proof, not the reason the product exists**. Three surfaces are what a new reader
actually meets first:

1. ``README.md`` -- the headline block above the fold;
2. ``pyproject.toml`` ``[project].description`` -- the one-liner PyPI, ``pip
   show`` and every package index repeat verbatim;
3. ``lc review --help`` -- the command the story is about.

Each of those drifted back to context-engineering/cost framing at least once
before. These are cheap invariants so the next drift fails ``pytest -q`` instead
of shipping. They assert *ordering and absence*, never exact marketing prose --
copy is allowed to improve, it is not allowed to bury the review story or lead
with savings.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"

# The headline block is everything between the centred header div and the badge
# wall -- the part a reader sees without scrolling.
_BLOCK_START = '<div align="center">'
_BADGE_MARKER = "[![License]"

# Words that would mean the story leads with cost again. Checked only inside the
# headline block: the Results section below it is *supposed* to talk money.
_COST_WORDS = ("cheaper", "savings", "cost", "$", "spend", "bill")


def _headline_block() -> str:
    text = README.read_text(encoding="utf-8")
    _, opened, rest = text.partition(_BLOCK_START)
    assert opened, f"{README.name}: centred header div not found; the headline block markers moved"
    block, closed, _ = rest.partition(_BADGE_MARKER)
    assert closed, f"{README.name}: badge wall not found; the headline block markers moved"
    return block


def test_readme_headline_is_review_first() -> None:
    block = _headline_block()
    assert (
        "### Understand what your coding agents changed" in block
    ), "README headline must lead with the review story (plan §1), not with mechanism"
    assert "**Review-first developer workspace.**" in block


def test_readme_puts_benchmark_proof_below_the_review_story() -> None:
    """Plan §23: show the product before the benchmark numbers."""

    block = _headline_block()
    review_at = block.index("`lc review` answers what changed")
    proof_at = block.index("state-of-the-art context engineering")
    assert review_at < proof_at, "the context-engineering/benchmark paragraph must sit below the review story"


def test_readme_headline_does_not_lead_with_cost() -> None:
    block = _headline_block().lower()
    found = [word for word in _COST_WORDS if word in block]
    assert not found, f"README headline block leads with cost framing: {found} (plan §23)"


def test_pyproject_description_is_review_first() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    description = data["project"]["description"]
    assert description.startswith("Review what your coding agents changed"), description
    lowered = description.lower()
    assert "runtime" not in lowered, f"package one-liner still leads with mechanism: {description}"
    assert not any(word in lowered for word in _COST_WORDS), description


def test_pyproject_description_promises_only_what_ships() -> None:
    """Wheel metadata is the one surface with no room for a caveat.

    *"what changed since you last looked"* is revision reconciliation and the
    review frontier -- ``review/revisions.py`` plus ``review/anchors.py``
    (spec PR-R5). The sentence and those modules are one fact spelled two ways,
    so this asserts them against each other in **both** directions: the claim
    may not appear before the code does, and once the code is on disk the claim
    must be there.

    The earlier version of this guard retired itself -- ``return`` as soon as
    the module existed. It duly went quiet when R5 landed, and the softened
    one-liner it had been holding back stayed softened for a whole wave with
    nothing left to notice. A guard that passes by doing nothing is not a guard,
    which is why the post-condition is an assertion and not an early exit.
    """

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    description = data["project"]["description"].lower()
    review = ROOT / "src" / "lemoncrow" / "pro" / "capabilities" / "review"
    modules = ("revisions.py", "anchors.py")
    missing = [name for name in modules if not (review / name).exists()]
    claimed = "since you last looked" in description
    if missing:
        assert not claimed, (
            "the package one-liner promises revision reconciliation (PR-R5), which has not shipped "
            f"({', '.join(missing)} missing): {description}"
        )
        return
    assert claimed, (
        "revision reconciliation ships ("
        f"{', '.join(modules)} on disk) but the package one-liner still hides it: {description}"
    )
