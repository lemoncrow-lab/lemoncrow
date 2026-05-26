from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from atelier.core.foundation.lesson_models import LessonCandidate
from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.store import ContextStore
from atelier.gateway.cli import cli


def test_pr_bot_skips_when_disabled_without_side_effects(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    runner = CliRunner()

    init = runner.invoke(cli, ["--root", str(root), "init"])
    assert init.exit_code == 0, init.output

    store = ContextStore(root)
    block = ReasonBlock(
        id="rb.lesson.disabled",
        title="Disabled path block",
        domain="coding",
        situation="Skip when disabled.",
        triggers=["disabled test"],
        dead_ends=["none"],
        procedure=["Do nothing"],
        verification=["No side effects"],
        failure_signals=["n/a"],
    )
    lesson = LessonCandidate(
        id="lc-disabled",
        domain="coding",
        cluster_fingerprint="disabled test",
        kind="new_block",
        proposed_block=block,
        evidence_trace_ids=["tr-9"],
        confidence=0.8,
        status="approved",
    )
    store.upsert_lesson_candidate(lesson)

    env = {
        "ATELIER_LESSON_PR_BOT_ENABLED": "",
        "GITHUB_TOKEN": "",
    }
    res = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "lesson",
            "sync-pr",
            lesson.id,
            "--json",
        ],
        env=env,
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload == {"skipped": True, "reason": "disabled"}
    assert not (root / "blocks" / f"{block.id}.md").exists()
