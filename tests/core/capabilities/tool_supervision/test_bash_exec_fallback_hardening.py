"""Fallback-tier hardening: dedup-with-count, wide test-runner detection, flag injection.

These layers apply when no external compactor wraps the command -- the no-rtk
tier -- so they must be safe post-hoc transforms (dedup, extraction) or
lossless upstream flag injections announced in the output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.tool_supervision import output_delta
from lemoncrow.pro.capabilities.tool_supervision.bash_exec import (
    _TEST_CMD_RE,
    _dedupe_repeated_lines,
    _extract_test_output,
    _inject_stable_flags,
    run_command,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    output_delta.reset()
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    monkeypatch.delenv("LEMONCROW_BASH_UNCHANGED_DELTA", raising=False)
    monkeypatch.delenv("LEMONCROW_BASH_FLAG_INJECTION", raising=False)


# ── dedup-with-count ─────────────────────────────────────────────────────


def test_dedupe_collapses_long_run_with_count() -> None:
    text = "\n".join(["retrying connection..."] * 40)
    deduped, saved = _dedupe_repeated_lines(text)
    assert "retrying connection..." in deduped
    assert "(line repeated 40 times)" in deduped
    assert saved == len(text) - len(deduped)
    assert len(deduped) < len(text)


def test_dedupe_leaves_short_runs_alone() -> None:
    text = "a\na\nb"
    assert _dedupe_repeated_lines(text) == (text, 0)


def test_dedupe_blank_runs_collapse_silently() -> None:
    text = "start\n\n\n\n\n\nend"
    deduped, saved = _dedupe_repeated_lines(text)
    assert deduped == "start\n\nend"
    assert saved == len(text) - len(deduped)
    assert "repeated" not in deduped


def test_dedupe_preserves_interleaved_lines() -> None:
    text = "\n".join(["x", "x", "x", "y", "x", "x", "x"])
    deduped, _ = _dedupe_repeated_lines(text)
    assert deduped.splitlines().count("y") == 1
    assert deduped.count("(line repeated 3 times)") == 2


def test_run_command_dedupes_repeated_output() -> None:
    result = run_command("for i in $(seq 1 50); do echo repeated-line-payload; done", timeout=15)
    assert result.exit_code == 0
    assert "(line repeated 50 times)" in result.stdout
    assert result.stdout.count("repeated-line-payload") == 1
    assert result.chars_omitted > 0


# ── test-runner detection ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "npx jest src/",
        "vitest run",
        "cargo test --workspace",
        "go test ./...",
        "npm test",
        "pnpm run test",
        "yarn test",
        "bun test",
        "./gradlew test",
        "mvn -B test",
        "npx playwright test",
        "rspec spec/",
        "mocha test/",
        "phpunit tests/",
        "mix test",
        "dotnet test",
        "ctest --output-on-failure",
    ],
)
def test_test_cmd_re_matches_mainstream_runners(command: str) -> None:
    assert _TEST_CMD_RE.search(command)


@pytest.mark.parametrize(
    "command",
    ["go build ./...", "npm install", "cargo build", "gofmt -l .", "make lint"],
)
def test_test_cmd_re_ignores_non_test_commands(command: str) -> None:
    assert not _TEST_CMD_RE.search(command)


def test_extract_go_test_failure_keeps_fail_block() -> None:
    out = "\n".join(
        [
            "=== RUN   TestOK",
            "--- PASS: TestOK (0.00s)",
            "=== RUN   TestBroken",
            "--- FAIL: TestBroken (0.01s)",
            "    thing_test.go:42: got 1, want 2",
            "FAIL",
            "FAIL\texample.com/pkg\t0.012s",
        ]
    )
    kept = _extract_test_output(out)
    assert "--- FAIL: TestBroken" in kept
    assert "--- PASS: TestOK" not in kept


def test_extract_jest_green_keeps_summary_only() -> None:
    out = "\n".join(
        [
            "PASS src/a.test.ts",
            "PASS src/b.test.ts",
            "Test Suites: 2 passed, 2 total",
            "Tests:       12 passed, 12 total",
            "Time:        1.4 s",
        ]
    )
    kept = _extract_test_output(out)
    assert "Tests:       12 passed, 12 total" in kept
    assert "PASS src/a.test.ts" not in kept


def test_extract_cargo_failure_cuts_at_failures_section() -> None:
    out = "\n".join(
        [
            "running 3 tests",
            "test tests::ok_one ... ok",
            "test tests::broken ... FAILED",
            "test tests::ok_two ... ok",
            "",
            "failures:",
            "",
            "---- tests::broken stdout ----",
            "assertion failed: left == right",
            "",
            "test result: FAILED. 2 passed; 1 failed",
        ]
    )
    kept = _extract_test_output(out)
    assert "---- tests::broken stdout ----" in kept
    assert "running 3 tests" not in kept


# ── upstream flag injection ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", "git status --porcelain=v1 -b"),
        ("cd /tmp/repo && git status", "cd /tmp/repo && git status --porcelain=v1 -b"),
        ("git log", "git log --oneline -n 50"),
        ("pytest tests/", "pytest tests/ -q --tb=short"),
        ("uv run pytest tests/foo.py -x", "uv run pytest tests/foo.py -x -q --tb=short"),
        ("python -m pytest tests/", "python -m pytest tests/ -q --tb=short"),
    ],
)
def test_inject_stable_flags_appends(command: str, expected: str) -> None:
    exec_command, note = _inject_stable_flags(command)
    assert exec_command == expected
    assert expected in note


@pytest.mark.parametrize(
    "command",
    [
        "git status -s",
        "git status --porcelain",
        "git log -p",
        "git log --oneline",
        "pytest -v tests/",
        "pytest --tb=long tests/",
        "pytest -q tests/",
        "pytest -ra tests/",
        "pip install pytest",
        "pytest tests/ | tee out.log",
        "git status && git diff",
        "echo pytest",
    ],
)
def test_inject_stable_flags_leaves_alone(command: str) -> None:
    assert _inject_stable_flags(command) == (command, "")


def test_inject_stable_flags_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_BASH_FLAG_INJECTION", "0")
    assert _inject_stable_flags("git status") == ("git status", "")


def test_run_command_injects_git_status_flags(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = run_command("git status", cwd=str(tmp_path), timeout=15)
    assert result.exit_code == 0
    assert result.stdout.startswith("[ran: git status --porcelain=v1 -b]")
    assert "##" in result.stdout
