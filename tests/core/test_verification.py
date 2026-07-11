"""Tests for the M3 verification / counterexample capability."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lemoncrow.core.capabilities.prompt_compilation import BlockKind, Stability
from lemoncrow.core.capabilities.verification import Counterexample, RetryBudget, VerifierCapability
from lemoncrow.core.capabilities.verification.checks.semantic_review import run_semantic_review
from lemoncrow.infra.internal_llm import InternalLLMError


def _proc(stdout: str = "", stderr: str = "", returncode: int = 1) -> Any:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_lint_failure_becomes_counterexample() -> None:
    payload = json.dumps(
        [
            {
                "code": "F401",
                "message": "imported but unused",
                "filename": "src/x.py",
                "location": {"row": 3, "column": 1},
            }
        ]
    )

    def fake_run(args: Any, cwd: Path) -> Any:
        return _proc(stdout=payload)

    ces = VerifierCapability(run=fake_run).run(scope_files=["src/x.py"], checks=["lint"])
    assert len(ces) == 1
    ce = ces[0]
    assert ce.check == "lint" and ce.file_path == "src/x.py" and ce.line == 3
    assert "F401" in ce.diagnostic


def test_typecheck_parsing() -> None:
    def fake_run(args: Any, cwd: Path) -> Any:
        return _proc(stdout="src/x.py:42: error: Incompatible types in assignment\n")

    ces = VerifierCapability(run=fake_run).run(scope_files=["src/x.py"], checks=["typecheck"])
    assert len(ces) == 1
    assert ces[0].check == "typecheck" and ces[0].line == 42 and ces[0].severity == "error"
    assert ces[0].file_path == "src/x.py"
    assert ces[0].diagnostic == "Incompatible types in assignment"
    assert ces[0].repro_command == "uv run mypy src/x.py"


def test_typecheck_targets_exact_python_files() -> None:
    seen: dict[str, Any] = {}

    def fake_run(args: Any, cwd: Path) -> Any:
        seen["args"] = list(args)
        return _proc(returncode=0)

    ces = VerifierCapability(run=fake_run).run(
        scope_files=[
            "/tmp/pytest-of-user/pytest-1/test_case0/latest.py",
            "src/pkg/helper.py",
            "tests/test_helper.py",
            "README.md",
        ],
        checks=["typecheck"],
    )

    assert ces == []
    assert seen["args"] == [
        "uv",
        "run",
        "mypy",
        "/tmp/pytest-of-user/pytest-1/test_case0/latest.py",
        "src/pkg/helper.py",
    ]


def test_tests_parsing() -> None:
    def fake_run(args: Any, cwd: Path) -> Any:
        return _proc(stdout="FAILED tests/test_x.py::test_a - AssertionError: nope\n")

    ces = VerifierCapability(run=fake_run).run(scope_files=["tests/test_x.py"], checks=["tests"])
    assert len(ces) == 1
    assert ces[0].check == "tests" and ces[0].file_path == "tests/test_x.py"
    assert ces[0].repro_command == "pytest -q tests/test_x.py::test_a"


def test_verifier_runs_semantic_review_when_task_intent_present() -> None:
    seen: dict[str, Any] = {}
    expected = Counterexample(
        check="semantic",
        severity="error",
        file_path="src/x.py",
        line=5,
        diagnostic="Edit drifted from the requested auth fix",
        expected="fix the auth flow",
        actual="rewrote unrelated logging",
    )

    def fake_semantic(files: list[str], task_intent: str, *, cwd: Path) -> list[Counterexample]:
        seen["files"] = files
        seen["task_intent"] = task_intent
        seen["cwd"] = cwd
        return [expected]

    ces = VerifierCapability(
        cwd="/repo",
        task_intent="fix the auth flow",
        semantic_review=fake_semantic,
    ).run(scope_files=["src/x.py"], checks=["semantic"])

    assert ces == [expected]
    assert seen == {
        "files": ["src/x.py"],
        "task_intent": "fix the auth flow",
        "cwd": Path("/repo"),
    }


def test_run_semantic_review_parses_counterexample(tmp_path: Path, monkeypatch: Any) -> None:
    target = tmp_path / "src" / "auth.py"
    target.parent.mkdir(parents=True)
    target.write_text("def helper() -> str:\n    return 'noop'\n", encoding="utf-8")
    seen_messages: list[dict[str, str]] = []

    def fake_chat(messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None) -> dict[str, object]:
        _ = json_schema
        seen_messages.extend(messages)
        return {
            "mismatches": [
                {
                    "file_path": "src/auth.py",
                    "line": 1,
                    "diagnostic": "Edited file does not address the requested auth fix.",
                    "actual": "adds an unrelated helper",
                }
            ]
        }

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.verification.checks.semantic_review.chat",
        fake_chat,
    )

    ces = run_semantic_review(["src/auth.py"], "fix login bug", cwd=tmp_path)

    assert len(ces) == 1
    assert ces[0].check == "semantic"
    assert ces[0].file_path == "src/auth.py"
    assert ces[0].line == 1
    assert ces[0].expected == "fix login bug"
    assert ces[0].actual == "adds an unrelated helper"
    assert seen_messages[1]["content"] == json.dumps(
        {
            "task_intent": "fix login bug",
            "files": [
                {
                    "file_path": "src/auth.py",
                    "content": "def helper() -> str:\n    return 'noop'\n",
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_run_semantic_review_is_fail_open_without_backend(tmp_path: Path, monkeypatch: Any) -> None:
    target = tmp_path / "src" / "auth.py"
    target.parent.mkdir(parents=True)
    target.write_text("def helper() -> str:\n    return 'noop'\n", encoding="utf-8")

    def boom(messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None) -> Any:
        raise InternalLLMError("offline")

    monkeypatch.setattr("lemoncrow.core.capabilities.verification.checks.semantic_review.chat", boom)

    assert run_semantic_review(["src/auth.py"], "fix login bug", cwd=tmp_path) == []


def test_budget_exhaustion() -> None:
    budget = RetryBudget(max_attempts=3)
    for _ in range(3):
        assert not budget.exhausted()
        budget.consume()
    assert budget.exhausted() and budget.remaining() == 0


def test_budget_tracks_signatures_independently_and_resets() -> None:
    budget = RetryBudget(max_attempts=3)

    assert budget.consume("alpha") == 1
    assert budget.consume("beta") == 1
    assert budget.consume("alpha") == 2
    assert budget.used_for("alpha") == 2
    assert budget.used_for("beta") == 1
    assert budget.remaining("alpha") == 1
    assert budget.remaining("beta") == 2

    budget.reset("alpha")
    assert budget.used_for("alpha") == 0
    assert budget.used_for("beta") == 1

    budget.reset()
    assert budget.used == 0
    assert budget.attempts_by_key == {}


def test_to_prompt_block_is_structured() -> None:
    block = Counterexample(
        check="typecheck",
        severity="error",
        file_path="foo.py",
        line=42,
        diagnostic="Incompatible types",
        expected="x is int",
        actual="x is str | None",
        repro_command="uv run mypy src/foo.py",
    ).to_prompt_block()
    assert 'check="typecheck"' in block and "repro:    uv run mypy src/foo.py" in block


def test_counterexample_compiler_block_is_canonical() -> None:
    block = Counterexample(
        check="typecheck",
        severity="error",
        file_path="foo.py",
        line=42,
        diagnostic="Incompatible types",
        expected="x is int",
        actual="x is str | None",
        repro_command="uv run mypy src/foo.py",
    ).to_compiler_block()

    assert block.kind is BlockKind.TOOL_RESULT
    assert block.stability is Stability.TURN
    assert block.cacheable is False
    assert block.is_counterexample is True
    assert block.content.startswith('<counterexample check="typecheck"')


def test_fail_open_on_runner_error() -> None:
    def boom(args: Any, cwd: Path) -> Any:
        raise RuntimeError("tool missing")

    ces = VerifierCapability(run=boom).run(scope_files=["src/x.py"], checks=["lint", "typecheck", "tests"])
    assert ces == []
