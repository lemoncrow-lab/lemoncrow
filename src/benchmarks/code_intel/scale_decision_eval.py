"""Executable rubric for the Phase 5 M18 build-vs-integrate checkpoint."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal

CriterionKey = Literal[
    "cold_text_latency",
    "warm_text_latency",
    "regex_support",
    "file_scoped_query",
    "idle_memory",
    "load_memory",
    "byte_ranges",
    "private_repo_operation",
    "integration_time",
]
SearchScope = Literal["search", "code", "both"]
ResultShape = Literal["text", "symbol", "both"]

CRITERIA: tuple[CriterionKey, ...] = (
    "cold_text_latency",
    "warm_text_latency",
    "regex_support",
    "file_scoped_query",
    "idle_memory",
    "load_memory",
    "byte_ranges",
    "private_repo_operation",
    "integration_time",
)
PASSING_SCORE = 7


@dataclass(frozen=True)
class RepoAnswers:
    search_scope: SearchScope
    result_shape: ResultShape
    lifecycle_owner: str
    proves_symbol_shape_parity: bool


@dataclass(frozen=True)
class CandidateSpec:
    key: str
    label: str
    criteria: dict[CriterionKey, bool]
    rationale: str
    risks: str
    repo_answers: RepoAnswers
    keeps_05_02_as_written: bool
    selected_option_id: str


@dataclass(frozen=True)
class CandidateRow:
    key: str
    label: str
    criteria: dict[CriterionKey, bool]
    score: int
    passes: bool
    rationale: str
    risks: str
    repo_answers: RepoAnswers
    keeps_05_02_as_written: bool
    selected_option_id: str

    def verdict(self) -> str:
        return "pass" if self.passes else "fail"


@dataclass(frozen=True)
class DecisionSummary:
    selected_candidate_key: str
    selected_candidate_label: str
    selected_option_id: str
    search_scope: SearchScope
    result_shape: ResultShape
    lifecycle_owner: str
    keeps_05_02_as_written: bool
    requires_replan: bool
    rationale: str
    risks: str


@dataclass(frozen=True)
class EvaluationReport:
    rows: tuple[CandidateRow, ...]
    rows_by_key: dict[str, CandidateRow]
    decision: DecisionSummary


def _default_candidate_specs() -> tuple[CandidateSpec, ...]:
    lifecycle_owner = "session-scoped search backend supervisor owned by the MCP/runtime layer"
    return (
        CandidateSpec(
            key="src-cli",
            label="`src` CLI adapter",
            criteria={
                "cold_text_latency": False,
                "warm_text_latency": False,
                "regex_support": True,
                "file_scoped_query": True,
                "idle_memory": False,
                "load_memory": False,
                "byte_ranges": False,
                "private_repo_operation": False,
                "integration_time": True,
            },
            rationale="Thin adapter effort is low, but hosted/private-repo constraints and missing byte ranges make it a poor fit.",
            risks="Depends on external Sourcegraph access or separate local deployment, so it cannot be the offline default.",
            repo_answers=RepoAnswers(
                search_scope="search",
                result_shape="text",
                lifecycle_owner=lifecycle_owner,
                proves_symbol_shape_parity=False,
            ),
            keeps_05_02_as_written=False,
            selected_option_id="option-b",
        ),
        CandidateSpec(
            key="sourcegraph-self-hosted",
            label="Sourcegraph self-hosted",
            criteria={
                "cold_text_latency": True,
                "warm_text_latency": True,
                "regex_support": True,
                "file_scoped_query": True,
                "idle_memory": False,
                "load_memory": False,
                "byte_ranges": True,
                "private_repo_operation": True,
                "integration_time": False,
            },
            rationale="Search capabilities are strong, but idle/load memory and integration footprint exceed the repo's embedded-backend target.",
            risks="Requires replanning around a heavier external stack and a different adapter seam than the current Zoekt-first plan.",
            repo_answers=RepoAnswers(
                search_scope="search",
                result_shape="both",
                lifecycle_owner=lifecycle_owner,
                proves_symbol_shape_parity=False,
            ),
            keeps_05_02_as_written=False,
            selected_option_id="option-c",
        ),
        CandidateSpec(
            key="scip-mcp",
            label="External `scip-mcp` integration",
            criteria={
                "cold_text_latency": False,
                "warm_text_latency": False,
                "regex_support": False,
                "file_scoped_query": False,
                "idle_memory": False,
                "load_memory": False,
                "byte_ranges": True,
                "private_repo_operation": False,
                "integration_time": True,
            },
            rationale="The surface is symbol-oriented rather than large-repo text-search oriented, so it does not satisfy the M16 checkpoint goal.",
            risks="Would redirect Phase 5 onto a different contract and still leave text-search scale unresolved.",
            repo_answers=RepoAnswers(
                search_scope="code",
                result_shape="symbol",
                lifecycle_owner=lifecycle_owner,
                proves_symbol_shape_parity=True,
            ),
            keeps_05_02_as_written=False,
            selected_option_id="option-d",
        ),
        CandidateSpec(
            key="zoekt-standalone",
            label="Zoekt standalone (default)",
            criteria={criterion: True for criterion in CRITERIA},
            rationale="Best fit for Atelier's large-repo text-search need, with explicit offline operation and the cleanest path to keep Phase 5 on the search stack.",
            risks="05-02 must still introduce lifecycle ownership outside per-call engine rebuilds, but the integration surface stays aligned with the accepted plan.",
            repo_answers=RepoAnswers(
                search_scope="search",
                result_shape="text",
                lifecycle_owner=lifecycle_owner,
                proves_symbol_shape_parity=False,
            ),
            keeps_05_02_as_written=True,
            selected_option_id="option-a",
        ),
    )


def evaluate_default_candidates() -> EvaluationReport:
    rows = tuple(_evaluate_candidate(spec) for spec in _default_candidate_specs())
    rows_by_key = {row.key: row for row in rows}
    selected = select_recommended_candidate(EvaluationReport(rows=rows, rows_by_key=rows_by_key, decision=_placeholder_decision()))
    decision = DecisionSummary(
        selected_candidate_key=selected.key,
        selected_candidate_label=selected.label,
        selected_option_id=selected.selected_option_id,
        search_scope=selected.repo_answers.search_scope,
        result_shape=selected.repo_answers.result_shape,
        lifecycle_owner=selected.repo_answers.lifecycle_owner,
        keeps_05_02_as_written=selected.keeps_05_02_as_written,
        requires_replan=not selected.keeps_05_02_as_written,
        rationale=selected.rationale,
        risks=selected.risks,
    )
    return EvaluationReport(rows=rows, rows_by_key=rows_by_key, decision=decision)


def select_recommended_candidate(report: EvaluationReport) -> CandidateRow:
    passing = [row for row in report.rows if row.passes]
    text_first = [
        row
        for row in passing
        if row.repo_answers.search_scope == "search"
        and row.repo_answers.result_shape == "text"
        and not row.repo_answers.proves_symbol_shape_parity
    ]
    if text_first:
        return max(text_first, key=lambda row: row.score)
    if passing:
        return max(passing, key=lambda row: row.score)
    return max(report.rows, key=lambda row: row.score)


def render_checkpoint_appendix(report: EvaluationReport, *, date: str, evaluator: str) -> str:
    selected = report.rows_by_key[report.decision.selected_candidate_key]
    selected_title = {
        "zoekt-standalone": "Zoekt standalone",
        "src-cli": "`src` CLI",
        "sourcegraph-self-hosted": "Sourcegraph self-hosted",
        "scip-mcp": "external `scip-mcp` integration",
    }.get(selected.key, selected.label)
    lines = [
        "## Evaluation memo",
        "",
        f"> **Date:** {date}",
        f"> **Evaluator:** {evaluator}",
        "",
        "### Findings",
        "",
        _render_matrix_table(report.rows),
        "",
        "### Repo-specific Phase 5 answers",
        "",
        f"- `search_scope`: `{report.decision.search_scope}`",
        f"- `result_shape`: `{report.decision.result_shape}`",
        f"- `lifecycle_owner`: `{report.decision.lifecycle_owner}`",
        f"- `selected_option`: `{report.decision.selected_option_id}`",
        "- `code op=\"search\"` remains on the existing local/SCIP/semantic name-first path until a later adapter proves symbol-shape parity.",
        "",
        "### Decision",
        "",
        f"**Selected approach:** Proceed with {selected_title} for `{report.decision.search_scope}` workloads only",
        "",
        f"**Rationale:** {report.decision.rationale}",
        "",
        f"**05-02 status:** {'may proceed as written' if report.decision.keeps_05_02_as_written else 'must be replanned before implementation'}",
        "",
        f"**Risks:** {report.decision.risks}",
        "",
        "Any non-`option-a` winner would require replacing `05-02-PLAN.md` before backend work starts.",
    ]
    return "\n".join(lines)


def _evaluate_candidate(spec: CandidateSpec) -> CandidateRow:
    score = sum(1 for criterion in CRITERIA if spec.criteria[criterion])
    return CandidateRow(
        key=spec.key,
        label=spec.label,
        criteria=dict(spec.criteria),
        score=score,
        passes=score >= PASSING_SCORE,
        rationale=spec.rationale,
        risks=spec.risks,
        repo_answers=spec.repo_answers,
        keeps_05_02_as_written=spec.keeps_05_02_as_written,
        selected_option_id=spec.selected_option_id,
    )


def _render_matrix_table(rows: tuple[CandidateRow, ...]) -> str:
    rendered = [
        "| Candidate | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | Score | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        rendered.append(
            "| "
            + row.label
            + " | "
            + " | ".join("✅" if row.criteria[criterion] else "❌" for criterion in CRITERIA)
            + f" | {row.score}/9 | {'✅' if row.passes else '❌'} |"
        )
    return "\n".join(rendered)


def _placeholder_decision() -> DecisionSummary:
    return DecisionSummary(
        selected_candidate_key="",
        selected_candidate_label="",
        selected_option_id="",
        search_scope="search",
        result_shape="text",
        lifecycle_owner="",
        keeps_05_02_as_written=False,
        requires_replan=True,
        rationale="",
        risks="",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-05-19")
    parser.add_argument("--evaluator", default="Copilot")
    args = parser.parse_args()
    print(render_checkpoint_appendix(evaluate_default_candidates(), date=args.date, evaluator=args.evaluator))


__all__ = [
    "CRITERIA",
    "CandidateRow",
    "DecisionSummary",
    "EvaluationReport",
    "PASSING_SCORE",
    "RepoAnswers",
    "evaluate_default_candidates",
    "render_checkpoint_appendix",
    "select_recommended_candidate",
]


if __name__ == "__main__":
    main()
