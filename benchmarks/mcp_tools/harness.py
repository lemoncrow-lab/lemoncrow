"""Core benchmark harness: case schema, runner, and results."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

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
    # Estimated token cost without Atelier (what agent would read/write)
    baseline_tokens: int = 0
    # Optional callable for custom assertions: fn(result) -> None (raise on fail)
    custom_assert: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.op


@dataclass
class CaseResult:
    case: BenchCase
    response: dict[str, Any]
    atelier_tokens: int
    elapsed_ms: float
    passed: bool
    failure: str = ""

    @property
    def tokens_saved(self) -> int:
        return max(0, self.case.baseline_tokens - self.atelier_tokens)

    @property
    def savings_pct(self) -> float:
        if self.case.baseline_tokens == 0:
            return 0.0
        return self.tokens_saved / self.case.baseline_tokens * 100


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
    def avg_savings_pct(self) -> float:
        with_baseline = [r for r in self.results if r.case.baseline_tokens > 0]
        if not with_baseline:
            return 0.0
        return sum(r.savings_pct for r in with_baseline) / len(with_baseline)


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
        return CaseResult(
            case=case,
            response={},
            atelier_tokens=0,
            elapsed_ms=elapsed_ms,
            passed=False,
            failure=f"exception: {exc}",
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    atelier_tokens = _tokens(response) if response is not None else 4  # "null" is 4 chars

    failure = _check(case, response)
    return CaseResult(
        case=case,
        response=response or {},
        atelier_tokens=atelier_tokens,
        elapsed_ms=elapsed_ms,
        passed=failure == "",
        failure=failure,
    )


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
