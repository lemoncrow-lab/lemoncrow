from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.live_reviewer import runner as runner_mod
from atelier.core.capabilities.live_reviewer.runner import parse_verdict, run_review
from atelier.core.capabilities.live_reviewer.settings import ReviewerSettings


class _FakeResult:
    def __init__(self, output: str) -> None:
        self.output = output


def test_parse_needs_fix() -> None:
    text = 'pre\n```json\n{"verdict":"NEEDS_FIX","checklist":"c","missing":"- x"}\n```'
    v = parse_verdict(text)
    assert v["verdict"] == "NEEDS_FIX"
    assert v["missing"] == "- x"


def test_parse_done() -> None:
    v = parse_verdict('```json\n{"verdict":"DONE","checklist":"ok","missing":""}\n```')
    assert v["verdict"] == "DONE"


def test_parse_malformed_is_error_never_raises() -> None:
    assert parse_verdict("no json here")["verdict"] == "ERROR"
    assert parse_verdict("")["verdict"] == "ERROR"


def test_parse_uses_last_block() -> None:
    text = '```json\n{"verdict":"DONE"}\n```\nmore\n```json\n{"verdict":"NEEDS_FIX"}\n```'
    assert parse_verdict(text)["verdict"] == "NEEDS_FIX"


def test_run_review_no_diff_is_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner_mod, "_git_diff", lambda p: "")
    v = run_review("s1", "live", ["a.py"], ReviewerSettings(), tmp_path)
    assert v["verdict"] == "DONE"


def test_run_review_pins_explicit_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(runner_mod, "_git_diff", lambda p: "diff --git a b")

    def fake_select(root, request, *, env=None):
        captured["mode"] = request.mode
        captured["provider"] = request.provider
        captured["model"] = request.model
        return "DECISION"

    def fake_exec(prompt, *, root, tool_name, task_text, decision, host_agent="", allow_fallback=True):
        captured["allow_fallback"] = allow_fallback
        return _FakeResult('```json\n{"verdict":"NEEDS_FIX","checklist":"c","missing":"m"}\n```')

    monkeypatch.setattr(runner_mod, "select_owned_route", fake_select)
    monkeypatch.setattr(runner_mod, "execute_owned_prompt", fake_exec)
    v = run_review("s1", "deep", ["a.py"], ReviewerSettings(review_model="anthropic/claude-x", agentic=False), tmp_path)
    assert v["verdict"] == "NEEDS_FIX"
    assert captured["mode"] == "explicit"
    assert captured["provider"] == "anthropic"
    assert captured["model"] == "claude-x"
    assert captured["allow_fallback"] is False


def test_run_review_auto_when_no_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(runner_mod, "_git_diff", lambda p: "diff")

    def fake_select(root, request, *, env=None):
        captured["mode"] = request.mode
        captured["budget"] = request.budget
        return "D"

    def fake_exec(prompt, *, root, tool_name, task_text, decision, host_agent="", allow_fallback=True):
        captured["allow_fallback"] = allow_fallback
        return _FakeResult('```json\n{"verdict":"DONE"}\n```')

    monkeypatch.setattr(runner_mod, "select_owned_route", fake_select)
    monkeypatch.setattr(runner_mod, "execute_owned_prompt", fake_exec)
    run_review("s1", "live", ["a.py"], ReviewerSettings(agentic=False), tmp_path)
    assert captured["mode"] == "auto"
    assert captured["budget"] == "cheap"
    assert captured["allow_fallback"] is True
