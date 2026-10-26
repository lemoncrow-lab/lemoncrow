"""``lc dev eval review benchmark``: run the PR reviewer on pinned real pull requests and report cost, catch and noise.

The one command the owner runs once a model key is set::

    export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY
    uv run lemoncrow dev eval review benchmark --corpus evals/review/real_prs.yaml \\
        --depth quick,auto,deep --max-usd 20

It spends money only with ``--driver live`` (the default) and a ceiling, and refuses to run in CI or under pytest.
``--plan`` prints what it would do and calls nothing. ``--driver fake`` checks the harness with a scripted model;
``--driver replay`` re-reads outputs an earlier run recorded (``--record-dir``). ``import-reviews`` turns a pull
request's comments from a baseline reviewer (another bot that already reviewed it) into the JSON the benchmark reads.
See ``docs/reference/review-benchmark.md``.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.evaluation.real_prs import RealPrCorpus

_PR_URL = re.compile(r"^https?://[^/]+/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)")
_PR_SHORT = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")
EXIT_STOPPED = 3


def _gate_root() -> Path | None:
    from lemoncrow.pro.capabilities.review.evaluation.gate import GateError, find_gate_root

    try:
        return find_gate_root(Path.cwd())
    except GateError:
        return None


def _checkout_root(gate_root: Path | None) -> Path | None:
    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root

    try:
        return detect_repo_root(gate_root or Path.cwd())
    except ValueError:
        return None


def _depths(raw: str) -> tuple[str, ...]:
    from lemoncrow.pro.capabilities.review.evaluation.benchmark import DEPTHS

    depths = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [depth for depth in depths if depth not in DEPTHS]
    if unknown or not depths:
        raise click.BadParameter(f"choose from {', '.join(DEPTHS)} (comma separated)", param_hint="--depth")
    return depths


def _plan_text(corpus: RealPrCorpus, depths: tuple[str, ...], driver: str, pushes: bool, ceiling: str) -> list[str]:
    from lemoncrow.pro.capabilities.review.evaluation.benchmark import CALLS_AT_MOST

    reviews = len(corpus.prs) * len(depths)
    push_runs = 2 * sum(len(pr.pushes) for pr in corpus.prs) * len(depths) if pushes else 0
    worst = sum(CALLS_AT_MOST[depth] for depth in depths) * (
        len(corpus.prs) + (2 * sum(len(pr.pushes) for pr in corpus.prs) if pushes else 0)
    )
    lines = [
        f"corpus {corpus.id} ({corpus.visibility}): {len(corpus.prs)} pull requests, "
        f"{sum(1 for pr in corpus.prs if pr.labelled)} labelled, {sum(len(pr.labels) for pr in corpus.prs)} labels",
        f"driver {driver}; depths {', '.join(depths)}; {ceiling}",
        f"{reviews} full reviews"
        + (
            f" and {push_runs} push reviews (each push once with carry-forward, once re-reviewing everything)"
            if pushes
            else ""
        ),
        f"at most {worst} model calls in all: what one review can make at each depth "
        f"({', '.join(f'{name} {count}' for name, count in CALLS_AT_MOST.items())}; the hard cap is 20, and an Auto review "
        "of an ordinary pull request makes a handful)",
        "dollars cannot be bounded before a call returns its tokens: the ceiling stops the run, not this plan",
    ]
    for pr in corpus.prs:
        lines.append(
            f"  {pr.id:<28} {pr.repository}  {'labelled' if pr.labelled else 'unlabelled'} "
            f"({len(pr.labels)} labels), {len(pr.pushes)} push(es), baseline reviewer: {'comments' if pr.baseline else 'none'}"
        )
    return lines


@click.command("benchmark")
@click.option(
    "--corpus",
    "corpus_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Real-PR corpus file (default: real_prs.yaml beside the nearest evals/review/gate.yaml).",
)
@click.option(
    "--depth",
    default="quick,auto,deep",
    show_default=True,
    help="Depths to run, comma separated: quick auto thorough deep.",
)
@click.option(
    "--driver",
    type=click.Choice(["live", "replay", "fake"]),
    default="live",
    show_default=True,
    help="live: the model route in the environment (spends money). replay: recorded fixtures. fake: a scripted stand-in.",
)
@click.option(
    "--max-usd", type=click.FloatRange(min=0, min_open=True), default=None, help="Stop after this many dollars."
)
@click.option("--max-calls", type=click.IntRange(min=1), default=None, help="Stop after this many model calls.")
@click.option("--pr", "pr_ids", multiple=True, metavar="ID", help="Only these pull request ids.")
@click.option(
    "--pushes/--no-pushes",
    default=True,
    show_default=True,
    help="Simulate follow-up pushes, with and without carry-forward.",
)
@click.option(
    "--impact/--no-impact",
    "with_impact",
    default=True,
    show_default=True,
    help="Capture change impact, as the Action does.",
)
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Where pinned commits are fetched (default: <store root>/review-benchmark).",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Where the report goes (default: evals/review/reports; required, and outside the checkout, for an organization corpus).",
)
@click.option("--name", default=None, help="Report base name (default: <corpus>-<driver>-<UTC time>).")
@click.option(
    "--record-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Record every model call here, for replay.",
)
@click.option(
    "--fixtures-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="Replay from fixtures recorded here.",
)
@click.option(
    "--input-usd-per-mtok", type=float, default=None, help="Price override for a model the local price table lacks."
)
@click.option(
    "--output-usd-per-mtok", type=float, default=None, help="Price override for a model the local price table lacks."
)
@click.option(
    "--line-window",
    type=click.IntRange(min=0),
    default=8,
    show_default=True,
    help="Comment/finding proximity, in lines.",
)
@click.option(
    "--similarity",
    type=click.FloatRange(min=0, max=1),
    default=0.2,
    show_default=True,
    help="Text similarity needed to pair a finding with a comment.",
)
@click.option(
    "--reviewer-login",
    "logins",
    multiple=True,
    help="GitHub login the baseline reviewer comments as (repeatable). Needed with --fetch-baseline; no default.",
)
@click.option(
    "--fetch-baseline",
    is_flag=True,
    help="Fetch the baseline reviewer's comments from GitHub for every pull request that names a PR number.",
)
@click.option(
    "--baseline-usd-per-review",
    type=float,
    default=None,
    help="The baseline reviewer's price per review, as you know it. Reported as your input.",
)
@click.option(
    "--baseline-label",
    default="",
    help="What that price is (for example 'Plan X, from its pricing page on <date>').",
)
@click.option("--team-prs-per-month", type=int, default=300, show_default=True)
@click.option("--team-reviews-per-pr", type=float, default=1.2, show_default=True)
@click.option("--team-developers", type=int, default=10, show_default=True)
@click.option(
    "--organization-opt-in", metavar="ORG", default=None, help="Explicit opt-in to load an organization corpus."
)
@click.option(
    "--plan", "plan_only", is_flag=True, help="Print what would run and its call bound; call nothing, fetch nothing."
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def benchmark(
    corpus_path: Path | None,
    depth: str,
    driver: str,
    max_usd: float | None,
    max_calls: int | None,
    pr_ids: tuple[str, ...],
    pushes: bool,
    with_impact: bool,
    workdir: Path | None,
    out_dir: Path | None,
    name: str | None,
    record_dir: Path | None,
    fixtures_dir: Path | None,
    input_usd_per_mtok: float | None,
    output_usd_per_mtok: float | None,
    line_window: int,
    similarity: float,
    logins: tuple[str, ...],
    fetch_baseline: bool,
    baseline_usd_per_review: float | None,
    baseline_label: str,
    team_prs_per_month: int,
    team_reviews_per_pr: float,
    team_developers: int,
    organization_opt_in: str | None,
    plan_only: bool,
    as_json: bool,
) -> None:
    """Review pinned real pull requests at each depth and report cost, catch, noise and push cost.

    Exit status: 0 complete, 1 an error, 3 the spending ceiling stopped the run (the report is written and partial).
    """

    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.review import ai_review
    from lemoncrow.pro.capabilities.review.evaluation.benchmark import (
        BenchmarkError,
        BenchmarkOptions,
        run_benchmark,
    )
    from lemoncrow.pro.capabilities.review.evaluation.benchmark_report import build_report, render_markdown
    from lemoncrow.pro.capabilities.review.evaluation.corpus import CorpusError, bundled_roots
    from lemoncrow.pro.capabilities.review.evaluation.real_prs import load_real_pr_corpus
    from lemoncrow.pro.capabilities.review.repo_context import CONTEXT_VERSION, repo_context_enabled

    if fetch_baseline and not logins:
        raise click.UsageError("--fetch-baseline needs --reviewer-login: the bot the baseline reviewer comments as")
    gate_root = _gate_root()
    checkout = _checkout_root(gate_root)
    if corpus_path is None:
        if gate_root is None:
            raise click.UsageError("no evals/review directory found; pass --corpus")
        corpus_path = gate_root / "real_prs.yaml"
    try:
        corpus = load_real_pr_corpus(
            corpus_path, organization_opt_in=organization_opt_in, repository_root=checkout, pr_ids=pr_ids
        )
    except CorpusError as exc:
        raise click.ClickException(str(exc)) from exc
    depths = _depths(depth)
    reports = out_dir or (gate_root / "reports" if gate_root is not None else Path("reports"))
    if corpus.visibility == "organization":
        if out_dir is None:
            raise click.UsageError("an organization corpus needs --out-dir outside this checkout")
        for bundled in bundled_roots(checkout):
            if reports.resolve() == bundled or bundled in reports.resolve().parents:
                raise click.ClickException(
                    f"organization results must stay outside {bundled}; choose another --out-dir"
                )
    ceiling = ", ".join(
        part
        for part in (
            f"max ${max_usd:g}" if max_usd is not None else "",
            f"max {max_calls} calls" if max_calls is not None else "",
        )
        if part
    ) or ("no ceiling (not a live run)" if driver != "live" else "no ceiling")
    if plan_only:
        for line in _plan_text(corpus, depths, driver, pushes, ceiling):
            click.echo(line)
        return
    try:
        options = BenchmarkOptions(
            depths=depths,
            mode=driver,  # type: ignore[arg-type]
            max_usd=max_usd,
            max_calls=max_calls,
            pushes=pushes,
            with_impact=with_impact,
            workdir=(workdir or default_store_root() / "review-benchmark"),
            record_dir=record_dir,
            fixtures_dir=fixtures_dir,
            input_usd_per_mtok=input_usd_per_mtok,
            output_usd_per_mtok=output_usd_per_mtok,
            line_window=line_window,
            similarity=similarity,
            reviewer_logins=tuple(logins),
            fetch_baseline=fetch_baseline,
        )
        started = datetime.now(UTC)
        results, ledger, facts = run_benchmark(corpus, options, say=lambda message: click.echo(message, err=True))
    except BenchmarkError as exc:
        raise click.ClickException(str(exc)) from exc
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    base = name or f"{corpus.id}-{driver}-{stamp}"
    report = build_report(
        corpus,
        options,
        results,
        ledger,
        facts,
        name=base,
        command=_command_line(click.get_current_context()),
        code={
            "git_head": _git_head(checkout),
            "prompt_version": ai_review.PROMPT_VERSION,
            "schema_version": ai_review.SCHEMA_VERSION,
            "repo_context": {"enabled": repo_context_enabled(), "version": CONTEXT_VERSION},
        },
        baseline_usd_per_review=baseline_usd_per_review,
        baseline_label=baseline_label,
        team=(team_prs_per_month, team_reviews_per_pr, team_developers),
        generated_at=started.isoformat(timespec="seconds"),
    )
    reports.mkdir(parents=True, exist_ok=True)
    json_path, md_path = reports / f"{base}.json", reports / f"{base}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        click.echo(_summary(report))
        click.echo(f"report: {md_path}\n        {json_path}")
    errored = any(run.status == "error" for result in results for run in result.reviews.values())
    if ledger.stopped:
        click.echo(f"stopped: {ledger.stopped} (partial report written)", err=True)
        sys.exit(EXIT_STOPPED)
    if errored:
        sys.exit(1)


def _command_line(ctx: click.Context) -> str:
    """The command as it was given: the options that came from the command line, not their defaults.

    No secrets can be in it: model keys are read from the environment.
    """

    parts = ["lemoncrow", "dev", "eval", "review", "benchmark"]
    for param in ctx.command.params:
        if not isinstance(param, click.Option) or param.name is None:
            continue
        if ctx.get_parameter_source(param.name) != click.core.ParameterSource.COMMANDLINE:
            continue
        value = ctx.params.get(param.name)
        if param.is_flag:
            parts.append(param.opts[0] if value or not param.secondary_opts else param.secondary_opts[0])
        elif param.multiple:
            for item in value or ():
                parts += [param.opts[0], str(item)]
        elif value is not None:
            parts += [param.opts[0], str(value)]
    return shlex.join(parts)


def _git_head(checkout: Path | None) -> str:
    import subprocess

    if checkout is None:
        return ""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _summary(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [f"{report['name']}: basis {run['basis']}"]
    for depth, agg in report["aggregate"].items():
        usd = agg["provider_usd"]
        catch = agg["catch"]
        noise = agg["noise"]
        lines.append(
            f"  {depth:<9} {agg['complete']}/{agg['prs']} complete  "
            f"calls {agg['model_calls']['mean'] if agg['model_calls']['mean'] is not None else 'n/a'}/review  "
            f"usd {'n/a' if usd['mean'] is None else f'{usd['mean']:.4f}'}/review  "
            f"findings p0/p1/p2 {agg['findings']['p0']}/{agg['findings']['p1']}/{agg['findings']['p2']}  "
            f"catch {catch['caught']}/{catch['expected']}  noise {noise['unmatched']}/{noise['findings']}"
        )
    publication = report["publication"]
    ready = [key for key in ("cost_per_review", "quality", "head_to_head") if publication[key]["ready"]]
    lines.append("  ready to publish: " + (", ".join(ready) if ready else "nothing yet (see the report's checklist)"))
    return "\n".join(lines)


@click.command("import-reviews")
@click.option(
    "--pr", "pr_spec", default=None, metavar="OWNER/REPO#N|URL", help="Fetch this pull request's comments from GitHub."
)
@click.option(
    "--from-json",
    "source_file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Normalise a JSON export of the comments instead (GitHub REST shape, or an earlier import).",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="Where to write the normalised export.",
)
@click.option(
    "--reviewer-login",
    "logins",
    multiple=True,
    help="GitHub login the baseline reviewer comments as (repeatable). Required unless --all-authors; no default.",
)
@click.option("--all-authors", is_flag=True, help="Keep every author's comments (a file you already filtered).")
def import_reviews(
    pr_spec: str | None, source_file: Path | None, out: Path, logins: tuple[str, ...], all_authors: bool
) -> None:
    """Fetch or normalise a pull request's comments from a baseline reviewer for the benchmark.

    The baseline reviewer is the bot named by --reviewer-login, another reviewer that already commented on the pull
    request. The output is its baseline: it is compared with LemonCrow's findings and with the labels, and is never
    treated as ground truth. Add ``"verdict": "valid"`` or ``"invalid"`` to a comment to record that a person judged it.
    """

    from lemoncrow.pro.capabilities.review.evaluation.baseline_reviews import (
        BaselineError,
        export_comments,
        fetch_pull_comments,
        parse_comments,
    )

    if (pr_spec is None) == (source_file is None):
        raise click.UsageError("name exactly one of --pr or --from-json")
    if not logins and not all_authors:
        raise click.UsageError(
            "name the baseline reviewer's bot with --reviewer-login (repeatable), or pass --all-authors"
        )
    chosen = None if all_authors else set(logins)
    try:
        if source_file is not None:
            comments = parse_comments(json.loads(source_file.read_text(encoding="utf-8")), logins=chosen)
            source = str(source_file.name)
        else:
            assert pr_spec is not None
            match = _PR_URL.match(pr_spec) or _PR_SHORT.match(pr_spec)
            if match is None:
                raise click.BadParameter("use OWNER/REPO#N or a pull request URL", param_hint="--pr")
            comments = fetch_pull_comments(match["repo"], int(match["number"]), logins=chosen)
            source = f"github:{match['repo']}#{match['number']}"
    except (BaselineError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            export_comments(comments, source=source, reviewer=", ".join(sorted(logins))),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    inline = sum(1 for comment in comments if comment.kind == "inline")
    authors = sorted({comment.author for comment in comments if comment.author})
    click.echo(
        f"{len(comments)} comments ({inline} inline, {len(comments) - inline} summary) by {', '.join(authors) or 'unknown'}"
    )
    click.echo(f"wrote {out}")
    click.echo("point a corpus entry at it:  baseline: {comments_file: <path relative to the corpus file>}")


@click.command("derive-label")
@click.option(
    "--repo",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    required=True,
    help="A clone with the history between the two commits.",
)
@click.option("--fix", required=True, help="The commit that fixed the bug.")
@click.option(
    "--introduced",
    required=True,
    help="The change that introduced it: a squash or ordinary commit, or the merge commit of a pull request.",
)
@click.option(
    "--head",
    default=None,
    help="Override the reviewed head (default: the introducing commit, or a merge's second parent).",
)
@click.option("--id", "label_id", required=True, help="An id for the label and the pull request entry.")
@click.option("--repository", default="OWNER/NAME", show_default=True)
@click.option("--pull", type=int, default=0, help="The pull request number, if the change was one.")
@click.option("--title", default="", help="The pull request or commit title.")
@click.option("--license", "license_id", default="", help="The repository's SPDX licence, read from its LICENSE file.")
@click.option(
    "--symbols",
    default=None,
    help="Comma-separated identifiers to keep (default: all the candidates, to be trimmed by hand).",
)
@click.option(
    "--within-change",
    is_flag=True,
    help="Keep only fix sites on lines the reviewed change itself added or changed (a wide fix otherwise labels code "
    "the pull request never touched).",
)
def derive_label_command(
    repo: Path,
    fix: str,
    introduced: str,
    head: str | None,
    label_id: str,
    repository: str,
    pull: int,
    title: str,
    license_id: str,
    symbols: str | None,
    within_change: bool,
) -> None:
    """Print a corpus entry for a bug fixed by a later commit, derived from git alone.

    Nothing is judged: the label points where the fix edited, mapped back to the reviewed head. You still choose
    which of the candidate symbols name the bug, and you add `pushes:` if you want a follow-up push simulated.
    """

    import yaml

    from lemoncrow.pro.capabilities.review.evaluation.label_derivation import DerivationError, derive_label, pr_entry

    try:
        derivation = derive_label(repo, fix, introduced, head=head, pull=pull, within_change=within_change)
    except DerivationError as exc:
        raise click.ClickException(str(exc)) from exc
    chosen = [part.strip() for part in symbols.split(",") if part.strip()] if symbols is not None else None
    entry = dict(
        pr_entry(
            derivation,
            label_id=label_id,
            repository=repository,
            pull=pull,
            title=title or derivation.fix_subject,
            license_id=license_id,
            symbols=chosen,
        )
    )
    entry["pushes"] = [
        {
            "id": "apply-fix",
            "apply_commit": derivation.fix_commit,
            "apply_paths": sorted({r.path for r in derivation.regions}),
        }
    ]
    click.echo(yaml.safe_dump([entry], sort_keys=False, width=120).rstrip())
    for note in derivation.notes:
        click.echo(f"# note: {note}", err=True)
    click.echo(
        f"# attribution: {derivation.attribution}; candidate symbols: {', '.join(derivation.symbols) or 'none'}",
        err=True,
    )


@click.command("init-corpus")
@click.option("--pr", "specs", multiple=True, metavar="URL|OWNER/REPO#N", help="A pull request to include. Repeatable.")
@click.option(
    "--pr-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="A file with one pull request URL per line (blank lines and # comments are ignored).",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="The corpus file to write, outside this checkout.",
)
@click.option("--id", "corpus_id", default=None, help="Corpus id (default: <organization>-prs).")
@click.option("--organization", required=True, help="The customer organization these pull requests belong to.")
@click.option("--approved-by", required=True, help="Who at the customer approved using their pull requests.")
@click.option("--reference", required=True, help="Where that approval is recorded (a ticket, an email thread).")
@click.option(
    "--reviewer-login",
    "logins",
    multiple=True,
    help="GitHub login the baseline reviewer comments as (repeatable). Required unless --no-comments; no default.",
)
@click.option("--no-comments", is_flag=True, help="Do not fetch the baseline reviewer's comments.")
def init_corpus_command(
    specs: tuple[str, ...],
    pr_file: Path | None,
    out: Path,
    corpus_id: str | None,
    organization: str,
    approved_by: str,
    reference: str,
    logins: tuple[str, ...],
    no_comments: bool,
) -> None:
    """Write a benchmark corpus from a list of pull request URLs, with the baseline reviewer's comments beside each.

    This is the first run for a customer whose pull requests already carry another reviewer's comments (name its bot
    with --reviewer-login). It needs `gh` signed in with read access to those repositories. The result is an organization corpus: nothing is labelled, and it
    stays outside this checkout.
    """

    from lemoncrow.pro.capabilities.review.evaluation.corpus import bundled_roots
    from lemoncrow.pro.capabilities.review.evaluation.corpus_init import InitError, init_corpus
    from lemoncrow.pro.capabilities.review.sources.github import run_gh

    listed = list(specs)
    if pr_file is not None:
        listed += [
            line.strip()
            for line in pr_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    resolved_out = out.resolve()
    for bundled in bundled_roots(_checkout_root(_gate_root())):
        if resolved_out == bundled or bundled in resolved_out.parents:
            raise click.ClickException(f"a customer's corpus must stay outside {bundled}; choose another --out")
    try:
        result = init_corpus(
            lambda args, stdin: run_gh(args, stdin),
            listed,
            out=out,
            corpus_id=corpus_id or f"{re.sub(r'[^a-z0-9._-]+', '-', organization.lower()).strip('-')}-prs",
            organization=organization,
            approved_by=approved_by,
            reference=reference,
            approved_at=datetime.now(UTC).date().isoformat(),
            logins=tuple(logins),
            fetch_comments=not no_comments,
        )
    except InitError as exc:
        raise click.ClickException(str(exc)) from exc
    for note in result.notes:
        click.echo(note)
    click.echo(
        f"wrote {result.corpus_path} ({len(result.entries)} pull requests, {len(result.comment_files)} with comments)"
    )
    click.echo(
        "next: uv run lemoncrow dev eval review benchmark --corpus "
        f"{result.corpus_path} --organization-opt-in {organization} --out-dir <a private directory> "
        "--depth quick,auto,deep --max-usd 20"
    )


__all__ = ["EXIT_STOPPED", "benchmark", "derive_label_command", "import_reviews", "init_corpus_command"]
