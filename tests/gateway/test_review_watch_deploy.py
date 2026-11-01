"""``lc review watch-deploy`` (C11 / P3.3): one read-only production signal tied to the deployed SHA.

Every external API is a local stdlib HTTP server: Sentry, Prometheus and GitHub
(reached through a ``gh api`` stand-in that forwards each call to the fake).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli.commands import review_watch_deploy_cli
from lemoncrow.gateway.cli.commands.review_watch_deploy_cli import watch_deploy_cmd
from lemoncrow.pro.capabilities.review.outcomes import deploy_watch
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
OTHER_SHA = "ffffffffffffffffffffffffffffffffffffffff"
TOKEN = "sntrys_super-secret-token-value"
PROM_TOKEN = "prom-secret-bearer"
DEPLOYED = (datetime.now(UTC) - timedelta(hours=3)).replace(microsecond=0)

Handler = Callable[[str, dict[str, list[str]], Any, dict[str, str]], tuple[int, Any]]


class FakeServer:
    """One stdlib HTTP server; ``routes[(method, path)]`` answers ``(status, json)``; every request is recorded."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[dict[str, Any]] = []
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null") if length else None
                headers = {key.lower(): value for key, value in self.headers.items()}
                query = parse_qs(parts.query)
                fake.requests.append(
                    {"method": self.command, "path": parts.path, "query": query, "headers": headers, "body": body}
                )
                route = fake.routes.get((self.command, parts.path))
                status, payload = (
                    (404, {"message": "Not Found"}) if route is None else route(parts.path, query, body, headers)
                )
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = _serve

            def log_message(self, *args: Any) -> None:
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def json(self, method: str, path: str, payload: Any, status: int = 200) -> None:
        self.routes[(method, path)] = lambda *_args: (status, payload)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class GhViaHttp:
    """A ``gh api`` stand-in: forwards each call to the fake GitHub server, raising like ``run_gh`` on errors."""

    def __init__(self, server: FakeServer) -> None:
        self.server = server

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        call = list(args)
        assert call[0] == "api", call
        method, endpoint, paginate, rest = "GET", "", False, call[1:]
        while rest:
            item = rest.pop(0)
            if item in {"-H", "--input"}:
                rest.pop(0)
            elif item == "--method":
                method = rest.pop(0)
            elif item in {"--paginate", "--slurp"}:
                paginate = True
            else:
                endpoint = item
        data = stdin.encode() if stdin is not None else None
        request = Request(f"{self.server.url}/{endpoint}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise GitHubCliError(f"gh: {json.loads(exc.read()).get('message')} (HTTP {exc.code})") from exc
        return json.dumps([payload] if paginate else payload)


@pytest.fixture
def sentry() -> Iterator[FakeServer]:
    server = FakeServer()
    yield server
    server.close()


@pytest.fixture
def prometheus() -> Iterator[FakeServer]:
    server = FakeServer()
    yield server
    server.close()


class GitHub(FakeServer):
    """PR #42 introduced SHA (squash merge) and changed two files; issue comments are stored."""

    def __init__(self) -> None:
        super().__init__()
        self.comments: list[dict[str, Any]] = []
        self.json(
            "GET",
            f"/repos/acme/web/commits/{SHA}/pulls",
            [
                {
                    "number": 42,
                    "title": "Users: cache the profile lookup",
                    "state": "closed",
                    "merged_at": "2026-10-31T08:00:00Z",
                    "merge_commit_sha": SHA,
                    "html_url": "https://github.com/acme/web/pull/42",
                }
            ],
        )
        self.json("GET", f"/repos/acme/web/commits/{OTHER_SHA}/pulls", [])
        self.json(
            "GET",
            "/repos/acme/web/pulls/42/files",
            [{"filename": "src/api/users.py"}, {"filename": "src/api/cache.py", "previous_filename": "src/cache.py"}],
        )
        self.json("GET", "/user", {"message": "Resource not accessible by integration"}, status=403)
        self.routes[("GET", "/repos/acme/web/issues/42/comments")] = lambda *_a: (200, self.comments)
        self.routes[("POST", "/repos/acme/web/issues/42/comments")] = self._create

    def _create(self, _path: str, _query: Any, body: Any, _headers: Any) -> tuple[int, Any]:
        comment = {
            "id": 900 + len(self.comments),
            "body": body["body"],
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "html_url": f"https://github.com/acme/web/pull/42#issuecomment-{900 + len(self.comments)}",
        }
        self.comments.append(comment)
        self.routes[("PATCH", f"/repos/acme/web/issues/comments/{comment['id']}")] = self._update
        return 201, comment

    def _update(self, path: str, _query: Any, body: Any, _headers: Any) -> tuple[int, Any]:
        comment = next(item for item in self.comments if path.endswith(f"/{item['id']}"))
        comment["body"] = body["body"]
        return 200, comment


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[GitHub]:
    server = GitHub()
    monkeypatch.setattr(review_watch_deploy_cli, "_runner", lambda: GhViaHttp(server))
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "no-store"))
    for name in (
        "SENTRY_URL",
        "SENTRY_ORG",
        "SENTRY_PROJECT",
        "SENTRY_AUTH_TOKEN",
        "PROMETHEUS_URL",
        "PROMETHEUS_QUERY",
    ):
        monkeypatch.delenv(name, raising=False)
    yield server
    server.close()


def _issue(ident: str, short: str, culprit: str, count: int, users: int, kind: str = "TypeError") -> dict[str, Any]:
    return {
        "id": ident,
        "shortId": short,
        "title": "TypeError: secret customer value 4111-1111",
        "metadata": {"type": kind, "value": "secret customer value 4111-1111"},
        "culprit": culprit,
        "count": str(count),
        "userCount": users,
        "firstSeen": (DEPLOYED + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "permalink": f"https://evil.example/{short}",
    }


def _event(*frames: dict[str, Any]) -> dict[str, Any]:
    return {"entries": [{"type": "exception", "data": {"values": [{"stacktrace": {"frames": list(frames)}}]}}]}


def _sentry_issues(server: FakeServer, issues: list[dict[str, Any]], events: dict[str, Any], sha: str = SHA) -> None:
    server.json("GET", f"/api/0/organizations/acme/releases/{sha}/", {"version": sha})
    server.json("GET", "/api/0/projects/acme/web/issues/", issues)
    for ident, event in events.items():
        server.json("GET", f"/api/0/organizations/acme/issues/{ident}/events/latest/", event)


def _run(*args: str, env: dict[str, str] | None = None) -> Result:
    return CliRunner().invoke(
        watch_deploy_cmd,
        ["--repo", "acme/web", "--deployed-at", DEPLOYED.strftime("%Y-%m-%dT%H:%M:%SZ"), *args],
        env=env,
        catch_exceptions=False,
    )


def _sentry_args(server: FakeServer) -> list[str]:
    return ["--sentry-url", server.url, "--sentry-org", "acme", "--sentry-project", "web"]


def test_issue_in_a_changed_file_is_a_linked_signal_and_others_are_listed_unlinked(
    github: GitHub, sentry: FakeServer
) -> None:
    _sentry_issues(
        sentry,
        [_issue("1001", "WEB-1", "api.users in get_profile", 12, 3), _issue("1002", "WEB-2", "billing", 40, 9)],
        {
            "1001": _event(
                {
                    "filename": "django/core/handlers.py",
                    "absPath": "/usr/lib/python3/site-packages/django/core/handlers.py",
                },
                {
                    "filename": "api/users.py",
                    "absPath": "/srv/app/src/api/users.py",
                    "function": "get_profile",
                    "lineNo": 41,
                    "inApp": True,
                },
            ),
            "1002": _event({"filename": "src/billing/invoice.py", "function": "total", "lineNo": 7, "inApp": True}),
        },
    )

    result = _run("--sha", SHA, *_sentry_args(sentry), "--json", env={"SENTRY_AUTH_TOKEN": TOKEN})

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["outcome"] == "signal"
    assert report["pull_request"]["number"] == 42
    linked, unlinked = report["sentry"]["issues"]
    assert (linked["short_id"], linked["linked"], linked["linked_file"]) == ("WEB-1", True, "src/api/users.py")
    assert (linked["count"], linked["users"], linked["linked_frame"]["line"]) == (12, 3, 41)
    assert (unlinked["short_id"], unlinked["linked"]) == ("WEB-2", False)
    assert unlinked["top_frame"]["path"] == "src/billing/invoice.py"
    # Issue messages are never read; a permalink off the Sentry host is dropped.
    assert "4111" not in result.stdout and "evil.example" not in result.stdout
    assert sentry.requests[0]["path"] == f"/api/0/organizations/acme/releases/{SHA}/"
    listing = sentry.requests[1]
    assert f'firstRelease:"{SHA}"' in listing["query"]["query"][0]
    assert all(item["method"] == "GET" for item in sentry.requests)
    assert all(item["headers"]["authorization"] == f"Bearer {TOKEN}" for item in sentry.requests)
    assert not any("authorization" in item["headers"] for item in github.requests)

    markdown = _run("--sha", SHA, *_sentry_args(sentry), env={"SENTRY_AUTH_TOKEN": TOKEN}).stdout
    assert "**Outcome: `signal`**" in markdown
    assert "**linked**: `src/api/users.py`" in markdown
    assert "in this release, not linked to its files" in markdown
    assert TOKEN not in markdown


def test_nothing_new_is_no_new_signal_with_the_window_stated(github: GitHub, sentry: FakeServer) -> None:
    _sentry_issues(sentry, [], {})

    result = _run("--sha", SHA, "--window", "90m", *_sentry_args(sentry), env={"SENTRY_AUTH_TOKEN": TOKEN})

    assert result.exit_code == 0
    assert "**Outcome: `no_new_signal`**: Nothing new observed in 90m after the deploy." in result.stdout
    assert "healthy" not in result.stdout.lower()


@pytest.mark.parametrize("case", ["missing_token", "api_500"])
def test_missing_token_or_api_error_is_not_observed_and_never_prints_the_token(
    github: GitHub, sentry: FakeServer, case: str
) -> None:
    _sentry_issues(sentry, [], {})
    sentry.json("GET", "/api/0/projects/acme/web/issues/", {"detail": TOKEN}, status=500)
    env = {} if case == "missing_token" else {"SENTRY_AUTH_TOKEN": TOKEN}

    result = _run("--sha", SHA, *_sentry_args(sentry), "--json", env=env)

    report = json.loads(result.stdout)
    assert report["outcome"] == "not_observed"
    expected = (
        "$SENTRY_AUTH_TOKEN is not set" if case == "missing_token" else "Sentry: the API answered with an HTTP error"
    )
    assert report["reasons"] == [expected]
    assert "not a verdict" in report["headline"]
    assert TOKEN not in result.output
    assert (len(sentry.requests) == 0) is (case == "missing_token")


def test_unrelated_sha_says_no_pull_request_and_links_nothing(github: GitHub, sentry: FakeServer) -> None:
    _sentry_issues(
        sentry,
        [_issue("1003", "WEB-3", "api.users", 2, 1)],
        {"1003": _event({"filename": "src/api/users.py", "inApp": True})},
        sha=OTHER_SHA,
    )

    result = _run("--sha", OTHER_SHA, *_sentry_args(sentry), env={"SENTRY_AUTH_TOKEN": TOKEN})

    assert "No pull request in acme/web contains `fffffff`" in result.stdout
    assert "**Outcome: `no_new_signal`**: Nothing linked to this change observed in 2h" in result.stdout
    assert "in this release, not linked to its files" in result.stdout
    commented = _run("--sha", OTHER_SHA, "--comment", env={})
    assert commented.exit_code == 1
    assert "nothing to comment on" in commented.stderr
    assert github.comments == []


def test_a_release_sentry_does_not_know_is_not_observed_rather_than_nothing_new(
    github: GitHub, sentry: FakeServer
) -> None:
    sentry.json("GET", "/api/0/projects/acme/web/issues/", [])

    result = _run(
        "--sha",
        SHA,
        *_sentry_args(sentry),
        "--sentry-release",
        "web@{short_sha}",
        "--json",
        env={"SENTRY_AUTH_TOKEN": TOKEN},
    )

    report = json.loads(result.stdout)
    assert report["outcome"] == "not_observed"
    assert report["reasons"] == [
        "Sentry has no release `web@a1b2c3d`; set --sentry-release to the name the app reports"
    ]
    assert [item["path"] for item in sentry.requests] == ["/api/0/organizations/acme/releases/web%40a1b2c3d/"]


def test_commit_github_does_not_know_is_said_plainly(github: GitHub) -> None:
    unknown = "0" * 40
    github.json("GET", f"/repos/acme/web/commits/{unknown}/pulls", {"message": "No commit found for SHA"}, 422)

    lookup = deploy_watch.find_change(GhViaHttp(github), "acme/web", unknown)

    assert lookup.change is None
    assert lookup.note == "GitHub does not know commit `0000000` in acme/web; no pull request can be linked."


def test_rerun_updates_the_same_comment_in_place(github: GitHub, sentry: FakeServer) -> None:
    _sentry_issues(sentry, [], {})
    args = ["--sha", SHA, *_sentry_args(sentry), "--comment"]

    first = _run(*args, env={})
    second = _run(*args, env={"SENTRY_AUTH_TOKEN": TOKEN})

    assert first.exit_code == second.exit_code == 0
    assert "created the comment on pull request #42" in first.stderr
    assert "updated the comment on pull request #42" in second.stderr
    assert len(github.comments) == 1
    body = github.comments[0]["body"]
    assert body.startswith(deploy_watch.comment_marker(SHA))
    assert "`no_new_signal`" in body and "`not_observed`**" not in body
    methods = [(item["method"], item["path"]) for item in github.requests if item["method"] != "GET"]
    assert methods == [("POST", "/repos/acme/web/issues/42/comments"), ("PATCH", "/repos/acme/web/issues/comments/900")]


def test_a_person_quoting_the_marker_is_never_edited(github: GitHub) -> None:
    github.comments.append(
        {"id": 5, "body": deploy_watch.comment_marker(SHA), "user": {"login": "mallory", "type": "User"}}
    )

    action, _url = deploy_watch.upsert_comment(GhViaHttp(github), "acme/web", 42, SHA, "report")

    assert action == "created"
    assert github.comments[0]["body"] == deploy_watch.comment_marker(SHA)


def _prometheus_answers(server: FakeServer, before: float, after: float) -> None:
    deploy_time = DEPLOYED.timestamp()

    def answer(_path: str, query: dict[str, list[str]], _body: Any, _headers: Any) -> tuple[int, Any]:
        at = float(query["time"][0])
        value = before if abs(at - deploy_time) < 1 else after
        return 200, {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [at, str(value)]}]},
        }

    server.routes[("GET", "/api/v1/query")] = answer


QUERY = 'sum(rate(http_requests_total{status=~"5..",route=~"{route}"}[5m]))'


def test_prometheus_route_crossing_the_threshold_is_a_signal(github: GitHub, prometheus: FakeServer) -> None:
    _prometheus_answers(prometheus, before=0.01, after=0.05)
    args = ["--sha", SHA, "--prom-url", prometheus.url, "--prom-query", QUERY, "--prom-token-env", "PROM_TOKEN"]
    routes = ["--route", "src/api/users.py=/api/users", "--route", "src/billing/*=/api/billing"]

    result = _run(*args, *routes, "--prom-max-increase", "0.02", "--json", env={"PROM_TOKEN": PROM_TOKEN})

    report = json.loads(result.stdout)
    assert report["outcome"] == "signal"
    (series,) = report["prometheus"]["series"]
    assert (series["route"], series["before"], series["after"], series["crossed"]) == ("/api/users", 0.01, 0.05, True)
    sent = [item["query"]["query"][0] for item in prometheus.requests]
    assert sent[0] == 'avg_over_time((sum(rate(http_requests_total{status=~"5..",route=~"/api/users"}[5m])))[7200s:])'
    assert all(item["headers"]["authorization"] == f"Bearer {PROM_TOKEN}" for item in prometheus.requests)
    assert PROM_TOKEN not in result.output


def test_prometheus_without_threshold_reports_numbers_but_never_a_regression(
    github: GitHub, prometheus: FakeServer
) -> None:
    _prometheus_answers(prometheus, before=0.01, after=0.5)

    result = _run("--sha", SHA, "--prom-url", prometheus.url, "--prom-query", QUERY)

    assert "**Outcome: `no_new_signal`**" in result.stdout
    assert "| `overall` | 0.01 | 0.5 | +0.49 |" in result.stdout
    assert "No threshold is configured, so these numbers are not judged." in result.stdout
    assert "regress" not in result.stdout.lower()


def test_window_not_elapsed_queries_nothing(github: GitHub, sentry: FakeServer) -> None:
    result = CliRunner().invoke(
        watch_deploy_cmd, ["--repo", "acme/web", "--sha", SHA, *_sentry_args(sentry)], env={"SENTRY_AUTH_TOKEN": TOKEN}
    )

    assert "**Outcome: `not_observed`**: Not observed yet: the 2h window closes at" in result.stdout
    assert sentry.requests == []


def test_no_connector_is_not_observed(github: GitHub) -> None:
    result = _run("--sha", SHA, "--json")

    report = json.loads(result.stdout)
    assert report["outcome"] == "not_observed"
    assert report["reasons"] == ["no telemetry connector is configured (Sentry or Prometheus)"]


def test_frame_matching_is_conservative() -> None:
    def frame(path: str, in_app: bool = False) -> deploy_watch.Frame:
        return deploy_watch.Frame(path=path, in_app=in_app)

    assert deploy_watch.frame_matches(frame("webpack:///./src/app.ts"), "src/app.ts")
    assert deploy_watch.frame_matches(frame("/srv/app/src/x.py"), "src/x.py")
    assert deploy_watch.frame_matches(frame("api/x.py"), "src/api/x.py")
    assert not deploy_watch.frame_matches(frame("x.py"), "src/api/x.py")
    assert not deploy_watch.frame_matches(frame("/venv/lib/site-packages/requests/utils.py", True), "utils.py")
    assert not deploy_watch.frame_matches(frame("/srv/lib/utils.py"), "utils.py")
    assert deploy_watch.frame_matches(frame("/srv/app/utils.py", in_app=True), "utils.py")


def test_inputs_are_validated() -> None:
    assert deploy_watch.parse_window("24h") == 86400
    for bad in ("25h", "1m", "2d"):
        with pytest.raises(ValueError):
            deploy_watch.parse_window(bad)
    with pytest.raises(ValueError):
        deploy_watch.validate_base_url("http://sentry.example.com", flag="--sentry-url")
    with pytest.raises(ValueError):
        deploy_watch.validate_base_url("https://user:pw@sentry.example.com", flag="--sentry-url")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_result_is_recorded_on_the_local_review_of_the_pull_request(
    github: GitHub, sentry: FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, record_revision
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "users.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "users.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "head")
    root = tmp_path / "store"
    store = ReviewStore(root)
    rng = resolve_rev_range(repo, base="HEAD~1", head="HEAD")
    session = open_or_create_session(store, repo, rng, title="PR #42")
    record_revision(store, session, repo, rng, store_root=root)
    store.update_session(session.id, source_ref="github:acme/web#42")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    _sentry_issues(sentry, [], {})

    for _ in range(2):  # a rerun with the same outcome adds nothing
        result = _run("--sha", SHA, *_sentry_args(sentry), env={"SENTRY_AUTH_TOKEN": TOKEN})
        assert f"recorded on local Review r/{session.id[:8]}" in result.stderr

    rows = [row for row in ReviewStore(root).list_evidence(session.id) if row.source_ref.startswith("watch-deploy:")]
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert (rows[0].kind, rows[0].provider, rows[0].provider_revision) == ("deployment", "sentry", SHA)
    assert (detail["freshness_class"], detail["outcome"], detail["pull_request"]) == (
        "external_observation",
        "no_new_signal",
        42,
    )
