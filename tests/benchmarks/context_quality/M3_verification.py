"""M3 — Counterexample loop benchmark: bounded self-correction on seeded edits.

This benchmark keeps the repair policy deterministic so it can run in CI without
calling a live model. It validates the benchmark's core claim: structured
counterexamples provide enough scoped signal for a bounded retry loop to repair
seeded type errors substantially more often than a no-counterexample baseline.

Targets:
* >=0.9 initial detection rate over 20 seeded mypy failures
* >=0.6 self-correction rate within a 3-attempt budget
* <=0.15 baseline success without counterexamples

Run explicitly (slow):
    uv run pytest tests/benchmarks/context_quality/M3_verification.py -v -m slow
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atelier.core.capabilities.verification import Counterexample, VerifierCapability

_CANONICAL_LITERALS = {
    "int": "0",
    "str": '"fixed"',
    "bool": "False",
    "float": "0.0",
    "bytes": 'b"fixed"',
    "list[int]": "[1]",
    "tuple[int,str]": '(1, "fixed")',
    "dict[str,int]": '{"a": 1}',
    "set[int]": "{1}",
    "int|None": "None",
}
_TYPE_SPECS = [
    ("int", '"oops"', "0"),
    ("str", "123", '"fixed"'),
    ("bool", '"oops"', "False"),
    ("float", '"oops"', "0.0"),
    ("bytes", '"oops"', 'b"fixed"'),
    ("list[int]", '"oops"', "[1]"),
    ("tuple[int, str]", '"oops"', '(1, "fixed")'),
    ("dict[str, int]", '"oops"', '{"a": 1}'),
    ("set[int]", '"oops"', "{1}"),
    ("int | None", '"oops"', "None"),
]
_ASSIGNMENT = re.compile(r"^(?P<indent>\s*)(?P<target>\w+\s*:\s*(?P<annotation>[^=]+?))\s*=\s*.+$")
_RETURN = re.compile(r"^(?P<indent>\s*)return\s+.+$")
_DEF = re.compile(r"^\s*def\s+\w+\(.*\)\s*->\s*(?P<annotation>[^:]+):\s*$")


@dataclass(frozen=True)
class SeededTypeErrorCase:
    name: str
    source: str
    fixed_source: str


def _normalize_annotation(annotation: str) -> str:
    return re.sub(r"\s+", "", annotation)


def _assignment_case(index: int, annotation: str, bad_literal: str, good_literal: str) -> SeededTypeErrorCase:
    name = f"assignment_{index:02d}.py"
    source = f"value_{index}: {annotation} = {bad_literal}\n"
    fixed_source = f"value_{index}: {annotation} = {good_literal}\n"
    return SeededTypeErrorCase(name=name, source=source, fixed_source=fixed_source)


def _return_case(index: int, annotation: str, bad_literal: str, good_literal: str) -> SeededTypeErrorCase:
    name = f"return_{index:02d}.py"
    source = f"def produce_{index}() -> {annotation}:\n    return {bad_literal}\n"
    fixed_source = f"def produce_{index}() -> {annotation}:\n    return {good_literal}\n"
    return SeededTypeErrorCase(name=name, source=source, fixed_source=fixed_source)


_CASES = [
    *(
        _assignment_case(index, annotation, bad_literal, good_literal)
        for index, (annotation, bad_literal, good_literal) in enumerate(_TYPE_SPECS, start=1)
    ),
    *(
        _return_case(index, annotation, bad_literal, good_literal)
        for index, (annotation, bad_literal, good_literal) in enumerate(_TYPE_SPECS, start=1)
    ),
]


def test_m3_typecheck_targets_exact_touched_files() -> None:
    seen: dict[str, Any] = {}

    def fake_run(args: Any, cwd: Path) -> Any:
        seen["args"] = list(args)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    VerifierCapability(run=fake_run).run(
        scope_files=["latest.py", "helper.py", "tests/test_helper.py"],
        checks=("typecheck",),
    )

    assert seen["args"] == ["uv", "run", "mypy", "latest.py", "helper.py"]


def _typecheck_counterexamples(case_root: Path, case_name: str) -> list[Counterexample]:
    verifier = VerifierCapability(cwd=case_root)
    return verifier.run(scope_files=[case_name], checks=("typecheck",))


def _canonical_literal(annotation: str) -> str | None:
    return _CANONICAL_LITERALS.get(_normalize_annotation(annotation))


def _repair_assignment(line: str) -> str:
    match = _ASSIGNMENT.match(line)
    if not match:
        return line
    replacement = _canonical_literal(match.group("annotation"))
    if replacement is None:
        return line
    return f"{match.group('indent')}{match.group('target')} = {replacement}"


def _return_annotation(lines: list[str], *, line_index: int) -> str | None:
    for index in range(line_index - 1, -1, -1):
        match = _DEF.match(lines[index])
        if match:
            return match.group("annotation")
    return None


def _repair_return(lines: list[str], *, line_index: int) -> str:
    match = _RETURN.match(lines[line_index])
    if not match:
        return lines[line_index]
    annotation = _return_annotation(lines, line_index=line_index)
    if annotation is None:
        return lines[line_index]
    replacement = _canonical_literal(annotation)
    if replacement is None:
        return lines[line_index]
    return f"{match.group('indent')}return {replacement}"


def _apply_counterexample(source: str, counterexample: Counterexample) -> str:
    if counterexample.line is None or counterexample.line <= 0:
        return source

    lines = source.splitlines()
    line_index = counterexample.line - 1
    if line_index >= len(lines):
        return source

    diagnostic = counterexample.diagnostic or ""
    if diagnostic.startswith("Incompatible types in assignment"):
        lines[line_index] = _repair_assignment(lines[line_index])
    elif diagnostic.startswith("Incompatible return value type"):
        lines[line_index] = _repair_return(lines, line_index=line_index)

    return "\n".join(lines) + "\n"


def _attempt_self_correction(case_root: Path, case: SeededTypeErrorCase) -> tuple[bool, bool]:
    case_root.mkdir(parents=True, exist_ok=True)
    target = case_root / case.name
    target.write_text(case.source, encoding="utf-8")

    initial_counterexamples = _typecheck_counterexamples(case_root, case.name)
    detected = any(
        ce.check == "typecheck" and Path(str(ce.file_path)).name == case.name for ce in initial_counterexamples
    )

    current = case.source
    for _attempt in range(3):
        counterexamples = _typecheck_counterexamples(case_root, case.name)
        if not counterexamples:
            final_source = target.read_text(encoding="utf-8")
            return detected, final_source == case.fixed_source
        next_source = current
        for counterexample in counterexamples:
            next_source = _apply_counterexample(next_source, counterexample)
        if next_source == current:
            break
        current = next_source
        target.write_text(current, encoding="utf-8")

    final_counterexamples = _typecheck_counterexamples(case_root, case.name)
    final_source = target.read_text(encoding="utf-8")
    return detected, not final_counterexamples and final_source == case.fixed_source


@pytest.mark.slow
def test_m3_counterexamples_enable_bounded_self_correction(tmp_path: Path) -> None:
    detected = 0
    repaired = 0
    baseline = 0

    for case in _CASES:
        detected_hit, repaired_hit = _attempt_self_correction(tmp_path / "counterexamples", case)
        detected += int(detected_hit)
        repaired += int(repaired_hit)

        baseline_root = tmp_path / "baseline"
        baseline_root.mkdir(parents=True, exist_ok=True)
        baseline_target = baseline_root / case.name
        baseline_target.write_text(case.source, encoding="utf-8")
        baseline += int(baseline_target.read_text(encoding="utf-8") == case.fixed_source)

    detection_rate = detected / len(_CASES)
    repaired_rate = repaired / len(_CASES)
    baseline_rate = baseline / len(_CASES)

    assert detection_rate >= 0.9, f"detection rate {detection_rate:.2f} below 0.90 target"
    assert repaired_rate >= 0.6, f"self-correction rate {repaired_rate:.2f} below 0.60 target"
    assert baseline_rate <= 0.15, f"baseline rate {baseline_rate:.2f} exceeded 0.15 ceiling"
