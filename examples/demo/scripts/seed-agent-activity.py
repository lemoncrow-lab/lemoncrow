#!/usr/bin/env python3
"""Seed the Review demo through LemonCrow's real Codex capture/import path."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from lemoncrow.core.capabilities.plugin_runtime import (
    append_visible_session_turns,
    build_codex_post_tool_use_ledger_output,
    build_codex_session_end_output,
    build_codex_stop_output,
    build_codex_subagent_output,
)
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.review.development import DevelopmentStore
from lemoncrow.pro.capabilities.review.development_ingest import sync_before_capture
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_comments import SessionCommentIngestor, brief_agent_context
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

MODEL = "gpt-5.6"


def _prune_previous_demo_ledgers(root: Path, repo: Path) -> int:
    """Remove only earlier demo-owned ledgers for this exact demo workspace."""

    removed = 0
    sessions_root = root / "sessions"
    if not sessions_root.is_dir():
        return 0
    for run_file in sessions_root.rglob("run.json"):
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session_id = str(data.get("session_id") or "")
        workspace = str(data.get("workspace_path") or "")
        if not session_id or not workspace:
            continue
        try:
            matches = Path(workspace).expanduser().resolve() == repo
        except OSError:
            matches = False
        if not matches:
            continue
        shutil.rmtree(run_file.parent, ignore_errors=True)
        removed += 1
    return removed


def _server_review(root: Path, review_id: str, repo: Path) -> tuple[ReviewStore, object, object]:
    review_root = Path(
        os.environ.get("LEMONCROW_SERVER_REVIEW_ROOT") or (root / "server" / "data" / "reviews")
    ).expanduser()
    matches: list[tuple[ReviewStore, object, object]] = []
    if not review_root.is_dir():
        raise RuntimeError(f"local server Review root does not exist: {review_root}")
    for partition in sorted(review_root.iterdir()):
        if not partition.is_dir() or not (partition / "lemoncrow_reviews.db").exists():
            continue
        store = ReviewStore(partition)
        try:
            session = store.resolve_session(review_id)
        except (RuntimeError, ValueError):
            continue
        if session is None:
            continue
        marker = partition / ".workspace-roots" / session.id
        try:
            hinted = Path(marker.read_text(encoding="utf-8").strip()).expanduser().resolve()
        except (OSError, ValueError):
            hinted = None
        if hinted is not None and hinted != repo:
            continue
        revision = store.latest_revision(session.id)
        if revision is not None:
            matches.append((store, session, revision))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one local-server Review {review_id!r} for {repo}, found {len(matches)}")
    return matches[0]


def _line(path: Path, needle: str) -> int:
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if needle in text:
            return number
    raise RuntimeError(f"demo seed target not found in {path}: {needle}")


def _write_transcript(path: Path, messages: list[tuple[str, str]]) -> None:
    rows: list[dict[str, object]] = [
        {"type": "turn_context", "payload": {"model": MODEL}},
    ]
    for phase, body in messages:
        rows.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": phase,
                    "id": str(uuid.uuid4()),
                    "content": [{"type": "output_text", "text": body}],
                },
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _stop(root: Path, repo: Path, transcripts: Path, *, session_id: str, role: str, body: str) -> None:
    transcript = transcripts / f"{session_id}.jsonl"
    _write_transcript(transcript, [("final_answer", body)])
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "cwd": str(repo),
        "transcript_path": str(transcript),
        "agent_role": role,
        "model": MODEL,
    }
    build_codex_stop_output(root, payload)
    build_codex_session_end_output(root, {**payload, "hook_event_name": "SessionEnd"})


def _code_session(root: Path, repo: Path, transcripts: Path, *, session_id: str, line: int) -> None:
    transcript = transcripts / f"{session_id}.jsonl"
    _write_transcript(
        transcript,
        [
            (
                "commentary",
                f"I updated the shared pricing surface first; frontend/src/pages/PricingPage.jsx:{line} now carries the featured Team plan.",
            ),
            (
                "final_answer",
                "Implemented the pricing and review-state update across the web app, API, Storybook, CSV, media, and Compose surfaces while keeping the release coherent.",
            ),
        ],
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "cwd": str(repo),
        "transcript_path": str(transcript),
        "agent_role": "code",
        "model": MODEL,
    }
    build_codex_stop_output(root, payload)
    for path in ("frontend/src/pages/PricingPage.jsx", "backend/app.py"):
        build_codex_post_tool_use_ledger_output(
            root,
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "cwd": str(repo),
                "tool_name": "edit",
                "tool_input": {"file_path": path},
                "tool_response": {"exit_code": 0},
            },
        )
    build_codex_session_end_output(root, {**payload, "hook_event_name": "SessionEnd"})


def _test_subagent(
    root: Path,
    repo: Path,
    transcripts: Path,
    *,
    parent_id: str,
    child_id: str,
    line: int,
    message: str | None = None,
    command: list[str] | None = None,
    cwd: Path | None = None,
) -> None:
    transcript = transcripts / f"{child_id}.jsonl"
    _write_transcript(
        transcript,
        [
            (
                "final_answer",
                message
                or f"Backend verification passed after the update; backend/tests/test_app.py:{line} covers the new stale-mark count.",
            )
        ],
    )
    base = {
        "session_id": parent_id,
        "agent_id": child_id,
        "agent_type": "lemoncrow:test",
        "cwd": str(repo),
        "agent_transcript_path": str(transcript),
        "model": MODEL,
    }
    build_codex_subagent_output(root, {**base, "hook_event_name": "SubagentStart"})
    build_codex_subagent_output(root, {**base, "hook_event_name": "SubagentStop"})
    if command is None:
        command = (
            ["make", "test"]
            if shutil.which("make")
            else [sys.executable, "-m", "unittest", "discover", "-s", "backend/tests", "-v"]
        )
    command_display = " ".join(command)
    test_run = subprocess.run(
        command,
        cwd=cwd or repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    build_codex_post_tool_use_ledger_output(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": child_id,
            "cwd": str(repo),
            "tool_name": "shell",
            "tool_input": {"command": command_display},
            "tool_response": {
                "stdout": test_run.stdout,
                "stderr": test_run.stderr,
                "exit_code": test_run.returncode,
            },
        },
    )
    build_codex_session_end_output(
        root,
        {
            "hook_event_name": "SessionEnd",
            "session_id": child_id,
            "cwd": str(repo),
            "transcript_path": str(transcript),
            "agent_role": "test",
            "model": MODEL,
        },
    )
    if test_run.returncode:
        raise RuntimeError(f"demo backend verification failed:\n{test_run.stdout}\n{test_run.stderr}")


ORDERS_TASK = "Let customers view a guest checkout order before they sign in, and let promo codes stack."
ORDERS_REVISION_2_TASK = "Address the review notes: guest order visibility and the order total."
# Deliberately skips test_rejects_percent_over_100 and anything that sums an
# order, so the seeded verifier is green on the changed tree and only a full
# backend run (the Review's backend-tests runtime) catches the discount change.
ORDERS_VERIFIER_TESTS = [
    "tests.test_app",
    "tests.test_orders.CanViewOrderTest",
    "tests.test_orders.OrderDetailTest",
    "tests.test_orders.ApplyDiscountTest.test_percent_off",
    "tests.test_orders.ApplyDiscountTest.test_rejects_negative_percent",
]


def _seed_orders(root: Path, repo: Path, transcripts: Path, *, revision: int) -> tuple[str, str, str, str]:
    """Plan/code/test/review sessions for the orders scenario (Revision 2: code/test only)."""

    plan_id, code_id, test_id, review_id = (uuid.uuid4().hex for _ in range(4))
    guest_needle = "Guest checkout" if revision == 1 else "Ownerless guest orders"
    guest_line = _line(repo / "backend/orders.py", guest_needle)
    test_line = _line(repo / "backend/tests/test_orders.py", "def test_owner_can_view")
    if revision == 1:
        plan = (repo / "PLAN.md").read_text(encoding="utf-8")
        _stop(root, repo, transcripts, session_id=plan_id, role="plan", body=plan)
    task = ORDERS_TASK if revision == 1 else ORDERS_REVISION_2_TASK
    commentary = (
        f"I opened guest orders to signed-in users first; backend/orders.py:{guest_line} now allows ownerless orders."
        if revision == 1
        else f"Ownerless guest orders are admin-only again; backend/orders.py:{guest_line} holds the rule."
    )
    final = (
        "Let guests view their checkout order before sign-in and allowed promo codes to stack."
        if revision == 1
        else "Restricted guest orders to admins and fixed the order total to include every item."
    )
    transcript = transcripts / f"{code_id}.jsonl"
    _write_transcript(transcript, [("commentary", commentary), ("final_answer", final)])
    append_visible_session_turns(
        root,
        host="codex",
        session_id=code_id,
        turns=[("user", f"task-{code_id}", task)],
        workspace=str(repo),
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": code_id,
        "cwd": str(repo),
        "transcript_path": str(transcript),
        "agent_role": "code",
        "model": MODEL,
    }
    build_codex_stop_output(root, payload)
    paths = ["backend/orders.py"] if revision == 1 else ["backend/orders.py", "backend/tests/test_orders.py"]
    for path in paths:
        build_codex_post_tool_use_ledger_output(
            root,
            {
                "hook_event_name": "PostToolUse",
                "session_id": code_id,
                "cwd": str(repo),
                "tool_name": "edit",
                "tool_input": {"file_path": path},
                "tool_response": {"exit_code": 0},
            },
        )
    build_codex_session_end_output(root, {**payload, "hook_event_name": "SessionEnd"})

    tests = ORDERS_VERIFIER_TESTS + (["tests.test_orders.GuestOrderTest"] if revision == 2 else [])
    _test_subagent(
        root,
        repo,
        transcripts,
        parent_id=code_id,
        child_id=test_id,
        line=test_line,
        message=(
            f"Backend verification passed; backend/tests/test_orders.py:{test_line} covers the owner and admin view rules."
        ),
        command=["python3", "-m", "unittest", *tests],
        cwd=repo / "backend",
    )
    if revision == 2:
        return "", code_id, test_id, ""
    finding = json.dumps(
        {
            "findings": [
                {
                    "title": "Order access rules changed",
                    "body": "This changes who can open an order; check the access rules against the plan.",
                    "code_location": {
                        "absolute_file_path": str(repo / "backend/orders.py"),
                        "line_range": {"start": guest_line, "end": guest_line},
                    },
                }
            ],
            "overall_correctness": "patch is correct",
        }
    )
    _stop(root, repo, transcripts, session_id=review_id, role="review", body=finding)
    return plan_id, code_id, test_id, review_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--server-review", default="")
    parser.add_argument("--prune-only", action="store_true")
    parser.add_argument("--ledger-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--scenario", choices=("standard", "orders"), default="standard")
    parser.add_argument("--revision", type=int, choices=(1, 2), default=1, help="orders: agent revision to seed")
    args = parser.parse_args()

    # Match the installed Codex hook command. Runtime state paths use the active
    # host identity, so direct helper calls without this would be test-only
    # behavior and could split stats from the Codex run ledger.
    os.environ["LEMONCROW_AGENT"] = "codex"
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"demo repository is not initialized: {repo}")

    root = default_store_root()
    # Orders Revision 2 follows an already-captured Revision 1: keep its ledgers.
    keep = args.verify_only or (args.scenario == "orders" and args.revision == 2)
    removed = 0 if keep else _prune_previous_demo_ledgers(root, repo)
    if args.prune_only:
        print(f"Pruned prior demo ledgers: {removed}")
        return 0
    rng = resolve_rev_range(repo, working_tree=True)
    if args.verify_only and not args.server_review:
        raise SystemExit("--verify-only requires --server-review")
    if args.server_review and not args.ledger_only:
        store, session, revision = _server_review(root, args.server_review, repo)
    elif args.ledger_only:
        # Ledger seeding is intentionally server-agnostic. The next normal
        # Review capture imports these Codex/LemonCrow ledgers into whichever
        # configured Review server owns the Snapshot.
        store = None
        session = None
        revision = None
    else:
        store = ReviewStore(root)
        session = open_or_create_session(store, repo, rng, title="", force_new=True)
        revision = None

    if args.verify_only:
        SessionCommentIngestor(store, root).sync(session, force=True)
        sync_before_capture(store, session, repo_root=repo, source_store_root=root)
        revision = store.latest_revision(session.id) or revision
        annotations = store.list_annotations_at_revision(session.id, revision.id)
        context = brief_agent_context(store, session, revision)
        development = DevelopmentStore(store).list_sessions(session.id)
        print(f"Verified seeded server Review: {session.id}")
        print(f"  Snapshot: {revision.id}")
        print(f"  Comments/annotations on Snapshot: {len(annotations)}")
        print(f"  Captured agent context rows:      {len(context)}")
        print("  Development: " + ", ".join(f"{item.session_id}={item.role}/{item.status}" for item in development))
        # Only the pricing update, test result, and structured review finding
        # have explicit file locations. Plan/summary prose stays in discussion;
        # Brief also filters the intermediate pricing progress message.
        # Orders Revision 2 seeds only the follow-up code and test sessions.
        minimum = 2 if (args.scenario == "orders" and args.revision == 2) else 3
        if len(annotations) < minimum:
            raise RuntimeError(f"expected at least {minimum} seeded annotations, found {len(annotations)}")
        if len(context) < minimum:
            raise RuntimeError(f"expected at least {minimum} captured agent-context rows, found {len(context)}")
        roles = {item.role for item in development}
        if not {"author", "verifier"} <= roles:
            raise RuntimeError(f"server Review is missing author/verifier Development roles: {sorted(roles)}")
        return 0

    token = uuid.uuid4().hex[:8]
    transcripts = root / "review" / "agent-transcripts" / (session.id if session is not None else f"pre-review-{token}")
    if args.scenario == "orders":
        plan_id, code_id, test_id, review_id = _seed_orders(root, repo, transcripts, revision=args.revision)
    else:
        plan_id = uuid.uuid4().hex
        code_id = uuid.uuid4().hex
        test_id = uuid.uuid4().hex
        review_id = uuid.uuid4().hex

        pricing_line = _line(repo / "frontend/src/pages/PricingPage.jsx", "featured: true")
        test_line = _line(repo / "backend/tests/test_app.py", 'stale_marks"], 3')
        summary_line = _line(repo / "backend/app.py", '"stale_marks": 3')

        _stop(
            root,
            repo,
            transcripts,
            session_id=plan_id,
            role="plan",
            body=(
                "Keep pricing, review-summary counts, preview timeout, Storybook, CSV, media, and Compose behavior internally consistent. "
                "Preserve the same product surfaces and verification paths."
            ),
        )
        _code_session(root, repo, transcripts, session_id=code_id, line=pricing_line)
        _test_subagent(root, repo, transcripts, parent_id=code_id, child_id=test_id, line=test_line)

        finding = json.dumps(
            {
                "findings": [
                    {
                        "title": "Review summary counts changed",
                        "body": "This changes open, reviewed, and stale-mark counts; verify the API and request preview remain consistent.",
                        "code_location": {
                            "absolute_file_path": str(repo / "backend/app.py"),
                            "line_range": {"start": summary_line, "end": summary_line},
                        },
                    }
                ],
                "overall_correctness": "patch is correct",
            }
        )
        _stop(root, repo, transcripts, session_id=review_id, role="review", body=finding)

    if args.ledger_only:
        print("Seeded real LemonCrow agent ledgers before Review capture:")
        if removed:
            print(f"  Pruned prior demo ledgers: {removed}")
        print(f"  Sessions: {', '.join(filter(None, (plan_id, code_id, test_id, review_id)))}")
        return 0

    if revision is None:
        build = build_review_packet_with_blobs(
            repo,
            rng,
            store_root=root,
            with_patch_text=True,
            unbounded_patch_text=True,
        )
        revision = snapshot_revision(store, session, repo, rng, store_root=root, build=build)
        store.publish_snapshot(session.id, revision.id)
    SessionCommentIngestor(store, root).sync(session, force=True)
    sync_before_capture(store, session, repo_root=repo, source_store_root=root)

    annotations = store.list_annotations_at_revision(session.id, revision.id)
    context = brief_agent_context(store, session, revision)
    development = DevelopmentStore(store).list_sessions(session.id)
    sessions = tuple(filter(None, (plan_id, code_id, test_id, review_id)))
    print("Seeded real LemonCrow Review activity:")
    if removed:
        print(f"  Pruned prior demo ledgers: {removed}")
    print(f"  Review:   {session.id}")
    print(f"  Snapshot: {revision.id}")
    print(f"  Sessions: {', '.join(sessions)}")
    print(f"  Comments/annotations on Snapshot: {len(annotations)}")
    print(f"  Captured agent context rows:      {len(context)}")
    print("  Development: " + ", ".join(f"{item.session_id}={item.role}/{item.status}" for item in development))
    if not annotations:
        raise RuntimeError("demo activity seeded no Review annotations")
    if not any(row.get("source") == "author" for row in context):
        raise RuntimeError("demo activity seeded no captured author context")
    roles = {item.session_id: item.role for item in development}
    if roles.get(code_id) != "author":
        raise RuntimeError(f"demo code session was not classified as author: {roles.get(code_id)!r}")
    if roles.get(test_id) != "verifier":
        raise RuntimeError(f"demo test session was not classified as verifier: {roles.get(test_id)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
