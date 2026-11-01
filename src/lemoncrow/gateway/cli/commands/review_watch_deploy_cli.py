"""``lc review watch-deploy``: what production saw in a window after one deployed SHA (read-only).

Finds the pull request that introduced the SHA (``gh api``), asks Sentry for the
issues first seen in its release and Prometheus for one error-rate query before
versus after the deploy, and prints a Markdown report (``--json`` for the
machine-readable one). The outcome is ``signal``, ``no_new_signal`` or
``not_observed``; it is never a health verdict.

``--comment`` writes the report as one comment on that pull request and updates
the same comment on a rerun. When the local Review store holds a Review of that
pull request (or of the exact SHA), the result is also appended to it as
``external_observation`` deployment evidence (``--no-record`` skips it).

Telemetry is only ever read: GET requests to the configured base URLs, which
are the only places a credential is sent. Credentials come from environment
variables named by flags; no output carries one. Exit status 0 for every
outcome; 1 when an asked-for comment could not be written.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from lemoncrow.pro.capabilities.review.outcomes import deploy_watch
from lemoncrow.pro.capabilities.review.sources.github import GhRunner, GitHubCliError


def _runner() -> GhRunner:
    from lemoncrow.pro.capabilities.review.sources.github import run_gh

    def run(args: Sequence[str], stdin: str | None = None) -> str:
        return run_gh(args, stdin)

    return run


def _store_root(ctx: click.Context) -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    obj = ctx.find_root().obj or {}
    return Path(obj.get("root") or default_store_root())


def _slug(explicit: str, runner: GhRunner) -> str:
    raw = explicit or os.environ.get("GITHUB_REPOSITORY", "")
    if not raw:
        try:
            raw = runner(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], None).strip()
        except GitHubCliError as exc:
            raise click.ClickException(f"name the repository with --repo owner/name ({exc})") from exc
    try:
        return deploy_watch.validate_slug(raw)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _deployed_at(text: str) -> datetime:
    if not text.strip():
        return datetime.now(UTC)
    from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

    parsed = parse_timestamp(text)
    if parsed is None:
        raise click.ClickException("--deployed-at must be an ISO 8601 time with a zone, e.g. 2026-10-31T09:00:00Z")
    return parsed


def _sentry(
    url: str, org: str, project: str, token_env: str, release: str, environment: str, errors: list[str]
) -> deploy_watch.SentryTarget | None:
    if not (org or project):
        return None
    if not (org and project):
        errors.append("Sentry needs both --sentry-org and --sentry-project ($SENTRY_ORG, $SENTRY_PROJECT)")
        return None
    try:
        target = deploy_watch.SentryTarget(
            base_url=deploy_watch.validate_base_url(url, flag="--sentry-url"),
            organization=org,
            project=project,
            token_env=token_env,
            release_template=release,
            environment=environment,
        )
        target.release("0" * 40)  # the template itself must be usable
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    return target


def _prometheus(
    url: str, query: str, token_env: str, max_increase: float | None, routes: Sequence[str], errors: list[str]
) -> deploy_watch.PrometheusTarget | None:
    if not (url or query):
        return None
    if not (url and query):
        errors.append("Prometheus needs both --prom-url and --prom-query ($PROMETHEUS_URL, $PROMETHEUS_QUERY)")
        return None
    try:
        return deploy_watch.PrometheusTarget(
            base_url=deploy_watch.validate_base_url(url, flag="--prom-url"),
            query=query,
            token_env=token_env,
            max_increase=max_increase,
            routes=tuple(deploy_watch.RouteMap.parse(spec) for spec in routes),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _record(ctx: click.Context, report: deploy_watch.WatchReport) -> None:
    from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore

    root = _store_root(ctx)
    if not (root / DB_NAME).is_file():
        return  # no local Review store: nothing to record on, and none is created
    try:
        review_id = deploy_watch.record_evidence(ReviewStore(root), report)
    except Exception as exc:  # recording is a side output; the report stands without it
        click.echo(f"could not record the result on the local Review: {type(exc).__name__}", err=True)
        return
    if review_id:
        click.echo(f"recorded on local Review r/{review_id[:8]} as production evidence", err=True)


@click.command("watch-deploy")
@click.option("--sha", required=True, help="The deployed commit SHA.")
@click.option("--deployed-at", default="", help="When it reached production, ISO 8601 with a zone (default: now).")
@click.option("--window", "window_text", default="2h", show_default=True, help="How long after the deploy (max 24h).")
@click.option("--repo", "slug", default="", help="owner/name (default: $GITHUB_REPOSITORY, else this checkout).")
@click.option("--sentry-url", envvar="SENTRY_URL", default="https://sentry.io", show_default=True)
@click.option("--sentry-org", envvar="SENTRY_ORG", default="", help="Sentry organization slug [$SENTRY_ORG].")
@click.option("--sentry-project", envvar="SENTRY_PROJECT", default="", help="Sentry project slug [$SENTRY_PROJECT].")
@click.option("--sentry-token-env", default="SENTRY_AUTH_TOKEN", show_default=True, help="Variable holding the token.")
@click.option(
    "--sentry-release", default="{sha}", show_default=True, help="Release name template ({sha}, {short_sha})."
)
@click.option("--sentry-environment", envvar="SENTRY_ENVIRONMENT", default="", help="Only this Sentry environment.")
@click.option("--prom-url", envvar="PROMETHEUS_URL", default="", help="Prometheus base URL [$PROMETHEUS_URL].")
@click.option(
    "--prom-query",
    envvar="PROMETHEUS_QUERY",
    default="",
    help='Error-rate PromQL; {route} becomes a regex, so match it with =~ (route=~"{route}").',
)
@click.option("--prom-token-env", default="", help="Variable holding a bearer token for Prometheus (optional).")
@click.option(
    "--prom-max-increase", type=float, default=None, help="Rise (after minus before) that counts as a signal."
)
@click.option("--route", "routes", multiple=True, help="GLOB=ROUTE: a changed file matching GLOB touches ROUTE.")
@click.option("--json", "as_json", is_flag=True, help="Print the machine-readable report.")
@click.option("--comment", is_flag=True, help="Write the report on the pull request (updated in place on reruns).")
@click.option("--no-record", is_flag=True, help="Do not append the result to a local Review.")
@click.pass_context
def watch_deploy_cmd(
    ctx: click.Context,
    sha: str,
    deployed_at: str,
    window_text: str,
    slug: str,
    sentry_url: str,
    sentry_org: str,
    sentry_project: str,
    sentry_token_env: str,
    sentry_release: str,
    sentry_environment: str,
    prom_url: str,
    prom_query: str,
    prom_token_env: str,
    prom_max_increase: float | None,
    routes: tuple[str, ...],
    as_json: bool,
    comment: bool,
    no_record: bool,
) -> None:
    """Report what production saw after a deploy of SHA: signal, no_new_signal or not_observed."""

    try:
        commit = deploy_watch.validate_sha(sha)
        window = deploy_watch.Window(_deployed_at(deployed_at), deploy_watch.parse_window(window_text))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    runner = _runner()
    repository = _slug(slug, runner)
    errors: list[str] = []
    sentry = _sentry(
        sentry_url, sentry_org, sentry_project, sentry_token_env, sentry_release, sentry_environment, errors
    )
    prometheus = _prometheus(prom_url, prom_query, prom_token_env, prom_max_increase, routes, errors)
    report = deploy_watch.watch_deploy(
        sha=commit,
        slug=repository,
        window=window,
        lookup=deploy_watch.find_change(runner, repository, commit),
        sentry=sentry,
        prometheus=prometheus,
        environ=os.environ,
        configuration_errors=errors,
    )
    markdown = deploy_watch.render_markdown(report)
    wire: dict[str, Any] = report.to_wire()
    failed = ""
    if comment:
        if report.change is None:
            failed = "no pull request introduced this SHA, so there is nothing to comment on"
        else:
            try:
                action, url = deploy_watch.upsert_comment(runner, repository, report.change.number, commit, markdown)
                wire["comment"] = {"action": action, "url": url}
                click.echo(f"{action} the comment on pull request #{report.change.number} {url}".rstrip(), err=True)
            except GitHubCliError as exc:
                failed = f"the pull request comment could not be written ({exc})"
    if not no_record:
        _record(ctx, report)
    if as_json:
        click.echo(json.dumps(wire, indent=2, sort_keys=True))
    else:
        click.echo(markdown, nl=False)
    if failed:
        click.echo(f"error: {failed}", err=True)
        raise SystemExit(1)


__all__ = ["watch_deploy_cmd"]
