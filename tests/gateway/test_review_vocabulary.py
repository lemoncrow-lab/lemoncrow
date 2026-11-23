"""One vocabulary for Review: ``Revision N`` counted from 1, and "parts" for the reviewable pieces.

The server, the wire and the stores count revisions from 0 (``revision_number``/``snapshot_number`` stay raw in JSON);
every text a person reads adds one exactly once, through ``revision_name``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from lemoncrow_client.review_terminal import revision_label
from lemoncrow_client.review_words import part_count, revision_name

from lemoncrow.pro.capabilities.review.feedback import FeedbackBundle, render_markdown

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("raw", "shown"), [(0, "Revision 1"), (1, "Revision 2"), (9, "Revision 10"), ("3", "Revision 4")]
)
def test_revision_name_counts_from_one(raw: object, shown: str) -> None:
    assert revision_name(raw) == shown
    assert revision_label(raw) == shown


@pytest.mark.parametrize("raw", [None, "", "x"])
def test_revision_name_never_invents_a_number(raw: object) -> None:
    assert revision_name(raw) == "Revision ?"


def test_part_count_uses_the_one_unit_with_correct_plural() -> None:
    assert [part_count(n) for n in (0, 1, 2, 6)] == ["0 parts", "1 part", "2 parts", "6 parts"]


def test_feedback_for_an_agent_names_the_revision_once_from_one() -> None:
    bundle = FeedbackBundle(review_id="r1", revision_id="rev-a", revision_number=0, range_mode="working_tree")
    text = render_markdown(bundle)
    assert "Revision 1 · working tree" in text
    assert "Snapshot" not in text
    assert "Revision 0" not in text
    assert "Revision 2" not in render_markdown(FeedbackBundle(review_id="r1", revision_id="rev-a", revision_number=0))


def test_cli_status_labels_use_the_same_rule() -> None:
    from lemoncrow.gateway.cli.commands.review import _revision_label

    assert _revision_label(0) == "Revision 1"
    assert _revision_label(4) == "Revision 5"


# Files whose "Snapshot" strings are model prompts, tool descriptions or matching markers: they never reach a person.
_NOT_USER_FACING = {
    "ai_review.py",
    "rule_checks.py",
    "ask.py",
    "tool_server.py",
    "repo_context.py",
    "review_model_broker.py",
    "review_agent_adapters.py",
    "diagnostic_bundle.py",
    "docker_verification_executor.py",
}
_SNAPSHOT_WORD = re.compile(r"(?<![_\w$/.])Snapshots?(?![_\w(])")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def test_review_text_never_says_snapshot() -> None:
    roots = [
        ROOT / "src/lemoncrow/pro/capabilities/review",
        ROOT / "src/lemoncrow/gateway/cli/commands",
        ROOT / "client/src/lemoncrow_client",
        ROOT / "enterprise/server/src/lemoncrow_server",
        ROOT / "server/src/lemoncrow_server_core",
    ]
    offenders: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        # The local server core is Review-only, so every file in it reaches the reader.
        every_file = root.name == "lemoncrow_server_core"
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if (
                path.name in _NOT_USER_FACING
                or "/ops/" in relative
                or not (
                    every_file
                    or path.name.startswith("review")
                    or "/review" in relative
                    or "github_review" in relative
                    or "capabilities/review/" in relative
                )
            ):
                continue
            tree = ast.parse(path.read_text())
            docstrings = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and _SNAPSHOT_WORD.search(node.value)
                ):
                    offenders.append(f"{relative}:{node.lineno}: {node.value[:80]!r}")
    assert not offenders, "\n".join(offenders)
