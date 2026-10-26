"""``lc dev eval review golden-score``: score a reviewer's posted comments against an answer key's issues.

Two sources of comments, one scorer and one judge::

    # LemonCrow: score a benchmark report (the review itself was run by `lc dev eval review benchmark`)
    uv run lemoncrow dev eval review golden-score --corpus evals/review/golden_comment_prs.yaml \\
        --report ~/lc-bench-reports/<name>.json --out-dir ~/lc-bench-reports

    # Another reviewer: its comments, one <pull request id>.json file each, are scored the same way
    uv run lemoncrow dev eval review golden-score --corpus evals/review/golden_comment_prs.yaml \\
        --reviewer-comments ~/bench-data/comments --reviewer-label "another reviewer" --out-dir ~/lc-bench-reports

The judge is fixed (the claude CLI's ``sonnet``, one prompt) and its answers are cached by what it was asked, so a second
run over the same comments is free. ``--plan`` prints how many judge calls a run would make and calls none. The answer key
is read from a checkout pinned by the corpus (``--golden-dir``, or cloned at run time) and is never copied into this
repository. See ``docs/reference/review-benchmark.md``.

When the scoring is publishable the JSON report also carries a ``benchmark_summary`` that ``make benchmark-publish RUN=...``
imports (``evals/review/published/README.md``); otherwise the report says why not. A report of another reviewer's comments
needs the ``--tool-id``, ``--tool-name`` and ``--comments-*`` options to become a row of the published table::

    uv run lemoncrow dev eval review golden-score --corpus evals/review/golden_comment_prs.yaml \\
        --reviewer-comments ~/bench-data/comments --out-dir ~/lc-bench-reports \\
        --tool-id some-tool --tool-name "Some Tool" --comments-description "its posted comments, read on the PR pages" \\
        --comments-url https://example.com/where-they-are --comments-collected-on 2031-01-02
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

EXIT_UNSCORED = 3


@click.command("golden-score")
@click.option(
    "--corpus",
    "corpus_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Golden-comment corpus (default: golden_comment_prs.yaml beside the nearest evals/review/gate.yaml).",
)
@click.option(
    "--golden-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="A checkout of the answer key at the commit the corpus pins (default: cloned into the cache at run time).",
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="A review-benchmark JSON report whose findings are scored (LemonCrow's own reviews).",
)
@click.option("--depth", default="auto", show_default=True, help="Which depth of the report to score.")
@click.option(
    "--finding-threshold",
    type=click.Choice(["critical", "high", "medium", "suggestions"]),
    default=None,
    help="Count only the findings the bot shows at this finding threshold (default: the product's default).",
)
@click.option(
    "--reviewer-comments",
    "comments_dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="A directory of <pull request id>.json comment files of another reviewer.",
)
@click.option("--reviewer-label", default="", help="What to call the reviewer in the report.")
@click.option(
    "--tool-id",
    default=None,
    metavar="ID",
    help="With --reviewer-comments, for the benchmark_summary: the tool's id in the published file (a lowercase slug).",
)
@click.option(
    "--tool-name",
    default=None,
    metavar="NAME",
    help="With --reviewer-comments, for the benchmark_summary: the tool's name exactly as the published file spells it.",
)
@click.option(
    "--comments-description",
    default=None,
    metavar="TEXT",
    help="With --reviewer-comments, for the benchmark_summary: what the comments are and how they were taken.",
)
@click.option(
    "--comments-url",
    default=None,
    metavar="URL",
    help="With --reviewer-comments, for the benchmark_summary: an https URL of where the comments were taken from.",
)
@click.option(
    "--comments-collected-on",
    default=None,
    metavar="YYYY-MM-DD",
    help="With --reviewer-comments, for the benchmark_summary: the day the comments were collected.",
)
@click.option(
    "--judge-cache",
    "cache_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Where judge answers are kept (default: <store root>/golden-judge-cache).",
)
@click.option(
    "--cached-only", is_flag=True, help="Never call the judge: a pull request with no cached answer is not scored."
)
@click.option("--pr", "pr_ids", multiple=True, metavar="ID", help="Only these pull request ids.")
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Where the report goes. Required, and outside the checkout: it holds the reviewers' comment texts.",
)
@click.option("--name", default=None, help="Report base name (default: <corpus>-<reviewer>-<UTC time>).")
@click.option(
    "--plan",
    "plan_only",
    is_flag=True,
    help="Print what would be judged and how many judge calls it needs; call nothing.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def golden_score(
    corpus_path: Path | None,
    golden_dir: Path | None,
    report_path: Path | None,
    depth: str,
    finding_threshold: str | None,
    comments_dir: Path | None,
    reviewer_label: str,
    tool_id: str | None,
    tool_name: str | None,
    comments_description: str | None,
    comments_url: str | None,
    comments_collected_on: str | None,
    cache_dir: Path | None,
    cached_only: bool,
    pr_ids: tuple[str, ...],
    out_dir: Path | None,
    name: str | None,
    plan_only: bool,
    as_json: bool,
) -> None:
    """Score posted review comments against the answer key: precision, recall and F, overall and per language.

    Exit status: 0 all scored, 1 an error, 3 some pull requests could not be scored (they are listed and in no total).
    """

    from lemoncrow import __version__ as engine_version
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.gateway.cli.commands.review_benchmark import _checkout_root, _gate_root
    from lemoncrow.pro.capabilities.review.evaluation import benchmark_summary, golden_scoring
    from lemoncrow.pro.capabilities.review.evaluation.corpus import CorpusError, bundled_roots
    from lemoncrow.pro.capabilities.review.evaluation.golden_comments import (
        GoldenError,
        load_golden_corpus,
        open_golden_key,
    )
    from lemoncrow.pro.capabilities.review.evaluation.golden_judge import JudgeRequest, default_judge

    if (report_path is None) == (comments_dir is None):
        raise click.UsageError(
            "name exactly one source of comments: --report (LemonCrow) or --reviewer-comments (another reviewer)"
        )
    competitor = benchmark_summary.CompetitorRow(
        tool_id or "", tool_name or "", comments_description or "", comments_url or "", comments_collected_on or ""
    )
    if competitor.given():
        if comments_dir is None:
            raise click.UsageError(
                "--tool-id, --tool-name and --comments-* describe another reviewer: use --reviewer-comments"
            )
        if wrong := competitor.malformed():
            raise click.UsageError("; ".join(wrong))
    limited_to = tuple(pr_ids)  # the person's --pr, before a report narrows the pull requests to the ones it holds
    gate_root = _gate_root()
    checkout = _checkout_root(gate_root)
    if corpus_path is None:
        if gate_root is None:
            raise click.UsageError("no evals/review directory found; pass --corpus")
        corpus_path = gate_root / "golden_comment_prs.yaml"
    try:
        golden = load_golden_corpus(corpus_path, pr_ids=pr_ids, repository_root=checkout)
        store = default_store_root()
        key = open_golden_key(golden.source, golden_dir, cache_root=store)
    except (CorpusError, GoldenError) as exc:
        raise click.ClickException(str(exc)) from exc
    ids = [pr.id for pr in golden.corpus.prs]
    if report_path is not None:
        report_in = json.loads(report_path.read_text(encoding="utf-8"))
        if not pr_ids:
            # A report made with --pr covers only those pull requests; the rest of the corpus was not asked for.
            present = {str(item.get("id")) for item in report_in.get("prs", ())}
            pr_ids = tuple(pr_id for pr_id in ids if pr_id in present)
            if not pr_ids:
                raise click.ClickException(f"{report_path.name} holds none of this corpus's pull requests")
        try:
            submissions = golden_scoring.submissions_from_benchmark_report(
                report_in, depth, ids, threshold=finding_threshold
            )
        except ValueError as exc:
            raise click.ClickException(f"{report_path}: {exc}") from exc
        run = report_in.get("run", {})
        reviewer: dict[str, Any] = {
            "label": reviewer_label or f"LemonCrow ({run.get('driver', '?')}/{run.get('model', '?')}, {depth})",
            "kind": "lemoncrow",
            "depth": depth,
            "finding_threshold": finding_threshold or "default",
            "benchmark_report": report_path.name,
            "benchmark_basis": run.get("basis", ""),
            "benchmark_driver": run.get("driver", ""),
            "benchmark_model": run.get("model", ""),
            "benchmark_code": run.get("code", {}),
            "description": f"LemonCrow's AI review at depth {depth}: the findings the bot would post (benchmark report {report_path.name}).",
        }
    else:
        assert comments_dir is not None
        submissions = golden_scoring.load_reviewer_comments(comments_dir, ids)
        reviewer = {
            "label": reviewer_label or comments_dir.name,
            "kind": "comments",
            "source": str(comments_dir),
            "description": f"another reviewer's saved comments ({comments_dir.name}); same judge and rule.",
        }
    judge = default_judge(cache_dir or store / "golden-judge-cache", cached_only=cached_only)
    if plan_only:
        asks = 0
        for pr in golden.corpus.prs:
            submission = submissions.get(pr.id)
            if submission is None or not submission.reviewed or not submission.comments:
                click.echo(
                    f"  {pr.id:<14} nothing to judge ({'no review' if submission is None or not submission.reviewed else 'no comments'})"
                )
                continue
            goldens = key.comments(golden.ref(pr.id))
            request = JudgeRequest(
                tuple(c.judge_text() for c in submission.comments), tuple((g.text, g.severity) for g in goldens)
            )
            cached = judge.has(request)
            asks += 0 if cached else 1
            click.echo(
                f"  {pr.id:<14} {len(submission.comments):>3} comments vs {len(goldens)} golden: "
                f"{'cached' if cached else 'needs one judge call'}"
            )
        click.echo(f"{asks} judge calls would be made (one per pull request with comments and no cached answer)")
        return
    if out_dir is None:
        raise click.UsageError(
            "--out-dir is required (outside this checkout): the report holds the reviewers' comment texts"
        )
    for bundled in bundled_roots(checkout):
        if out_dir.resolve() == bundled or bundled in out_dir.resolve().parents:
            raise click.ClickException(f"reports must stay outside {bundled}; choose another --out-dir")
    scores = golden_scoring.score_corpus(golden, key, submissions, judge, pr_ids=pr_ids)
    started = datetime.now(UTC)
    label_slug = "".join(ch if ch.isalnum() else "-" for ch in str(reviewer["label"]).lower()).strip("-")[:40]
    base = name or f"{golden.corpus.id}-{label_slug}-{started.strftime('%Y%m%dT%H%M%SZ')}"
    report = golden_scoring.build_report(
        golden, scores, judge, name=base, reviewer=reviewer, generated_at=started.isoformat(timespec="seconds")
    )
    benchmark_summary.attach(
        report,
        benchmark_summary.build_benchmark_summary(
            report, engine_version=engine_version, competitor=competitor, limited_to=limited_to
        ),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{base}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / f"{base}.md").write_text(golden_scoring.render_markdown(report), encoding="utf-8")
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        click.echo(_summary(report))
        click.echo(f"report: {out_dir / (base + '.md')}\n        {out_dir / (base + '.json')}")
    if report["unscored"]:
        click.echo(f"{len(report['unscored'])} pull request(s) could not be scored and are in no total", err=True)
        sys.exit(EXIT_UNSCORED)


def _summary(report: dict[str, Any]) -> str:
    def line(label: str, totals: dict[str, Any]) -> str:
        s = totals["strict"]

        def pct(value: Any) -> str:
            return "n/a" if value is None else f"{round(value * 100)}"

        return (
            f"  {label:<18} PRs {totals['prs']:>2}  TP {s['tp']:>3} FP {s['fp']:>3} FN {s['fn']:>3}  "
            f"precision {pct(s['precision']):>3}  recall {pct(s['recall']):>3}  F {pct(s['f']):>3}"
        )

    lines = [f"{report['name']}: {report['reviewer']['label']}", line("all", report["overall"])]
    lines += [line(language, totals) for language, totals in report["by_language"].items()]
    judge = report["judge"]
    lines.append(
        f"  judge {judge['driver']}/{judge['model']}: {judge['asked_now']} asked now, {judge['answered_from_cache']} cached, "
        f"{judge['cost_now']['calls']} calls"
    )
    if "benchmark_summary" in report:
        lines.append("  ready to publish: benchmark_summary written (make benchmark-publish RUN=<the .json report>)")
    elif report.get("benchmark_summary_omitted"):
        lines.append(f"  ready to publish: nothing yet ({report['benchmark_summary_omitted']})")
    return "\n".join(lines)
