from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.core.foundation.lesson_models import LessonCandidate
from lemoncrow.core.foundation.models import Playbook
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.gateway.cli import cli


def test_pr_bot_skips_when_disabled_without_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
    runner = CliRunner()

    # Run outside a git repo so `init` skips the ~40s code-index bootstrap and
    # project-setup writes; this test only needs an initialized store.
    monkeypatch.chdir(tmp_path)
    from tests.helpers import grant_oauth_pro

    grant_oauth_pro(monkeypatch)
    init = runner.invoke(cli, ["--root", str(root), "init"])
    assert init.exit_code == 0, init.output

    store = ContextStore(root)
    block = Playbook(
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
        "LEMONCROW_LESSON_PR_BOT_ENABLED": "",
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
