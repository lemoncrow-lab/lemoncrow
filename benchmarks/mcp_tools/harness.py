"""Core benchmark harness: case schema, runner, and results."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tiktoken

_ENCODING = tiktoken.get_encoding("cl100k_base")


def _tokens(value: Any) -> int:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return len(_ENCODING.encode(text))


@dataclass
class BenchCase:
    """A single benchmark op: inputs, correctness checks, and savings estimate."""

    op: str
    args: dict[str, Any]
    # Keys that must be present in the response
    assert_keys: list[str] = field(default_factory=list)
    # Exact key=value pairs to assert in response
    assert_values: dict[str, Any] = field(default_factory=dict)
    # Human-readable description of what agent would do without Atelier
    baseline_description: str = ""
    # Fixed baseline token cost without Atelier (what agent would read/write).
    # Use baseline_builder for measured dynamic baselines.
    baseline_tokens: int = 0
    # Optional callable to construct a measured baseline payload.
    # Signature: fn(case) -> BaselineMeasurement | serializable payload/string
    baseline_builder: Callable[[BenchCase], Any] | None = field(default=None, repr=False)
    # Optional quality gate for measured baseline hardness.
    min_baseline_tokens: int = 0
    # Optional callable for custom assertions: fn(result) -> None (raise on fail)
    custom_assert: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)
    # Optional grep needle used when a response spills to overflow artifact.
    spill_probe_pattern: str | None = None
    # Optional quality score used for effective-token reporting/comparisons.
    quality_score: float = 1.0
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.op


@dataclass
class CaseResult:
    case: BenchCase
    response: dict[str, Any]
    atelier_tokens: int
    baseline_tokens: int
    quality_score: float
    input_file_tokens: int
    baseline_commands: list[str]
    spill_probe_tokens: int
    spill_probe_hits: int
    elapsed_ms: float
    passed: bool
    failure: str = ""

    @property
    def tokens_saved(self) -> int:
        return max(0, self.baseline_tokens - self.atelier_tokens)

    @property
    def savings_pct(self) -> float:
        if self.baseline_tokens == 0:
            return 0.0
        return self.tokens_saved / self.baseline_tokens * 100

    @property
    def effective_tokens(self) -> float:
        return self.atelier_tokens / max(self.quality_score, 0.1)


@dataclass
class ToolReport:
    tool_name: str
    results: list[CaseResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def total_saved_tokens(self) -> int:
        return sum(r.tokens_saved for r in self.results)

    @property
    def total_effective_tokens(self) -> float:
        return sum(r.effective_tokens for r in self.results)

    @property
    def avg_effective_tokens(self) -> float:
        if not self.results:
            return 0.0
        return self.total_effective_tokens / len(self.results)

    @property
    def avg_savings_pct(self) -> float:
        with_baseline = [r for r in self.results if r.baseline_tokens > 0]
        if not with_baseline:
            return 0.0
        return sum(r.savings_pct for r in with_baseline) / len(with_baseline)


@dataclass
class BaselineMeasurement:
    payload: Any
    input_file_tokens: int = 0
    commands: list[str] = field(default_factory=list)


def run_case(
    case: BenchCase,
    tool_fn: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> CaseResult:
    """Run a single benchmark case and return a result."""
    t0 = time.perf_counter()
    try:
        response = tool_fn(case.args)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        # Use baseline_tokens (not 0) so a crash doesn't look like 100% savings.
        # The agent would still have to use the baseline on failure.
        return CaseResult(
            case=case,
            response={},
            atelier_tokens=case.baseline_tokens,
            baseline_tokens=case.baseline_tokens,
            quality_score=case.quality_score,
            input_file_tokens=0,
            baseline_commands=[],
            spill_probe_tokens=0,
            spill_probe_hits=0,
            elapsed_ms=elapsed_ms,
            passed=False,
            failure=f"exception: {exc}",
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    atelier_tokens = _tokens(response) if response is not None else 4  # "null" is 4 chars
    baseline_tokens = case.baseline_tokens
    input_file_tokens = 0
    baseline_commands: list[str] = []
    spill_probe_tokens = 0
    spill_probe_hits = 0
    if case.baseline_builder is not None:
        measurement = case.baseline_builder(case)
        if isinstance(measurement, BaselineMeasurement):
            baseline_tokens = _tokens(measurement.payload)
            input_file_tokens = int(measurement.input_file_tokens)
            baseline_commands = list(measurement.commands)
        else:
            baseline_tokens = _tokens(measurement)

    if isinstance(response, dict):
        probe_failure, spill_probe_tokens, spill_probe_hits = _probe_spilled_artifact(response, case)
        atelier_tokens += spill_probe_tokens
    else:
        probe_failure = ""

    failure = _check(case, response)
    if failure == "" and probe_failure:
        failure = probe_failure
    if failure == "" and case.min_baseline_tokens > 0 and baseline_tokens < case.min_baseline_tokens:
        failure = (
            f"measured baseline too small: baseline_tokens={baseline_tokens} "
            f"< min_baseline_tokens={case.min_baseline_tokens}"
        )
    return CaseResult(
        case=case,
        response=response or {},
        atelier_tokens=atelier_tokens,
        baseline_tokens=baseline_tokens,
        quality_score=case.quality_score,
        input_file_tokens=input_file_tokens,
        baseline_commands=baseline_commands,
        spill_probe_tokens=spill_probe_tokens,
        spill_probe_hits=spill_probe_hits,
        elapsed_ms=elapsed_ms,
        passed=failure == "",
        failure=failure,
    )


def _probe_spilled_artifact(response: dict[str, Any], case: BenchCase) -> tuple[str, int, int]:
    overflow = response.get("overflow")
    if not isinstance(overflow, dict):
        return "", 0, 0
    artifact_path_value = overflow.get("artifact_path")
    if not artifact_path_value:
        return "overflow metadata missing artifact_path", 0, 0
    artifact_path = Path(str(artifact_path_value))
    if not artifact_path.exists():
        return f"overflow artifact not found: {artifact_path}", 0, 0
    probe_patterns = _spill_probe_patterns(response, case)
    last_failure = "spill probe did not run"
    total_probe_tokens = 0
    for pattern in probe_patterns:
        found, tokens, failure = _run_spill_probe(artifact_path, pattern)
        total_probe_tokens += tokens
        if found > 0:
            return "", total_probe_tokens, found
        last_failure = failure
    return last_failure, total_probe_tokens, 0


def _spill_probe_patterns(response: dict[str, Any], case: BenchCase) -> list[str]:
    patterns: list[str] = []
    if case.spill_probe_pattern:
        patterns.append(case.spill_probe_pattern)
    if isinstance(response.get("matches"), list):
        for match in response["matches"]:
            if isinstance(match, dict):
                file_path = match.get("file_path")
                if isinstance(file_path, str) and file_path:
                    patterns.append(file_path)
                    break
    if isinstance(response.get("symbols"), list):
        for symbol in response["symbols"]:
            if isinstance(symbol, dict):
                symbol_name = symbol.get("symbol_name")
                if isinstance(symbol_name, str) and symbol_name:
                    patterns.append(symbol_name)
                    break
    for key in ("total_matches", "symbol_count", "tokens_saved", "provenance"):
        if key in response:
            patterns.append(f'"{key}"')
    patterns.append(str(case.op))
    seen: set[str] = set()
    ordered: list[str] = []
    for pattern in patterns:
        if pattern and pattern not in seen:
            seen.add(pattern)
            ordered.append(pattern)
    return ordered


def _run_spill_probe(artifact_path: Path, pattern: str) -> tuple[int, int, str]:
    try:
        proc = subprocess.run(
            [
                "rg",
                "-n",
                "--no-heading",
                "--color",
                "never",
                "--fixed-strings",
                "--max-count",
                "200",
                pattern,
                str(artifact_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        content = artifact_path.read_text(encoding="utf-8", errors="replace")
        lines = [line for line in content.splitlines() if pattern in line]
        probe_text = "\n".join(lines[:200])
        hits = len(lines)
        if hits == 0:
            return 0, _tokens(probe_text), f"spill probe found no matches for pattern={pattern!r}"
        return hits, _tokens(probe_text), ""

    probe_text = proc.stdout or ""
    probe_tokens = _tokens(probe_text)
    if proc.returncode not in {0, 1}:
        return 0, probe_tokens, f"spill probe failed with exit={proc.returncode}"
    hit_count = 0 if proc.returncode == 1 else len([line for line in probe_text.splitlines() if line.strip()])
    if hit_count == 0:
        return 0, probe_tokens, f"spill probe found no matches for pattern={pattern!r}"
    return hit_count, probe_tokens, ""


def _check(case: BenchCase, response: dict[str, Any] | None) -> str:
    if response is None:
        # Custom assert may explicitly expect None
        if case.custom_assert is not None:
            try:
                case.custom_assert(response)  # type: ignore[arg-type]
            except AssertionError as exc:
                return str(exc)
            return ""
        if case.assert_keys or case.assert_values:
            return "response was None"
        return ""
    for key in case.assert_keys:
        if key not in response:
            return f"missing key '{key}' in response"
    for key, expected in case.assert_values.items():
        actual = response.get(key)
        if actual != expected:
            return f"key '{key}': expected {expected!r}, got {actual!r}"
    if case.custom_assert is not None:
        try:
            case.custom_assert(response)
        except AssertionError as exc:
            return str(exc)
    return ""


def run_tool_benchmark(
    tool_name: str,
    cases: list[BenchCase],
    tool_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> ToolReport:
    results = [run_case(case, tool_fn) for case in cases]
    return ToolReport(tool_name=tool_name, results=results)
