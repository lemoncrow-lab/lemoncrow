"""``lc review proof plan|run|report``: reproduce the open P0/P1 findings and check the added tests on base.

The three steps hand off through JSON files, so in GitHub Actions each runs in its own job and pull request
code never runs where the model key or a write token is:

* ``plan`` (model key, no pull request code) writes ``--out plan.json``;
* ``run`` (no secret) reads it, runs pull request code, and writes ``--out results.json``; ``--local`` runs on
  this machine in bubblewrap only, and ``--review ID`` records the result on that local Review's Snapshot;
* ``report`` (``checks: write``, ``pull-requests: write``, no pull request code) posts the ``LemonCrow Proof``
  check run and a reply on each finding's thread.

Exit status: 0 when the step wrote its file or posted; 2 when it could not run at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import click

from lemoncrow.pro.capabilities.review.proof import wire


class _CouldNotRun(click.ClickException):
    """Setup that stops the whole step: exit 2."""

    exit_code = 2


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _CouldNotRun(f"git could not run: {exc}") from exc
    if done.returncode != 0:
        raise _CouldNotRun(f"git {' '.join(args)} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def _commit(repo: Path, ref: str, what: str) -> str:
    """The full SHA of *ref*, which must be a commit already in this clone."""

    try:
        return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except _CouldNotRun as exc:
        raise _CouldNotRun(
            f"the {what} commit {ref[:12]} is not in this clone; check out with fetch-depth: 0 (or fetch it)"
        ) from exc


def _repo_root(repo: Path | None) -> Path:
    return Path(_git(repo or Path.cwd(), "rev-parse", "--show-toplevel").strip())


def _gh_runner(repo: Path) -> Any:
    from lemoncrow.pro.capabilities.review.pr_bot.github import with_backoff
    from lemoncrow.pro.capabilities.review.sources.github import run_gh

    def run(args: Sequence[str], stdin: str | None = None) -> str:
        return run_gh(args, stdin, cwd=repo)

    return with_backoff(run)


def _actions_run_url() -> str:
    server, repo, run = (
        os.environ.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


@click.group("proof")
def proof_group() -> None:
    """Reproduce open P0/P1 findings and check that the pull request's added tests fail on base."""


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


@proof_group.command("plan")
@click.option("--pr", "pr_spec", default="", help="Pull request: number, URL or OWNER/REPO#N (reads its summary).")
@click.option(
    "--state-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="A saved LemonCrow summary comment body to read the findings from instead of GitHub.",
)
@click.option("--repo", "repo_dir", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--head", "head_ref", default="", help="Head commit (default: the pull request's head).")
@click.option("--base", "base_ref", default="", help="Base commit (default: the merge base the summary names).")
@click.option("--max-usd", default=None, help="max-usd-per-pr (default: LEMONCROW_REVIEW_MAX_USD, else $3.00).")
@click.option("--max-runs", type=click.IntRange(1, 10_000), default=None, help="max-runs-per-pr.")
@click.option("--max-findings", type=click.IntRange(0, 20), default=5, show_default=True)
@click.option("--out", "out_path", type=click.Path(path_type=Path, dir_okay=False), required=True)
def proof_plan(
    pr_spec: str,
    state_file: Path | None,
    repo_dir: Path | None,
    head_ref: str,
    base_ref: str,
    max_usd: str | None,
    max_runs: int | None,
    max_findings: int,
    out_path: Path,
) -> None:
    """Ask the model for one failing test per open P0/P1 finding. Runs no pull request code."""

    from lemoncrow.pro.capabilities.review.pr_bot import spend
    from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
    from lemoncrow.pro.capabilities.review.proof import plan as plan_mod

    repo = _repo_root(repo_dir)
    try:
        cap = spend.resolve_max_usd(max_usd)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--max-usd") from exc
    repository, number, host = "", 0, "github.com"
    live_head = ""
    if state_file is not None:
        state = parse_state(state_file.read_text(encoding="utf-8", errors="replace"))
        if state is None:
            raise _CouldNotRun(f"{state_file} holds no LemonCrow summary state block")
    elif pr_spec:
        from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub
        from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, pull_request_ref

        runner = _gh_runner(repo)
        try:
            ref = pull_request_ref(repo, pr_spec, runner=runner)
            github = BotGitHub(ref, runner)
            live_head = github.pull_facts().head_sha
            _, found = github.summary()
        except GitHubCliError as exc:
            raise _CouldNotRun(f"GitHub: {exc}") from exc
        if found is None:
            raise _CouldNotRun(f"{ref.url} has no LemonCrow summary with state; run the review first")
        state = found
        repository, number, host = ref.slug, ref.number, ref.host
    else:
        raise click.UsageError("name the pull request with --pr, or a saved summary with --state-file")
    head = _commit(repo, head_ref or live_head or state.head_sha, "head")
    base_name = base_ref or state.merge_base_sha or state.base_sha
    if not base_name:
        raise _CouldNotRun("the summary state names no base commit; pass --base")
    base = _commit(repo, base_name, "base")
    from lemoncrow.pro.capabilities.review.pr_bot import ci_env

    # The key in the environment (ANTHROPIC_API_KEY / OPENAI_API_KEY) picks the route, as in the review job.
    route = ci_env.resolve_model_route(os.environ, str(os.environ.get(ci_env.PROVIDER_ENV) or "auto"))
    if route is not None:
        ci_env.apply_model_route(route, os.environ)
    document = plan_mod.build_plan(
        plan_mod.PlanInputs(
            repo=repo,
            state=state,
            head_sha=head,
            base_sha=base,
            repository=repository,
            pull_request=number,
            host=host,
            max_usd=cap,
            max_runs=max_runs,
            max_findings=max_findings,
        )
    )
    wire.write(out_path, document)
    planned = sum(1 for row in document["repros"] if row["status"] == "planned")
    click.echo(f"Planned {planned} of {len(document['repros'])} reproduction(s) for {head[:12]}: {out_path}")
    for row in document["repros"]:
        if row["status"] != "planned":
            click.echo(f"  - {row['finding_id']}: {row['reason']}")
    for note in document["notes"]:
        click.echo(f"  note: {note}")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def _record_on_review(review_id: str, summary: Any) -> str:
    """Record the proof on the local Review's Snapshot of the head; returns the Snapshot's status line."""

    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted import server_review_request
    from lemoncrow.pro.capabilities.review.web_qa import snapshot_for_sha

    config = load_config(product="review")
    path = f"/api/reviews/{quote(review_id, safe='')}"
    try:
        rows = server_review_request(config, "GET", f"{path}/revisions").get("revisions")
        snapshot = snapshot_for_sha([row for row in rows or () if isinstance(row, dict)], summary.head_sha)
        if snapshot is None:
            return f"Review {review_id} has no clean Snapshot of {summary.head_sha[:12]}; not recorded"
        conclusion = summary.conclusion
        annotations = [
            {
                "path": str(item.plan.get("path") or ""),
                "start_line": max(1, wire.count(item.plan.get("line"))),
                "end_line": max(1, wire.count(item.plan.get("line"))),
                "annotation_level": "failure" if item.outcome == "supported" else "notice",
                "title": f"Proof: {item.outcome}",
                "message": f"{item.plan.get('title') or ''}: {item.reason}"[:900],
            }
            for item in summary.findings
            if item.plan.get("path")
        ]
        body = {
            "format": "check",
            "source": "ci",
            "category": "tests",
            "tool": "lemoncrow-proof",
            "title": wire.CHECK_NAME,
            "stable_key": "lemoncrow-proof",
            "status": {"failure": "FAIL", "success": "PASS"}.get(conclusion, "UNKNOWN"),
            "head_sha": summary.head_sha,
            "revision_id": str(snapshot["id"]),
            "annotations": annotations,
        }
        server_review_request(config, "POST", f"{path}/checks/ingest", body=body)
    except ClientError as exc:
        raise click.ClickException(f"Review server failed: {exc.message}") from exc
    return f"recorded on Review {review_id} Snapshot {snapshot.get('revision_number')} ({body['status']})"


@proof_group.command("run")
@click.option("--plan", "plan_path", type=click.Path(path_type=Path, dir_okay=False, exists=True), default=None)
@click.option("--repo", "repo_dir", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--head", "head_ref", default="", help="Head commit when there is no plan (default: HEAD).")
@click.option("--base", "base_ref", default="", help="Base commit when there is no plan.")
@click.option("--sandbox", type=click.Choice(["auto", "bubblewrap", "none"]), default="auto", show_default=True)
@click.option(
    "--toolchain",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    multiple=True,
    help="Directory mounted read-only in the sandbox at the same path (interpreter, toolchain, venv).",
)
@click.option("--deps", multiple=True, help="Checkout-relative dependency directory to expose (node_modules).")
@click.option("--timeout", "timeout_s", type=click.IntRange(5, 900), default=300, show_default=True)
@click.option("--local", "local", is_flag=True, help="Run on this machine: bubblewrap required, never plain.")
@click.option("--review", "review_id", default="", help="With --local: record the result on this local Review.")
@click.option("--out", "out_path", type=click.Path(path_type=Path, dir_okay=False), default=None)
@click.option("--json", "as_json", is_flag=True, help="Print the results document.")
def proof_run(
    plan_path: Path | None,
    repo_dir: Path | None,
    head_ref: str,
    base_ref: str,
    sandbox: str,
    toolchain: tuple[Path, ...],
    deps: tuple[str, ...],
    timeout_s: int,
    local: bool,
    review_id: str,
    out_path: Path | None,
    as_json: bool,
) -> None:
    """Run the planned reproductions and the changed tests on head and base. Holds no secret."""

    from lemoncrow.pro.capabilities.review.proof import report as report_mod
    from lemoncrow.pro.capabilities.review.proof import run as run_mod

    if review_id and not local:
        raise click.UsageError("--review records a local run; add --local")
    if local and sys.platform == "darwin":
        raise _CouldNotRun("local proof runs need bubblewrap, which is Linux-only for now")
    repo = _repo_root(repo_dir)
    plan: dict[str, Any] | None = None
    plan_raw = b""
    if plan_path is not None:
        try:
            plan, plan_raw = wire.load(plan_path, wire.PLAN_SCHEMA)
        except wire.ProofFileError as exc:
            raise _CouldNotRun(str(exc)) from exc
        head_name, base_name = str(plan.get("head_sha") or ""), str(plan.get("base_sha") or "")
    else:
        head_name, base_name = head_ref or "HEAD", base_ref
        if not base_name:
            raise click.UsageError("without --plan, name the base commit with --base")
    head, base = _commit(repo, head_name, "head"), _commit(repo, base_name, "base")
    for rel in deps:
        if not wire.SAFE_PATH.match(rel) or not (repo / rel).is_dir():
            raise click.BadParameter(f"{rel} is not a dependency directory of the checkout", param_hint="--deps")
    roots = tuple(path.resolve() for path in toolchain)
    if any(str(root) == "/" for root in roots):
        raise click.BadParameter("mount a toolchain directory, not /", param_hint="--toolchain")
    chosen = run_mod.resolve_sandbox(sandbox, require=local)
    if plan is None:
        # A plan-less local run checks the changed tests only; this stand-in plan binds the results to it.
        plan = {"schema": wire.PLAN_SCHEMA, "head_sha": head, "base_sha": base, "repros": [], "notes": []}
        plan_raw = wire.dump(plan)
    config = run_mod.RunConfig(
        repo=repo, head_sha=head, base_sha=base, sandbox=chosen, toolchain=roots, deps=deps, timeout=timeout_s
    )
    try:
        results = run_mod.run_proof(config, plan, plan_digest=wire.digest(plan_raw))
    except Exception as exc:  # materializing or diffing failed: nothing ran
        raise _CouldNotRun(f"could not prepare the run: {exc}") from exc
    if out_path is not None:
        wire.write(out_path, results)
    summary = report_mod.summarize(plan, plan_raw, results)
    if as_json:
        click.echo(json.dumps(results, indent=2, sort_keys=True))
    else:
        click.echo(f"Proof {summary.conclusion.upper()}  {summary.title()}  head {head[:12]}  sandbox {chosen.mode}")
        if chosen.reason:
            click.echo(f"  sandbox: {chosen.reason}")
        for item in summary.findings:
            click.echo(f"  {item.outcome:<13} {item.plan.get('finding_id')}  {item.reason}")
        for row in summary.red:
            name = f"{row['path']}::{row['test']}" if row["test"] else row["path"]
            click.echo(f"  {row['outcome']:<15} {name}  {row['reason']}")
        if out_path is not None:
            click.echo(f"  results: {out_path}")
    if review_id:
        click.echo(f"  {_record_on_review(review_id, summary)}")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


@proof_group.command("report")
@click.option("--plan", "plan_path", type=click.Path(path_type=Path, dir_okay=False, exists=True), required=True)
@click.option("--results", "results_path", type=click.Path(path_type=Path, dir_okay=False), default=None)
@click.option("--repo", "repo_dir", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--no-replies", is_flag=True, help="Post the check run only, no thread replies.")
@click.option("--dry-run", is_flag=True, help="Print what would be posted; post nothing.")
@click.option("--json", "as_json", is_flag=True)
def proof_report(
    plan_path: Path,
    results_path: Path | None,
    repo_dir: Path | None,
    no_replies: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Post the LemonCrow Proof check run and the thread replies. Runs no pull request code."""

    from lemoncrow.pro.capabilities.review.proof import report as report_mod
    from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

    try:
        plan, plan_raw = wire.load(plan_path, wire.PLAN_SCHEMA)
    except wire.ProofFileError as exc:
        raise _CouldNotRun(str(exc)) from exc
    results: dict[str, Any] | None = None
    missing = ""
    if results_path is not None and results_path.is_file():
        try:
            results, _ = wire.load(results_path, wire.RESULTS_SCHEMA)
        except wire.ProofFileError as exc:
            missing = f"the results file was refused: {exc}"
    else:
        missing = "no results file: the run job did not finish"
    summary = report_mod.summarize(plan, plan_raw, results)
    if missing:
        summary.notes.append(missing)
    repository = str(plan.get("repository") or os.environ.get("GITHUB_REPOSITORY") or "")
    number = wire.count(plan.get("pull_request"))
    if dry_run:
        payload = report_mod.check_payload(summary, details_url=_actions_run_url())
        replies = [
            {"thread_id": item.plan.get("thread_id"), "body": report_mod.thread_reply(item, summary)}
            for item in summary.findings
            if wire.count(item.plan.get("thread_id"))
        ]
        click.echo(json.dumps({"check_run": payload, "replies": replies}, indent=2, sort_keys=True))
        return
    if not repository or "/" not in repository:
        raise _CouldNotRun("the plan names no repository (and GITHUB_REPOSITORY is not set)")
    repo = repo_dir or Path.cwd()  # gh needs no checkout here: every call names the repository
    try:
        posted = report_mod.post(
            summary,
            repository=repository,
            number=number,
            host=str(plan.get("host") or "github.com"),
            runner=_gh_runner(repo),
            details_url=_actions_run_url(),
            replies=not no_replies,
        )
    except GitHubCliError as exc:
        raise _CouldNotRun(f"GitHub refused the check run: {exc}") from exc
    if as_json:
        click.echo(json.dumps(posted, indent=2, sort_keys=True))
        return
    click.echo(f"{wire.CHECK_NAME}: {posted['conclusion']}  {summary.title()}  {posted['check_url']}")
    for reply in posted["replies"]:
        click.echo(f"  replied on {reply['finding_id']}: {reply['url']}")
    for error in posted["errors"]:
        click.echo(f"  ! {error}", err=True)


__all__ = ["proof_group"]
