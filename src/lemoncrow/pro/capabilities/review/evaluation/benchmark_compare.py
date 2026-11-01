"""The reviewer comparison: raw, auditable TP / FP / FN per pull request, and the same totals side by side per reviewer.

Every reviewer in a run is scored by the same code against the same labels:

* ``lemoncrow:<depth>``: the product's findings at each depth;
* ``diy``: one plain prompt over the unified diff (:mod:`diy_review`), the homemade review Action;
* ``imported:<author>``: comments another tool already left on the pull request (``import-reviews``, ``--fetch-baseline``).

On a labelled pull request whose review finished, a surfaced finding or inline comment is a **true positive** when it
is assigned to a label by :func:`scoring.match_labels` (the rule that fired is recorded), a **false positive** when it
matches no label (with the reason), and a label no finding was assigned to is a **false negative**. Labels are bugs a
later commit fixed, so a false positive is an upper bound: a real bug nobody fixed counts against the reviewer.
Precision, recall and F1 carry Wilson 95% intervals. With ``--repeat N`` every reviewer reviews each pull request N
times and the report says how much the findings move between reruns.

Nothing here calls a model or a judge. The matcher is deterministic (``MATCHER_VERSION``) and the report records it.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.benchmark import BenchmarkOptions, PrResult, RunRecord
from lemoncrow.pro.capabilities.review.evaluation.matching import _assign
from lemoncrow.pro.capabilities.review.evaluation.real_prs import RealPr, RealPrCorpus
from lemoncrow.pro.capabilities.review.evaluation.scoring import (
    Candidate,
    LabelScore,
    line_gap,
    match_labels,
    percentile,
    wilson,
)

COMPARISON_SCHEMA: Final[int] = 1
MATCHER_VERSION: Final[str] = "label-match-1"
_EXCERPT: Final[int] = 300
_BASIS: Final[Mapping[str, str]] = {
    "provider": "reported by the provider",
    "price_table": "worked out from the local price table",
    "override": "worked out from the rates you passed",
    "supplied": "a price you supplied",
}
FALSE_POSITIVE_REASONS: Final[Mapping[str, str]] = {
    "duplicate_of_caught_label": "would also catch a label that another finding already caught (the same bug twice)",
    "same_file_as_missed_label": "on a file with a label no finding caught, but not near enough to it",
    "same_file_as_caught_label": "on a file whose label was caught by another finding, but matches no label itself",
    "no_label_in_file": "on a file no label points at",
}


@dataclass(frozen=True, slots=True)
class _Run:
    """One review of one pull request by one reviewer, reduced to what scoring and cost need."""

    pr_id: str
    repeat: int
    status: str
    complete: bool
    reason: str
    candidates: tuple[Candidate, ...]
    pr_paths: tuple[str, ...] = ()
    usd: float | None = None
    usd_sources: tuple[str, ...] = ()
    calls: int = 0
    wall_seconds: float | None = None
    stopped: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _Series:
    id: str
    kind: str  # lemoncrow | diy | imported
    name: str
    runs: dict[str, list[_Run]] = field(default_factory=dict)
    depth: str = ""
    model: str = ""
    usd_supplied: float | None = None


# --------------------------------------------------------------------------- #
# runs -> series
# --------------------------------------------------------------------------- #


def _from_record(record: RunRecord, repeat: int) -> _Run:
    usage = record.usage()
    return _Run(
        pr_id=record.pr_id,
        repeat=repeat,
        status=record.status,
        complete=bool(record.complete and record.status in {"ran", "nothing_changed"}),
        reason=record.reason,
        candidates=tuple(finding.candidate() for finding in record.findings),
        pr_paths=record.pr_paths,
        usd=usage["provider_usd"],
        usd_sources=tuple(usage["usd_sources"]),
        calls=int(usage["model_calls"]),
        wall_seconds=record.wall_seconds,
        stopped=record.stopped_by_budget,
        extra=dict(record.extra),
    )


def _series(
    results: Sequence[PrResult], options: BenchmarkOptions, *, usd_supplied: float | None, facts_model: str
) -> list[_Series]:
    out: list[_Series] = []
    if "lemoncrow" in options.reviewers:
        for depth in options.depths:
            series = _Series(f"lemoncrow:{depth}", "lemoncrow", f"LemonCrow ({depth})", depth=depth, model=facts_model)
            for result in results:
                attempts = result.attempts.get(depth) or ([result.reviews[depth]] if depth in result.reviews else [])
                if attempts:
                    series.runs[result.pr.id] = [_from_record(rec, index) for index, rec in enumerate(attempts, 1)]
            out.append(series)
    if "diy" in options.reviewers:
        version = options.diy_prompt.version if options.diy_prompt is not None else ""
        series = _Series("diy", "diy", f"DIY prompt ({version})", depth="diy", model=facts_model)
        for result in results:
            if diy_attempts := result.attempts.get("diy"):
                series.runs[result.pr.id] = [_from_record(rec, index) for index, rec in enumerate(diy_attempts, 1)]
        out.append(series)
    # Imported comments: every author seen anywhere is a reviewer on every pull request whose comments were read, so
    # an author with no inline comment on one of them scores as having found nothing there, not as absent.
    loaded = [r for r in results if r.baseline_note.startswith(("read ", "fetched "))]
    authors = sorted({c.author or "imported" for r in loaded for c in r.comments if c.kind == "inline"})
    for author in authors:
        series = _Series(f"imported:{author}", "imported", f"{author} (imported comments)", usd_supplied=usd_supplied)
        for result in loaded:
            mine = tuple(
                c.candidate() for c in result.comments if c.kind == "inline" and (c.author or "imported") == author
            )
            series.runs[result.pr.id] = [_Run(result.pr.id, 1, "imported", True, result.baseline_note, mine)]
        out.append(series)
    return out


# --------------------------------------------------------------------------- #
# scoring one run
# --------------------------------------------------------------------------- #


def _excerpt(text: str) -> str:
    return " ".join(text.split())[:_EXCERPT]


def _finding_wire(candidate: Candidate) -> dict[str, Any]:
    return {**candidate.to_wire(), "excerpt": _excerpt(candidate.text)}


def _false_positive_reason(candidate: Candidate, score: LabelScore) -> tuple[str, str]:
    if candidate.id in set(score.duplicates):
        return "duplicate_of_caught_label", FALSE_POSITIVE_REASONS["duplicate_of_caught_label"]
    missed_paths = {region.path: label.id for label in score.missed for region in label.regions}
    if candidate.path in missed_paths:
        return (
            "same_file_as_missed_label",
            f"{FALSE_POSITIVE_REASONS['same_file_as_missed_label']} ({missed_paths[candidate.path]})",
        )
    caught_paths = {region.path: m.label.id for m in score.matches for region in m.label.regions}
    if candidate.path in caught_paths:
        return (
            "same_file_as_caught_label",
            f"{FALSE_POSITIVE_REASONS['same_file_as_caught_label']} ({caught_paths[candidate.path]})",
        )
    return "no_label_in_file", FALSE_POSITIVE_REASONS["no_label_in_file"]


def _score(pr: RealPr, run: _Run, corpus: RealPrCorpus, pr_paths: Sequence[str]) -> dict[str, Any]:
    score = match_labels(
        pr.labels, run.candidates, mode=corpus.match, tolerance=corpus.line_tolerance, pr_paths=pr_paths
    )
    summaries = {label.id: label.summary for label in pr.labels}
    false_positives = []
    for candidate in score.unmatched:
        reason, detail = _false_positive_reason(candidate, score)
        false_positives.append({"finding": _finding_wire(candidate), "reason": reason, "detail": detail})
    return {
        "tp": score.caught,
        "fp": len(score.unmatched),
        "fn": len(score.missed),
        "matches": [
            {
                "label": match.label.id,
                "label_summary": summaries[match.label.id],
                "rule": match.rule,
                "line_gap": match.gap,
                "finding": _finding_wire(match.candidate),
            }
            for match in score.matches
        ],
        "false_positives": false_positives,
        "missed": [{"label": label.id, "label_summary": label.summary} for label in score.missed],
    }


def _rate(numerator: int, denominator: int, *, note: str = "") -> dict[str, Any]:
    return {
        "value": round(numerator / denominator, 4) if denominator else None,
        "ci95": wilson(numerator, denominator),
        "numerator": numerator,
        "denominator": denominator,
        "interval": "wilson" + (f" ({note})" if note else ""),
    }


def _totals(scores: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    tp = sum(int(s["tp"]) for s in scores.values())
    fp = sum(int(s["fp"]) for s in scores.values())
    fn = sum(int(s["fn"]) for s in scores.values())
    return {
        "prs": len(scores),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "labels": tp + fn,
        "precision": _rate(tp, tp + fp),
        "recall": _rate(tp, tp + fn),
        "f1": _rate(
            2 * tp, 2 * tp + fp + fn, note="on 2TP out of 2TP+FP+FN, an approximation: F1 is not a binomial rate"
        ),
    }


# --------------------------------------------------------------------------- #
# stability between reruns
# --------------------------------------------------------------------------- #


def _same_finding(tolerance: int) -> Any:
    def candidates(item: Candidate, others: Sequence[Candidate]) -> list[int]:
        rows = [
            (line_gap(item.start_line, item.end_line, other.start_line, other.end_line), other.id, index)
            for index, other in enumerate(others)
            if other.path == item.path
            and line_gap(item.start_line, item.end_line, other.start_line, other.end_line) <= tolerance
        ]
        return [index for _gap, _id, index in sorted(rows)]

    return candidates


def _pairs(left: Sequence[Candidate], right: Sequence[Candidate], tolerance: int) -> dict[int, int]:
    """One-to-one ``left index -> right index``: two findings are the same when on one file within *tolerance* lines.

    The wording is not compared: a model words the same finding differently each time, and where it points is what a
    reader acts on.
    """

    find = _same_finding(tolerance)
    return _assign(list(left), list(right), lambda item: find(item, right))


def _stability_pr(runs: Sequence[_Run], tolerance: int) -> dict[str, Any] | None:
    sets = [run.candidates for run in runs if run.complete]
    count = len(sets)
    if count < 2:
        return None
    parent = {(i, j): (i, j) for i, s in enumerate(sets) for j in range(len(s))}

    def root(node: tuple[int, int]) -> tuple[int, int]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    jaccards: list[float] = []
    for a in range(count):
        for b in range(a + 1, count):
            pairs = _pairs(sets[a], sets[b], tolerance)
            for left, right in pairs.items():
                parent[root((a, left))] = root((b, right))
            union = len(sets[a]) + len(sets[b]) - len(pairs)
            jaccards.append(len(pairs) / union if union else 1.0)
    clusters: dict[tuple[int, int], set[int]] = {}
    for node in parent:
        clusters.setdefault(root(node), set()).add(node[0])
    stable = sum(1 for runs_with in clusters.values() if len(runs_with) == count)
    return {
        "runs": count,
        "findings_per_run": [len(s) for s in sets],
        "mean_jaccard": round(statistics.fmean(jaccards), 4),
        "min_jaccard": round(min(jaccards), 4),
        "findings": len(clusters),
        "stable": stable,
        "flipping": len(clusters) - stable,
    }


def _spread(values: Sequence[float | None]) -> dict[str, Any]:
    known = [v for v in values if v is not None]
    if not known:
        return {"n": 0, "min": None, "max": None, "mean": None, "stdev": None}
    return {
        "n": len(known),
        "min": round(min(known), 4),
        "max": round(max(known), 4),
        "mean": round(statistics.fmean(known), 4),
        "stdev": round(statistics.stdev(known), 4) if len(known) > 1 else 0.0,
    }


def _label_flips(per_run: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """``(labels caught in some reruns and not others, labels scored)`` over one pull request's scored reruns."""

    caught: dict[str, int] = {}
    for score in per_run:
        for match in score["matches"]:
            caught[match["label"]] = caught.get(match["label"], 0) + 1
        for missed in score["missed"]:
            caught.setdefault(missed["label"], 0)
    return sum(1 for n in caught.values() if 0 < n < len(per_run)), len(caught)


# --------------------------------------------------------------------------- #
# cost and time
# --------------------------------------------------------------------------- #


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "total": None, "mean": None, "median": None, "p90": None, "max": None}
    p90 = percentile(values, 0.9)
    return {
        "n": len(values),
        "total": round(sum(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "median": round(statistics.median(values), 6),
        "p90": None if p90 is None else round(p90, 6),
        "max": round(max(values), 6),
    }


def _cost(series: _Series) -> dict[str, Any]:
    runs = [run for pr_runs in series.runs.values() for run in pr_runs if run.complete]
    if series.kind == "imported":
        supplied = series.usd_supplied
        return {
            "per_pr": _stats([supplied] * len(runs) if supplied is not None else []),
            "usd_basis": "supplied" if supplied is not None else "none",
            "usd_basis_note": _BASIS["supplied"] if supplied is not None else "no price was supplied for this reviewer",
            "unknown_cost_runs": 0 if supplied is not None else len(runs),
            "calls_per_pr": None,
        }
    sources = sorted({source for run in runs for source in run.usd_sources})
    unknown = sum(1 for run in runs if run.usd is None)
    if not sources:
        basis = "none" if not unknown else "unknown"
    elif len(sources) == 1:
        basis = sources[0]
    else:
        basis = "mixed"
    note = "; ".join(f"{source}: {_BASIS.get(source, source)}" for source in sources) or "no model call was made"
    return {
        "per_pr": _stats([run.usd for run in runs if run.usd is not None]),
        "usd_basis": basis,
        "usd_basis_note": note,
        "unknown_cost_runs": unknown,
        "calls_per_pr": round(statistics.fmean([run.calls for run in runs]), 3) if runs else None,
    }


def _wall(series: _Series) -> dict[str, Any]:
    runs = [run for pr_runs in series.runs.values() for run in pr_runs if run.complete and run.wall_seconds is not None]
    return _stats([float(run.wall_seconds or 0.0) for run in runs]) if series.kind != "imported" else _stats([])


# --------------------------------------------------------------------------- #
# one reviewer
# --------------------------------------------------------------------------- #


def _summarise(
    series: _Series,
    corpus: RealPrCorpus,
    paths_by_pr: Mapping[str, Sequence[str]],
    repeat: int,
) -> dict[str, Any]:
    per_pr: list[dict[str, Any]] = []
    scores: dict[int, dict[str, dict[str, Any]]] = {}  # repeat -> pr -> score
    not_scored: list[dict[str, str]] = []
    label_flips = labels_scored = 0
    pr_stability: list[dict[str, Any]] = []
    for pr in corpus.prs:
        runs = series.runs.get(pr.id)
        if not runs:
            continue
        paths = paths_by_pr.get(pr.id) or sorted({c.path for run in runs for c in run.candidates})
        rows: list[dict[str, Any]] = []
        scored: list[dict[str, Any]] = []
        for run in runs:
            row: dict[str, Any] = {
                "repeat": run.repeat,
                "status": run.status,
                "complete": run.complete,
                "reason": run.reason,
                "model_calls": run.calls,
                "usd": run.usd,
                "usd_sources": list(run.usd_sources),
                "wall_seconds": None if run.wall_seconds is None else round(run.wall_seconds, 3),
                "findings": [_finding_wire(c) for c in run.candidates],
                **({"extra": dict(run.extra)} if run.extra else {}),
            }
            if pr.labelled and run.complete:
                score = _score(pr, run, corpus, paths)
                row.update(score)
                scores.setdefault(run.repeat, {})[pr.id] = score
                scored.append(score)
            elif pr.labelled and run.repeat == 1:
                not_scored.append({"pr": pr.id, "reason": run.reason or f"the review did not finish ({run.status})"})
            rows.append(row)
        entry: dict[str, Any] = {"pr": pr.id, "labelled": pr.labelled, "labels": len(pr.labels), "runs": rows}
        stability = _stability_pr(runs, corpus.line_tolerance) if repeat > 1 else None
        if stability is not None:
            if len(scored) > 1:
                flips, count = _label_flips(scored)
                stability["labels_flipping"], stability["labels"] = flips, count
                label_flips, labels_scored = label_flips + flips, labels_scored + count
            entry["stability"] = stability
            pr_stability.append(stability)
        per_pr.append(entry)
    headline = scores.get(1, {})
    run_totals = [{"repeat": k, **_totals(scores[k])} for k in sorted(scores)]
    reasons: dict[str, int] = {}
    for score in headline.values():
        for item in score["false_positives"]:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    stability_block: dict[str, Any] | None = None
    if repeat > 1:
        clusters = sum(s["findings"] for s in pr_stability)
        flipping = sum(s["flipping"] for s in pr_stability)
        nonempty = [s for s in pr_stability if any(s["findings_per_run"])]
        stability_block = {
            "definition": (
                "two findings from different reruns are the same when they are on one file within the corpus "
                "line_tolerance lines of each other; wording is not compared. Agreement is the Jaccard index of the "
                "two reruns' finding sets (two empty sets agree fully), averaged over every pair of reruns."
            ),
            "runs_per_pr": repeat,
            "prs_compared": len(pr_stability),
            "mean_jaccard": _mean([s["mean_jaccard"] for s in pr_stability]),
            "mean_jaccard_prs_with_findings": _mean([s["mean_jaccard"] for s in nonempty]),
            "findings": clusters,
            "stable_findings": clusters - flipping,
            "flipping_findings": flipping,
            "flip_rate": round(flipping / clusters, 4) if clusters else None,
            "labels_scored": labels_scored,
            "labels_flipping": label_flips,
            "precision_across_reruns": _spread([t["precision"]["value"] for t in run_totals]),
            "recall_across_reruns": _spread([t["recall"]["value"] for t in run_totals]),
            "f1_across_reruns": _spread([t["f1"]["value"] for t in run_totals]),
        }
    return {
        "id": series.id,
        "kind": series.kind,
        "name": series.name,
        "depth": series.depth,
        "model": series.model,
        "headline_run": 1,
        "totals": _totals(headline),
        "false_positive_reasons": reasons,
        "prs_scored": sorted(headline),
        "prs_not_scored": not_scored,
        "cost": _cost(series),
        "wall_seconds": _wall(series),
        "runs": run_totals,
        "stability": stability_block,
        "per_pr": per_pr,
    }


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def scoring_metadata(corpus: RealPrCorpus, options: BenchmarkOptions) -> dict[str, Any]:
    """How findings are matched to labels: no model judge, a deterministic rule, and where text similarity is used."""

    return {
        "matcher_version": MATCHER_VERSION,
        "kind": "deterministic",
        "llm_judge": False,
        "judge_prompt": None,
        "label_match": {
            "mode": corpus.match,
            "line_tolerance": corpus.line_tolerance,
            "location": "same file, and the finding's changed region lies within the tolerance of a line the fix edited",
            "symbol": "the finding's text names an identifier the fix edited, on a file the pull request changed",
            "assignment": "one finding per label and one label per finding (maximum assignment, fixed order)",
            "text_similarity_used": False,
        },
        "agreement_with_imported_reviewers": {
            "used_for": "the agreement tables against an imported reviewer only; never for TP, FP or FN",
            "similarity": "Ochiai coefficient over content words",
            "threshold": options.similarity,
            "line_window": options.line_window,
        },
        "golden_comment_scoring": (
            "the 50-pull-request golden-comment corpus is scored by `golden-score`, which does use an LLM judge "
            "(prompt golden-judge-1); that judge is not part of this report"
        ),
        "false_positive_means": (
            "a false positive is a surfaced finding that matches no label; labels are bugs a later commit fixed, so "
            "this is an upper bound"
        ),
    }


def build_comparison(
    corpus: RealPrCorpus,
    options: BenchmarkOptions,
    results: Sequence[PrResult],
    *,
    model: str,
    usd_supplied: float | None = None,
) -> dict[str, Any]:
    """Every reviewer in the run, scored by the same rules, with its cost, time and (with ``--repeat``) stability."""

    series = _series(results, options, usd_supplied=usd_supplied, facts_model=model)
    paths_by_pr: dict[str, Sequence[str]] = {}
    for item in series:
        for pr_id, runs in item.runs.items():
            for run in runs:
                if run.pr_paths and pr_id not in paths_by_pr:
                    paths_by_pr[pr_id] = run.pr_paths
    reviewers = [_summarise(item, corpus, paths_by_pr, options.repeat) for item in series]
    sets = [set(row["prs_scored"]) for row in reviewers]
    common = sorted(set.intersection(*sets)) if sets else []
    out: dict[str, Any] = {
        "schema": COMPARISON_SCHEMA,
        "scope": (
            "labelled pull requests whose review finished. TP: a finding assigned to a label. FP: a surfaced finding "
            "that matches no label (an upper bound: labels are bugs a later commit fixed). FN: a label no finding "
            "was assigned to. The headline row is each reviewer's first run; every rerun is under `runs`."
        ),
        "repeat": options.repeat,
        "scoring": scoring_metadata(corpus, options),
        "reviewers": reviewers,
        "same_pull_requests": all(s == sets[0] for s in sets) if sets else True,
        "common_pull_requests": common,
    }
    if not out["same_pull_requests"]:
        for row in reviewers:
            first: dict[str, dict[str, Any]] = {}
            for entry in row["per_pr"]:
                if entry["pr"] in common and entry["runs"] and "tp" in entry["runs"][0]:
                    first[entry["pr"]] = entry["runs"][0]
            row["totals_on_common_pull_requests"] = _totals(first)
    if options.diy_prompt is not None and "diy" in options.reviewers:
        out["diy_prompt"] = options.diy_prompt.to_wire()
    return out


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.0f}%"


def _rate_text(rate: Mapping[str, Any]) -> str:
    if rate["value"] is None:
        return "n/a"
    ci = rate["ci95"]
    return f"{_pct(rate['value'])} ({_pct(ci[0])} to {_pct(ci[1])})" if ci else _pct(rate["value"])


def _usd(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"${float(value):.4f}" if float(value) < 1 else f"${float(value):.2f}"


def _num(value: Any, digits: int = 1) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def _rows(reviewers: Sequence[Mapping[str, Any]], key: str) -> list[list[str]]:
    out = []
    for row in reviewers:
        totals = row[key] if key in row else row["totals"]
        cost = row["cost"]
        out.append(
            [
                row["name"],
                str(totals["prs"]),
                str(totals["tp"]),
                str(totals["fp"]),
                str(totals["fn"]),
                _rate_text(totals["precision"]),
                _rate_text(totals["recall"]),
                _rate_text(totals["f1"]),
                f"{_usd(cost['per_pr']['mean'])} ({cost['usd_basis']})",
                _num(row["wall_seconds"]["mean"]),
            ]
        )
    return out


_HEADER: Final[tuple[str, ...]] = (
    "Reviewer",
    "PRs",
    "TP",
    "FP",
    "FN",
    "Precision (95% CI)",
    "Recall (95% CI)",
    "F1 (95% CI)",
    "USD per PR (basis)",
    "Wall s per PR",
)


def render_comparison_markdown(comparison: Mapping[str, Any]) -> list[str]:
    reviewers: Sequence[Mapping[str, Any]] = comparison["reviewers"]
    if not reviewers:
        return []
    lines = ["## Reviewer comparison", "", f"_{comparison['scope']}_", ""]
    lines += _table(_HEADER, _rows(reviewers, "totals"))
    lines += [
        "",
        "Intervals are Wilson 95% intervals; the F1 interval is an approximation. USD per PR is the mean over every "
        "finished review of the reviewer (labelled or not), and its basis says who priced it.",
        "",
    ]
    if all(row["totals"]["prs"] == 0 for row in reviewers):
        lines += [
            "**No pull request in this run was scored here**: this corpus has no fix-derived labels (or no review "
            "finished). On the golden-comment corpus the TP / FP / FN come from `golden-score`, run on this report once "
            "per reviewer (`--depth <depth>`, `--depth diy`); see docs/reference/review-benchmark.md. Cost, time and "
            "rerun stability below do not need labels.",
            "",
        ]
    if not comparison["same_pull_requests"]:
        lines += [
            f"**The reviewers were not all scored on the same pull requests.** On the {len(comparison['common_pull_requests'])} "
            "that every reviewer was scored on:",
            "",
        ]
        lines += _table(_HEADER, _rows(reviewers, "totals_on_common_pull_requests"))
        lines.append("")
    for row in reviewers:
        for item in row["prs_not_scored"]:
            lines.append(f"- {row['name']}: {item['pr']} not scored: {item['reason']}")
    lines += ["", "### Per pull request (first run): TP / FP / FN", ""]
    ids = sorted({entry["pr"] for row in reviewers for entry in row["per_pr"] if entry["labelled"]})
    body = []
    for pr_id in ids:
        cells = [pr_id]
        for row in reviewers:
            entry = next((e for e in row["per_pr"] if e["pr"] == pr_id), None)
            run = entry["runs"][0] if entry and entry["runs"] else None
            cells.append(f"{run['tp']} / {run['fp']} / {run['fn']}" if run and "tp" in run else "not scored")
        body.append(cells)
    lines += (
        _table(["Pull request", *[row["name"] for row in reviewers]], body) if body else ["No labelled pull request."]
    )
    lines += ["", "### What each finding matched (first run)", ""]
    for row in reviewers:
        lines.append(f"**{row['name']}**")
        for entry in row["per_pr"]:
            run = entry["runs"][0] if entry["runs"] else {}
            if "tp" not in run:
                continue
            lines.append(f"- {entry['pr']}")
            for match in run["matches"]:
                found = match["finding"]
                lines.append(
                    f"  - TP `{match['label']}` by {found['path']}:{found['lines'][0]} "
                    f"({match['rule']}, gap {match['line_gap']}): {found['title']}"
                )
            for item in run["false_positives"]:
                found = item["finding"]
                lines.append(
                    f"  - FP {found['path']}:{found['lines'][0]} `{item['reason']}`: {found['title']} ({item['detail']})"
                )
            for missed in run["missed"]:
                lines.append(f"  - FN `{missed['label']}`: {missed['label_summary']}")
        lines.append("")
    if comparison["repeat"] > 1:
        lines += [f"### Rerun stability ({comparison['repeat']} runs per pull request)", ""]
        body = []
        for row in reviewers:
            stab = row["stability"]
            if stab is None:
                continue
            spread = (stab["precision_across_reruns"], stab["recall_across_reruns"])
            body.append(
                [
                    row["name"],
                    _num(stab["mean_jaccard"], 2),
                    _num(stab["mean_jaccard_prs_with_findings"], 2),
                    f"{stab['stable_findings']} / {stab['findings']}",
                    f"{stab['labels_flipping']} / {stab['labels_scored']}",
                    *[f"{_pct(s['min'])} to {_pct(s['max'])}" for s in spread],
                ]
            )
        lines += _table(
            [
                "Reviewer",
                "Agreement (Jaccard)",
                "Agreement, PRs with findings",
                "Findings in every run",
                "Labels caught in some runs only",
                "Precision across runs",
                "Recall across runs",
            ],
            body,
        )
        first = next((row["stability"] for row in reviewers if row["stability"]), None)
        lines += ["", f"_{first['definition']}_" if first else "", ""]
    scoring = comparison["scoring"]
    lines += [
        "### How findings are matched",
        "",
        f"Matcher `{scoring['matcher_version']}`: {scoring['kind']}, no model judge. Label match mode "
        f"`{scoring['label_match']['mode']}`, line tolerance {scoring['label_match']['line_tolerance']}. "
        f"Text similarity (Ochiai over content words, threshold {scoring['agreement_with_imported_reviewers']['threshold']}, "
        f"line window {scoring['agreement_with_imported_reviewers']['line_window']}) is used only for agreement with "
        "imported reviewers, never for TP, FP or FN. "
        + scoring["false_positive_means"][0].upper()
        + scoring["false_positive_means"][1:]
        + ".",
        "",
    ]
    prompt = comparison.get("diy_prompt")
    if prompt:
        lines += [
            f"### The DIY prompt (`{prompt['file']}`, {prompt['version']}, sha256 `{prompt['sha256'][:16]}`)",
            "",
            "```text",
            prompt["text"],
            "```",
            "",
            "It is the system prompt of a single model call; the model gets the plain unified diff and nothing else.",
            "",
        ]
    return lines


__all__ = [
    "COMPARISON_SCHEMA",
    "FALSE_POSITIVE_REASONS",
    "MATCHER_VERSION",
    "build_comparison",
    "render_comparison_markdown",
    "scoring_metadata",
]
