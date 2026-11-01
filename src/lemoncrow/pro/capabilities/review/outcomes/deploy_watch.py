"""Post-deploy watch (C11, launch P3.3): what production saw in a window after one deployed SHA.

The self-serve counterpart of the customer-worker observation seam, for the
GitHub Action on the customer's own keys. One run takes a deployed SHA, a
deploy time and a window, and asks:

* **GitHub** (``gh api``): the pull request that introduced the SHA and the
  files it changed.
* **Sentry**: the issues first seen in the release (the SHA, or a release name
  template) within the window, each with its event and user counts, culprit and
  frames of its latest event. An issue is **linked** only when one of its stack
  frames names a file the pull request changed.
* **Prometheus**: one PromQL error-rate template, averaged over the window
  before and after the deploy, per route the pull request touched (from a
  file-glob to route mapping) or for the overall series. A change is a signal
  only when it crosses a configured threshold.

The outcome is exactly one of ``signal``, ``no_new_signal`` (every configured
connector answered and nothing linked or crossed: "nothing new observed", never
"healthy") or ``not_observed`` (no connector, a missing credential, an API
error, no data, or the window has not elapsed): never a verdict.

Telemetry requests are GET only, never follow a redirect, and send a credential
only to the base URL it was configured with. Production text (culprits,
function names, file paths) is untrusted: it is clipped, collapsed to one line
and rendered inside code spans; issue titles and messages are never read.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import quote, urlencode, urlsplit

from lemoncrow.pro.capabilities.review.freshness import format_timestamp
from lemoncrow.pro.capabilities.review.outcomes.observability import _base_url
from lemoncrow.pro.capabilities.review.outcomes.observability_connectors import (
    DEFAULT_TIMEOUT_SECONDS,
    ConnectorFailure,
    HttpGet,
    _call,
    _json,
    urllib_get,
)
from lemoncrow.pro.capabilities.review.sources.github import GhRunner, GitHubCliError, gh_json, gh_paginated

Outcome = Literal["signal", "no_new_signal", "not_observed"]
ConnectorStatus = Literal["answered", "not_observed"]

DEFAULT_WINDOW_SECONDS: Final[int] = 2 * 3600
MIN_WINDOW_SECONDS: Final[int] = 5 * 60
MAX_WINDOW_SECONDS: Final[int] = 24 * 3600
# lc-debt: one page of Sentry issues (the most frequent 25) and their latest events; a release with more new
# issues is reported as "at least 25" and the rest are not linked. Upgrade path: follow Sentry's Link cursor.
MAX_ISSUES: Final[int] = 25
MAX_ROUTES: Final[int] = 10
MAX_TEXT: Final[int] = 120
COMMENT_MARKER_PREFIX: Final[str] = "<!-- lemoncrow:watch-deploy"
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_SLUG = re.compile(r"^[\w.-]+/[\w.-]+$")
_RELEASE = re.compile(r"^[\w.+/@:-]{1,200}$")
_WINDOW = re.compile(r"^(\d{1,5})\s*([mh])$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:/+", re.IGNORECASE)
_URL_CHARS = re.compile(r"^https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]{1,500}$")
_VENDORED = ("site-packages/", "dist-packages/", "node_modules/")
_REASONS: Final[Mapping[str, str]] = {
    "unauthorized": "the credential was refused",
    "http_error": "the API answered with an HTTP error",
    "timeout": "the request timed out",
    "connection_failed": "the endpoint could not be reached",
    "unparseable": "the answer could not be read",
    "no_data": "the query returned no data before or after the deploy",
}


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def parse_window(text: str) -> int:
    """``"90m"`` / ``"2h"`` -> seconds, between 5 minutes and 24 hours."""

    match = _WINDOW.match(text.strip().lower())
    if match is None:
        raise ValueError("the window must look like 90m or 2h")
    seconds = int(match[1]) * (3600 if match[2] == "h" else 60)
    if not MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS:
        raise ValueError("the window must be between 5m and 24h")
    return seconds


def duration_label(seconds: int) -> str:
    return f"{seconds // 3600}h" if seconds % 3600 == 0 else f"{seconds // 60}m"


def validate_sha(sha: str) -> str:
    text = sha.strip().lower()
    if not _SHA.match(text):
        raise ValueError("--sha must be a hex commit SHA (7 to 40 characters)")
    return text


def validate_slug(slug: str) -> str:
    text = slug.strip()
    if not _SLUG.match(text):
        raise ValueError("--repo must be owner/name")
    return text


def validate_base_url(raw: str, *, flag: str) -> str:
    """https, or plain http for a loopback endpoint; no credentials, query or fragment in the URL."""

    return _base_url(raw, where=flag, locus="local")


@dataclass(frozen=True)
class Window:
    deployed_at: datetime
    seconds: int = DEFAULT_WINDOW_SECONDS

    @property
    def end(self) -> datetime:
        return self.deployed_at + timedelta(seconds=self.seconds)

    @property
    def label(self) -> str:
        return duration_label(self.seconds)

    def to_wire(self) -> dict[str, Any]:
        return {
            "deployed_at": format_timestamp(self.deployed_at),
            "ends_at": format_timestamp(self.end),
            "seconds": self.seconds,
        }


@dataclass(frozen=True)
class SentryTarget:
    base_url: str
    organization: str
    project: str
    token_env: str = "SENTRY_AUTH_TOKEN"
    release_template: str = "{sha}"
    environment: str = ""

    def release(self, sha: str) -> str:
        name = self.release_template.replace("{sha}", sha).replace("{short_sha}", sha[:7])
        if not _RELEASE.match(name):
            raise ValueError("the Sentry release name must be 1-200 characters of letters, digits and ._-+/@:")
        return name


@dataclass(frozen=True)
class RouteMap:
    """``glob=route``: a changed file matching the glob touches the route."""

    pattern: str
    route: str

    @classmethod
    def parse(cls, spec: str) -> RouteMap:
        pattern, sep, route = spec.partition("=")
        if not sep or not pattern.strip() or not route.strip():
            raise ValueError(f"--route {spec!r} must look like 'src/api/users/*=/api/users'")
        return cls(pattern.strip(), route.strip())


@dataclass(frozen=True)
class PrometheusTarget:
    base_url: str
    query: str
    """An error-rate PromQL template; ``{route}`` becomes a regex (match it with ``=~``), ``.*`` for overall."""
    token_env: str = ""
    max_increase: float | None = None
    """The absolute rise of the averaged value (after minus before) that counts as a signal."""
    routes: tuple[RouteMap, ...] = ()


# --------------------------------------------------------------------------- #
# The change behind the SHA
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Change:
    number: int
    url: str
    title: str
    state: str
    merged: bool
    files: tuple[str, ...] = ()
    files_error: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "title": self.title,
            "state": self.state,
            "merged": self.merged,
            "files": list(self.files),
            "files_error": self.files_error,
        }


@dataclass(frozen=True)
class ChangeLookup:
    change: Change | None
    note: str = ""
    """Said plainly when no pull request is known: why nothing can be linked."""


def _clip(text: object, limit: int = MAX_TEXT) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def find_change(runner: GhRunner, slug: str, sha: str) -> ChangeLookup:
    """The pull request that introduced *sha* and its changed files, through ``gh api``."""

    short = sha[:7]
    try:
        pulls = gh_json(
            runner, ["api", "-H", "Accept: application/vnd.github+json", f"repos/{slug}/commits/{sha}/pulls"]
        )
    except GitHubCliError as exc:
        message = str(exc)
        if "422" in message or "No commit found" in message:
            return ChangeLookup(
                None, f"GitHub does not know commit `{short}` in {slug}; no pull request can be linked."
            )
        return ChangeLookup(None, f"The pull request lookup failed ({_clip(message)}); nothing can be linked.")
    rows = [item for item in pulls if isinstance(item, Mapping)] if isinstance(pulls, list) else []
    if not rows:
        return ChangeLookup(
            None,
            f"No pull request in {slug} contains `{short}` (a direct push, a revert pushed without a pull request, "
            "or an unrelated SHA); nothing can be linked to a change.",
        )
    chosen = (
        next((item for item in rows if str(item.get("merge_commit_sha") or "").lower() == sha), None)
        or next((item for item in rows if item.get("merged_at")), None)
        or rows[0]
    )
    try:
        number = int(chosen.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        return ChangeLookup(None, "GitHub returned a pull request without a number; nothing can be linked.")
    files: list[str] = []
    files_error = ""
    try:
        for item in gh_paginated(runner, f"repos/{slug}/pulls/{number}/files?per_page=100"):
            for key in ("filename", "previous_filename"):
                name = str(item.get(key) or "").strip()
                if name and name not in files:
                    files.append(name)
    except GitHubCliError as exc:
        files_error = f"its changed files could not be read ({_clip(str(exc))})"
    return ChangeLookup(
        Change(
            number=number,
            url=str(chosen.get("html_url") or ""),
            title=_clip(chosen.get("title"), 200),
            state=str(chosen.get("state") or ""),
            merged=bool(chosen.get("merged_at")),
            files=tuple(files),
            files_error=files_error,
        )
    )


# --------------------------------------------------------------------------- #
# Sentry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Frame:
    path: str
    function: str = ""
    line: int = 0
    in_app: bool = False
    abs_path: str = ""

    @property
    def label(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where} in {self.function}" if self.function else where

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "abs_path": self.abs_path,
            "function": self.function,
            "line": self.line,
            "in_app": self.in_app,
        }


@dataclass(frozen=True)
class SentryIssue:
    id: str
    short_id: str
    kind: str
    """The exception type (``metadata.type``), never the message."""
    culprit: str
    count: int
    users: int
    first_seen: str
    permalink: str = ""
    top_frame: Frame | None = None
    linked_frame: Frame | None = None
    linked_file: str = ""
    frames_error: str = ""

    @property
    def linked(self) -> bool:
        return bool(self.linked_file)

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_id": self.short_id,
            "type": self.kind,
            "culprit": self.culprit,
            "count": self.count,
            "users": self.users,
            "first_seen": self.first_seen,
            "permalink": self.permalink,
            "top_frame": None if self.top_frame is None else self.top_frame.to_wire(),
            "linked": self.linked,
            "linked_file": self.linked_file,
            "linked_frame": None if self.linked_frame is None else self.linked_frame.to_wire(),
            "frames_error": self.frames_error,
        }


@dataclass(frozen=True)
class SentryResult:
    status: ConnectorStatus
    reason: str = ""
    release: str = ""
    issues: tuple[SentryIssue, ...] = ()
    truncated: bool = False

    @property
    def linked(self) -> tuple[SentryIssue, ...]:
        return tuple(issue for issue in self.issues if issue.linked)

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "release": self.release,
            "issues": [issue.to_wire() for issue in self.issues],
            "linked": len(self.linked),
            "truncated": self.truncated,
        }


def normalize_frame_path(path: str) -> str:
    text = str(path or "").strip().split("?", 1)[0].replace("\\", "/")
    text = _SCHEME.sub("", text)
    while text.startswith(("./", "~/")):
        text = text[2:]
    return text.lstrip("/")


def _path_matches(raw: str, changed: str, *, in_app: bool) -> bool:
    path = normalize_frame_path(raw)
    target = normalize_frame_path(changed)
    if not path or not target or any(part in f"/{path}" for part in _VENDORED):
        return False
    if path == target:
        return True
    if path.endswith("/" + target) and ("/" in target or in_app):
        return True
    return "/" in path and target.endswith("/" + path)


def frame_matches(frame: Frame, changed: str) -> bool:
    """Whether a stack frame's file (``filename`` or ``absPath``) is the changed repository file *changed*.

    Equal paths match; so does a deployed absolute path ending in the repository
    path (``/srv/app/src/x.py`` vs ``src/x.py``) and a frame relative to a source
    root (``api/x.py`` vs ``src/api/x.py``). A vendored frame never matches, and a
    root-level changed file matches a longer frame path only in an in-app frame.
    """

    return any(_path_matches(raw, changed, in_app=frame.in_app) for raw in (frame.path, frame.abs_path) if raw)


def _int(value: object) -> int:
    try:
        return max(0, min(int(str(value or "0")), 10**12))
    except ValueError:
        return 0


def _frames(event: Any) -> list[Frame]:
    """Every frame of the event's exception (and bare stacktrace) entries, oldest first."""

    frames: list[Frame] = []
    entries = event.get("entries") if isinstance(event, Mapping) else None
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, Mapping) or entry.get("type") not in {"exception", "stacktrace"}:
            continue
        data = entry.get("data")
        if not isinstance(data, Mapping):
            continue
        values = data.get("values") if entry.get("type") == "exception" else [{"stacktrace": data}]
        for value in values if isinstance(values, list) else ():
            stack = value.get("stacktrace") if isinstance(value, Mapping) else None
            raw_frames = stack.get("frames") if isinstance(stack, Mapping) else None
            for raw in raw_frames if isinstance(raw_frames, list) else ():
                if not isinstance(raw, Mapping):
                    continue
                absolute = _clip(raw.get("absPath"), 300)
                path = _clip(raw.get("filename"), 300) or absolute
                if path:
                    frames.append(
                        Frame(
                            path=path,
                            function=_clip(raw.get("function")),
                            line=_int(raw.get("lineNo")),
                            in_app=bool(raw.get("inApp")),
                            abs_path=absolute if absolute != path else "",
                        )
                    )
    return frames


def _same_site(url: str, base_url: str) -> str:
    """*url* when it is https on the Sentry host (or a subdomain of it); else ``""``."""

    if not _URL_CHARS.match(url):
        return ""  # no spaces, brackets or parentheses: the link cannot break out of its Markdown
    parts, base = urlsplit(url), urlsplit(base_url)
    host, base_host = (parts.hostname or "").lower(), (base.hostname or "").lower()
    if parts.scheme not in {"https", base.scheme} or not host or not base_host:
        return ""
    return url if host == base_host or host.endswith("." + base_host) else ""


def watch_sentry(
    target: SentryTarget,
    *,
    sha: str,
    window: Window,
    files: Sequence[str],
    environ: Mapping[str, str],
    http_get: HttpGet = urllib_get,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> SentryResult:
    release = target.release(sha)
    token = str(environ.get(target.token_env) or "")
    if not token:
        return SentryResult("not_observed", f"${target.token_env} is not set", release)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    start, end = format_timestamp(window.deployed_at), format_timestamp(window.end)
    params = {
        "query": f'firstRelease:"{release}" firstSeen:>={start[:-1]} firstSeen:<={end[:-1]}',
        "start": start,
        "end": end,
        "sort": "freq",
        "limit": str(MAX_ISSUES),
    }
    if target.environment:
        params["environment"] = target.environment
    org, project = quote(target.organization, safe=""), quote(target.project, safe="")
    url = f"{target.base_url}/api/0/projects/{org}/{project}/issues/?{urlencode(params)}"
    release_url = f"{target.base_url}/api/0/organizations/{org}/releases/{quote(release, safe='')}/"
    try:
        # An unknown release would list no issue at all: that is a misnamed release, not "nothing new".
        status, body = _call(http_get, release_url, headers, timeout)
        if status == 404:
            return SentryResult(
                "not_observed",
                f"Sentry has no release `{release}`; set --sentry-release to the name the app reports",
                release,
            )
        _json(status, body)
        document = _json(*_call(http_get, url, headers, timeout))
        if not isinstance(document, list):
            raise ConnectorFailure("unparseable")
    except ConnectorFailure as exc:
        return SentryResult("not_observed", f"Sentry: {_REASONS.get(exc.reason, exc.reason)}", release)
    issues: list[SentryIssue] = []
    for raw in [item for item in document if isinstance(item, Mapping)][:MAX_ISSUES]:
        identifier = str(raw.get("id") or "")
        if not identifier.isdigit():
            continue
        frames: list[Frame] = []
        frames_error = ""
        try:
            event = _json(
                *_call(
                    http_get,
                    f"{target.base_url}/api/0/organizations/{org}/issues/{identifier}/events/latest/",
                    headers,
                    timeout,
                )
            )
            frames = _frames(event)
        except ConnectorFailure as exc:
            frames_error = _REASONS.get(exc.reason, exc.reason)
        linked_frame, linked_file = None, ""
        for frame in reversed(frames):  # the crash site first
            hit = next((name for name in files if frame_matches(frame, name)), "")
            if hit:
                linked_frame, linked_file = frame, hit
                break
        metadata = raw.get("metadata")
        kind = str(metadata.get("type") or "") if isinstance(metadata, Mapping) else ""
        issues.append(
            SentryIssue(
                id=identifier,
                short_id=_clip(raw.get("shortId"), 40),
                kind=_clip(kind, 80),
                culprit=_clip(raw.get("culprit")),
                count=_int(raw.get("count")),
                users=_int(raw.get("userCount")),
                first_seen=str(raw.get("firstSeen") or "")[:40],
                permalink=_same_site(str(raw.get("permalink") or ""), target.base_url),
                top_frame=next((frame for frame in reversed(frames) if frame.in_app), frames[-1] if frames else None),
                linked_frame=linked_frame,
                linked_file=linked_file,
                frames_error=frames_error,
            )
        )
    issues.sort(key=lambda item: (not item.linked, -item.count, item.id))
    return SentryResult("answered", "", release, tuple(issues), truncated=len(document) >= MAX_ISSUES)


# --------------------------------------------------------------------------- #
# Prometheus
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Series:
    label: str
    route: str
    before: float | None
    after: float | None
    crossed: bool | None
    """``None`` when no threshold is configured or a side has no data: not judged."""

    @property
    def delta(self) -> float | None:
        return None if self.before is None or self.after is None else self.after - self.before

    def to_wire(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "route": self.route,
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
            "crossed": self.crossed,
        }


@dataclass(frozen=True)
class PrometheusResult:
    status: ConnectorStatus
    reason: str = ""
    series: tuple[Series, ...] = ()
    threshold: float | None = None
    note: str = ""

    @property
    def crossed(self) -> tuple[Series, ...]:
        return tuple(item for item in self.series if item.crossed)

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "series": [item.to_wire() for item in self.series],
            "threshold": self.threshold,
            "note": self.note,
        }


def touched_routes(routes: Sequence[RouteMap], files: Sequence[str]) -> list[str]:
    out: list[str] = []
    for mapping in routes:
        if mapping.route not in out and any(fnmatch.fnmatchcase(name, mapping.pattern) for name in files):
            out.append(mapping.route)
    return out[:MAX_ROUTES]


def _promql_regex(route: str) -> str:
    return re.escape(route).replace("\\", "\\\\").replace('"', '\\"')


def _instant(
    target: PrometheusTarget, token: str, promql: str, at: datetime, http_get: HttpGet, timeout: float
) -> float | None:
    params = urlencode({"query": promql, "time": f"{at.timestamp():.3f}"})
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    document = _json(*_call(http_get, f"{target.base_url}/api/v1/query?{params}", headers, timeout))
    data = document.get("data") if isinstance(document, Mapping) else None
    if not isinstance(document, Mapping) or document.get("status") != "success" or not isinstance(data, Mapping):
        raise ConnectorFailure("unparseable")
    result = data.get("result")
    if data.get("resultType") == "scalar":
        result = [{"value": result}]
    for item in result if isinstance(result, list) else ():
        value = item.get("value") if isinstance(item, Mapping) else None
        try:
            number = float(value[1]) if isinstance(value, list) and len(value) == 2 else None
        except (TypeError, ValueError):
            number = None
        if number is not None and math.isfinite(number):
            return number
    return None


def watch_prometheus(
    target: PrometheusTarget,
    *,
    window: Window,
    files: Sequence[str],
    environ: Mapping[str, str],
    http_get: HttpGet = urllib_get,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PrometheusResult:
    token = str(environ.get(target.token_env) or "") if target.token_env else ""
    if target.token_env and not token:
        return PrometheusResult("not_observed", f"${target.token_env} is not set", threshold=target.max_increase)
    note = ""
    routes = touched_routes(target.routes, files) if "{route}" in target.query else []
    if target.routes and "{route}" in target.query and not routes:
        note = "The pull request touched no mapped route; showing the overall series."
    subjects = [(f"route {route}", route) for route in routes] or [("overall", "")]
    series: list[Series] = []
    try:
        for label, route in subjects:
            query = target.query.replace("{route}", _promql_regex(route) if route else ".*")
            promql = f"avg_over_time(({query})[{window.seconds}s:])"
            before = _instant(target, token, promql, window.deployed_at, http_get, timeout)
            after = _instant(target, token, promql, window.end, http_get, timeout)
            crossed = (
                None
                if target.max_increase is None or before is None or after is None
                else after - before > target.max_increase
            )
            series.append(Series(label, route, before, after, crossed))
    except ConnectorFailure as exc:
        return PrometheusResult(
            "not_observed", f"Prometheus: {_REASONS.get(exc.reason, exc.reason)}", threshold=target.max_increase
        )
    if all(item.before is None and item.after is None for item in series):
        return PrometheusResult(
            "not_observed", f"Prometheus: {_REASONS['no_data']}", tuple(series), target.max_increase, note
        )
    return PrometheusResult("answered", "", tuple(series), target.max_increase, note)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WatchReport:
    sha: str
    slug: str
    window: Window
    observed_at: datetime
    lookup: ChangeLookup
    sentry: SentryResult | None = None
    prometheus: PrometheusResult | None = None
    outcome: Outcome = "not_observed"
    headline: str = ""
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def change(self) -> Change | None:
        return self.lookup.change

    def to_wire(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "repository": self.slug,
            "outcome": self.outcome,
            "headline": self.headline,
            "reasons": list(self.reasons),
            "window": self.window.to_wire(),
            "observed_at": format_timestamp(self.observed_at),
            "pull_request": None if self.change is None else self.change.to_wire(),
            "pull_request_note": self.lookup.note,
            "sentry": None if self.sentry is None else self.sentry.to_wire(),
            "prometheus": None if self.prometheus is None else self.prometheus.to_wire(),
            "read_only": True,
        }


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def decide(
    *,
    sha: str,
    slug: str,
    window: Window,
    now: datetime,
    lookup: ChangeLookup,
    configured: bool,
    sentry: SentryResult | None = None,
    prometheus: PrometheusResult | None = None,
    configuration_errors: Sequence[str] = (),
) -> WatchReport:
    """Exactly one outcome; ``not_observed`` whenever any configured connector did not answer."""

    label = window.label
    report = WatchReport(
        sha=sha, slug=slug, window=window, observed_at=now, lookup=lookup, sentry=sentry, prometheus=prometheus
    )
    reasons = list(configuration_errors)
    if not configured and not reasons:
        reasons.append("no telemetry connector is configured (Sentry or Prometheus)")
    if not reasons and now < window.end:
        reason = f"the {label} window closes at {_when(window.end)}; rerun after it"
        return replace(report, headline=f"Not observed yet: {reason}.", reasons=(reason,))
    reasons += [item.reason for item in (sentry, prometheus) if item is not None and item.status != "answered"]
    linked = sentry.linked if sentry is not None else ()
    crossed = prometheus.crossed if prometheus is not None else ()
    parts: list[str] = []
    if linked:
        verb = "touches a file" if len(linked) == 1 else "touch files"
        parts.append(f"{_plural(len(linked), 'new issue')} in this release {verb} this pull request changed")
    if crossed:
        routes = ", ".join(item.label for item in crossed)
        parts.append(f"the error rate rose past the configured threshold ({routes})")
    if parts:
        headline = f"Signal in the {label} after the deploy: " + "; ".join(parts) + "."
        return replace(report, outcome="signal", headline=headline, reasons=tuple(reasons))
    if reasons:
        headline = "Not observed: " + "; ".join(reasons) + ". This is not a verdict."
        return replace(report, headline=headline, reasons=tuple(reasons))
    unlinked = len(sentry.issues) if sentry is not None else 0
    if unlinked:
        more = "at least " if sentry is not None and sentry.truncated else ""
        headline = (
            f"Nothing linked to this change observed in {label}: {more}{_plural(unlinked, 'new issue')} first seen "
            "in this release, none in files this pull request changed."
        )
    else:
        headline = f"Nothing new observed in {label} after the deploy."
    return replace(report, outcome="no_new_signal", headline=headline)


def _when(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _code(text: str) -> str:
    """Untrusted production text as one inline code span (no backticks, pipes or newlines survive)."""

    clean = _clip(text).replace("`", "'").replace("|", "/")
    return f"`{clean}`" if clean else ""


def _number(value: float | None) -> str:
    return "no data" if value is None else f"{value:.4g}"


def comment_marker(sha: str) -> str:
    return f"{COMMENT_MARKER_PREFIX} sha={sha} -->"


def render_markdown(report: WatchReport) -> str:
    short = report.sha[:7]
    lines = [
        f"### LemonCrow production watch: `{short}`",
        "",
        f"**Outcome: `{report.outcome}`**: {report.headline}",
        "",
        f"Deployed {_when(report.window.deployed_at)}; window {report.window.label} "
        f"(to {_when(report.window.end)}); checked {_when(report.observed_at)}.",
    ]
    change = report.change
    if change is not None:
        title = _code(change.title)
        state = "merged" if change.merged else change.state or "unknown state"
        lines.append(
            f"Introduced by pull request #{change.number} {title} ({state}), {_plural(len(change.files), 'changed file')}."
        )
        if change.files_error:
            lines.append(f"Linking is limited: {change.files_error}.")
    else:
        lines.append(report.lookup.note)
    sentry = report.sentry
    if sentry is not None:
        lines += ["", f"#### Sentry, release `{sentry.release}`" if sentry.release else "#### Sentry"]
        if sentry.status != "answered":
            lines.append(f"Not observed: {sentry.reason}.")
        elif not sentry.issues:
            lines.append(f"No issue was first seen in this release in the {report.window.label} window.")
        else:
            if sentry.truncated:
                lines.append(f"Showing the {MAX_ISSUES} most frequent new issues; there may be more.")
            lines += ["", "| Issue | Events | Users | Where | Link to this change |", "| --- | --- | --- | --- | --- |"]
            for issue in sentry.issues:
                name = _code(issue.short_id or issue.id)
                if issue.permalink:
                    name = f"[{name}]({issue.permalink})"
                if issue.kind:
                    name += f" {_code(issue.kind)}"
                frame = issue.linked_frame or issue.top_frame
                where = _code(frame.label) if frame is not None else _code(issue.culprit) or "unknown"
                if issue.linked:
                    link = f"**linked**: {_code(issue.linked_file)}"
                elif issue.frames_error:
                    link = f"not linked (stack unavailable: {issue.frames_error})"
                else:
                    link = "in this release, not linked to its files"
                lines.append(f"| {name} | {issue.count} | {issue.users} | {where} | {link} |")
    prometheus = report.prometheus
    if prometheus is not None:
        lines += ["", f"#### Prometheus error rate, averaged over {report.window.label} before and after"]
        if prometheus.status != "answered":
            lines.append(f"Not observed: {prometheus.reason}.")
        if prometheus.series and prometheus.status == "answered":
            if prometheus.note:
                lines.append(prometheus.note)
            lines += ["", "| Series | Before | After | Change |", "| --- | --- | --- | --- |"]
            for item in prometheus.series:
                delta = item.delta
                change_text = "not comparable" if delta is None else f"{delta:+.4g}"
                if item.crossed:
                    change_text += " (**past the threshold**)"
                lines.append(
                    f"| {_code(item.label)} | {_number(item.before)} | {_number(item.after)} | {change_text} |"
                )
            if prometheus.threshold is None:
                lines.append("\nNo threshold is configured, so these numbers are not judged.")
            else:
                lines.append(f"\nThreshold: a rise of more than {prometheus.threshold:g}.")
    lines += [
        "",
        "<sub>Read-only: LemonCrow sent GET requests to the configured telemetry endpoints and changed nothing. "
        "A missing or failed signal is `not_observed`, never a health verdict.</sub>",
    ]
    return "\n".join(lines) + "\n"


def watch_deploy(
    *,
    sha: str,
    slug: str,
    window: Window,
    lookup: ChangeLookup,
    sentry: SentryTarget | None,
    prometheus: PrometheusTarget | None,
    environ: Mapping[str, str],
    now: datetime | None = None,
    http_get: HttpGet = urllib_get,
    configuration_errors: Sequence[str] = (),
) -> WatchReport:
    """Query the configured connectors (only once the window has elapsed) and decide the outcome."""

    instant = now or datetime.now(UTC)
    files = lookup.change.files if lookup.change is not None else ()
    sentry_result: SentryResult | None = None
    prometheus_result: PrometheusResult | None = None
    if instant >= window.end:
        if sentry is not None:
            sentry_result = watch_sentry(
                sentry, sha=sha, window=window, files=files, environ=environ, http_get=http_get
            )
        if prometheus is not None:
            prometheus_result = watch_prometheus(
                prometheus, window=window, files=files, environ=environ, http_get=http_get
            )
    return decide(
        sha=sha,
        slug=slug,
        window=window,
        now=instant,
        lookup=lookup,
        configured=sentry is not None or prometheus is not None,
        sentry=sentry_result,
        prometheus=prometheus_result,
        configuration_errors=configuration_errors,
    )


# --------------------------------------------------------------------------- #
# The pull-request comment (updated in place) and the local Review evidence
# --------------------------------------------------------------------------- #


def _viewer(runner: GhRunner) -> str:
    try:
        user = gh_json(runner, ["api", "user"])
    except GitHubCliError:
        return ""  # an Actions installation token cannot read /user; its comments are typed Bot
    return str(user.get("login") or "") if isinstance(user, Mapping) else ""


def upsert_comment(runner: GhRunner, slug: str, number: int, sha: str, body: str) -> tuple[str, str]:
    """Create, or update in place, this SHA's watch comment on the pull request: ``(action, html_url)``.

    Only a comment carrying the marker *and* written by this identity (a Bot, or
    the signed-in user) is adopted, so a person quoting the marker is never edited.
    """

    marker = comment_marker(sha)
    viewer = _viewer(runner)
    comments = gh_paginated(runner, f"repos/{slug}/issues/{number}/comments?per_page=100")
    ours = [
        item
        for item in comments
        if marker in str(item.get("body") or "")
        and isinstance(item.get("user"), Mapping)
        and (item["user"].get("type") == "Bot" or (viewer and item["user"].get("login") == viewer))
    ]
    payload = json.dumps({"body": f"{marker}\n{body}"})
    if ours:
        endpoint, method, action = f"repos/{slug}/issues/comments/{int(ours[0]['id'])}", "PATCH", "updated"
    else:
        endpoint, method, action = f"repos/{slug}/issues/{number}/comments", "POST", "created"
    posted = gh_json(runner, ["api", "--method", method, endpoint, "--input", "-"], payload)
    return action, (str(posted.get("html_url") or "") if isinstance(posted, Mapping) else "")


# lc-debt: the local Review lookup reads the newest 200 Reviews of the store (and their Snapshots when no pull
# request matches); an older Review is not found. Upgrade path: an indexed lookup by head SHA / source ref.
MAX_LOCAL_REVIEWS: Final[int] = 200
WATCH_SOURCE_PREFIX: Final[str] = "watch-deploy:"


def find_local_review(store: Any, report: WatchReport) -> tuple[Any, str] | None:
    """``(session, revision_id)`` of the local Review of this pull request or this exact SHA; ``None`` otherwise."""

    from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import merged_revision_id
    from lemoncrow.pro.capabilities.review.sources.github import parse_pr_source_ref

    sessions = store.list_sessions(status="", limit=MAX_LOCAL_REVIEWS)
    change = report.change
    if change is not None:
        owner, _sep, repo = report.slug.partition("/")
        for session in sessions:
            ref = parse_pr_source_ref(str(session.source_ref or ""))
            if ref is not None and (ref.owner, ref.repo, ref.number) == (owner, repo, change.number):
                for revision in store.list_revisions(session.id):
                    if report.sha in {revision.head_sha, revision.commit_sha}:
                        return session, str(revision.id)
                revision_id = merged_revision_id(store, session)
                return (session, revision_id) if revision_id else None
    for session in sessions:
        for revision in store.list_revisions(session.id):
            if report.sha in {revision.head_sha, revision.commit_sha}:
                return session, str(revision.id)
    return None


def record_evidence(store: Any, report: WatchReport) -> str:
    """Append the result to the local Review as ``external_observation`` deployment evidence; the Review id or ``""``.

    Immutable and idempotent: a rerun with the same outcome adds nothing; a
    changed outcome (``not_observed`` -> ``no_new_signal``) adds one more row.
    """

    from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence

    found = find_local_review(store, report)
    if found is None:
        return ""
    session, revision_id = found
    deployed = format_timestamp(report.window.deployed_at)
    source_ref = f"{WATCH_SOURCE_PREFIX}{report.sha}@{deployed}:{report.window.seconds}:{report.outcome}"
    if store.get_evidence_by_source(session.id, "external", source_ref) is not None:
        return str(session.id)
    providers = [name for name, item in (("sentry", report.sentry), ("prometheus", report.prometheus)) if item]
    wire = report.to_wire()
    detail = {
        "freshness_class": "external_observation",
        "observation": True,
        "watch_deploy": True,
        "deployed_commit": report.sha,
        "source_timestamp": format_timestamp(report.observed_at),
        **{key: wire[key] for key in ("outcome", "headline", "reasons", "window", "pull_request_note")},
        "pull_request": None if report.change is None else report.change.number,
        "sentry": wire["sentry"],
        "prometheus": wire["prometheus"],
    }
    store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=session.id,
            revision_id=revision_id,
            kind="deployment",
            title=f"Production watch {report.sha[:7]} ({report.window.label}): {report.outcome}"[:200],
            source="external",
            source_ref=source_ref,
            detail=json.dumps(detail, sort_keys=True),
            provider="+".join(providers),
            provider_revision=report.sha,
            environment="production",
            remote_identity=f"deployment:{report.sha}@{deployed}",
            observed_at=format_timestamp(report.observed_at),
        )
    )
    return str(session.id)


__all__ = [
    "COMMENT_MARKER_PREFIX",
    "DEFAULT_WINDOW_SECONDS",
    "MAX_WINDOW_SECONDS",
    "Change",
    "ChangeLookup",
    "Frame",
    "PrometheusResult",
    "PrometheusTarget",
    "RouteMap",
    "SentryIssue",
    "SentryResult",
    "SentryTarget",
    "Series",
    "WatchReport",
    "Window",
    "comment_marker",
    "decide",
    "duration_label",
    "find_change",
    "find_local_review",
    "frame_matches",
    "normalize_frame_path",
    "parse_window",
    "record_evidence",
    "render_markdown",
    "touched_routes",
    "upsert_comment",
    "validate_base_url",
    "validate_sha",
    "validate_slug",
    "watch_deploy",
    "watch_prometheus",
    "watch_sentry",
]
