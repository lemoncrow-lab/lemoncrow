"""The ``benchmark_summary`` of a golden-score report: what ``make benchmark-publish`` merges into the published table.

``scripts/render_code_review_benchmark.py --from-run REPORT`` imports a run only if the report has a top-level
``benchmark_summary`` object holding the published block's fields (``evals/review/published/README.md``). This module builds
that object from a finished score report (:func:`golden_scoring.build_report`), or says in one sentence why it cannot.

A summary is written when, and only when, the scoring is publishable:

* every pull request of the corpus was scored, none was unscored and no review was partial;
* the judge was the fixed one, and the report carries the full digest of its prompt;
* for LemonCrow's own reviews: the benchmark was a live provider run at the product's default finding threshold, no
  pull request has an unknown cost, and the report names the review's model, route and depth and the commit that ran,
  on a checkout with no uncommitted changes to tracked files (a report that does not say is accepted);
* for another tool's comments: the person said which tool they are and where they came from
  (:class:`CompetitorRow`), because the code cannot know.

Nothing here estimates, defaults or fills a figure. A value the report does not hold keeps the summary out, and the reason
says which. The importer validates the result again (the dataset's pull-request count, the pinned answer key, the
rounding), so this module does not read the published file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.benchmark_report import BASIS

_SHA40: Final = re.compile(r"[0-9a-f]{40}")
_SHA64: Final = re.compile(r"[0-9a-f]{64}")
_SLUG: Final = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
#: The finding threshold the product posts at when nobody set one (:func:`golden_scoring.comments_from_findings`).
DEFAULT_THRESHOLD: Final = "default"
#: The command-line option that supplies each field of :class:`CompetitorRow`, for the messages.
COMPETITOR_FLAGS: Final[Mapping[str, str]] = {
    "tool_id": "--tool-id",
    "tool": "--tool-name",
    "description": "--comments-description",
    "url": "--comments-url",
    "collected_on": "--comments-collected-on",
}
#: How many pull request ids a reason names before it says "and N more".
_NAMED: Final = 4


@dataclass(frozen=True, slots=True)
class CompetitorRow:
    """What the code cannot know about another tool's comments: how the published file names the tool, and their source.

    ``tool_id`` and ``tool`` must be those of a ``third_party`` row of the published file (the importer checks).
    """

    tool_id: str = ""
    tool: str = ""
    description: str = ""
    url: str = ""
    collected_on: str = ""

    def given(self) -> bool:
        return any(getattr(self, name).strip() for name in COMPETITOR_FLAGS)

    def missing(self) -> list[str]:
        """The options not given."""

        return [flag for name, flag in COMPETITOR_FLAGS.items() if not getattr(self, name).strip()]

    def malformed(self) -> list[str]:
        """What is wrong with the fields that were given."""

        found: list[str] = []
        if self.tool_id.strip() and not _SLUG.fullmatch(self.tool_id.strip()):
            found.append(f"{COMPETITOR_FLAGS['tool_id']} must be a lowercase slug such as `some-tool`")
        if self.url.strip() and not (self.url.strip().startswith("https://") and " " not in self.url.strip()):
            found.append(f"{COMPETITOR_FLAGS['url']} must be an https URL")
        if self.collected_on.strip() and not _is_day(self.collected_on.strip()):
            found.append(f"{COMPETITOR_FLAGS['collected_on']} must be a date, YYYY-MM-DD")
        return found


@dataclass(frozen=True, slots=True)
class Outcome:
    """The summary, or ``None`` and every reason it cannot be written."""

    summary: dict[str, Any] | None
    reasons: tuple[str, ...] = ()

    @property
    def why_not(self) -> str:
        return "; ".join(self.reasons)


def build_benchmark_summary(
    report: Mapping[str, Any],
    *,
    engine_version: str,
    competitor: CompetitorRow | None = None,
    limited_to: Sequence[str] = (),
) -> Outcome:
    """The ``benchmark_summary`` for a finished score *report*, or the reasons there is none.

    *engine_version* is the LemonCrow package version doing the scoring. *competitor* is required for a report of another
    tool's comments. *limited_to* is the pull request ids the person narrowed the run to (``--pr``): a narrowed corpus is
    not the benchmark.
    """

    reviewer = _object(report.get("reviewer"))
    kind = reviewer.get("kind")
    if kind not in ("lemoncrow", "comments"):
        return Outcome(None, (f"the reviewer is of kind {kind!r}, which has no published block",))
    gaps: list[str] = []
    if limited_to:
        gaps.append(f"the run was limited to {len(limited_to)} pull request(s) with --pr")
    gaps += _coverage(report)
    scores = _scores(report, gaps)
    judge = _judge(report, gaps)
    day = _day(report, gaps)
    commit = _answer_key(report, gaps)
    extra: dict[str, Any]
    if kind == "lemoncrow":
        extra = {
            "review": _review(reviewer, gaps),
            "engine": _engine(reviewer, engine_version, gaps),
            "cost": _cost(report, gaps),
        }
    else:
        extra = _competitor(competitor, gaps)
    if gaps:
        return Outcome(None, tuple(gaps))
    summary = {
        "date": day,
        "prs_reviewed": report["scored_prs"],
        "answer_key_commit": commit,
        **scores,
        "judge": judge,
        **extra,
    }
    return Outcome(summary)


def attach(report: dict[str, Any], outcome: Outcome) -> None:
    """Put the summary into *report*, or, when there is none, the reason (``benchmark_summary_omitted``)."""

    if outcome.summary is not None:
        report["benchmark_summary"] = outcome.summary
    else:
        report["benchmark_summary_omitted"] = outcome.why_not


def publishing_line(report: Mapping[str, Any], *, run: str = "<this report>") -> str:
    """One line for the Markdown report and the terminal: whether the JSON can go into ``make benchmark-publish``.

    Empty for a report that went through no summary at all (one made before this existed, or by a library caller).
    """

    summary = report.get("benchmark_summary")
    if isinstance(summary, Mapping):
        rescored = f" RESCORED={summary['tool_id']}" if "tool_id" in summary else ""
        return f"Ready to publish: yes. This report carries a `benchmark_summary`; `make benchmark-publish RUN={run}{rescored}` merges it."
    reason = report.get("benchmark_summary_omitted")
    if isinstance(reason, str) and reason:
        return f"Ready to publish: no. No `benchmark_summary` was written because {reason}."
    return ""


# --------------------------------------------------------------------------- #
# the pieces
# --------------------------------------------------------------------------- #


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_day(value: str) -> bool:
    if not _DAY.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _named(ids: Sequence[object]) -> str:
    names = [str(item) for item in ids]
    shown = ", ".join(names[:_NAMED])
    return f"{shown} and {len(names) - _NAMED} more" if len(names) > _NAMED else shown


def _percent(numerator: int, denominator: int) -> float:
    """*numerator* over *denominator* as a percentage with two decimals, rounded half up."""

    return float((Decimal(100 * numerator) / Decimal(denominator)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _coverage(report: Mapping[str, Any]) -> list[str]:
    """Why the report does not cover every pull request of the corpus, completely."""

    found: list[str] = []
    corpus_prs, scored = _object(report.get("corpus")).get("prs"), report.get("scored_prs")
    unscored, partial = report.get("unscored"), report.get("partial_reviews")
    if not (_whole(corpus_prs) and _whole(scored) and isinstance(unscored, list) and isinstance(partial, list)):
        return ["the report does not say how many pull requests it covers"]
    if unscored:
        ids = [_object(item).get("id", "?") for item in unscored]
        found.append(f"{len(unscored)} of the corpus's {corpus_prs} pull requests could not be scored ({_named(ids)})")
    elif scored != corpus_prs:
        found.append(f"only {scored} of the corpus's {corpus_prs} pull requests were scored")
    if partial:
        found.append(f"{len(partial)} review(s) were partial ({_named(partial)})")
    return found


def _scores(report: Mapping[str, Any], gaps: list[str]) -> dict[str, Any]:
    """Precision, recall, F and the counts they follow from, all from the strict counts (neutral comments are out)."""

    overall = _object(report.get("overall"))
    strict = _object(overall.get("strict"))
    tp, fp, fn, counted = strict.get("tp"), strict.get("fp"), strict.get("fn"), overall.get("golden_counted")
    if not all(_whole(n) for n in (tp, fp, fn, counted)):
        gaps.append("the report has no strict counts")
        return {}
    assert isinstance(tp, int) and isinstance(fp, int) and isinstance(fn, int) and isinstance(counted, int)
    if counted != tp + fn:
        gaps.append(f"the report's {counted} counted golden comments are not its {tp} caught plus {fn} missed")
        return {}
    if tp + fp == 0:
        gaps.append("no reviewer comment was scored, so precision is undefined")
        return {}
    if counted == 0:
        gaps.append("no golden comment was counted, so recall is undefined")
        return {}
    matched = counted - fn
    return {
        "precision": _percent(tp, tp + fp),
        "recall": _percent(matched, counted),
        "f_score": _percent(2 * tp, 2 * tp + fp + fn),
        "counts": {
            "tool_comments_scored": tp + fp,
            "tool_comments_matched": tp,
            "golden_scored": counted,
            "golden_matched": matched,
        },
    }


def _judge(report: Mapping[str, Any], gaps: list[str]) -> dict[str, str]:
    judge = _object(report.get("judge"))
    driver, model = judge.get("driver"), judge.get("model")
    digest, short = judge.get("prompt_sha256"), judge.get("prompt_digest")
    if not (_text(driver) and _text(model)) or (driver, model) != (judge.get("driver_fixed"), judge.get("model_fixed")):
        gaps.append(f"the judge ({driver}/{model}) was not the fixed judge, so the scores are not comparable")
    if not (isinstance(digest, str) and _SHA64.fullmatch(digest)):
        gaps.append(
            "the report does not carry the full sha256 of the judge prompt (score again; the judge's answers are cached)"
        )
    elif isinstance(short, str) and short and not digest.startswith(short):
        gaps.append("the judge prompt's sha256 does not match the report's own short digest of it")
    return {"model": str(model), "prompt_sha256": str(digest)}


def _day(report: Mapping[str, Any], gaps: list[str]) -> str:
    stamp = report.get("generated_at")
    day = stamp[:10] if isinstance(stamp, str) else ""
    if not _is_day(day):
        gaps.append("the report has no generated_at date")
    return day


def _answer_key(report: Mapping[str, Any], gaps: list[str]) -> str:
    commit = _object(report.get("key")).get("commit")
    if not (isinstance(commit, str) and _SHA40.fullmatch(commit)):
        gaps.append("the report does not pin the answer key to a 40-hex commit")
    return str(commit)


def _review(reviewer: Mapping[str, Any], gaps: list[str]) -> dict[str, str]:
    """The review that made the comments, and whether it was the kind of run a published figure may come from."""

    basis, threshold = reviewer.get("benchmark_basis"), reviewer.get("finding_threshold")
    if basis != BASIS["live"]:
        gaps.append(f"the reviews were not a live provider run ({basis or 'the report does not say what they were'})")
    if threshold != DEFAULT_THRESHOLD:
        gaps.append(f"the comments were counted at finding threshold {threshold!r}, not the product's default")
    model, route, depth = reviewer.get("benchmark_model"), reviewer.get("benchmark_driver"), reviewer.get("depth")
    if not all(_text(item) for item in (model, route, depth)):
        gaps.append("the report does not name the review's model, route and depth (score again with this version)")
    return {"model": str(model), "route": str(route), "depth": str(depth)}


def _engine(reviewer: Mapping[str, Any], version: str, gaps: list[str]) -> dict[str, str]:
    code = _object(reviewer.get("benchmark_code"))
    commit = code.get("git_head")
    if not (isinstance(commit, str) and _SHA40.fullmatch(commit)):
        gaps.append("the benchmark report did not record the 40-hex git commit it ran on")
    if code.get("dirty") is True:
        gaps.append(
            "the benchmark ran on a checkout with uncommitted changes to tracked files, so its commit is not the code that ran"
        )
    if not version.strip():
        gaps.append("the engine version is unknown")
    return {"version": version.strip(), "commit": str(commit)}


def _cost(report: Mapping[str, Any], gaps: list[str]) -> dict[str, float | int]:
    """What the reviews cost, from the report's cost block. An unknown cost is not zero: it keeps the summary out."""

    cost = report.get("cost")
    if not isinstance(cost, Mapping):
        gaps.append("the report has no cost of the reviews")
        return {}
    usd, calls = _object(cost.get("usd")), _object(cost.get("model_calls"))
    scored = report.get("scored_prs")
    unknown = {name: block.get("unknown_prs") for name, block in (("dollar", usd), ("model-call", calls))}
    if cost.get("prs") != scored or any(n != 0 for n in unknown.values()):
        gaps.append("the cost of some reviews is unknown (a missing cost is not zero)")
        return {}
    figures = (usd.get("median"), usd.get("total"), calls.get("median"), calls.get("total"))
    if not all(_number(n) for n in figures):
        gaps.append("the report's cost figures are not all numbers")
        return {}
    median_usd, total_usd, median_calls, total_calls = figures
    assert isinstance(median_usd, int | float) and isinstance(total_usd, int | float)
    assert isinstance(median_calls, int | float) and isinstance(total_calls, int | float)
    return {
        "median_usd_per_pr": median_usd,
        "total_usd": total_usd,
        "median_calls_per_pr": median_calls,
        "total_calls": int(total_calls) if float(total_calls).is_integer() else total_calls,
    }


def _competitor(row: CompetitorRow | None, gaps: list[str]) -> dict[str, Any]:
    """The other tool's identity and where its comments came from: the person's words, never inferred."""

    if row is None or not row.given():
        gaps.append(
            "another tool's row needs its name, id and the source of its comments, which the code cannot know "
            f"(options {', '.join(COMPETITOR_FLAGS.values())})"
        )
        return {}
    if missing := row.missing():
        gaps.append(f"another tool's row is missing {', '.join(missing)}")
    gaps.extend(row.malformed())
    return {
        "tool_id": row.tool_id.strip(),
        "tool": row.tool.strip(),
        "comments_source": {
            "description": row.description.strip(),
            "url": row.url.strip(),
            "collected_on": row.collected_on.strip(),
        },
    }


__all__ = [
    "COMPETITOR_FLAGS",
    "CompetitorRow",
    "Outcome",
    "attach",
    "build_benchmark_summary",
    "publishing_line",
]
