from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from atelier.core.foundation.lesson_models import LessonCandidate
from atelier.core.foundation.models import ReasonBlock
from atelier.gateway.cli import cli


def test_import_style_guide_cli_dry_run(monkeypatch: Any, tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    guide = tmp_path / "STYLE.md"
    guide.write_text("## Rule\nAlways add a focused test.\n", encoding="utf-8")
    runner = CliRunner()
    init = runner.invoke(cli, ["--root", str(root), "init"])
    assert init.exit_code == 0, init.output

    def fake_import_files(
        paths: tuple[Path, ...],
        domain: str,
        *,
        store: object,
        write: bool,
        limit: int,
    ) -> list[LessonCandidate]:
        assert paths == (guide,)
        assert domain == "coding"
        assert write is False
        assert limit == 3
        return [
            LessonCandidate(
                id="lc-style",
                domain="coding",
                cluster_fingerprint="style-guide-import:test",
                kind="new_block",
                proposed_block=ReasonBlock(
                    id="rb-style",
                    title="Focused Tests",
                    domain="coding",
                    situation="A style guide requires focused tests.",
                    procedure=["Add a focused test."],
                ),
                evidence_trace_ids=[],
                body="A style guide requires focused tests.",
                confidence=0.8,
            )
        ]

    monkeypatch.setattr("atelier.core.capabilities.style_import.import_files", fake_import_files)

    result = runner.invoke(
        cli,
        ["--root", str(root), "import-style-guide", str(guide), "--dry-run", "--limit", "3"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["candidates"][0]["id"] == "lc-style"
