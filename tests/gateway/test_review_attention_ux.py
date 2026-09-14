from __future__ import annotations

from lemoncrow.pro.capabilities.review.chapters import (
    _dependency_order,
    _intent_chapters,
    _scope_label,
    change_story_labels,
)
from lemoncrow.pro.capabilities.review.models import ChangedFile, ReviewOrderEntry, ReviewPacket
from lemoncrow.pro.capabilities.review.render import render_review


def test_multi_scope_commit_is_not_one_fake_concept() -> None:
    assert _scope_label("review") == "Review"
    assert _scope_label("review-ux") == "Review Ux"
    assert _scope_label("review,usage,model") == ""
    assert _scope_label("review/usage") == ""


def test_broad_commit_scope_falls_back_to_human_sized_structure() -> None:
    rows = [
        {"path": f"frontend/src/review/F{index}.tsx", "group": "unreviewed", "attention_rank": index + 1}
        for index in range(18)
    ] + [
        {"path": f"docs/review-{index}.md", "group": "unreviewed", "attention_rank": index + 19} for index in range(18)
    ]
    paths = {str(row["path"]) for row in rows}
    chapters = _intent_chapters(rows, [{"scope": "review", "paths": paths}])

    assert {chapter["label"] for chapter in chapters} == {"Frontend", "Docs"}
    assert max(chapter["file_count"] for chapter in chapters) == 18


def test_dependency_lens_spends_attention_first_when_chapters_are_equally_ready() -> None:
    rows = [
        {"path": "src/lemoncrow/infra/runtime.py", "group": "needs_attention", "attention_rank": 1},
        {"path": "src/lemoncrow/gateway/api.py", "group": "needs_attention", "attention_rank": 2},
        {"path": "docs/design.md", "group": "unreviewed", "attention_rank": 0},
        {"path": "frontend/src/App.tsx", "group": "unreviewed", "attention_rank": 0},
    ]
    chapters = _intent_chapters(rows, [])

    ordered = _dependency_order(chapters, {"impact": []})

    assert [chapter["label"] for chapter in ordered[:2]] == ["Infra", "Gateway"]


def test_dependency_lens_respects_dependency_before_attention_priority() -> None:
    rows = [
        {"path": "src/lemoncrow/infra/runtime.py", "group": "needs_attention", "attention_rank": 1},
        {"path": "docs/design.md", "group": "unreviewed", "attention_rank": 0},
    ]
    chapters = _intent_chapters(rows, [])
    packet = {
        "impact": [
            {
                "source_path": "docs/design.md",
                "path": "src/lemoncrow/infra/runtime.py:L10",
            }
        ]
    }

    ordered = _dependency_order(chapters, packet)

    assert [chapter["label"] for chapter in ordered] == ["Docs", "Infra"]


def test_change_story_prefers_concepts_over_utility_buckets() -> None:
    chapters = [
        {"label": "Other", "file_count": 20, "min_attention_rank": 1},
        {"label": "Tests", "file_count": 18, "min_attention_rank": 2},
        {"label": "Review", "file_count": 40, "min_attention_rank": 3},
        {"label": "Usage", "file_count": 12, "min_attention_rank": 4},
        {"label": "Gateway", "file_count": 4, "min_attention_rank": 5},
        {"label": "Core", "file_count": 3, "min_attention_rank": 6},
        {"label": "Install", "file_count": 2, "min_attention_rank": 0},
    ]

    labels, remaining = change_story_labels(chapters, limit=4)

    assert labels == ("Review", "Usage", "Gateway", "Core")
    assert remaining == 1


def test_default_terminal_review_spends_attention_on_only_eight_ranked_files() -> None:
    files = tuple(
        ChangedFile(path=f"src/f{index}.py", old_path=None, status="modified", additions=index, deletions=0)
        for index in range(1, 13)
    )
    order = tuple(
        ReviewOrderEntry(path=item.path, rank=index, score=float(20 - index), reasons=("public contract changed",))
        for index, item in enumerate(files, start=1)
    )
    packet = ReviewPacket(
        schema_version=2,
        generated_at="2026-09-09T00:00:00+00:00",
        repo_root="/tmp/repo",
        range_mode="commit_range",
        base_rev="main",
        head_rev="HEAD",
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=files,
        order=order,
        stats={
            "files": 12,
            "additions": sum(range(1, 13)),
            "deletions": 0,
            "hunks": 0,
            "symbols": 0,
            "impact_sites": 0,
        },
    )

    compact = render_review(packet, no_color=True)
    expanded = render_review(packet, no_color=True, show_all=True)

    assert "8. src/f8.py" in compact
    assert "9. src/f9.py" not in compact
    assert "… and 4 more (lc review --all)" in compact
    assert "9. src/f9.py" in expanded
    assert "12. src/f12.py" in expanded
