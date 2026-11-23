"""A fake GitLab REST API v4 for the merge request bot's tests, answered through ``GitLabApi``'s transport.

``FakeGitLab`` is a route table of ``(method, path pattern)`` -> handler over in-memory state -- overview notes,
discussions, commit statuses the bot posted, the statuses CI reported, award emoji -- so a test asserts what the merge
request ends up holding, not what was sent. Every list endpoint pages with ``X-Next-Page`` (``page_limit`` rows a
page), every call must carry the token in ``PRIVATE-TOKEN``, and ``failures`` makes one route answer with an error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from lemoncrow.pro.capabilities.review.pr_bot.gitlab import BotGitLab
from lemoncrow.pro.capabilities.review.sources.gitlab import GitLabApi, HttpResponse, MergeRequestRef

SERVER = "https://gitlab.example.com"
API = f"{SERVER}/api/v4"
TOKEN = "glpat-test-0123456789abcdef"
PROJECT_ID = "42"
PATH = "acme/platform/widgets"
IID = 7
MR_ID = 70007
BASE_SHA = "b" * 40
START_SHA = "s" * 40
HEAD_SHA = "h" * 40
BOT = {"id": 900, "username": "project_42_bot"}
PERSON = {"id": 11, "username": "alice"}

Handler = Callable[[re.Match[str], dict[str, str], Any], tuple[int, Any]]


def ref() -> MergeRequestRef:
    return MergeRequestRef(
        owner="acme/platform",
        repo="widgets",
        number=IID,
        host="gitlab.example.com",
        project_id=PROJECT_ID,
        server_url=SERVER,
    )


class FakeGitLab:
    """The slice of GitLab the bot talks to, for one project and one merge request."""

    def __init__(self) -> None:
        self.user: dict[str, Any] = dict(BOT)
        self.access_level = 30
        self.merge_request: dict[str, Any] = {
            "id": MR_ID,
            "iid": IID,
            "state": "opened",
            "draft": False,
            "title": "Add widgets",
            "author": dict(PERSON),
            "description": "",
            "sha": HEAD_SHA,
            "diff_refs": {"base_sha": BASE_SHA, "start_sha": START_SHA, "head_sha": HEAD_SHA},
            "source_project_id": int(PROJECT_ID),
            "target_project_id": int(PROJECT_ID),
            "source_branch": "feature/widgets",
            "target_branch": "main",
            "labels": [],
            "updated_at": "2026-10-05T10:00:00Z",
            "merge_when_pipeline_succeeds": False,
            "head_pipeline": {
                "project_id": int(PROJECT_ID),
                "id": 555,
                "sha": HEAD_SHA,
                "status": "running",
                "updated_at": "2026-10-05T10:00:00Z",
            },
            "web_url": f"{SERVER}/{PATH}/-/merge_requests/{IID}",
        }
        self.reviewers: list[dict[str, Any]] = []
        """``GET .../reviewers`` rows: ``{"user": ..., "state": ...}``."""
        self.approved_by: list[dict[str, Any]] = []
        """Who approved the merge request, as GitLab's users."""
        self.approvals_sent: list[dict[str, Any]] = []
        """Every accepted ``POST .../approve`` body."""
        self.merges: list[dict[str, Any]] = []
        """Every ``PUT .../merge`` body GitLab accepted."""
        self.default_branch = "main"
        self.default_tip = BASE_SHA
        self.files: dict[tuple[str, str], bytes] = {}
        """Repository contents by ``(path, ref)``."""
        self.pipelines: dict[str, dict[str, Any]] = {}
        self.members: dict[str, int] = {str(PERSON["id"]): 30}
        self.diffs: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        """Overview notes (``type`` None); diff notes live in their discussions."""
        self.discussions: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        """Every ``POST .../statuses/:sha`` body, with its ``sha``."""
        self.commit_statuses: list[dict[str, Any]] = []
        """What ``GET .../repository/commits/:sha/statuses`` reports (CI's jobs and external statuses)."""
        self.commits: list[dict[str, Any]] = []
        self.awards: dict[str, list[dict[str, Any]]] = {}
        self.page_limit = 100
        self.failures: dict[tuple[str, str], tuple[int, Any, dict[str, str]]] = {}
        """``(method, route pattern)`` -> ``(status, body, headers)`` answered instead of the route, once each."""
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self._ids = 1000
        mr = rf"projects/{PROJECT_ID}/merge_requests/{IID}"
        self.routes: list[tuple[str, str, Handler]] = [
            ("GET", r"user", lambda m, q, b: (200, self.user)),
            ("GET", rf"projects/{PROJECT_ID}", self._project),
            ("GET", mr, lambda m, q, b: (200, self.merge_request)),
            ("GET", rf"{mr}/diffs", lambda m, q, b: (200, self.diffs)),
            ("GET", rf"{mr}/commits", lambda m, q, b: (200, self.commits)),
            ("GET", rf"{mr}/notes", lambda m, q, b: (200, self.notes)),
            ("POST", rf"{mr}/notes", self._post_note),
            ("GET", rf"{mr}/notes/(\d+)", self._get_note),
            ("PUT", rf"{mr}/notes/(\d+)", self._put_note),
            ("GET", rf"{mr}/reviewers", lambda m, q, b: (200, self.reviewers)),
            (
                "GET",
                rf"{mr}/approvals",
                lambda m, q, b: (200, {"approved_by": [{"user": u} for u in self.approved_by]}),
            ),
            ("POST", rf"{mr}/approve", self._approve),
            ("POST", rf"{mr}/unapprove", self._unapprove),
            ("PUT", rf"{mr}/merge", self._merge),
            ("POST", rf"{mr}/cancel_merge_when_pipeline_succeeds", self._cancel_merge),
            ("GET", rf"projects/{PROJECT_ID}/pipelines/(\d+)", self._pipeline),
            ("GET", rf"projects/{PROJECT_ID}/members/all/(\d+)", self._member),
            ("GET", rf"projects/{PROJECT_ID}/repository/branches/([^/]+)", self._branch),
            ("GET", rf"projects/{PROJECT_ID}/repository/merge_base", lambda m, q, b: (200, {"id": self.default_tip})),
            ("GET", rf"projects/{PROJECT_ID}/repository/files/([^/]+)", self._file),
            ("GET", rf"{mr}/discussions", lambda m, q, b: (200, self.discussions)),
            ("POST", rf"{mr}/discussions", self._post_discussion),
            ("POST", rf"{mr}/discussions/(\w+)/notes", self._reply),
            ("PUT", rf"{mr}/discussions/(\w+)", self._resolve),
            ("POST", rf"projects/{PROJECT_ID}/statuses/(\w+)", self._post_status),
            ("GET", rf"projects/{PROJECT_ID}/repository/commits/(\w+)/statuses", self._commit_statuses),
            ("GET", rf"projects/{PROJECT_ID}/issues/(\d+)", lambda m, q, b: (404, {"message": "404 Not found"})),
            ("GET", rf"({mr}(?:/notes/\d+)?/award_emoji)", lambda m, q, b: (200, self.awards.get(m[1], []))),
            ("POST", rf"({mr}(?:/notes/\d+)?/award_emoji)", self._award),
            ("DELETE", rf"({mr}(?:/notes/\d+)?/award_emoji)/(\d+)", self._unaward),
        ]

    # ----- helpers for tests -------------------------------------------- #

    def api(self, **kwargs: Any) -> GitLabApi:
        return GitLabApi(API, TOKEN, transport=self.transport, **kwargs)

    def bot(self) -> BotGitLab:
        return BotGitLab(ref(), self.api(), pipeline_id="555")

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    def note(self, body: str, author: Mapping[str, Any] = PERSON, **fields: Any) -> dict[str, Any]:
        row = {"id": self.next_id(), "body": body, "author": dict(author), "system": False, "type": None, **fields}
        self.notes.append(row)
        return row

    def fail(
        self, method: str, path: str, status: int, body: Any = None, headers: Mapping[str, str] | None = None
    ) -> None:
        """Answer the next *method* call on the route that serves *path* with *status* instead."""

        pattern = next(p for verb, p, _ in self.routes if verb == method and re.fullmatch(p, path))
        self.failures[(method, pattern)] = (status, body, dict(headers or {}))

    def called(self, method: str, fragment: str) -> list[tuple[str, str, dict[str, str], Any]]:
        return [call for call in self.calls if call[0] == method and fragment in call[1]]

    # ----- the transport ------------------------------------------------ #

    def transport(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        parts = urlsplit(url)
        assert url.startswith(f"{API}/"), url
        path = parts.path[len(urlsplit(API).path) + 1 :]
        query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
        payload = json.loads(body) if body else None
        self.calls.append((method, path, query, payload))
        if headers.get("PRIVATE-TOKEN") != TOKEN:
            return self._answer(401, {"message": "401 Unauthorized"})
        for verb, pattern, handler in self.routes:
            found = re.fullmatch(pattern, path)
            if verb != method or found is None:
                continue
            if (method, pattern) in self.failures:
                status, data, extra = self.failures.pop((method, pattern))
                return self._answer(status, data, extra)
            status, data = handler(found, query, payload)
            if method == "GET" and isinstance(data, list):
                return self._page(data, query)
            return self._answer(status, data)
        return self._answer(404, {"message": "404 Not Found"})

    @staticmethod
    def _answer(status: int, data: Any, headers: Mapping[str, str] | None = None) -> HttpResponse:
        raw = b"" if data is None else json.dumps(data).encode("utf-8")
        return HttpResponse(status, {key.lower(): value for key, value in (headers or {}).items()}, raw)

    def _page(self, rows: list[Any], query: Mapping[str, str]) -> HttpResponse:
        size = min(int(query.get("per_page", "20")), self.page_limit)
        page = int(query.get("page", "1"))
        chunk = rows[(page - 1) * size : page * size]
        following = str(page + 1) if page * size < len(rows) else ""
        return self._answer(200, chunk, {"X-Next-Page": following, "X-Page": str(page)})

    # ----- routes ------------------------------------------------------- #

    def _project(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        return 200, {
            "id": int(PROJECT_ID),
            "path_with_namespace": PATH,
            "default_branch": self.default_branch,
            "permissions": {"project_access": {"access_level": self.access_level}, "group_access": None},
        }

    def _post_note(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        return 201, self.note(str(body["body"]), self.user)

    def _get_note(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        for row in self.notes:
            if row["id"] == int(match[1]):
                return 200, row
        return 404, {"message": "404 Note Not Found"}

    def _approve(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        """As GitLab: a wrong ``sha`` is 409, and approving twice is refused with 401."""

        if body.get("sha") and body["sha"] != self.merge_request["sha"]:
            return 409, {"message": "SHA does not match HEAD of source branch"}
        if any(row["id"] == self.user["id"] for row in self.approved_by):
            return 401, {"message": "401 Unauthorized"}
        self.approved_by.append(dict(self.user))
        self.approvals_sent.append(dict(body))
        return 201, {"approved_by": [{"user": row} for row in self.approved_by]}

    def _unapprove(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        kept = [row for row in self.approved_by if row["id"] != self.user["id"]]
        if len(kept) == len(self.approved_by):
            return 404, {"message": "404 Not Found"}
        self.approved_by = kept
        return 201, {"approved_by": [{"user": row} for row in kept]}

    def _merge(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        """As GitLab: bound to ``sha``; with no active pipeline to wait for, it merges now."""

        row = self.merge_request
        if body.get("sha") != row["sha"]:
            return 409, {"message": "SHA does not match HEAD of source branch"}
        if row["state"] != "opened" or row["draft"]:
            return 405, {"message": "405 Method Not Allowed"}
        self.merges.append(dict(body))
        status = str((row.get("head_pipeline") or {}).get("status") or "")
        if body.get("merge_when_pipeline_succeeds") and status in {"created", "pending", "running"}:
            row["merge_when_pipeline_succeeds"] = True
        else:
            row["state"] = "merged"
        return 200, row

    def _cancel_merge(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        if not self.merge_request["merge_when_pipeline_succeeds"]:
            return 406, {"message": "406 Not Acceptable"}
        self.merge_request["merge_when_pipeline_succeeds"] = False
        return 201, self.merge_request

    def _pipeline(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        row = self.pipelines.get(match[1])
        return (200, row) if row is not None else (404, {"message": "404 Not found"})

    def _member(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        level = self.members.get(match[1])
        return (200, {"id": int(match[1]), "access_level": level}) if level else (404, {"message": "404 Not found"})

    def _branch(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        name = unquote(match[1])
        tip = self.default_tip if name == self.default_branch else BASE_SHA
        return 200, {"name": name, "protected": name == self.default_branch, "commit": {"id": tip}}

    def _file(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        path = unquote(match[1])
        raw = self.files.get((path, query.get("ref", "")))
        if raw is None:
            return 404, {"message": "404 File Not Found"}
        blob = hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()
        return 200, {
            "file_path": path,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
            "blob_id": blob,
        }

    def _put_note(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        for row in self.notes:
            if row["id"] == int(match[1]):
                row["body"] = str(body["body"])
                return 200, row
        return 404, {"message": "404 Note Not Found"}

    def _post_discussion(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        note = {
            "id": self.next_id(),
            "type": "DiffNote",
            "body": str(body["body"]),
            "author": dict(self.user),
            "system": False,
            "position": dict(body["position"]),
            "resolvable": True,
            "resolved": False,
        }
        discussion = {"id": f"d{note['id']:039x}", "individual_note": False, "notes": [note]}
        self.discussions.append(discussion)
        return 201, discussion

    def _discussion(self, did: str) -> dict[str, Any] | None:
        return next((row for row in self.discussions if row["id"] == did), None)

    def _reply(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        discussion = self._discussion(match[1])
        if discussion is None:
            return 404, {"message": "404 Discussion Not Found"}
        first = discussion["notes"][0]
        note = {
            "id": self.next_id(),
            "type": first["type"],
            "body": str(body["body"]),
            "author": dict(self.user),
            "system": False,
            "position": first.get("position"),
        }
        discussion["notes"].append(note)
        return 201, note

    def _resolve(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        discussion = self._discussion(match[1])
        if discussion is None:
            return 404, {"message": "404 Discussion Not Found"}
        for note in discussion["notes"]:
            note["resolved"] = query.get("resolved") == "true"
        return 200, discussion

    def _post_status(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        row = {"id": self.next_id(), "sha": match[1], **body}
        self.statuses.append(row)
        return 201, row

    def _commit_statuses(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        return 200, [row for row in self.commit_statuses if row.get("sha", match[1]) == match[1]]

    def _award(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        rows = self.awards.setdefault(match[1], [])
        if any(row["name"] == body["name"] and row["user"]["id"] == self.user["id"] for row in rows):
            return 404, {"message": "404 Award Emoji Name has already been taken"}
        row = {"id": self.next_id(), "name": str(body["name"]), "user": dict(self.user)}
        rows.append(row)
        return 201, row

    def _unaward(self, match: re.Match[str], query: dict[str, str], body: Any) -> tuple[int, Any]:
        rows = self.awards.get(match[1], [])
        self.awards[match[1]] = [row for row in rows if row["id"] != int(match[2])]
        return 204, None
