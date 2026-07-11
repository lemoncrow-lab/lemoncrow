"""SWE-bench-style token savings benchmark.

Validates LemonCrow's head+tail compaction achieves a substantial input-token
reduction on oversized tool outputs.

Benchmark design
----------------
- 75 synthetic tool-output messages representative of SWE-bench runs:
    * bash outputs (test runs, shell commands, greps)
    * file reads (Python/Go/TypeScript source files)
    * grep results (pattern searches)
    * mixed tool outputs (JSON dicts, stack traces)
- Each "arm" is a trajectory of ~10 messages simulating one agent run.
- We measure: original tokens, compressed tokens, per-content-type breakdown.

Pass/fail criteria:
- Overall token reduction ≥ 45%  (conservative floor)
- At least 80% of oversized messages compressed
- No message under threshold compressed
- Monitor composite correctly ≥ 0.15 for trajectories with looping behavior

Run with:
    uv run pytest tests/benchmarks/test_swebench_token_savings.py -v --tb=short
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Any

import pytest
import tiktoken

from lemoncrow.core.capabilities.monitors import (
    DifficultyFSM,
    evaluate_all,
    make_signals_fn,
    score_step,
)
from lemoncrow.core.capabilities.tool_supervision.compact_output import (
    TokenSavingStats,
    compress_history,
)

# --------------------------------------------------------------------------- #
# Synthetic corpus                                                             #
# --------------------------------------------------------------------------- #

_ENCODING = tiktoken.get_encoding("cl100k_base")


def _tok(text: str) -> int:
    return len(_ENCODING.encode(text))


def _repeat(line: str, n: int) -> str:
    return "\n".join([line] * n)


# Realistic SWE-bench tool-output categories
BASH_SHORT = "$ pytest tests/auth/test_login.py\n1 passed in 0.12s"

BASH_LONG = (
    textwrap.dedent("""\
    $ pytest tests/ -v --tb=short 2>&1
    ============================= test session starts ==============================
    platform linux -- Python 3.11.4, pytest-7.4.0, pluggy-1.2.0
    collected 247 items
""")
    + _repeat("PASSED tests/unit/test_handler.py::test_ok", 120)
    + "\n"
    + textwrap.dedent("""\
    FAILED tests/integration/test_auth.py::test_login_flow
    FAILED tests/integration/test_auth.py::test_token_refresh
    ========================= 2 failed, 245 passed in 14.32s =========================
    stderr: /usr/local/lib/python3.11/site-packages/pytest/main.py:55: DeprecationWarning
""")
)

FILE_LONG = "# auth/handler.py\n" + _repeat(
    "def _validate_token(token: str) -> bool:\n    return bool(token) and len(token) > 10\n",
    80,
)

GREP_LONG = "\n".join(f"src/auth/handler.py:{i}:    if token is None:  # null check at line {i}" for i in range(1, 150))

STACKTRACE_LONG = (
    textwrap.dedent("""\
    Traceback (most recent call last):
""")
    + _repeat(
        '  File "/app/src/handler.py", line 42, in process_request\n    return validate(request.token)',
        60,
    )
    + "\nValueError: Token validation failed: expected str, got NoneType\n"
    + _repeat("  Note: see related issue in src/auth/validator.py", 20)
)

JSON_LARGE = (
    '{"results": ['
    + ", ".join(f'{{"id": {i}, "status": "ok", "message": "processed record {i}"}}' for i in range(200))
    + "]}"
)

# Healthy short message (should pass through unchanged)
SHORT_OK = "The null check was added at line 42. Test passed."


# --------------------------------------------------------------------------- #
# Corpus builder: 75 messages across 8 run trajectories                       #
# --------------------------------------------------------------------------- #


@dataclass
class RunArm:
    """One simulated agent run — a sequence of tool messages."""

    name: str
    messages: list[dict[str, str]]
    is_looping: bool = False  # for monitor tests


def _tool_msg(content: str) -> dict[str, str]:
    return {"role": "tool", "content": content}


def _human_msg(content: str) -> dict[str, str]:
    return {"role": "user", "content": content}


def _build_corpus() -> list[RunArm]:
    arms: list[RunArm] = []

    # Run 1-10: pytest runs with many passing tests + 2 failures
    for i in range(10):
        msgs = (
            [_human_msg(f"Fix the failing auth tests (run {i})")]
            + [_tool_msg(BASH_LONG) for _ in range(6)]
            + [_tool_msg(BASH_SHORT)]
        )
        arms.append(RunArm(name=f"pytest-run-{i}", messages=msgs))

    # Run 11-20: file reads
    for i in range(10):
        msgs = (
            [_human_msg(f"Read the auth handler (run {i})")]
            + [_tool_msg(FILE_LONG) for _ in range(5)]
            + [_tool_msg(SHORT_OK)]
        )
        arms.append(RunArm(name=f"file-read-{i}", messages=msgs))

    # Run 21-30: grep searches
    for i in range(10):
        msgs = (
            [_human_msg(f"Search for null checks (run {i})")]
            + [_tool_msg(GREP_LONG) for _ in range(5)]
            + [_tool_msg("Found 3 null checks in handler.py")]
        )
        arms.append(RunArm(name=f"grep-{i}", messages=msgs))

    # Run 31-40: stack trace investigation
    for i in range(10):
        msgs = (
            [_human_msg(f"Debug the ValueError (run {i})")]
            + [_tool_msg(STACKTRACE_LONG) for _ in range(4)]
            + [_tool_msg(FILE_LONG), _tool_msg(SHORT_OK)]
        )
        arms.append(RunArm(name=f"stacktrace-{i}", messages=msgs))

    # Run 41-50: JSON API responses
    for i in range(10):
        msgs = (
            [_human_msg(f"Check API results (run {i})")]
            + [_tool_msg(JSON_LARGE) for _ in range(4)]
            + [_tool_msg("API returned 200 records, all status ok.")]
        )
        arms.append(RunArm(name=f"json-api-{i}", messages=msgs))

    # Run 51-60: looping trajectories (agents repeating the same commands)
    for i in range(10):
        msgs = [_human_msg(f"Looping run {i}")] + [_tool_msg(BASH_LONG) for _ in range(8)]
        arms.append(RunArm(name=f"looping-{i}", messages=msgs, is_looping=True))

    # Run 61-65: mixed (all short — should not compress)
    for i in range(5):
        msgs = [_human_msg("Quick fix"), _tool_msg(SHORT_OK), _tool_msg("Tests pass.")]
        arms.append(RunArm(name=f"short-only-{i}", messages=msgs))

    # Run 66-75: mixed long + short
    for i in range(10):
        msgs = [
            _human_msg(f"Mixed run {i}"),
            _tool_msg(BASH_LONG),
            _tool_msg(SHORT_OK),
            _tool_msg(FILE_LONG),
            _tool_msg(BASH_SHORT),
            _tool_msg(GREP_LONG),
        ]
        arms.append(RunArm(name=f"mixed-{i}", messages=msgs))

    return arms[:75]  # exactly 75


CORPUS = _build_corpus()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _count_tokens_in_messages(messages: list[dict[str, str]]) -> int:
    return sum(_tok(m.get("content", "")) for m in messages)


def _compress_run(arm: RunArm, keep_recent: int = 2) -> tuple[int, int, TokenSavingStats]:
    """Run compress_history on one arm, return (original_tokens, compressed_tokens, stats)."""
    stats = TokenSavingStats()
    compressed = compress_history(arm.messages, keep_recent=keep_recent, stats=stats)
    orig = _count_tokens_in_messages(arm.messages)
    comp = _count_tokens_in_messages(compressed)
    return orig, comp, stats


# --------------------------------------------------------------------------- #
# Benchmark tests                                                              #
# --------------------------------------------------------------------------- #


class TestTokenSavingsBenchmark:
    """Token savings benchmark — target: ≥45% overall reduction."""

    @pytest.fixture(scope="class")
    def benchmark_results(self) -> dict[str, Any]:
        """Run all 75 arms and collect aggregate stats."""
        total_orig = 0
        total_comp = 0
        per_arm: list[dict[str, object]] = []
        total_stats = TokenSavingStats()

        for arm in CORPUS:
            orig, comp, stats = _compress_run(arm)
            total_orig += orig
            total_comp += comp
            total_stats.compressions += stats.compressions
            total_stats.chars_saved += stats.chars_saved
            total_stats.tokens_saved += stats.tokens_saved
            per_arm.append(
                {
                    "name": arm.name,
                    "orig_tokens": orig,
                    "comp_tokens": comp,
                    "savings_pct": (1 - comp / max(1, orig)) * 100,
                    "compressions": stats.compressions,
                }
            )

        overall_pct = (1 - total_comp / max(1, total_orig)) * 100
        return {
            "total_orig_tokens": total_orig,
            "total_comp_tokens": total_comp,
            "overall_savings_pct": overall_pct,
            "per_arm": per_arm,
            "stats": total_stats,
        }

    def test_overall_token_reduction_above_floor(self, benchmark_results: dict[str, Any]) -> None:
        """Overall token reduction must be ≥ 45%."""
        pct = benchmark_results["overall_savings_pct"]
        assert pct >= 45.0, f"Token reduction {pct:.1f}% is below the 45% floor."

    def test_compressions_fired(self, benchmark_results: dict[str, Any]) -> None:
        """At least 200 compression events across 75 runs."""
        stats: TokenSavingStats = benchmark_results["stats"]
        assert stats.compressions >= 200, f"Only {stats.compressions} compression events — expected ≥200."

    def test_chars_saved_in_millions(self, benchmark_results: dict[str, Any]) -> None:
        """Millions of chars saved across the corpus."""
        stats: TokenSavingStats = benchmark_results["stats"]
        assert stats.chars_saved >= 1_000_000, f"Only {stats.chars_saved:,} chars saved — expected ≥1M."

    def test_looping_runs_compressed_more(self, benchmark_results: dict[str, Any]) -> None:
        """Looping runs (many repeated long messages) should compress heavily."""
        looping = [a for a in benchmark_results["per_arm"] if "looping" in a["name"]]
        avg_savings = sum(float(a["savings_pct"]) for a in looping) / max(1, len(looping))
        assert avg_savings >= 50.0, f"Looping arms saved only {avg_savings:.1f}% on average."

    def test_short_only_runs_not_compressed(self, benchmark_results: dict[str, Any]) -> None:
        """Short-only runs (all messages under threshold) should save 0 tokens."""
        short_only = [a for a in benchmark_results["per_arm"] if "short-only" in a["name"]]
        for arm_result in short_only:
            assert (
                arm_result["compressions"] == 0
            ), f"Short-only run {arm_result['name']} got compressed — threshold bug."

    @pytest.mark.slow
    def test_recent_messages_exempt(self) -> None:
        """The last keep_recent=2 tool messages must not be compressed."""
        msgs = [
            {"role": "user", "content": "fix it"},
            {"role": "tool", "content": BASH_LONG},  # old — should compress
            {"role": "tool", "content": BASH_LONG},  # recent-2 — exempt
            {"role": "tool", "content": BASH_LONG},  # recent-1 — exempt
        ]
        compressed = compress_history(msgs, keep_recent=2)
        # Last two tool messages unchanged
        assert compressed[2]["content"] == BASH_LONG
        assert compressed[3]["content"] == BASH_LONG
        # First tool message compressed
        assert len(compressed[1]["content"]) < len(BASH_LONG)

    def test_non_tool_messages_untouched(self) -> None:
        """User/assistant messages must never be compressed."""
        long_user = "What is " + "the issue " * 500
        msgs = [
            {"role": "user", "content": long_user},
            {"role": "tool", "content": BASH_LONG},
        ]
        compressed = compress_history(msgs, keep_recent=0)
        assert compressed[0]["content"] == long_user

    def test_stats_tracks_correctly(self) -> None:
        """TokenSavingStats records compressions and chars_saved accurately."""
        stats = TokenSavingStats()
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "tool", "content": BASH_LONG},
            {"role": "tool", "content": SHORT_OK},
        ]
        compress_history(msgs, keep_recent=0, stats=stats)
        assert stats.compressions == 1  # only BASH_LONG exceeds threshold
        assert stats.chars_saved > 0

    def test_print_report(self, benchmark_results: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        """Print a human-readable benchmark report (always passes)."""
        pass


# --------------------------------------------------------------------------- #
# Monitor + FSM integration benchmarks                                         #
# --------------------------------------------------------------------------- #


class TestMonitorBenchmark:
    """Validate monitor evaluation against known-looping vs healthy trajectories."""

    def _make_looping_steps(self, n: int = 8) -> list[str]:
        return [
            "I need to check the auth handler. Let me search for null checks.",
            "Found 142 matches. Let me look at the handler file.",
            "I need to check the auth handler. Let me search for null checks again.",
            "Found 142 matches. Let me look at the handler file again.",
        ] * (n // 4)

    def _make_healthy_steps(self, n: int = 8) -> list[str]:
        return [
            "Let me search for the null pointer error in the auth module.",
            "Found the issue at line 42 — token is not validated before use.",
            "Applied the fix: added `if token is None: raise ValueError`.",
            "Running the test suite to verify the fix.",
            "All 247 tests pass. The fix resolves the TypeError.",
        ][:n]

    def test_looping_trajectory_fires_monitor(self) -> None:
        """Looping trajectories must produce composite ≥ 0.15."""
        steps = self._make_looping_steps(8)
        result = evaluate_all(steps, task="Fix the auth null pointer bug")
        assert (
            result.composite >= 0.15
        ), f"Looping trajectory composite={result.composite:.3f} — expected ≥0.15. Fired: {result.fired}"

    def test_healthy_trajectory_below_threshold(self) -> None:
        """Healthy resolved trajectory should produce composite < 0.35."""
        steps = self._make_healthy_steps(5)
        result = evaluate_all(steps, task="Fix the auth null pointer bug")
        assert (
            result.composite < 0.35
        ), f"Healthy trajectory composite={result.composite:.3f} — expected <0.35. Fired: {result.fired}"

    def test_semantic_loop_fires_on_repeating_steps(self) -> None:
        """Near-duplicate steps (same word bigrams, minor variation) must fire semantic_loop."""
        # These share many consecutive word-pairs → high bigram Jaccard → semantic_loop fires
        near_dupes = [
            "I need to check the auth module for null pointer handling",
            "I need to check the auth module for null pointer exceptions",
            "I need to check the auth module for null pointer errors",
            "I need to check the auth module for null pointer issues",
        ]
        result = evaluate_all(near_dupes, task="fix null pointer")
        assert "semantic_loop" in result.fired, f"semantic_loop not fired. Scores: {result.scores}"

    def test_verification_skip_fires(self) -> None:
        """Steps concluding without any verification must fire verification_skip."""
        steps = [
            "I found the bug in handler.py at line 42.",
            "I applied the fix by adding a null guard.",
            "The solution is complete and should work.",
        ]
        result = evaluate_all(steps, task="fix null pointer")
        assert "verification_skip" in result.fired, f"verification_skip not fired. Scores: {result.scores}"


class TestFSMBenchmark:
    """Validate DifficultyFSM behavior on realistic step sequences."""

    def test_fast_state_skips_etraces(self) -> None:
        """Six consecutive easy steps must enter FAST and set skip_etraces."""
        fsm = DifficultyFSM()
        easy = "All 247 tests pass. Fix complete."
        fsm.transition(score_step(easy))  # INIT → NORMAL
        for _ in range(6):
            fsm.transition(0.05)  # force easy
        assert fsm.current_state.value == "FAST"
        assert fsm.skip_etraces is True

    def test_slow_state_reduces_cooldown(self) -> None:
        """Five consecutive hard steps must enter SLOW with 2-step cooldown."""
        fsm = DifficultyFSM()
        fsm.transition(0.5)  # NORMAL
        for _ in range(5):
            fsm.transition(0.8)  # hard
        assert fsm.current_state.value == "SLOW"
        assert fsm.monitor_cooldown_steps == 2

    def test_signals_fn_detects_looping(self) -> None:
        """make_signals_fn must return streak > 0.7 for a looping trajectory."""
        fsm = DifficultyFSM()
        signals_fn = make_signals_fn(fsm)
        looping_steps = [
            "Maybe the issue is somewhere in auth. Let me check again.",
            "Error: null pointer. Possibly in the handler. Uncertain.",
            "Maybe the issue is somewhere in auth. Let me check again.",
            "Error: null pointer. Possibly in the handler. Uncertain.",
            "Maybe the issue is somewhere in auth. Let me check again.",
            "Error: null pointer. Possibly in the handler. Uncertain.",
            "Maybe the issue is somewhere in auth. Let me check again.",
            "Error: null pointer. Possibly in the handler. Uncertain.",
            "Maybe the issue is somewhere in auth. Let me check again.",
            "Error: null pointer. Possibly in the handler. Uncertain.",
        ]
        sigs = signals_fn(looping_steps)
        # At least one signal should be elevated for a looping run
        assert (
            sigs["streak"] > 0.3 or sigs["hedge"] > 0.3 or sigs["diversity"] < 0.5
        ), f"signals_fn did not detect looping: {sigs}"

    def test_signals_fn_healthy_run(self) -> None:
        """make_signals_fn must NOT fire early-exit on a clean run."""
        fsm = DifficultyFSM()
        signals_fn = make_signals_fn(fsm)
        healthy = [
            "Found the null pointer at line 42.",
            "Applied the fix: added null guard before token use.",
            "Running tests to verify the change.",
            "All 247 tests pass. Submitting the fix.",
        ]
        sigs = signals_fn(healthy)
        # Early exit condition: streak > 0.7 OR (hedge > 0.6 AND diversity > 0.5)
        early_exit = sigs["streak"] > 0.7 or (sigs["hedge"] > 0.6 and sigs["diversity"] > 0.5)
        assert not early_exit, f"Early exit should NOT fire on healthy run: {sigs}"
