from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lemoncrow.pro.capabilities.review.models import (
    ChangedFile,
    EvidenceRecord,
    ImpactSite,
    ReviewOrderEntry,
    ReviewPacket,
)
from lemoncrow.pro.capabilities.review.preparation import build_review_brief, project_lemoncrow_annotations
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewEvidence,
    ReviewRevision,
    ReviewSession,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id


def _seed(tmp_path: Path) -> tuple[ReviewStore, ReviewSession, ReviewRevision]:
    store = ReviewStore(tmp_path / "store")
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root=str(tmp_path / "repo"),
            range_mode="working_tree",
            title="review",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
        ),
        [
            ReviewUnit(
                revision_id="",
                unit_key="file:src/app.py",
                kind="file",
                path="src/app.py",
                content_fingerprint="fp-app-v1",
                attention_rank=1,
                reasons=("7 known callers", "high-centrality symbol"),
            )
        ],
    )
    return store, session, revision


def _packet() -> ReviewPacket:
    return ReviewPacket(
        schema_version=1,
        generated_at="2026-09-09T00:00:00+00:00",
        repo_root="/repo",
        range_mode="working_tree",
        base_rev="HEAD",
        head_rev="WORKDIR",
        base_sha="abc",
        head_sha="",
        files=(ChangedFile(path="src/app.py", old_path=None, status="modified", additions=8, deletions=2),),
        impact=(
            ImpactSite(
                kind="untouched_caller",
                path="src/caller.py:L12",
                old="app()",
                new=None,
                snippet="app()",
                in_patch=False,
                source_path="src/app.py",
            ),
        ),
        order=(
            ReviewOrderEntry(
                path="src/app.py",
                rank=1,
                score=10.0,
                reasons=("7 known callers", "high-centrality symbol"),
            ),
        ),
        evidence=(EvidenceRecord(name="Focused tests", status="PASS", detail="pytest (exit 0)", source="session:s1"),),
    )


def test_deterministic_preparation_adds_one_compact_lemoncrow_annotation_and_is_idempotent(tmp_path: Path) -> None:
    store, session, revision = _seed(tmp_path)
    packet = _packet()

    assert (
        project_lemoncrow_annotations(
            store, session, revision, packet, new_blobs={"src/app.py": "def app():\n    return 2\n"}
        )
        == 1
    )
    (annotation,) = store.list_annotations(session.id)
    assert annotation.source == "lemoncrow"
    assert annotation.created_by_actor == "unknown"
    assert annotation.kind == "comment"
    assert annotation.anchor.start_line == 0
    assert "7 known callers" in annotation.body
    assert "outside this patch" in annotation.body
    assert annotation.source_id == "attention:src/app.py:fp-app-v1"

    assert (
        project_lemoncrow_annotations(
            store, session, revision, packet, new_blobs={"src/app.py": "def app():\n    return 2\n"}
        )
        == 0
    )
    assert len(store.list_annotations(session.id)) == 1


def test_review_brief_orients_before_the_human_opens_a_file(tmp_path: Path) -> None:
    store, session, revision = _seed(tmp_path)
    project_lemoncrow_annotations(
        store, session, revision, _packet(), new_blobs={"src/app.py": "def app():\n    return 2\n"}
    )
    groups = [
        {
            "key": "needs_attention",
            "rows": [
                {
                    "path": "src/app.py",
                    "attention_rank": 1,
                    "reasons": ["+151 -9", "7 known callers"],
                    "group": "needs_attention",
                }
            ],
        }
    ]
    chapters = {
        "intent": [{"label": "Review", "file_count": 1, "min_attention_rank": 1}],
        "commits": [
            {
                "key": "commit:abc",
                "label": "feat(review): browser workspace and durable review state",
                "file_count": 12,
                "attention_count": 4,
                "min_attention_rank": 1,
                "rows": [
                    {"path": "src/review/store.py", "attention_rank": 1},
                    {"path": "src/review/api.py", "attention_rank": 3},
                ],
            },
            {
                "key": "commit:def",
                "label": "fix(review): small follow-up",
                "file_count": 2,
                "attention_count": 1,
                "min_attention_rank": 3,
                "rows": [{"path": "src/review/types.py", "attention_rank": 3}],
            },
        ],
    }
    brief = build_review_brief(
        packet=_packet().to_dict(),
        groups=groups,
        chapters=chapters,
        annotations=store.list_annotations(session.id),
        artifacts=(),
        revision_id=revision.id,
        targets=(
            SimpleNamespace(path="src/review/store.py"),
            SimpleNamespace(path="src/review/store.py"),
            SimpleNamespace(path="src/review/api.py"),
            SimpleNamespace(path="src/review/types.py"),
        ),  # type: ignore[arg-type]
    )

    assert brief["summary"] == "1 files across Review; 1 currently deserve focused attention."
    assert brief["major_changes"] == [
        {
            "key": "commit:abc",
            "label": "feat(review): browser workspace and durable review state",
            "file_count": 12,
            "target_count": 3,
            "attention_count": 4,
            "first_path": "src/review/store.py",
        },
        {
            "key": "commit:def",
            "label": "fix(review): small follow-up",
            "file_count": 2,
            "target_count": 1,
            "attention_count": 1,
            "first_path": "src/review/types.py",
        },
    ]
    assert brief["review_first"] == [{"path": "src/app.py", "rank": 1, "reason": "7 known callers"}]
    assert brief["verification"]["pass"] == 1
    assert brief["annotations"]["lemoncrow"] == 1


def test_review_brief_reports_the_newest_run_of_a_check_not_the_first(tmp_path: Path) -> None:
    """A check that passed and was then re-run to FAIL is outstanding, not clear.

    ``list_evidence`` hands back rows newest-first, so the first row carrying a
    check title is its newest run. Overwriting the entry as the loop walked on
    kept the *oldest* row's status, and the finish sheet reads ``fail`` straight
    out of this block: a failing re-run of a previously passing check counted as
    zero failures and rendered as "nothing outstanding" over a live FAIL.
    """

    store, session, revision = _seed(tmp_path)
    for created_at, status in (("2026-09-09T10:00:00+00:00", "PASS"), ("2026-09-09T11:00:00+00:00", "FAIL")):
        store.add_evidence(
            ReviewEvidence(
                id="",
                review_id=session.id,
                revision_id=revision.id,
                kind="log",
                title="Notes tests",
                path="src/app.py",
                source="agent",
                verification_status=status,
                created_at=created_at,
            )
        )
    artifacts = store.list_evidence(session.id)
    assert [item.verification_status for item in artifacts] == ["FAIL", "PASS"], "evidence is newest-first"

    brief = build_review_brief(
        packet=None,
        groups=[],
        chapters={},
        annotations=(),
        artifacts=artifacts,
        revision_id=revision.id,
    )

    # One check, one status: the newest run, deduped by title.
    assert brief["verification"] == {"pass": 0, "fail": 1, "not_run": 0, "unknown": 0}
    # `_finish_payload` derives `failed_verification` from exactly this field.
    assert int(brief["verification"]["fail"] or 0) == 1


def test_the_newest_run_wins_whatever_order_the_rows_are_handed_over_in(tmp_path: Path) -> None:
    """Newest-first is a fact about the rows, not a favour asked of the caller.

    ``artifacts`` is typed ``Sequence[Any]``, so nothing in the signature can
    hold a caller to the order ``list_evidence`` happens to return today, and a
    dedupe that keeps whichever row it saw first inverts its whole answer if one
    reverses it -- a live FAIL rendered as a passing check, which is the exact
    failure the newest-run rule exists to prevent. So the order is re-derived
    from the same key the store sorts on.
    """

    store, session, revision = _seed(tmp_path)
    for created_at, status in (("2026-09-09T10:00:00+00:00", "PASS"), ("2026-09-09T11:00:00+00:00", "FAIL")):
        store.add_evidence(
            ReviewEvidence(
                id="",
                review_id=session.id,
                revision_id=revision.id,
                kind="log",
                title="Notes tests",
                path="src/app.py",
                source="agent",
                verification_status=status,
                created_at=created_at,
            )
        )
    oldest_first = tuple(reversed(store.list_evidence(session.id)))
    assert [item.verification_status for item in oldest_first] == ["PASS", "FAIL"], "handed over the wrong way round"

    brief = build_review_brief(
        packet=None,
        groups=[],
        chapters={},
        annotations=(),
        artifacts=oldest_first,
        revision_id=revision.id,
    )

    assert brief["verification"] == {"pass": 0, "fail": 1, "not_run": 0, "unknown": 0}
