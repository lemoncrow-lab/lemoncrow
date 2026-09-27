from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import lemoncrow.pro.capabilities.review.chapters as chapters_mod
from lemoncrow.pro.capabilities.review.chapters import (
    _dependency_order,
    _intent_chapters,
    _scope_label,
    _summary_concept_label,
    build_chapter_lenses,
    change_story_labels,
)
from lemoncrow.pro.capabilities.review.models import ChangedFile, ReviewOrderEntry, ReviewPacket
from lemoncrow.pro.capabilities.review.preparation import build_review_brief
from lemoncrow.pro.capabilities.review.render import render_review


def test_multi_scope_commit_is_not_one_fake_concept() -> None:
    assert _scope_label("review") == "Review"
    assert _scope_label("review-ux") == "Review Ux"
    assert _scope_label("review,usage,model") == ""
    assert _scope_label("review/usage") == ""


def test_concept_label_truncation_does_not_leave_a_dangling_connector() -> None:
    assert (
        _summary_concept_label("feat(frontend): Code page file viewer, worktree picker and agent run")
        == "Code page file viewer, worktree picker"
    )


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


def test_change_story_prefers_specific_concepts_over_source_area_buckets() -> None:
    chapters = [
        {"label": "Review", "file_count": 40, "min_attention_rank": 1},
        {"label": "Frontend", "file_count": 18, "min_attention_rank": 2},
        {"label": "Server", "file_count": 16, "min_attention_rank": 3},
        {"label": "Live review ownership", "file_count": 10, "min_attention_rank": 4},
        {"label": "Human judgment pinning", "file_count": 9, "min_attention_rank": 5},
        {"label": "Agent feedback loop", "file_count": 8, "min_attention_rank": 6},
        {"label": "Verification concurrency", "file_count": 7, "min_attention_rank": 7},
        {"label": "Review history", "file_count": 6, "min_attention_rank": 8},
    ]

    labels, remaining = change_story_labels(chapters, limit=4)

    assert labels == (
        "Live review ownership",
        "Human judgment pinning",
        "Agent feedback loop",
        "Verification concurrency",
    )
    assert remaining == 1


def test_large_repository_change_starts_with_grounded_concepts_and_target_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-2's real 25-file Change is representative of the agentic-review shape."""

    root = Path(__file__).resolve().parents[2]
    paths = [
        "frontend/src/review/ReviewHistorySheet.tsx",
        "frontend/src/review/ReviewHistorySheet.test.tsx",
        "frontend/src/review/ReviewReadinessSheet.tsx",
        "frontend/src/review/ReviewReadinessSheet.test.tsx",
        "src/lemoncrow/pro/capabilities/review/api.py",
        "tests/gateway/test_review_api.py",
        "src/lemoncrow/pro/capabilities/review/store.py",
        "tests/gateway/test_review_store.py",
        "enterprise/server/src/lemoncrow_server/postgres_review.py",
        "enterprise/server/tests/test_postgres_review.py",
        "src/lemoncrow/pro/capabilities/review/session_models.py",
        "src/lemoncrow/pro/capabilities/review/relationships.py",
    ]
    assert all((root / path).is_file() for path in paths)
    rows = [
        {
            "path": path,
            "group": "changed_since_my_review" if index == 0 else "reviewed" if index % 3 == 0 else "unreviewed",
            "attention_rank": index + 1,
            "category": "test" if ".test." in path or "/tests/" in path or path.startswith("tests/") else "production",
        }
        for index, path in enumerate(paths)
    ]
    commit = {
        "sha": "4fa6e30a2",
        "summary": "feat(review): make Change identity durable",
        "scope": "review",
        "paths": set(paths),
    }
    monkeypatch.setattr(chapters_mod, "_commit_records", lambda *_args, **_kwargs: [commit])
    targets = tuple(
        SimpleNamespace(
            target_id=f"target-{index}",
            path=path,
            label=Path(path).stem,
            symbol="",
            kind="file",
            start_line=10 + index,
            end_line=12 + index,
            state="changed_since_review" if index == 0 else "reviewed" if index % 3 == 0 else "unreviewed",
            changed_since_mark=index == 0,
            attention_rank=index + 1,
        )
        for index, path in enumerate(paths)
    )

    lenses = build_chapter_lenses(rows, {"impact": []}, repo_root=root, targets=targets)
    labels = {chapter["label"] for chapter in lenses["intent"]}

    assert {"Review history", "Review readiness", "Review API", "Review store", "Review postgres"} <= labels
    history = next(chapter for chapter in lenses["intent"] if chapter["label"] == "Review history")
    assert history["file_count"] == 2
    assert history["target_count"] == 2
    assert history["progress"]["changed_since_review"] == 1
    assert history["targets"][0]["target_id"] == "target-0"
    assert history["targets"][0]["start_line"] == 10
    assert "Grounded in commit: Make Change identity durable" in history["explanation"]

    story, more = change_story_labels(lenses["intent"], limit=4)
    assert len(story) == 4
    assert "Review history" in story
    assert more >= 1
    assert lenses["dependency"]
    assert lenses["commits"][0]["label"] == "feat(review): make Change identity durable"

    brief = build_review_brief(
        packet={"evidence": []},
        groups=({"key": "unreviewed", "rows": rows},),
        chapters=lenses,
        annotations=(),
        artifacts=(),
        revision_id="revision-large",
        targets=targets,  # type: ignore[arg-type]
    )
    major_labels = [item["label"] for item in brief["major_changes"]]
    assert len(major_labels) == len(story) + more
    assert "Review history" in major_labels
    history_change = next(item for item in brief["major_changes"] if item["label"] == "Review history")
    assert history_change["target_count"] == 2
    assert history_change["changed_since_review"] == 1
    assert "Key targets:" in history_change["explanation"]


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
