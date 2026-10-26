"""Turn benchmark runs into a JSON and a markdown report, and say what stands between them and a published number.

The report states only what the runs produced. A figure whose inputs were not all known is ``null`` with its reason, not
a smaller number: tokens or dollars a provider did not report are never counted as zero. The ``basis`` of a report is its
driver: a report from the scripted driver describes the harness, a replay describes an earlier recording, only a live
report describes a model, and each says so at its head and in its publication checklist.

``publication`` is the checklist for the sentence the landing page promises ("cost per review, when early access
opens"). It does not publish anything. It lists, per claim, the conditions the run met and the ones it did not.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.benchmark import (
    SCHEMA,
    BenchmarkOptions,
    PrResult,
    RunRecord,
    SpendLedger,
    score_run,
)
from lemoncrow.pro.capabilities.review.evaluation.real_prs import RealPrCorpus
from lemoncrow.pro.capabilities.review.evaluation.scoring import match_labels, percentile, wilson

#: What a run must have behind it before the checklist says a claim is ready. These are the harness's own floors, chosen
#: so that one lucky pull request cannot carry a headline; the owner may choose stricter ones, not looser.
MIN_COST_REVIEWS: Final[int] = 8
MIN_QUALITY_PRS: Final[int] = 8
MIN_HEAD_TO_HEAD_PRS: Final[int] = 8
MIN_REPOSITORIES: Final[int] = 2
BASIS: Final[Mapping[str, str]] = {
    "live": "a live provider run",
    "replay": "a replay of fixtures an earlier run recorded",
    "fake": "the scripted driver (no model was called): the numbers describe the harness, not a model",
}


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "total": None, "mean": None, "median": None, "p90": None, "max": None}
    p90 = percentile(values, 0.9)
    return {
        "n": len(values),
        "total": round(sum(values), 6),
        "mean": _mean(values),
        "median": round(statistics.median(values), 6),
        "p90": None if p90 is None else round(p90, 6),
        "max": round(max(values), 6),
    }


def _known(runs: Sequence[RunRecord], key: str) -> list[float]:
    """The value of *key* over the runs that reported it; a run that did not is left out and counted by the caller."""

    return [float(value) for run in runs if (value := run.usage()[key]) is not None]


# --------------------------------------------------------------------------- #
# per depth
# --------------------------------------------------------------------------- #


def _depth_aggregate(
    depth: str, results: Sequence[PrResult], scores: Mapping[tuple[str, str], Mapping[str, Any]]
) -> dict[str, Any]:
    runs = [(r.pr, r.reviews[depth]) for r in results if depth in r.reviews]
    ran = [run for _pr, run in runs if run.status in {"ran", "nothing_changed"}]
    complete = [run for run in ran if run.complete]
    paid = [run for run in complete if run.calls]
    skipped = [run for _pr, run in runs if run.status == "skipped"]
    out: dict[str, Any] = {
        "prs": len(runs),
        "ran": len(ran),
        "complete": len(complete),
        "incomplete": len(ran) - len(complete),
        "skipped": len(skipped),
        "errored": sum(1 for _pr, run in runs if run.status == "error"),
    }
    out["model_calls"] = _stats([float(run.usage()["model_calls"]) for run in complete])
    out["zero_call_reviews"] = sum(1 for run in complete if not run.calls)
    out["input_tokens"] = _stats(_known(complete, "input_tokens"))
    out["output_tokens"] = _stats(_known(complete, "output_tokens"))
    out["tokens_unreported_reviews"] = sum(1 for run in paid if run.usage()["input_tokens"] is None)
    out["provider_usd"] = _stats(_known(complete, "provider_usd"))
    out["usd_unreported_reviews"] = sum(1 for run in paid if run.usage()["provider_usd"] is None)
    out["context_tokens"] = _stats([float(run.context_tokens) for run in complete])
    out["wall_seconds"] = _stats([run.wall_seconds for run in complete])
    out["model_seconds"] = _stats(_known(complete, "model_seconds"))
    sev = {key: sum(run.severity_counts()[key] for run in complete) for key in ("p0", "p1", "p2", "total")}
    out["findings"] = {
        **sev,
        "per_review": {key: round(value / len(complete), 3) for key, value in sev.items()} if complete else None,
    }
    expected = caught = noise = noise_inline = noise_duplicates = findings = 0
    by_rule = {"location": 0, "symbol": 0}
    causes = {"no_risk_candidate": 0, "not_asked": 0, "asked_not_reported": 0}
    missed: list[str] = []
    labelled_usd: list[float] = []
    labelled_prs = 0
    for pr, run in runs:
        label_score = scores.get((pr.id, depth), {}).get("labels")
        if label_score is None:
            continue
        labelled_prs += 1
        expected += label_score["expected"]
        caught += label_score["caught"]
        noise += label_score["noise"]
        noise_inline += label_score["noise_inline"]
        noise_duplicates += label_score["noise_duplicates"]
        findings += label_score["findings"]
        for match in label_score["matches"]:
            by_rule[match["rule"]] = by_rule.get(match["rule"], 0) + 1
        missed.extend(f"{pr.id}:{label}" for label in label_score["missed"])
        for detail in label_score["missed_detail"]:
            causes[detail["cause"]] += 1
        if (usd := run.usage()["provider_usd"]) is not None:
            labelled_usd.append(float(usd))
    out["catch"] = {
        "labelled_prs": labelled_prs,
        "expected": expected,
        "caught": caught,
        "rate": round(caught / expected, 4) if expected else None,
        "ci95": wilson(caught, expected),
        "by_rule": by_rule,
        "missed": missed,
        "missed_by_cause": causes,
    }
    out["noise"] = {
        "findings": findings,
        "unmatched": noise,
        "rate": round(noise / findings, 4) if findings else None,
        "ci95": wilson(noise, findings),
        "duplicates": noise_duplicates,
        "rate_excluding_duplicates": round((noise - noise_duplicates) / findings, 4) if findings else None,
        "unmatched_p0_p1": noise_inline,
        "rate_p0_p1": round(noise_inline / findings, 4) if findings else None,
    }
    out["cost_per_catch_usd"] = (
        round(sum(labelled_usd) / caught, 6) if caught and len(labelled_usd) == labelled_prs else None
    )
    return out


def _run_sum(run: RunRecord | str, key: str) -> float | None:
    if isinstance(run, str):
        return None
    value = run.usage()[key]
    return None if value is None else float(value)


def _push_aggregate(depth: str, results: Sequence[PrResult]) -> dict[str, Any]:
    pairs: list[tuple[RunRecord, RunRecord]] = []
    unavailable: dict[str, str] = {}
    for result in results:
        for push_id, cell in result.pushes.get(depth, {}).items():
            carry, full = cell.get("carry_forward"), cell.get("full")
            if isinstance(carry, RunRecord) and isinstance(full, RunRecord):
                pairs.append((carry, full))
            elif "unavailable" in cell and isinstance(cell["unavailable"], str):
                unavailable[f"{result.pr.id}:{push_id}"] = cell["unavailable"]
    usable = [(c, f) for c, f in pairs if c.complete and f.complete]
    out: dict[str, Any] = {"pushes": len(pairs), "complete_pairs": len(usable), "unavailable": unavailable}
    for label, index in (("carry_forward", 0), ("full", 1)):
        runs = [pair[index] for pair in usable]
        out[label] = {
            "model_calls": _stats([float(run.usage()["model_calls"]) for run in runs]),
            "provider_usd": _stats(_known(runs, "provider_usd")),
            "input_tokens": _stats(_known(runs, "input_tokens")),
            "rereviewed_targets_mean": _mean([float(run.rereviewed) for run in runs]),
        }
    both_usd = [
        (float(c.usage()["provider_usd"]), float(f.usage()["provider_usd"]))
        for c, f in usable
        if c.usage()["provider_usd"] is not None and f.usage()["provider_usd"] is not None
    ]
    full_calls = sum(float(f.usage()["model_calls"]) for _c, f in usable)
    carry_calls = sum(float(c.usage()["model_calls"]) for c, _f in usable)
    out["saved"] = {
        "calls_fraction": round(1 - carry_calls / full_calls, 4) if full_calls else None,
        "usd_mean": _mean([f - c for c, f in both_usd]),
        "usd_fraction": (
            round(1 - sum(c for c, _ in both_usd) / sum(f for _, f in both_usd), 4)
            if both_usd and sum(f for _, f in both_usd)
            else None
        ),
    }
    states = {"new": 0, "carried": 0, "fixed": 0, "stale": 0, "unreported": 0}
    for carry, _full in usable:
        for key in states:
            states[key] += carry.states.get(key, 0)
    out["findings_after_push"] = states
    return out


def _baseline_summary(
    results: Sequence[PrResult], scores: Mapping[tuple[str, str], Mapping[str, Any]], depths: Sequence[str]
) -> dict[str, Any]:
    with_comments = [r for r in results if r.comments]
    out: dict[str, Any] = {
        "prs_with_comments": len(with_comments),
        "inline_comments": sum(1 for r in with_comments for c in r.comments if c.kind == "inline"),
        "summary_comments": sum(1 for r in with_comments for c in r.comments if c.kind != "inline"),
        "judged": {
            "valid": sum(1 for r in with_comments for c in r.comments if c.verdict == "valid"),
            "invalid": sum(1 for r in with_comments for c in r.comments if c.verdict == "invalid"),
        },
        "by_depth": {},
    }
    for depth in depths:
        agreed = ours_only = theirs_only = near = 0
        for result in with_comments:
            agreement = scores.get((result.pr.id, depth), {}).get("agreement")
            if agreement is None:
                continue
            agreed += len(agreement["agreed"])
            ours_only += len(agreement["ours_only"])
            theirs_only += len(agreement["theirs_only"])
            near += len(agreement["near_misses"])
        out["by_depth"][depth] = {
            "agreed": agreed,
            "ours_only": ours_only,
            "theirs_only": theirs_only,
            "near_misses": near,
            "share_of_theirs_we_also_raised": (
                round(agreed / (agreed + theirs_only), 4) if agreed + theirs_only else None
            ),
        }
    return out


def _baseline_against_labels(results: Sequence[PrResult], corpus: RealPrCorpus) -> dict[str, Any]:
    """The baseline's own catch and noise against the labels, by the same rules as ours (where it has comments)."""

    expected = caught = noise = comments = 0
    prs = 0
    for result in results:
        if not result.pr.labelled or not result.comments:
            continue
        inline = [c.candidate() for c in result.comments if c.kind == "inline"]
        paths = {c.path for c in inline}
        score = match_labels(
            result.pr.labels, inline, mode=corpus.match, tolerance=corpus.line_tolerance, pr_paths=paths
        )
        prs += 1
        expected += len(result.pr.labels)
        caught += score.caught
        noise += len(score.unmatched)
        comments += len(inline)
    return {
        "labelled_prs_with_comments": prs,
        "expected": expected,
        "caught": caught,
        "rate": round(caught / expected, 4) if expected else None,
        "ci95": wilson(caught, expected),
        "comments": comments,
        "unmatched": noise,
        "noise_rate": round(noise / comments, 4) if comments else None,
    }


def _reference_team(
    aggregate: Mapping[str, Any], pushes: Mapping[str, Any], prs_per_month: int, reviews_per_pr: float, developers: int
) -> dict[str, Any]:
    """A projection from the report's own means: one full review per pull request, the rest of its reviews as pushes."""

    out: dict[str, Any] = {
        "developers": developers,
        "prs_per_month": prs_per_month,
        "reviews_per_pr": reviews_per_pr,
        "basis": "a projection from this report's mean cost per review and per push, not a quote and not a price",
        "by_depth": {},
    }
    extra = max(0.0, reviews_per_pr - 1.0)
    for depth, agg in aggregate.items():
        first = agg["provider_usd"]["mean"]
        push = pushes.get(depth, {})
        carry = (push.get("carry_forward") or {}).get("provider_usd", {}).get("mean")
        full = (push.get("full") or {}).get("provider_usd", {}).get("mean")
        row: dict[str, Any] = {"first_review_usd": first, "push_usd_carry_forward": carry, "push_usd_full": full}
        for label, value in (("monthly_usd_with_carry_forward", carry), ("monthly_usd_without", full)):
            row[label] = None if first is None or value is None else round(prs_per_month * (first + extra * value), 2)
        out["by_depth"][depth] = row
    return out


def _publication(
    mode: str,
    ledger: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    results: Sequence[PrResult],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    repositories = {r.pr.repository for r in results if not r.skipped}
    cost_reasons: list[str] = []
    quality_reasons: list[str] = []
    h2h_reasons: list[str] = []
    if mode != "live":
        for reasons in (cost_reasons, quality_reasons, h2h_reasons):
            reasons.append(f"the basis is {BASIS[mode]}; only a live run can support a published figure")
    if ledger.get("stopped"):
        for reasons in (cost_reasons, quality_reasons, h2h_reasons):
            reasons.append(f"the run stopped early: {ledger['stopped']}")
    if len(repositories) < MIN_REPOSITORIES:
        cost_reasons.append(
            f"only {len(repositories)} {'repository' if len(repositories) == 1 else 'repositories'}; "
            f"at least {MIN_REPOSITORIES} are needed"
        )
    per_depth: dict[str, Any] = {}
    for depth, agg in aggregate.items():
        usd = agg["provider_usd"]
        if usd["n"] < MIN_COST_REVIEWS:
            cost_reasons.append(
                f"{depth}: {usd['n']} complete reviews with a known cost; at least {MIN_COST_REVIEWS} are needed"
            )
        if agg["usd_unreported_reviews"]:
            cost_reasons.append(f"{depth}: {agg['usd_unreported_reviews']} reviews had no known cost")
        if agg["incomplete"] or agg["errored"] or agg["skipped"]:
            cost_reasons.append(
                f"{depth}: {agg['incomplete']} incomplete, {agg['errored']} errored, {agg['skipped']} skipped"
            )
        if agg["catch"]["labelled_prs"] < MIN_QUALITY_PRS:
            quality_reasons.append(
                f"{depth}: {agg['catch']['labelled_prs']} labelled pull requests scored; at least {MIN_QUALITY_PRS} are needed"
            )
        per_depth[depth] = {
            "reviews": usd["n"],
            "mean_usd": usd["mean"],
            "median_usd": usd["median"],
            "p90_usd": usd["p90"],
            "mean_tokens": (agg["input_tokens"]["mean"] or 0) + (agg["output_tokens"]["mean"] or 0) or None,
        }
    if baseline.get("prs_with_comments", 0) < MIN_HEAD_TO_HEAD_PRS:
        h2h_reasons.append(
            f"{baseline.get('prs_with_comments', 0)} pull requests carry the other reviewer's comments; at least "
            f"{MIN_HEAD_TO_HEAD_PRS} are needed"
        )
    if not baseline.get("judged", {}).get("valid") and not baseline.get("judged", {}).get("invalid"):
        h2h_reasons.append("no baseline comment has a verdict, so its precision is unknown")
    quality_reasons.append("catch and noise are rates over a handful of pull requests; quote them with their intervals")
    return {
        "cost_per_review": {"ready": not cost_reasons, "missing": cost_reasons, "figures": per_depth},
        "quality": {"ready": not [r for r in quality_reasons if "intervals" not in r], "missing": quality_reasons},
        "head_to_head": {"ready": not h2h_reasons, "missing": h2h_reasons},
        "always": [
            "name the model and its price list date; the figure is for that model only",
            "state that it is the mean over this corpus, which is not the reader's code",
            "do not publish another reviewer's price from this report: it holds only what you supplied",
        ],
    }


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def build_report(
    corpus: RealPrCorpus,
    options: BenchmarkOptions,
    results: Sequence[PrResult],
    ledger: SpendLedger,
    facts: Mapping[str, Any],
    *,
    name: str,
    command: str = "",
    code: Mapping[str, Any] | None = None,
    baseline_usd_per_review: float | None = None,
    baseline_label: str = "",
    team: tuple[int, float, int] = (300, 1.2, 10),
    generated_at: str | None = None,
) -> dict[str, Any]:
    mode = options.mode
    scores: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results:
        for depth, run in result.reviews.items():
            scores[(result.pr.id, depth)] = score_run(result.pr, run, corpus, result.comments, options)
    aggregate = {depth: _depth_aggregate(depth, results, scores) for depth in options.depths}
    pushes = {depth: _push_aggregate(depth, results) for depth in options.depths}
    baseline = _baseline_summary(results, scores, options.depths)
    baseline["against_labels"] = _baseline_against_labels(results, corpus)
    if baseline_usd_per_review is not None:
        baseline["usd_per_review_supplied"] = {"value": baseline_usd_per_review, "label": baseline_label}
        baseline["usd_ratio_by_depth"] = {
            depth: (
                round(baseline_usd_per_review / agg["provider_usd"]["mean"], 2) if agg["provider_usd"]["mean"] else None
            )
            for depth, agg in aggregate.items()
        }
    prs: list[dict[str, Any]] = []
    for result in results:
        pr = result.pr
        depths: dict[str, Any] = {}
        for depth, run in result.reviews.items():
            wire = run.to_wire()
            wire.update(scores[(pr.id, depth)])
            push_cells: dict[str, Any] = {}
            for push_id, cell in result.pushes.get(depth, {}).items():
                row: dict[str, Any] = {}
                for label, item in cell.items():
                    row[label] = item.to_wire() if isinstance(item, RunRecord) else item
                carry, full = cell.get("carry_forward"), cell.get("full")
                if isinstance(carry, RunRecord) and isinstance(full, RunRecord):
                    row["extra"] = {
                        "usd_with_carry_forward": carry.usage()["provider_usd"],
                        "usd_without": full.usage()["provider_usd"],
                        "calls_with_carry_forward": carry.usage()["model_calls"],
                        "calls_without": full.usage()["model_calls"],
                    }
                push_cells[push_id] = row
            wire["pushes"] = push_cells
            depths[depth] = wire
        prs.append(
            {
                "id": pr.id,
                "repository": pr.repository,
                "pull": pr.pull,
                "url": pr.url,
                "title": pr.title,
                "labelled": pr.labelled,
                "labels": [label.to_wire() for label in pr.labels],
                "skipped": result.skipped,
                "baseline": {
                    "note": result.baseline_note,
                    "inline_comments": sum(1 for c in result.comments if c.kind == "inline"),
                    "summary_comments": sum(1 for c in result.comments if c.kind != "inline"),
                    "comments": [c.to_wire() for c in result.comments],
                },
                "depths": depths,
            }
        )
    ledger_wire = ledger.to_wire()
    return {
        "schema": SCHEMA,
        "kind": "review-benchmark",
        "name": name,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "run": {
            "mode": mode,
            "basis": BASIS[mode],
            "driver": facts.get("driver", ""),
            "model": facts.get("model", ""),
            "options": options.to_wire(),
            "ledger": ledger_wire,
            "command": command,
            "code": dict(code or {}),
            "notes": list(facts.get("notes", [])),
            "wall_time_note": (
                "wall_seconds is the elapsed time of this replay and is not comparable; model_seconds sums the "
                "recorded latency of each call"
                if mode == "replay"
                else "wall_seconds is the elapsed time of the AI stage; model_seconds sums each call's latency"
            ),
            "overshoot_note": (
                "the spending ceiling is checked before each call, so a run can pass it by the calls in flight "
                "(at most the investigators of the deepest depth)"
            ),
        },
        "corpus": corpus.summary(),
        "aggregate": aggregate,
        "pushes": pushes,
        "baseline": baseline,
        "reference_team": _reference_team(aggregate, pushes, team[0], team[1], team[2]),
        "publication": _publication(mode, ledger_wire, aggregate, results, baseline),
        "prs": prs,
    }


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #


def _n(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _usd(value: Any) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _tok(value: Any) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    return f"{value / 1000:.1f}k" if value >= 1000 else f"{value:.0f}"


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.0f}%"


def _interval(rate: Any, ci: Any) -> str:
    if rate is None:
        return "n/a"
    return f"{_pct(rate)} ({_pct(ci[0])} to {_pct(ci[1])})" if ci else _pct(rate)


_CAUSE: Final[Mapping[str, str]] = {
    "no_risk_candidate": "triage found no risk signal, so no model was asked",
    "not_asked": "the model was not asked about this file at this depth",
    "asked_not_reported": "the model was asked about this file and did not report it",
}


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    run = report["run"]
    lines = [f"# Review benchmark: {report['name']}", ""]
    if run["mode"] != "live":
        lines += [
            f"> **Basis: {run['basis']}.** Nothing below is a result for a model. Do not quote it.",
            "",
        ]
    lines += [
        f"- Generated {report['generated_at']} from corpus `{report['corpus']['id']}` "
        f"({report['corpus']['prs']} pull requests, {report['corpus']['labelled_prs']} labelled, "
        f"{report['corpus']['labels']} labels, digest `{report['corpus']['digest'][:12]}`).",
        f"- Route: {run['driver'] or 'n/a'} / {run['model'] or 'n/a'}. Depths: {', '.join(run['options']['depths'])}.",
        f"- Ceiling: max USD {_n(run['ledger']['max_usd'])}, max calls {_n(run['ledger']['max_calls'])}; "
        f"used {run['ledger']['calls']} calls and {_usd(run['ledger']['spent_usd'])}"
        + (f"; **stopped: {run['ledger']['stopped']}**" if run["ledger"]["stopped"] else "."),
    ]
    if run["command"]:
        lines.append(f"- Command: `{run['command']}`")
    for note in run["notes"]:
        lines.append(f"- Note: {note}")
    lines += ["", f"_{run['wall_time_note']}._ _{run['overshoot_note']}._", ""]

    aggregate = report["aggregate"]
    lines += ["## Cost and speed per review", ""]
    rows = []
    for depth, agg in aggregate.items():
        rows.append(
            [
                depth,
                f"{agg['complete']}/{agg['prs']}",
                _n(agg["model_calls"]["mean"]),
                str(agg["zero_call_reviews"]),
                _tok(agg["input_tokens"]["mean"]),
                _tok(agg["output_tokens"]["mean"]),
                _tok(agg["context_tokens"]["mean"]),
                _usd(agg["provider_usd"]["mean"]),
                _usd(agg["provider_usd"]["median"]),
                _usd(agg["provider_usd"]["p90"]),
                _n(agg["wall_seconds"]["mean"]),
            ]
        )
    lines += _table(
        [
            "Depth",
            "Complete",
            "Calls",
            "No-call reviews",
            "Tokens in",
            "Tokens out",
            "of which context",
            "USD mean",
            "USD median",
            "USD p90",
            "Wall s",
        ],
        rows,
    )
    lines += [
        "",
        "Means over complete reviews. A review whose tokens or dollars the provider did not report is left out of those "
        "columns, and counted in the checklist below, never read as zero.",
        "",
        "## Findings, catch and noise",
        "",
    ]
    rows = []
    for depth, agg in aggregate.items():
        findings, catch, noise = agg["findings"], agg["catch"], agg["noise"]
        rows.append(
            [
                depth,
                f"{findings['p0']} / {findings['p1']} / {findings['p2']}",
                f"{catch['caught']}/{catch['expected']}",
                _interval(catch["rate"], catch["ci95"]),
                f"{noise['unmatched']}/{noise['findings']} ({noise['duplicates']} repeat a caught bug)",
                _interval(noise["rate"], noise["ci95"]),
                _usd(agg["cost_per_catch_usd"]),
            ]
        )
    lines += _table(
        [
            "Depth",
            "P0 / P1 / P2",
            "Caught",
            "Catch rate (95% interval)",
            "Unmatched",
            "Noise rate (95% interval)",
            "USD per catch",
        ],
        rows,
    )
    for depth, agg in aggregate.items():
        causes = agg["catch"]["missed_by_cause"]
        if agg["catch"]["expected"] and agg["catch"]["expected"] > agg["catch"]["caught"]:
            lines.append(
                f"- {depth}: {agg['catch']['expected'] - agg['catch']['caught']} labels missed: "
                f"{causes['no_risk_candidate']} where triage found no risk signal (no model was asked), "
                f"{causes['not_asked']} not asked about at this depth, "
                f"{causes['asked_not_reported']} asked about and not reported"
            )
    lines += [
        "",
        "Catch and noise are scored only on labelled pull requests, by the rules in the corpus (`match`). A finding "
        "catches a label by `location` (near a line the fix edited) or `symbol` (it names an identifier the fix edited); "
        "the per-pull-request tables say which. Noise is a finding that matches no label, so it is an upper bound on "
        "false positives wherever the labels are incomplete; some of it repeats a bug already caught (the same problem "
        "reported on two targets of one function).",
        "",
        "## Follow-up pushes",
        "",
    ]
    rows = []
    for depth, push in report["pushes"].items():
        carry, full = push.get("carry_forward"), push.get("full")
        if not push["pushes"]:
            rows.append([depth, "0", "n/a", "n/a", "n/a", "n/a", "n/a"])
            continue
        rows.append(
            [
                depth,
                f"{push['complete_pairs']}/{push['pushes']}",
                _n(carry["model_calls"]["mean"]),
                _n(full["model_calls"]["mean"]),
                _usd(carry["provider_usd"]["mean"]),
                _usd(full["provider_usd"]["mean"]),
                _pct(
                    push["saved"]["usd_fraction"]
                    if push["saved"]["usd_fraction"] is not None
                    else push["saved"]["calls_fraction"]
                ),
            ]
        )
    lines += _table(
        [
            "Depth",
            "Pushes",
            "Calls with carry-forward",
            "Calls re-reviewing all",
            "USD with carry-forward",
            "USD re-reviewing all",
            "Saved",
        ],
        rows,
    )
    unavailable = {k: v for push in report["pushes"].values() for k, v in push["unavailable"].items()}
    if unavailable:
        lines += [
            "",
            "Pushes that could not be built: " + "; ".join(f"{k} ({v})" for k, v in sorted(unavailable.items())),
        ]
    lines += [
        "",
        'A push is the fix commit applied on top of the reviewed head. "Re-reviewing all" is what a per-push reviewer '
        'spends; "with carry-forward" is what this reviewer spends when it reuses the first review.',
        "",
    ]

    baseline = report["baseline"]
    if baseline["prs_with_comments"]:
        lines += ["## Against the other reviewer", ""]
        lines.append(
            f"{baseline['prs_with_comments']} pull requests carry its comments "
            f"({baseline['inline_comments']} inline, {baseline['summary_comments']} summary; "
            f"{baseline['judged']['valid']} judged valid, {baseline['judged']['invalid']} invalid)."
        )
        lines.append("")
        rows = [
            [
                depth,
                str(row["agreed"]),
                str(row["ours_only"]),
                str(row["theirs_only"]),
                str(row["near_misses"]),
                _pct(row["share_of_theirs_we_also_raised"]),
            ]
            for depth, row in baseline["by_depth"].items()
        ]
        lines += _table(
            ["Depth", "Agreed", "Ours only", "Theirs only", "Near misses", "Of theirs, we also raised"], rows
        )
        base = baseline["against_labels"]
        if base["labelled_prs_with_comments"]:
            lines += [
                "",
                f"Its own catch against the labels, by the same rules: {base['caught']}/{base['expected']} "
                f"({_interval(base['rate'], base['ci95'])}), noise {base['unmatched']}/{base['comments']}.",
            ]
        if "usd_per_review_supplied" in baseline:
            supplied = baseline["usd_per_review_supplied"]
            lines += [
                "",
                f"Price per review you supplied: {_usd(supplied['value'])} ({supplied['label'] or 'unlabelled'}). "
                "It is your input, not something this run found out.",
            ]
        lines += [
            "",
            "Agreement is not accuracy: two reviewers can agree and both be wrong. Near misses are same-file, "
            "nearby comments whose text was too different to pair; settle them by hand.",
            "",
        ]

    team = report["reference_team"]
    lines += ["## Reference team (a projection)", ""]
    lines.append(
        f"{team['developers']} developers, {team['prs_per_month']} pull requests a month, {team['reviews_per_pr']} reviews each. "
        f"{team['basis']}."
    )
    lines.append("")
    rows = [
        [
            depth,
            _usd(row["first_review_usd"]),
            _usd(row["push_usd_carry_forward"]),
            _usd(row["push_usd_full"]),
            _usd(row["monthly_usd_with_carry_forward"]),
            _usd(row["monthly_usd_without"]),
        ]
        for depth, row in team["by_depth"].items()
    ]
    lines += _table(
        [
            "Depth",
            "First review",
            "Push (carry-forward)",
            "Push (re-review all)",
            "Month (carry-forward)",
            "Month (re-review all)",
        ],
        rows,
    )

    lines += ["", "## Is a number ready to publish?", ""]
    publication = report["publication"]
    for key, title in (
        ("cost_per_review", "Cost per review"),
        ("quality", "Catch and noise"),
        ("head_to_head", "Head to head with the other reviewer"),
    ):
        block = publication[key]
        lines.append(f"**{title}: {'ready' if block['ready'] else 'not ready'}.**")
        for reason in block["missing"]:
            lines.append(f"- {reason}")
        lines.append("")
    figures = publication["cost_per_review"]["figures"]
    if figures:
        lines += [
            "Figures a cost-per-review sentence would be built from (only when the cost block above is ready):",
            "",
        ]
        lines += _table(
            ["Depth", "Reviews", "Mean", "Median", "p90"],
            [
                [d, str(f["reviews"]), _usd(f["mean_usd"]), _usd(f["median_usd"]), _usd(f["p90_usd"])]
                for d, f in figures.items()
            ],
        )
        lines.append("")
    lines += ["Whatever is published:", ""] + [f"- {item}" for item in publication["always"]] + [""]

    lines += ["## Per pull request", ""]
    for pr in report["prs"]:
        title = f"{pr['id']}: {pr['title']}" if pr["title"] else pr["id"]
        lines += [f"### {title}", ""]
        lines.append(
            f"{pr['repository']}" + (f" [#{pr['pull']}]({pr['url']})" if pr["pull"] else "") + f"; "
            f"{'labelled' if pr['labelled'] else 'unlabelled'} ({len(pr['labels'])} labels)."
        )
        if pr["skipped"]:
            lines += ["", f"Skipped: {pr['skipped']}", ""]
            continue
        rows = []
        for depth, row in pr["depths"].items():
            labels = row.get("labels")
            sev = row["findings_by_severity"]
            rows.append(
                [
                    depth,
                    row["status"] + ("" if row["complete"] else " (incomplete)"),
                    str(row["model_calls"]),
                    (
                        _tok((row["input_tokens"] or 0) + (row["output_tokens"] or 0))
                        if row["input_tokens"] is not None
                        else "n/a"
                    ),
                    _usd(row["provider_usd"]),
                    _n(row["wall_seconds"]),
                    f"{sev['p0']} / {sev['p1']} / {sev['p2']}",
                    f"{labels['caught']}/{labels['expected']}" if labels else "n/a",
                    f"{labels['noise']}/{labels['findings']}" if labels else "n/a",
                ]
            )
        lines += [
            "",
            *_table(
                ["Depth", "Status", "Calls", "Tokens", "USD", "Wall s", "P0 / P1 / P2", "Caught", "Unmatched"], rows
            ),
        ]
        for depth, row in pr["depths"].items():
            labels = row.get("labels")
            if labels:
                for caught in labels["matches"]:
                    lines.append(
                        f"- {depth}: label `{caught['label']}` caught by finding `{caught['finding']}` ({caught['rule']})"
                    )
                causes = {item["label"]: item["cause"] for item in labels["missed_detail"]}
                for missed in labels["missed"]:
                    lines.append(f"- {depth}: label `{missed}` missed ({_CAUSE[causes[missed]]})")
            if row["errors"]:
                lines.append(f"- {depth}: errors: {'; '.join(row['errors'])[:300]}")
            if row["reason"] and not row["complete"]:
                lines.append(f"- {depth}: {row['reason']}")
        if pr["baseline"]["note"]:
            lines.append(f"- other reviewer: {pr['baseline']['note']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "BASIS",
    "MIN_COST_REVIEWS",
    "MIN_HEAD_TO_HEAD_PRS",
    "MIN_QUALITY_PRS",
    "MIN_REPOSITORIES",
    "build_report",
    "render_markdown",
]
