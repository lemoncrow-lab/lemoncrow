"""The GitLab merge-request source: a merge request in a GitLab CI pipeline -> a local commit range.

The GitLab counterpart of :mod:`sources.github`. A merge request is reviewed like any other commit range; this module
answers which range, which durable Review it belongs to (``gitlab:<project path>!<iid>``), and how to talk to GitLab's
REST API v4 at all.

:class:`GitLabApi` is the standard library's ``urllib`` with a ``PRIVATE-TOKEN`` header, nothing else installed. Three
rules keep the token where it belongs:

* it travels only in that header, never in a URL, a log line or an error message (an error that somehow echoes it has it
  replaced before it is raised);
* it is sent only over ``https``, and a redirect is never followed, so it cannot be carried to another host;
* the transport is injectable, so tests answer every call without a network.

Every failure is a :class:`GitLabApiError`, a ``GitHubCliError``, so the bot's existing handling holds unchanged: a 429
says ``(HTTP 429)`` and ``retry after Ns`` (what ``pr_bot.github.with_backoff`` waits on), and a 403 says ``resource not
accessible by integration`` (what ``pr_bot.github.is_denied`` reads as a missing permission, which waiting cannot fix).
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit

from lemoncrow.pro.capabilities.review.sources.github import (
    GhRunner,
    GitHubCliError,
    PullRequestRef,
    PullRequestSource,
)

GITLAB_SOURCE_PREFIX: Final[str] = "gitlab:"
#: The variable the bot's GitLab token is read from: a project (or group) access token, role Developer, scope ``api``.
TOKEN_ENV: Final[str] = "LEMONCROW_GITLAB_TOKEN"
PAGE_SIZE: Final[int] = 100

_TIMEOUT_S: Final[float] = 30.0
_FETCH_TIMEOUT_S: Final[float] = 600.0
_GIT_TIMEOUT_S: Final[float] = 60.0
_MESSAGE_LIMIT: Final[int] = 200
_IID: Final = re.compile(r"^!?(?P<iid>\d+)$")


class GitLabApiError(GitHubCliError):
    """GitLab refused a request or could not be reached. ``status`` is the HTTP status, ``0`` when there was none."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# The merge request's identity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MergeRequestRef(PullRequestRef):
    """The durable identity of one merge request.

    A :class:`PullRequestRef`, so a :class:`PullRequestSource` and every caller typed for a pull request carry it as is.
    ``owner`` is the project's namespace (``group`` or ``group/sub``), ``repo`` its path, ``number`` the merge request's
    iid; ``project_id`` is GitLab's numeric id the API is addressed by, ``server_url`` the instance's web root.
    """

    host: str = "gitlab.com"
    project_id: str = ""
    server_url: str = "https://gitlab.com"

    @property
    def source_ref(self) -> str:
        """One Review per merge request: every new head becomes a revision of it."""
        return f"{GITLAB_SOURCE_PREFIX}{self.slug}!{self.number}"

    @property
    def url(self) -> str:
        return f"{self.server_url.rstrip('/')}/{self.slug}/-/merge_requests/{self.number}"

    @property
    def project_path(self) -> str:
        """The API path of the project (``projects/<url-encoded id>``)."""
        return f"projects/{quote(self.project_id or self.slug, safe='')}"

    @property
    def api_path(self) -> str:
        return f"{self.project_path}/merge_requests/{self.number}"

    def commit_url(self, sha: str) -> str:
        return f"{self.server_url.rstrip('/')}/{self.slug}/-/commit/{sha}"


def merge_request_ref(environ: Mapping[str, str], spec: str = "") -> MergeRequestRef:
    """The merge request of this GitLab CI job, from its predefined variables.

    *spec* is what ``--pr`` was given: empty, or the iid (``7`` or ``!7``); any other merge request is refused, since the
    job's token and checkout belong to this one. Raises ``ValueError`` naming what is missing.
    """

    def var(*names: str) -> str:
        for name in names:
            value = str(environ.get(name) or "").strip()
            if value:
                return value
        return ""

    iid = var("CI_MERGE_REQUEST_IID")
    path = var("CI_MERGE_REQUEST_PROJECT_PATH", "CI_PROJECT_PATH").strip("/")
    project_id = var("CI_MERGE_REQUEST_PROJECT_ID", "CI_PROJECT_ID")
    server_url = var("CI_SERVER_URL").rstrip("/")
    host = var("CI_SERVER_HOST") or urlsplit(server_url).hostname or ""
    if not iid.isdigit():
        raise ValueError("this GitLab job is not in a merge request pipeline (CI_MERGE_REQUEST_IID is not set)")
    if not (path and "/" in path and project_id and server_url and host):
        raise ValueError("the GitLab CI variables CI_PROJECT_PATH, CI_PROJECT_ID and CI_SERVER_URL are not all set")
    wanted = _IID.match(spec.strip()) if spec.strip() else None
    if spec.strip() and (wanted is None or wanted["iid"] != iid):
        raise ValueError(f"--pr {spec!r} is not this pipeline's merge request (!{iid})")
    owner, _, repo = path.rpartition("/")
    return MergeRequestRef(
        owner=owner, repo=repo, number=int(iid), host=host.lower(), project_id=project_id, server_url=server_url
    )


# --------------------------------------------------------------------------- #
# The REST API
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP answer: its status, its headers (names lower-cased) and its body."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


#: ``(method, url, headers, body) -> response``. A non-2xx answer is a response, not an exception; only a failure to
#: get any answer raises (``OSError``, which ``URLError`` and a timeout are).
Transport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the token header would go wherever it points."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
    """The real transport: one ``urllib`` request, redirects refused, a bounded timeout."""

    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_S) as response:
            return HttpResponse(
                int(response.status), {k.lower(): v for k, v in response.headers.items()}, response.read()
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(int(exc.code), {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b"")


def _server_message(body: bytes) -> str:
    """GitLab's own words for a refusal (``{"message": ...}`` or ``{"error": ...}``), one line, bounded."""

    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        found = data.get("message") or data.get("error") or ""
        text = found if isinstance(found, str) else json.dumps(found, ensure_ascii=False)
    return " ".join(text.split())[:_MESSAGE_LIMIT]


def error_for(method: str, path: str, response: HttpResponse) -> GitLabApiError:
    """The error a refused call raises, worded so the bot's GitHub-era checks read it the same way."""

    what = f"GitLab {method} {path.split('?', 1)[0]}"
    said = _server_message(response.body)
    tail = f" ({said})" if said else ""
    status = response.status
    if status == 429:
        wait = str(response.headers.get("retry-after") or "").strip()
        hint = f"; retry after {wait}s" if wait.isdigit() else ""
        return GitLabApiError(f"{what} failed (HTTP 429): too many requests{hint}{tail}", status=status)
    if status == 403:
        return GitLabApiError(
            f"{what} failed (HTTP 403): resource not accessible by integration: the token's role or scope does not "
            f"allow it{tail}",
            status=status,
        )
    if status == 401:
        return GitLabApiError(
            f"{what} failed (HTTP 401): GitLab rejected the token in {TOKEN_ENV} (expired, revoked or mistyped){tail}",
            status=status,
        )
    return GitLabApiError(f"{what} failed (HTTP {status}){tail}", status=status)


class GitLabApi:
    """GitLab's REST API v4 under one base URL and one token.

    *wrap* decorates the one call every request goes through, which has the ``gh`` runner's shape (``(args, stdin) ->
    str``, ``args`` being ``(method, path)``), so ``pr_bot.github.with_backoff`` applies to GitLab unchanged.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: Transport = urllib_transport,
        wrap: Callable[[GhRunner], GhRunner] | None = None,
    ) -> None:
        base = base_url.strip().rstrip("/")
        if urlsplit(base).scheme != "https":
            raise ValueError(
                f"refusing to send the GitLab token to a non-https API URL ({urlsplit(base).scheme or 'none'})"
            )
        if not token.strip():
            raise ValueError(f"no GitLab token: set {TOKEN_ENV}")
        self.base_url = base
        self._token = token.strip()
        self._transport = transport
        #: The headers of the last answer, read for ``X-Next-Page``. Calls are sequential, so it is the call just made.
        self._headers: Mapping[str, str] = {}
        self._call: GhRunner = wrap(self._send) if wrap is not None else self._send

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        transport: Transport = urllib_transport,
        wrap: Callable[[GhRunner], GhRunner] | None = None,
    ) -> GitLabApi:
        """The API of this CI job's instance (``CI_API_V4_URL``) with the bot's token (:data:`TOKEN_ENV`)."""

        base = str(environ.get("CI_API_V4_URL") or "").strip()
        if not base:
            server = str(environ.get("CI_SERVER_URL") or "").strip().rstrip("/")
            base = f"{server}/api/v4" if server else ""
        if not base:
            raise ValueError("CI_API_V4_URL is not set: run this in a GitLab CI job")
        return cls(base, str(environ.get(TOKEN_ENV) or ""), transport=transport, wrap=wrap)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[masked]") if self._token else text

    def _send(self, args: Sequence[str], stdin: str | None = None) -> str:
        method, path = str(args[0]), str(args[1])
        headers = {"PRIVATE-TOKEN": self._token, "Accept": "application/json", "User-Agent": "lemoncrow-review"}
        if stdin is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._transport(
                method, f"{self.base_url}/{path}", headers, stdin.encode("utf-8") if stdin is not None else None
            )
        except OSError as exc:
            raise GitLabApiError(
                self._scrub(
                    f"GitLab {method} {path.split('?', 1)[0]} failed: could not reach GitLab ({type(exc).__name__})"
                )
            ) from exc
        self._headers = {key.lower(): value for key, value in response.headers.items()}
        if not 200 <= response.status < 300:
            error = error_for(method, path, response)
            raise GitLabApiError(self._scrub(str(error)), status=error.status)
        return response.body.decode("utf-8", errors="replace")

    @staticmethod
    def _with_query(path: str, params: Mapping[str, object] | None) -> str:
        if not params:
            return path
        query = urlencode({key: str(value) for key, value in params.items()})
        return f"{path}{'&' if '?' in path else '?'}{query}"

    def request(
        self, method: str, path: str, *, params: Mapping[str, object] | None = None, body: object | None = None
    ) -> Any:
        """One call; the parsed JSON answer, ``None`` for an empty one (a ``204``)."""

        target = self._with_query(path, params)
        raw = self._call([method, target], json.dumps(body, ensure_ascii=False) if body is not None else None)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitLabApiError(f"GitLab {method} {path} returned invalid JSON") from exc

    def get(self, path: str, params: Mapping[str, object] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: object | None = None, params: Mapping[str, object] | None = None) -> Any:
        return self.request("POST", path, params=params, body=body if body is not None else {})

    def put(self, path: str, body: object | None = None, params: Mapping[str, object] | None = None) -> Any:
        return self.request("PUT", path, params=params, body=body)

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def paginate(self, path: str, params: Mapping[str, object] | None = None) -> list[dict[str, Any]]:
        """Every row of a list endpoint, following ``X-Next-Page`` until GitLab sends none."""

        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get(path, {**(params or {}), "per_page": PAGE_SIZE, "page": page})
            if not isinstance(data, list):
                raise GitLabApiError(f"GitLab returned an unexpected shape for {path}")
            rows.extend(row for row in data if isinstance(row, dict))
            following = str(self._headers.get("x-next-page") or "").strip()
            # A next page that does not move forward would loop for ever: the list ends there.
            if not following.isdigit() or int(following) <= page:
                return rows
            page = int(following)


# --------------------------------------------------------------------------- #
# Resolving the range
# --------------------------------------------------------------------------- #


def _git(repo_root: Path, *args: str, timeout: float = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True, text=True, timeout=timeout, check=False
    )


def _remote_path(url: str) -> tuple[str, str] | None:
    """``(host, project path)`` of a git remote URL, lower-cased, credentials and port dropped; nested groups kept."""

    text = url.strip()
    if "://" in text:
        parts = urlsplit(text)
        host, path = parts.hostname or "", parts.path
    else:
        scp = re.match(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$", text)
        if scp is None:
            return None
        host, path = scp["host"], scp["path"]
    path = path.strip("/")
    path = path.removesuffix(".git")
    if not host or "/" not in path:
        return None
    return host.lower(), path.lower()


def find_remote(repo_root: Path, ref: MergeRequestRef) -> str:
    """The remote of this clone that points at the merge request's project (``origin`` in a GitLab CI checkout)."""

    listed = _git(repo_root, "config", "--get-regexp", r"^remote\..*\.url$")
    wanted = (ref.host.lower(), ref.slug.lower())
    names: list[str] = []
    for line in listed.stdout.splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote.") : -len(".url")]
        if name and _remote_path(url) == wanted and name not in names:
            names.append(name)
    if not names:
        raise ValueError(f"no git remote here points at {ref.host}/{ref.slug}; run the review in a clone of {ref.slug}")
    return "origin" if "origin" in names else names[0]


def _has_commit(repo_root: Path, sha: str) -> bool:
    return bool(sha) and _git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def resolve_merge_request(repo_root: Path, ref: MergeRequestRef, api: GitLabApi) -> PullRequestSource:
    """Read the merge request, fetch its head and target branch into this clone, and name the range to review.

    The diff base is GitLab's own ``diff_refs.base_sha``, the merge base its "Changes" tab diffs against, so a line
    number posted back lands on the hunk the reviewer read. Nothing is written but ``FETCH_HEAD``.
    """

    row = api.get(ref.api_path)
    if not isinstance(row, dict):
        raise GitLabApiError(f"GitLab returned an unexpected shape for {ref.url}")
    found_refs = row.get("diff_refs")
    refs: dict[str, Any] = found_refs if isinstance(found_refs, dict) else {}
    head_sha = str(refs.get("head_sha") or row.get("sha") or "")
    start_sha = str(refs.get("start_sha") or "")
    base_sha = str(refs.get("base_sha") or "")
    target = str(row.get("target_branch") or "")
    if not (head_sha and start_sha and target):
        raise GitLabApiError(f"GitLab did not report the head and target of {ref.url} (is its diff still being built?)")
    remote = find_remote(repo_root, ref)
    fetched = _git(
        repo_root,
        "fetch",
        "--no-tags",
        "--quiet",
        remote,
        f"refs/merge-requests/{ref.number}/head",
        f"refs/heads/{target}",
        timeout=_FETCH_TIMEOUT_S,
    )
    if fetched.returncode != 0:
        raise ValueError(f"git fetch of {ref.url} from {remote!r} failed: {fetched.stderr.strip()[:_MESSAGE_LIMIT]}")
    for sha, what in ((head_sha, "head"), (start_sha, "target")):
        if not _has_commit(repo_root, sha):
            raise ValueError(
                f"the merge request {what} moved while it was being fetched ({sha[:12]} is not in this clone); run it again"
            )
    if not _has_commit(repo_root, base_sha):
        found = _git(repo_root, "merge-base", start_sha, head_sha)
        base_sha = found.stdout.strip() if found.returncode == 0 else ""
    if not base_sha:
        raise ValueError(f"{ref.url} shares no history with {target}; nothing to diff against")
    author = row.get("author")
    return PullRequestSource(
        ref=ref,
        title=str(row.get("title") or ""),
        author=str(author.get("username") or "") if isinstance(author, dict) else "",
        state=str(row.get("state") or ""),
        head_sha=head_sha,
        base_ref=target,
        base_sha=start_sha,
        merge_base_sha=base_sha,
        remote=remote,
    )


__all__ = [
    "GITLAB_SOURCE_PREFIX",
    "PAGE_SIZE",
    "TOKEN_ENV",
    "GitLabApi",
    "GitLabApiError",
    "HttpResponse",
    "MergeRequestRef",
    "Transport",
    "error_for",
    "find_remote",
    "merge_request_ref",
    "resolve_merge_request",
    "urllib_transport",
]
