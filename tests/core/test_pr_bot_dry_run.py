from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from atelier.core.capabilities.lesson_promotion.pr_bot import LessonPrBot
from atelier.core.foundation.lesson_models import LessonCandidate
from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.store import ContextStore


def test_pr_bot_dry_run_emits_diff_without_side_effects(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    store = ContextStore(root)
    store.init()

    block = ReasonBlock(
        id="rb.lesson.test",
        title="Lesson test block",
        domain="coding",
        situation="When repeated failures happen.",
        triggers=["repeated failure"],
        dead_ends=["retry blindly"],
        procedure=["Inspect error signature", "Apply targeted fix"],
        verification=["Run tests"],
        failure_signals=["same stack trace"],
    )
    candidate = LessonCandidate(
        id="lc-dry-run",
        domain="coding",
        cluster_fingerprint="same stack trace",
        kind="new_block",
        proposed_block=block,
        evidence_trace_ids=["tr-1", "tr-2"],
        confidence=0.9,
        status="approved",
    )
    store.upsert_lesson_candidate(candidate)

    calls: list[list[str]] = []

    def _runner(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    bot = LessonPrBot(
        store=store,
        root=root,
        env={"ATELIER_LESSON_PR_BOT_ENABLED": "true", "GITHUB_TOKEN": "token"},
        run_cmd=_runner,
    )
    payload = bot.sync_pr(lesson_id=candidate.id, dry_run=True)

    assert payload["skipped"] is False
    assert payload["dry_run"] is True
    assert payload["branch"] == "atelier/lesson/lc-dry-run"
    assert "evidence_trace_ids" in payload["pr_body"]
    assert "rb.lesson.test.md" in payload["block_path"]
    assert payload["diff"]
    assert calls == []
    assert not (root / "blocks" / "rb.lesson.test.md").exists()
