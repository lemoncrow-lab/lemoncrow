"""Score a reviewer's posted comments against an answer key's issues, and report precision, recall and F per language.

The scoring rule is the answer key's own. For each pull request a judge says which reviewer comments and which golden
comments refer to the same underlying issue (:mod:`golden_judge`). Then:

* a **true positive** is a reviewer comment assigned to a golden comment, one-to-one;
* a **false positive** is a reviewer comment that matches no golden comment (or only one already taken: the same issue
  said twice is noise in the strict count, and a true positive in the ``lenient`` count that is reported beside it);
* a **false negative** is a golden comment no reviewer comment was assigned to;
* a golden comment of severity ``Low`` is **excluded**: never a false negative, and a reviewer comment that matches it
  (and no counted golden comment) is neither a true nor a false positive;
* precision = TP / (TP + FP), recall = TP / (TP + FN), F = their harmonic mean. Overall and per group the counts are
  summed over pull requests first (micro-average).

Which comments count as "posted" for LemonCrow is the product's own rule: a finding the bot would show a user, an inline
comment for P0 and P1 and a line in the summary's collapsed list for P2 (:class:`NoisePolicy` decides; a finding it
holds back is not posted). Another reviewer's comments are read from files in the shape :func:`load_reviewer_comments`
documents, so both are scored by the same code and the same judge.

Nothing here calls a model except through the :class:`~golden_judge.Judge` it is given.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.golden_comments import (
    GOLDEN_SEVERITIES,
    GoldenComment,
    GoldenCorpus,
    GoldenKey,
)
from lemoncrow.pro.capabilities.review.evaluation.golden_judge import (
    JUDGE_DRIVER,
    JUDGE_MODEL,
    Judge,
    JudgeError,
    JudgeRequest,
    JudgeUsage,
    JudgeVerdict,
)

SCHEMA: Final[int] = 1
KIND: Final[str] = "golden-comment-score"
#: Statuses of a benchmark run that produced a review a user would have seen.
_REVIEWED: Final[frozenset[str]] = frozenset({"ran", "nothing_changed"})


# --------------------------------------------------------------------------- #
# the reviewer's side
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PostedComment:
    """One comment a reviewer posted on a pull request, as the judge reads it."""

    id: str
    text: str
    path: str = ""
    line: int = 0
    severity: str = ""

    def judge_text(self) -> str:
        where = ""
        if self.path:
            where = f"[{self.path}:{self.line}] " if self.line > 0 else f"[{self.path}] "
        return where + self.text.strip()

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "line": self.line, "severity": self.severity, "text": self.text}


@dataclass(frozen=True, slots=True)
class PrUsage:
    """What one review cost, as the benchmark recorded it. ``None`` is unknown, never zero."""

    model_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usd: float | None = None
    wall_seconds: float | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usd": self.usd,
            "wall_seconds": self.wall_seconds,
        }


@dataclass(frozen=True, slots=True)
class Submission:
    """A reviewer's output on one pull request. ``reviewed`` is False when there was no review to score."""

    comments: tuple[PostedComment, ...] = ()
    reviewed: bool = True
    complete: bool = True
    note: str = ""
    usage: PrUsage | None = None
    held_back: int = 0


def comments_from_findings(
    rows: Sequence[Mapping[str, Any]], *, threshold: str | None = None
) -> tuple[tuple[PostedComment, ...], int]:
    """The comments the bot would post for a review's findings, and how many it would hold back.

    ``rows`` are the ``findings`` of a benchmark report's run. A P0 or P1 is an inline comment, a P2 is a line in the
    summary's list; one below the repository's finding threshold (never a P0) is held back and shows the user nothing.
    *threshold* is that setting (``critical``, ``high``, ``medium``, ``suggestions``); ``None`` is the product's default.
    """

    from lemoncrow.pro.capabilities.review.pr_bot.settings import NoisePolicy

    policy = NoisePolicy() if threshold is None else NoisePolicy(threshold=threshold)
    posted: list[PostedComment] = []
    held = 0
    for row in rows:
        severity = str(row.get("severity") or "")
        if policy.tier(severity) == "held":
            held += 1
            continue
        parts = [str(row.get(key) or "").strip() for key in ("title", "explanation", "consequence")]
        lines = row.get("lines")
        line = int(lines[0]) if isinstance(lines, list) and lines and isinstance(lines[0], int) else 0
        posted.append(
            PostedComment(
                id=str(row.get("id") or f"f{len(posted) + 1}"),
                text="\n".join(part for part in parts if part),
                path=str(row.get("path") or ""),
                line=line,
                severity=severity,
            )
        )
    return tuple(posted), held


def submissions_from_benchmark_report(
    report: Mapping[str, Any], depth: str, pr_ids: Sequence[str], *, threshold: str | None = None
) -> dict[str, Submission]:
    """One :class:`Submission` per pull request of a benchmark report, at *depth*. A missing review is unreviewed, not empty."""

    if report.get("kind") != "review-benchmark":
        raise ValueError("not a review-benchmark report")
    by_id = {str(item.get("id")): item for item in report.get("prs", ())}
    out: dict[str, Submission] = {}
    for pr_id in pr_ids:
        item = by_id.get(pr_id)
        if item is None:
            out[pr_id] = Submission(reviewed=False, note="not in the report")
            continue
        run = (item.get("depths") or {}).get(depth)
        if run is None:
            out[pr_id] = Submission(reviewed=False, note=str(item.get("skipped") or f"no {depth} review in the report"))
            continue
        if run.get("status") not in _REVIEWED:
            reason = str(run.get("reason") or "; ".join(run.get("errors") or ()) or run.get("status"))
            out[pr_id] = Submission(reviewed=False, note=f"the review did not run: {reason}"[:300])
            continue
        comments, held = comments_from_findings(run.get("findings") or (), threshold=threshold)
        out[pr_id] = Submission(
            comments=comments,
            complete=bool(run.get("complete")),
            note=str(run.get("reason") or "") if not run.get("complete") else "",
            usage=PrUsage(
                model_calls=run.get("model_calls"),
                input_tokens=run.get("input_tokens"),
                output_tokens=run.get("output_tokens"),
                usd=run.get("provider_usd"),
                wall_seconds=round(float(run.get("wall_seconds") or 0.0) + float(run.get("capture_seconds") or 0.0), 3),
            ),
            held_back=held,
        )
    return out


def load_reviewer_comments(directory: Path, pr_ids: Sequence[str]) -> dict[str, Submission]:
    """Another reviewer's comments, one file per pull request: ``<id>.json`` with ``{"comments": [{"id", "path", "lines", "text"}]}``.

    A file that is missing makes that pull request unreviewed. ``lines`` is a free-text range ("12-14", "+5 to +9", "");
    the first number in it, if any, is the line.
    """

    import re

    out: dict[str, Submission] = {}
    for pr_id in pr_ids:
        path = directory / f"{pr_id}.json"
        if not path.is_file():
            out[pr_id] = Submission(reviewed=False, note=f"no comments file {path.name}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        comments: list[PostedComment] = []
        for item in data.get("comments") or ():
            found = re.search(r"\d+", str(item.get("lines") or ""))
            comments.append(
                PostedComment(
                    id=str(item.get("id") or f"c{len(comments) + 1}"),
                    text=str(item.get("text") or ""),
                    path=str(item.get("path") or ""),
                    line=int(found.group()) if found else 0,
                )
            )
        extraction = data.get("extraction") or {}
        note = ""
        stated = extraction.get("stated_comments")
        if extraction.get("page_truncated") and isinstance(stated, int) and stated > len(comments):
            note = f"the page was truncated: {len(comments)} of {stated} comments could be read"
        out[pr_id] = Submission(comments=tuple(comments), note=note)
    return out


# --------------------------------------------------------------------------- #
# one pull request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CommentVerdict:
    id: str
    outcome: str  # tp | fp | neutral
    golden: str = ""  # the golden comment it was assigned to
    duplicate_of: str = ""  # a taken golden comment it also matches (a strict false positive, a lenient true positive)
    candidates: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "outcome": self.outcome,
            "golden": self.golden,
            "duplicate_of": self.duplicate_of,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True, slots=True)
class GoldenOutcome:
    id: str
    severity: str
    status: str  # caught | missed | excluded
    by: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity, "status": self.status, "by": self.by}


@dataclass(frozen=True, slots=True)
class Resolution:
    """The one-to-one assignment and the counts it gives."""

    comments: tuple[CommentVerdict, ...]
    goldens: tuple[GoldenOutcome, ...]
    tp: int
    fp: int
    fn: int
    neutral_comments: int
    neutral_goldens: int
    lenient_tp: int  # tp plus the comments that repeat an already-caught issue

    @property
    def lenient_fp(self) -> int:
        return self.fp - (self.lenient_tp - self.tp)


def resolve(
    pairs: Sequence[tuple[str, str, str]], comment_ids: Sequence[str], goldens: Sequence[GoldenComment]
) -> Resolution:
    """Turn the judge's candidate pairs into a one-to-one assignment and the counts.

    Only counted golden comments can be assigned. The assignment is a maximum bipartite matching, deterministic: reviewer
    comments in the order given, each trying the most severe golden comment first. A reviewer comment that matches two
    golden comments is therefore assigned to at most one; the other stays missed unless another comment catches it.
    """

    by_golden = {golden.id: golden for golden in goldens}
    candidates: dict[str, list[GoldenComment]] = {cid: [] for cid in comment_ids}
    for comment, golden_id, _reason in pairs:
        golden = by_golden.get(golden_id)
        if comment in candidates and golden is not None and golden not in candidates[comment]:
            candidates[comment].append(golden)
    order = {golden.id: index for index, golden in enumerate(goldens)}
    counted = {
        cid: sorted((g for g in found if g.counted), key=lambda g: (g.rank, order[g.id]))
        for cid, found in candidates.items()
    }

    owner: dict[str, str] = {}  # golden id -> the comment holding it

    def assign(cid: str, seen: set[str]) -> bool:
        for golden in counted[cid]:
            if golden.id in seen:
                continue
            seen.add(golden.id)
            if golden.id not in owner or assign(owner[golden.id], seen):
                owner[golden.id] = cid
                return True
        return False

    for cid in comment_ids:
        assign(cid, set())
    held = {cid: gid for gid, cid in owner.items()}

    verdicts: list[CommentVerdict] = []
    duplicates = 0
    for cid in comment_ids:
        found = candidates[cid]
        names = tuple(g.id for g in found)
        if cid in held:
            verdicts.append(CommentVerdict(cid, "tp", golden=held[cid], candidates=names))
        elif any(not g.counted for g in found):
            verdicts.append(CommentVerdict(cid, "neutral", candidates=names))
        elif counted[cid]:
            duplicates += 1
            verdicts.append(CommentVerdict(cid, "fp", duplicate_of=counted[cid][0].id, candidates=names))
        else:
            verdicts.append(CommentVerdict(cid, "fp", candidates=names))
    outcomes = tuple(
        (
            GoldenOutcome(g.id, g.severity, "excluded", "")
            if not g.counted
            else (
                GoldenOutcome(g.id, g.severity, "caught", owner[g.id])
                if g.id in owner
                else GoldenOutcome(g.id, g.severity, "missed")
            )
        )
        for g in goldens
    )
    tp = len(owner)
    return Resolution(
        comments=tuple(verdicts),
        goldens=outcomes,
        tp=tp,
        fp=sum(1 for v in verdicts if v.outcome == "fp"),
        fn=sum(1 for o in outcomes if o.status == "missed"),
        neutral_comments=sum(1 for v in verdicts if v.outcome == "neutral"),
        neutral_goldens=sum(1 for o in outcomes if o.status == "excluded"),
        lenient_tp=tp + duplicates,
    )


@dataclass(frozen=True, slots=True)
class PrScore:
    """One pull request's score. ``error`` set means it was not scored and is in no total."""

    pr_id: str
    repository: str
    language: str
    comments: tuple[PostedComment, ...] = ()
    golden: tuple[GoldenComment, ...] = ()
    resolution: Resolution | None = None
    pairs: tuple[tuple[str, str, str], ...] = ()
    judge: JudgeVerdict | None = None
    submission: Submission = field(default_factory=Submission)
    error: str = ""

    def to_wire(self) -> dict[str, Any]:
        res = self.resolution
        wire: dict[str, Any] = {
            "id": self.pr_id,
            "repository": self.repository,
            "language": self.language,
            "scored": res is not None,
            "error": self.error,
            "note": self.submission.note,
            "complete": self.submission.complete,
            "comments_posted": len(self.comments),
            "held_back": self.submission.held_back,
            "golden": [{"id": g.id, "severity": g.severity, "counted": g.counted} for g in self.golden],
            "usage": self.submission.usage.to_wire() if self.submission.usage is not None else None,
        }
        if res is not None:
            wire.update(
                tp=res.tp,
                fp=res.fp,
                fn=res.fn,
                neutral_comments=res.neutral_comments,
                neutral_goldens=res.neutral_goldens,
                lenient_tp=res.lenient_tp,
                comment_verdicts=[
                    {**verdict.to_wire(), "text": comment.text[:600], "path": comment.path, "line": comment.line}
                    for verdict, comment in zip(res.comments, self.comments, strict=True)
                ],
                golden_outcomes=[outcome.to_wire() for outcome in res.goldens],
                judge_pairs=[{"comment": c, "golden": g, "reason": reason} for c, g, reason in self.pairs],
                judge={
                    "digest": self.judge.digest if self.judge else "",
                    "cached": self.judge.cached if self.judge else None,
                    "ignored_pairs": self.judge.ignored if self.judge else 0,
                    "usage_now": self.judge.usage.to_wire() if self.judge else JudgeUsage().to_wire(),
                },
            )
        return wire


def score_pr(
    pr_id: str,
    repository: str,
    language: str,
    submission: Submission,
    golden: Sequence[GoldenComment],
    judge: Judge,
) -> PrScore:
    """Score one pull request. A pull request with no review, or whose judge failed, is returned unscored with the reason."""

    comments = submission.comments
    golden = tuple(golden)

    def made(**fields: Any) -> PrScore:
        return PrScore(pr_id, repository, language, comments, golden, submission=submission, **fields)

    if not submission.reviewed:
        return made(error=submission.note or "no review to score")
    if not comments:
        return made(resolution=resolve((), (), golden))
    request = JudgeRequest(
        tuple(comment.judge_text() for comment in comments), tuple((g.text, g.severity) for g in golden)
    )
    # The judge names golden comments by position, and the key numbers them by position: they must be the same names.
    if any(g.id != request.golden_id(index) for index, g in enumerate(golden)):
        raise ValueError("golden comments must be numbered G1.. in the order they are given")
    try:
        verdict = judge(request)
    except JudgeError as exc:
        return made(error=f"judge failed: {exc}")
    ids = [request.comment_id(index) for index in range(len(comments))]
    return made(resolution=resolve(verdict.pairs, ids, golden), pairs=verdict.pairs, judge=verdict)


# --------------------------------------------------------------------------- #
# totals
# --------------------------------------------------------------------------- #


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def metrics(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f_score = _ratio(2 * tp, 2 * tp + fp + fn)  # the harmonic mean of the two, without rounding either first
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f": f_score}


def _totals(scores: Sequence[PrScore]) -> dict[str, Any]:
    scored = [s.resolution for s in scores if s.resolution is not None]
    tp, fp, fn = sum(r.tp for r in scored), sum(r.fp for r in scored), sum(r.fn for r in scored)
    lenient_tp = sum(r.lenient_tp for r in scored)
    lenient_fp = sum(r.lenient_fp for r in scored)
    return {
        "prs": len(scored),
        "strict": metrics(tp, fp, fn),
        "lenient": metrics(lenient_tp, lenient_fp, fn),
        "comments_posted": sum(len(s.comments) for s in scores if s.resolution is not None),
        "neutral_comments": sum(r.neutral_comments for r in scored),
        "golden_counted": tp + fn,
        "golden_excluded": sum(r.neutral_goldens for r in scored),
        "prs_with_a_catch": sum(1 for r in scored if r.tp > 0),
    }


def _by(scores: Sequence[PrScore], attribute: str) -> dict[str, Any]:
    keys = sorted({getattr(s, attribute) for s in scores if s.resolution is not None})
    return {key: _totals([s for s in scores if getattr(s, attribute) == key]) for key in keys}


def _by_severity(scores: Sequence[PrScore]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for severity in GOLDEN_SEVERITIES:
        if severity == "Low":
            continue
        outcomes = [
            o for s in scores if s.resolution is not None for o in s.resolution.goldens if o.severity == severity
        ]
        caught = sum(1 for o in outcomes if o.status == "caught")
        out[severity] = {"golden": len(outcomes), "caught": caught, "recall": _ratio(caught, len(outcomes))}
    return out


def _sum_known(values: Sequence[int | float | None]) -> tuple[float | None, int]:
    """The sum over the values that are known, or ``None`` if none are, and how many were unknown."""

    known = [v for v in values if v is not None]
    return (round(sum(known), 6) if known else None), len(values) - len(known)


def _cost(scores: Sequence[PrScore]) -> dict[str, Any] | None:
    usages = [s.submission.usage for s in scores if s.submission.usage is not None and s.resolution is not None]
    if not usages:
        return None
    out: dict[str, Any] = {"prs": len(usages)}
    for key in ("model_calls", "input_tokens", "output_tokens", "usd", "wall_seconds"):
        total, unknown = _sum_known([getattr(u, key) for u in usages])
        known = [getattr(u, key) for u in usages if getattr(u, key) is not None]
        out[key] = {
            "total": total,
            "mean": round(statistics.fmean(known), 4) if known else None,
            "median": round(statistics.median(known), 4) if known else None,
            "unknown_prs": unknown,
        }
    return out


def _judge_summary(scores: Sequence[PrScore], judge: Judge) -> dict[str, Any]:
    verdicts = [s.judge for s in scores if s.judge is not None]
    spent = [v.usage for v in verdicts]
    models = sorted({m.strip() for v in verdicts for m in v.model_reported.split(",") if m.strip()})
    total_usd, usd_unknown = _sum_known([u.usd for u in spent if u.calls])
    return {
        **dict(judge.identity),
        "model_reported": models,
        "questions": len(verdicts),
        "answered_from_cache": sum(1 for v in verdicts if v.cached),
        "asked_now": sum(1 for v in verdicts if not v.cached),
        "ignored_pairs": sum(v.ignored for v in verdicts),
        "cost_now": {
            "calls": sum(u.calls for u in spent),
            "input_tokens": _sum_known([u.input_tokens for u in spent if u.calls])[0],
            "output_tokens": _sum_known([u.output_tokens for u in spent if u.calls])[0],
            "usd": total_usd,
            "usd_unknown_calls": usd_unknown,
            "seconds": round(sum(u.seconds for u in spent), 3),
        },
    }


def build_report(
    golden: GoldenCorpus,
    scores: Sequence[PrScore],
    judge: Judge,
    *,
    name: str,
    reviewer: Mapping[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    scored = [s for s in scores if s.resolution is not None]
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "name": name,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "reviewer": dict(reviewer),
        "rule": (
            "A reviewer comment is a true positive when the judge says it names the same underlying issue as a golden "
            "comment and the one-to-one assignment gives it that comment; a false positive when it matches none (strict: "
            "or only one another comment already holds); a golden comment nobody holds is a false negative. Golden "
            "comments of severity Low are excluded from all three. Counts are summed over pull requests."
        ),
        "key": {
            "repository": golden.source.repository,
            "commit": golden.source.commit,
            "licence": golden.source.licence,
        },
        "judge": {**_judge_summary(scores, judge), "driver_fixed": JUDGE_DRIVER, "model_fixed": JUDGE_MODEL},
        "corpus": {"id": golden.corpus.id, "digest": golden.corpus.digest(), "prs": len(golden.corpus.prs)},
        "scored_prs": len(scored),
        "unscored": [{"id": s.pr_id, "reason": s.error} for s in scores if s.resolution is None],
        "partial_reviews": [s.pr_id for s in scored if not s.submission.complete],
        "overall": _totals(scores),
        "by_language": _by(scores, "language"),
        "by_repository": _by(scores, "repository"),
        "recall_by_severity": _by_severity(scores),
        "cost": _cost(scores),
        "prs": [s.to_wire() for s in scores],
    }


# --------------------------------------------------------------------------- #
# the markdown
# --------------------------------------------------------------------------- #


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{round(value * 100)}"


def _row(label: str, totals: Mapping[str, Any]) -> str:
    s, lenient = totals["strict"], totals["lenient"]
    return (
        f"| {label} | {totals['prs']} | {s['tp']} | {s['fp']} | {s['fn']} | {_pct(s['precision'])} | {_pct(s['recall'])} | "
        f"{_pct(s['f'])} | {_pct(lenient['precision'])} / {_pct(lenient['f'])} |"
    )


def _usd(value: Any) -> str:
    return "n/a" if value is None else f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _num(value: Any, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    reviewer, judge, key = report["reviewer"], report["judge"], report["key"]
    overall = report["overall"]
    lines = [
        f"# Golden-comment score: {reviewer.get('label', report['name'])}",
        "",
        f"Report `{report['name']}`, {report['generated_at']}. Scored {report['scored_prs']} of {report['corpus']['prs']} pull "
        f"requests of corpus `{report['corpus']['id']}`.",
        "",
        f"* Reviewer: {reviewer.get('description', '')}",
        f"* Answer key: {key['repository']} at `{key['commit'][:12]}` (read at run time, not stored here).",
        f"* Judge: `{judge['driver']}` `{judge['model']}` (reported by the CLI: {', '.join(judge['model_reported']) or 'n/a'}), prompt "
        f"`{judge['prompt_version']}` / `{judge['prompt_digest']}`. {judge['questions']} questions: "
        f"{judge['answered_from_cache']} answered from the cache, {judge['asked_now']} asked now "
        f"({judge['cost_now']['calls']} calls, {_usd(judge['cost_now']['usd'])}). {judge['ignored_pairs']} judge pairs named an unknown id and were ignored.",
        f"* Rule: {report['rule']}",
        "",
        "Precision, recall and F are percentages. `lenient` counts a comment that repeats an issue another comment already "
        "caught as a true positive instead of a false positive.",
        "",
        "| Group | PRs | TP | FP | FN | Precision | Recall | F | Lenient P / F |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        _row("**All**", overall),
    ]
    for label, group in (("language", report["by_language"]), ("repository", report["by_repository"])):
        for name, totals in group.items():
            lines.append(_row(f"{label}: {name}", totals))
    lines += [
        "",
        f"Comments posted {overall['comments_posted']}; matched only to an excluded (Low) golden comment {overall['neutral_comments']}; "
        f"golden comments counted {overall['golden_counted']}, excluded {overall['golden_excluded']}; pull requests with at least one catch "
        f"{overall['prs_with_a_catch']} of {overall['prs']}.",
        "",
        "Recall by golden severity: "
        + "; ".join(
            f"{sev} {item['caught']}/{item['golden']} ({_pct(item['recall'])}%)"
            for sev, item in report["recall_by_severity"].items()
        )
        + ".",
    ]
    if report["unscored"]:
        lines += ["", "Not scored (in no total):", ""]
        lines += [f"* `{item['id']}`: {item['reason']}" for item in report["unscored"]]
    if report["partial_reviews"]:
        lines += [
            "",
            "Scored although the review was partial: " + ", ".join(f"`{p}`" for p in report["partial_reviews"]) + ".",
        ]
    cost = report.get("cost")
    if cost:
        lines += [
            "",
            f"## Cost of the reviews ({cost['prs']} pull requests)",
            "",
            "| | Total | Mean per PR | Median | PRs unknown |",
            "| --- | --- | --- | --- | --- |",
        ]
        labels = {
            "model_calls": "Model calls",
            "input_tokens": "Input tokens",
            "output_tokens": "Output tokens",
            "usd": "Dollars (the CLI's list-price estimate)",
            "wall_seconds": "Wall seconds",
        }
        for keyname, label in labels.items():
            item = cost[keyname]
            fmt = _usd if keyname == "usd" else _num
            lines.append(
                f"| {label} | {fmt(item['total'])} | {fmt(item['mean'])} | {fmt(item['median'])} | {item['unknown_prs']} |"
            )
    lines += [
        "",
        "## Per pull request",
        "",
        "| PR | Language | Comments | TP | FP | FN | Excl. | Calls | Tokens in/out | USD | Wall s | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report["prs"]:
        usage = item.get("usage") or {}
        tokens = f"{_num(usage.get('input_tokens'))}/{_num(usage.get('output_tokens'))}" if usage else "n/a"
        if item["scored"]:
            lines.append(
                f"| {item['id']} | {item['language']} | {item['comments_posted']} | {item['tp']} | {item['fp']} | {item['fn']} | "
                f"{item['neutral_comments']}+{item['neutral_goldens']} | {_num(usage.get('model_calls'))} | {tokens} | "
                f"{_usd(usage.get('usd'))} | {_num(usage.get('wall_seconds'))} | {item['note']} |"
            )
        else:
            lines.append(f"| {item['id']} | {item['language']} | | | | | | | | | | not scored: {item['error']} |")
    lines += [
        "",
        "`Excl.` is comments matched only to a Low golden comment + Low golden comments, both outside the totals.",
        "",
    ]
    return "\n".join(lines)


def score_corpus(
    golden: GoldenCorpus,
    key: GoldenKey,
    submissions: Mapping[str, Submission],
    judge: Judge,
    *,
    pr_ids: Sequence[str] = (),
) -> list[PrScore]:
    """Score every pull request of the corpus (or just *pr_ids*) with *judge*, in corpus order."""

    scores: list[PrScore] = []
    for pr in golden.corpus.prs:
        if pr_ids and pr.id not in pr_ids:
            continue
        ref = golden.ref(pr.id)
        submission = submissions.get(pr.id, Submission(reviewed=False, note="no submission"))
        scores.append(score_pr(pr.id, pr.repository, ref.language, submission, key.comments(ref), judge))
    return scores


__all__ = [
    "KIND",
    "SCHEMA",
    "CommentVerdict",
    "GoldenOutcome",
    "PostedComment",
    "PrScore",
    "PrUsage",
    "Resolution",
    "Submission",
    "build_report",
    "comments_from_findings",
    "load_reviewer_comments",
    "metrics",
    "render_markdown",
    "resolve",
    "score_corpus",
    "score_pr",
    "submissions_from_benchmark_report",
]
