from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.live_reviewer import runner as runner_mod
from lemoncrow.pro.capabilities.live_reviewer.runner import parse_verdict, run_review
from lemoncrow.pro.capabilities.live_reviewer.settings import ReviewerSettings


class _FakeResult:
    def __init__(self, output: str) -> None:
        self.output = output


def test_parse_verdict_extracts_typed_findings() -> None:
    text = (
        '```json\n{"verdict":"NEEDS_FIX","checklist":"c","missing":"m","findings":'
        '[{"type":"patch","file":"a.py","old_string":"x","new_string":"y","reason":"r"}]}\n```'
    )
    v = parse_verdict(text)
    assert v["verdict"] == "NEEDS_FIX"
    assert isinstance(v["findings"], list) and v["findings"][0]["type"] == "patch"


def test_parse_verdict_findings_default_empty() -> None:
    assert parse_verdict('```json\n{"verdict":"DONE"}\n```')["findings"] == []
    assert parse_verdict("garbage")["findings"] == []


def test_run_review_branch_mode_with_duplications(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(runner_mod, "_branch_diffs", lambda repo, base: {"a.py": "diff --git a b"})
    monkeypatch.setattr(runner_mod, "find_duplications", lambda repo, diffs: ["dupnote"])
    monkeypatch.setattr(runner_mod, "collect_review_context", lambda root, repo: "")

    def fake_select(root, request, *, env=None):
        captured["prompt"] = request.task_text
        return "DECISION"

    def fake_exec(prompt, *, root, tool_name, task_text, decision, host_agent="", allow_fallback=True):
        return _FakeResult('```json\n{"verdict":"NEEDS_FIX","findings":[]}\n```')

    monkeypatch.setattr(runner_mod, "select_owned_route", fake_select)
    monkeypatch.setattr(runner_mod, "execute_owned_prompt", fake_exec)
    v = run_review("sid", "deep", [], ReviewerSettings(agentic=False), tmp_path, base="main")
    assert v["paths"] == ["a.py"]
    assert v["verdict"] == "NEEDS_FIX"
    assert v["duplications"] == ["dupnote"]
    assert "Possible duplications" in captured["prompt"]
    assert "dupnote" in captured["prompt"]
