"""``lc review qa run``: replay saved browser scenarios against a preview (web QA).

The scenarios are the ``browser_checks`` entries with saved ``steps`` in the
*trusted* ``.lemoncrow/review.yaml`` (read from a git ref, never from the
working tree a pull request controls). Each run drives a real headless Chromium
through Node Playwright against a preview URL that serves one exact head SHA,
writes a sealed report, and optionally:

* ``--review ID`` records it on the local Review's Snapshot of that SHA
  (``POST /reviews/{id}/browser-checks``), where a newer Snapshot makes it stale;
* ``--github-check`` posts a ``LemonCrow QA`` check run on the SHA with
  ``gh api`` (``could_not_run`` is ``neutral``, never ``success``).

Exit status: 0 pass or warning, 1 fail, 2 could not run.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import click

from lemoncrow.pro.capabilities.review import web_qa


class _CouldNotRun(click.ClickException):
    """Setup that stops every scenario: exit 2, the could-not-run code."""

    exit_code = 2


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _CouldNotRun(f"git could not run: {exc}") from exc
    if done.returncode != 0:
        raise _CouldNotRun(f"git {' '.join(args)} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def _store_root(ctx: click.Context) -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    obj = ctx.find_root().obj or {}
    return Path(obj.get("root") or default_store_root())


def _review_config() -> Any:
    from lemoncrow_client.config import load_config as load_client_config

    return load_client_config(product="review")


def _review_request(config: Any, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted import server_review_request

    try:
        return server_review_request(config, method, path, body=body)
    except ClientError as exc:
        raise click.ClickException(f"Review server failed: {exc.message}") from exc


def _upload_screenshot(config: Any, review_id: str, revision_id: str, path: Path, title: str, source: str) -> str:
    """Attach one PNG to the exact Snapshot; ``""`` when the server refuses it."""

    from urllib.error import URLError
    from urllib.request import Request, urlopen

    query = urlencode(
        {"kind": "screenshot", "title": title, "revision_id": revision_id, "source": source, "mime_type": "image/png"}
    )
    request = Request(
        f"{str(config.url).rstrip('/')}/api/reviews/{quote(review_id, safe='')}/evidence/upload?{query}",
        data=path.read_bytes(),
        method="POST",
        headers={"Authorization": f"Bearer {config.token}", "Content-Type": "image/png", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # the configured LemonCrow origin only
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError):
        return ""
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    return str(evidence.get("id") or "") if isinstance(evidence, dict) else ""


def _post_setup_check(reason: str, *, head_sha: str, github_check: bool) -> None:
    if not github_check:
        return
    report = {"scenario": "setup", "title": "Setup", "outcome": "could_not_run", "reasons": [reason], "steps": []}
    payload = web_qa.github_check_payload([report], head_sha=head_sha, details_url=_actions_run_url())
    try:
        web_qa.post_github_check(payload, repository=os.environ.get("GITHUB_REPOSITORY", ""))
    except RuntimeError as exc:
        click.echo(f"  ! {exc}", err=True)


def _actions_run_url() -> str:
    server, repo, run = (
        os.environ.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


@click.group("qa")
def qa_group() -> None:
    """Replay saved browser scenarios against a preview in a real Chromium (web QA)."""


@qa_group.command("run")
@click.option(
    "--url", "base_url", required=True, help="Preview base URL serving --sha: loopback, or a trusted_origins entry."
)
@click.option(
    "--sha", "head_sha", default="", help="Full commit SHA the preview serves (default: HEAD of this checkout)."
)
@click.option("--scenario", "scenario_ids", multiple=True, help="Run only this scenario id (repeatable).")
@click.option(
    "--config-ref",
    default="",
    help="Trusted git ref holding .lemoncrow/review.yaml (default: the bound Snapshot's base commit).",
)
@click.option("--review", "review_id", default="", help="Record each run on this local Review's Snapshot of --sha.")
@click.option("--github-check", is_flag=True, help="Post a 'LemonCrow QA' check run on --sha with gh api.")
@click.option("--playwright-dir", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--playwright-version", default="", help="Install this exact Playwright version and Chromium first (CI).")
@click.option("--with-deps", is_flag=True, help="With --playwright-version: also install Chromium's OS packages.")
@click.option("--out", "out_dir", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--step-timeout-ms", type=click.IntRange(500, 120_000), default=web_qa.DEFAULT_STEP_TIMEOUT_MS)
@click.option("--slow-step-ms", type=click.IntRange(1, 600_000), default=web_qa.DEFAULT_SLOW_STEP_MS)
@click.option("--timeout", "timeout_s", type=click.FloatRange(5, 3600), default=web_qa.DEFAULT_RUN_TIMEOUT_S)
@click.option("--json", "as_json", is_flag=True, help="Print the reports as JSON.")
@click.pass_context
def qa_run(
    ctx: click.Context,
    base_url: str,
    head_sha: str,
    scenario_ids: tuple[str, ...],
    config_ref: str,
    review_id: str,
    github_check: bool,
    playwright_dir: Path | None,
    playwright_version: str,
    with_deps: bool,
    out_dir: Path | None,
    step_timeout_ms: int,
    slow_step_ms: int,
    timeout_s: float,
    as_json: bool,
) -> None:
    """Run saved browser scenarios against a preview of one exact commit.

    Exit status: 0 pass or warning, 1 fail, 2 could not run.
    """

    from lemoncrow.pro.capabilities.review.browser_checks import parse_browser_policy

    cwd = Path.cwd()
    repo = Path(_git(cwd, "rev-parse", "--show-toplevel").strip())
    try:
        sha = web_qa.validate_head_sha(head_sha or _git(repo, "rev-parse", "HEAD").strip())
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--sha") from exc

    def setup_failed(reason: str) -> _CouldNotRun:
        _post_setup_check(reason, head_sha=sha, github_check=github_check)
        return _CouldNotRun(f"could not run: {reason}")

    config: Any = None
    snapshot: dict[str, Any] = {}
    if review_id:
        config = _review_config()
        rows = _review_request(config, "GET", f"/api/reviews/{quote(review_id, safe='')}/revisions").get("revisions")
        found = web_qa.snapshot_for_sha([row for row in rows or () if isinstance(row, dict)], sha)
        if found is None:
            raise setup_failed(f"Review {review_id} has no clean Snapshot of commit {sha[:12]}; capture it first")
        snapshot = dict(found)
    ref = config_ref or str(snapshot.get("base_sha") or "")
    if not ref:
        raise setup_failed("name the trusted base with --config-ref (scenarios are never read from the change itself)")
    try:
        # The report names the exact trusted commit, not a ref that can move.
        ref = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        text = _git(repo, "show", f"{ref}:.lemoncrow/review.yaml")
    except _CouldNotRun as exc:
        raise setup_failed(f"no .lemoncrow/review.yaml at trusted ref {ref}") from exc
    policy = parse_browser_policy(text, source=f"{ref[:12]} .lemoncrow/review.yaml")
    if policy.error:
        raise setup_failed(f"browser_checks in {ref[:12]} .lemoncrow/review.yaml: {policy.error}")
    runnable = [item for item in policy.scenarios if item.steps]
    if scenario_ids:
        known = {item.id: item for item in policy.scenarios}
        unknown = [item for item in scenario_ids if item not in known]
        if unknown:
            raise setup_failed(f"unknown scenario(s) {', '.join(unknown)} in the trusted configuration")
        idle = [item for item in scenario_ids if not known[item].steps]
        if idle:
            raise setup_failed(f"scenario(s) {', '.join(idle)} have no saved steps")
        runnable = [known[item] for item in dict.fromkeys(scenario_ids)]
    if not runnable:
        raise setup_failed("no browser_checks scenario in the trusted configuration has saved steps")

    store_root = _store_root(ctx)
    install_error = ""
    if playwright_version:
        playwright_dir = playwright_dir or store_root / "qa-playwright" / playwright_version
        install_error = web_qa.install_playwright(playwright_version, playwright_dir, with_deps=with_deps)
    else:
        playwright_dir = web_qa.resolve_playwright_dir(playwright_dir, search=(repo / "frontend", repo, cwd))
    if out_dir is None:
        runs_root = store_root / "qa-runs"
        out_dir = runs_root / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{sha[:7]}-{os.getpid()}"
        web_qa.prune_runs(runs_root, keep=49)

    reports: list[dict[str, Any]] = []
    pairs = [(scenario, viewport) for scenario in runnable for viewport in (scenario.viewports or ("",))]
    for scenario, viewport in pairs:
        try:
            report = web_qa.run_scenario(
                scenario,
                policy=policy,
                base_url=base_url,
                head_sha=sha,
                out_dir=out_dir / (f"{scenario.id}@{viewport}" if viewport else scenario.id),
                playwright_dir=playwright_dir,
                viewport=viewport,
                config_ref=ref,
                step_timeout_ms=step_timeout_ms,
                slow_step_ms=slow_step_ms,
                timeout_s=timeout_s,
                setup_error=install_error,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        report_dir = out_dir / (f"{scenario.id}@{viewport}" if viewport else scenario.id)
        bound: dict[str, Any] = {}
        if config is not None:
            shot = report["screenshots"][-1] if report["screenshots"] else None
            evidence_id = (
                _upload_screenshot(
                    config,
                    review_id,
                    str(snapshot["id"]),
                    report_dir / shot["file"],
                    f"QA {scenario.id} {shot['name']}",
                    "test",
                )
                if shot
                else ""
            )
            body = web_qa.review_run_body(
                report, scenario, revision_id=str(snapshot["id"]), source="test", screenshot_evidence_id=evidence_id
            )
            response = _review_request(config, "POST", f"/api/reviews/{quote(review_id, safe='')}/browser-checks", body)
            record = response.get("browser_check") or {}
            bound = {
                "review_id": review_id,
                "snapshot_id": snapshot["id"],
                "snapshot_number": snapshot.get("revision_number"),
                "review_status": record.get("status"),
                "review_reasons": record.get("status_reasons") or [],
            }
        reports.append({"report": report, "report_path": str(report_dir / "report.json"), "review": bound})
        if not as_json:
            _echo_report(report, report_dir, bound)

    overall = web_qa.overall_outcome([item["report"] for item in reports])
    check_url = ""
    if github_check:
        payload = web_qa.github_check_payload(
            [item["report"] for item in reports], head_sha=sha, details_url=_actions_run_url()
        )
        try:
            check_url = web_qa.post_github_check(payload, repository=os.environ.get("GITHUB_REPOSITORY", ""))
        except RuntimeError as exc:
            raise _CouldNotRun(str(exc)) from exc
    if as_json:
        click.echo(json.dumps({"outcome": overall, "runs": reports, "check_url": check_url}, indent=2, sort_keys=True))
    else:
        click.echo(f"QA {overall.upper().replace('_', ' ')}  {len(reports)} run(s)  head {sha[:12]}  {out_dir}")
        if check_url:
            click.echo(f"  check run: {check_url}")
    ctx.exit(web_qa.exit_code(overall))


def _echo_report(report: dict[str, Any], report_dir: Path, bound: dict[str, Any]) -> None:
    label = str(report["outcome"]).upper().replace("_", " ")
    browser = report.get("browser") or {}
    seconds = int(report.get("duration_ms") or 0) / 1000
    click.echo(
        f"{label}  {report['scenario']}  {len(report['steps'])} steps  {seconds:.1f}s  "
        f"chromium {browser.get('version') or '-'}  {report['base_url']}"
    )
    for step in report["steps"]:
        error = f"  {step['error']}" if step.get("error") else ""
        click.echo(
            f"  {step['index']:>2}. {step['status']:<13} {step['description']}  {step.get('duration_ms', 0)} ms{error}"
        )
    for reason in report.get("reasons") or ():
        click.echo(f"  - {reason}")
    for shot in report.get("screenshots") or ():
        click.echo(f"  screenshot {shot['name']}: {report_dir / shot['file']}")
    if bound:
        click.echo(
            f"  Review {bound['review_id']} Snapshot {bound.get('snapshot_number')}: {bound.get('review_status')}"
            + (f" ({'; '.join(bound['review_reasons'])})" if bound.get("review_status") != "PASS" else "")
        )
    click.echo(f"  report: {report_dir / 'report.json'}  {report['digest']}")


__all__ = ["qa_group"]
