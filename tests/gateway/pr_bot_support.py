"""Fakes for the pull request bot's tests: a git origin, GitHub's REST and GraphQL surface, and a model.

``Origin`` is a real bare repository the CI clone fetches ``refs/pull/N/head`` from, so
capture, merge-base and diffing run for real. ``FakeGitHub`` answers the exact ``gh``
calls the bot makes and keeps what a real pull request would hold -- comments, reviews,
threads, statuses -- so tests assert what GitHub ends up with, not what was sent.
``FakeModel`` answers the investigator prompt from the code it is shown: a line
``BUG: <title>`` in the changed source is a confirmed finding, anything else is not.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.pr_bot.runner import RunOptions, RunReport, run_pr_review
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

SLUG = "acme/widgets"
NUMBER = 7
BOT = {"login": "github-actions[bot]", "type": "Bot"}
PERSON = {"login": "alice", "type": "User"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Origin:
    """A bare origin, the author's clone that pushes the pull request, and the CI checkout that reviews it."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.author = root / "author"
        self.clone = root / "clone"
        git(root, "init", "--bare", "-q", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.author))
        for key, value in (("user.name", "A"), ("user.email", "a@example.invalid")):
            git(self.author, "config", key, value)
        self.head = ""

    def seed_main(self, files: dict[str, str]) -> str:
        self._write(files)
        git(self.author, "add", "-A")
        git(self.author, "commit", "-qm", "base")
        git(self.author, "push", "-q", "origin", "main")
        root = self.clone.parent
        git(root, "clone", "-q", str(self.origin), str(self.clone))
        git(self.clone, "remote", "set-url", "origin", f"git@github.com:{SLUG}.git")
        git(self.clone, "config", f"url.{self.origin}.insteadOf", f"git@github.com:{SLUG}.git")
        git(self.author, "checkout", "-qb", "feature")
        return self.base_tip

    @property
    def base_tip(self) -> str:
        return git(self.author, "rev-parse", "main")

    def _write(self, files: dict[str, str | None]) -> None:
        for path, content in files.items():
            target = self.author / path
            if content is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def push(self, files: dict[str, str | None], message: str = "change") -> str:
        """Commit *files* onto the pull request branch and publish it as ``refs/pull/7/head``."""

        self._write(files)
        git(self.author, "add", "-A")
        git(self.author, "commit", "-qm", message)
        self.head = git(self.author, "rev-parse", "HEAD")
        git(self.author, "push", "-q", "-f", "origin", f"HEAD:refs/pull/{NUMBER}/head")
        return self.head

    def patches(self) -> list[dict[str, Any]]:
        """GitHub's ``files`` rows for the pull request: path and the diff from its first hunk."""

        rows = []
        for path in git(self.author, "diff", "--name-only", f"{self.base_tip}...{self.head}").splitlines():
            diff = git(self.author, "diff", "--no-color", "-U3", f"{self.base_tip}...{self.head}", "--", path)
            patch = diff[diff.index("@@") :] if "@@" in diff else None
            rows.append({"filename": path, "patch": patch})
        return rows


class FakeGitHub:
    """The slice of GitHub the bot talks to, behind ``gh api``."""

    def __init__(self, origin: Origin) -> None:
        self.origin = origin
        self.state = "open"
        self.draft = False
        self.head_repo: str | None = SLUG
        self.issue_comments: list[dict[str, Any]] = []
        self.review_comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.threads: dict[int, dict[str, Any]] = {}
        self.deny_resolve = False
        self.deny_status = False
        self.calls: list[tuple[str, str]] = []
        self.review_posts = 0
        self.posted_reviews: list[dict[str, Any]] = []
        """Every ``POST .../reviews`` body exactly as the bot sent it."""
        self.associations: dict[str, str] = {"alice": "MEMBER"}
        """``author_association`` GitHub reports for a login; any other login is ``NONE``."""
        self.permissions: dict[str, str] = {}
        """The repository permission (``admin``, ``write``, ``read``, ``none``) for a login; an unknown login is a 404."""
        self.reactions: dict[int, list[dict[str, str]]] = {}
        """Comment id -> its reactions as GraphQL reports them (``content`` enum, ``login`` of the user)."""
        self._ids = 1000

    # ----- helpers ------------------------------------------------------ #

    def _next(self) -> int:
        self._ids += 1
        return self._ids

    @property
    def api(self) -> str:
        return f"repos/{SLUG}/pulls/{NUMBER}"

    def summary_comments(self) -> list[dict[str, Any]]:
        return [row for row in self.issue_comments if "<!-- lemoncrow:summary v1 -->" in row["body"]]

    def inline(self) -> list[dict[str, Any]]:
        return [row for row in self.review_comments if not row.get("in_reply_to_id")]

    def replies(self) -> list[dict[str, Any]]:
        return [row for row in self.review_comments if row.get("in_reply_to_id")]

    def last_status(self) -> dict[str, Any]:
        return self.statuses[-1]

    def bot_replies(self) -> list[dict[str, Any]]:
        """Replies in review threads that the bot wrote."""

        return [row for row in self.replies() if row["user"] == BOT]

    def chat_replies(self) -> list[dict[str, Any]]:
        """Every comment the bot wrote in answer to a mention, inline or in the conversation."""

        return [row for row in (*self.review_comments, *self.issue_comments) if "<!-- lemoncrow:chat " in row["body"]]

    def add_review_comment(
        self,
        body: str,
        *,
        user: dict[str, str] = PERSON,
        in_reply_to: int = 0,
        path: str = "",
        line: int = 0,
        association: str | None = None,
    ) -> dict[str, Any]:
        """A comment a person (or another bot) wrote on a diff line or in a thread, as GitHub would hold it."""

        row: dict[str, Any] = {
            "id": self._next(),
            "body": body,
            "user": user,
            "author_association": association or self.associations.get(user["login"], "NONE"),
            "path": path,
            "html_url": f"https://github.com/{SLUG}/pull/{NUMBER}#discussion_r{self._ids}",
        }
        if line:
            row["line"] = line
        if in_reply_to:
            row["in_reply_to_id"] = in_reply_to
        else:
            self.threads[row["id"]] = {"id": f"PRRT_{row['id']}", "resolved": False}
        self.review_comments.append(row)
        return row

    def react(self, comment_id: int, content: str, login: str = "alice") -> None:
        """A person reacts to a comment (``THUMBS_UP``, ``THUMBS_DOWN``, ``LAUGH`` ...). An empty login is an app."""

        self.reactions.setdefault(comment_id, []).append({"content": content, "login": login})

    def resolve(self, comment_id: int, by: str = "alice") -> None:
        """A person resolves the review thread whose first comment is *comment_id*."""

        self.threads[comment_id].update(resolved=True, by=by, by_kind="Bot" if by.endswith("[bot]") else "User")

    def add_issue_comment(
        self, body: str, *, user: dict[str, str] = PERSON, association: str | None = None
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self._next(),
            "body": body,
            "user": user,
            "author_association": association or self.associations.get(user["login"], "NONE"),
            "html_url": f"https://github.com/{SLUG}/pull/{NUMBER}#issuecomment-{self._ids}",
        }
        self.issue_comments.append(row)
        return row

    # ----- the runner --------------------------------------------------- #

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        call = list(args)
        if call[:2] == ["pr", "view"]:
            return self._pr_view(call)
        assert call[0] == "api", call
        if call == ["api", "user"]:
            return json.dumps({"login": "reviewer"})
        if call[1] == "graphql":
            return self._graphql(json.loads(stdin or "{}"))
        if call[1] == "--paginate":
            self.calls.append(("GET", call[3]))
            return json.dumps([self._list(call[3])])
        if call[1] == "--method":
            method, path = call[2], call[3]
            self.calls.append((method, path))
            return self._write(method, path, json.loads(stdin or "{}"))
        self.calls.append(("GET", call[1]))
        collaborator = re.fullmatch(rf"repos/{SLUG}/collaborators/([^/]+)/permission", call[1])
        if collaborator is not None:
            if collaborator[1] not in self.permissions:
                raise GitHubCliError("Not Found (HTTP 404)")
            return json.dumps({"permission": self.permissions[collaborator[1]]})
        assert call[1] == self.api, call
        return json.dumps(self._pull())

    def _pr_view(self, call: list[str]) -> str:
        assert call[2] == str(NUMBER), call
        url = f"https://github.com/{SLUG}/pull/{NUMBER}"
        if call[-1] == "url":
            return json.dumps({"url": url})
        return json.dumps(
            {
                "url": url,
                "title": "Widgets",
                "author": {"login": "alice"},
                "state": self.state.upper(),
                "headRefOid": self.origin.head,
                "baseRefName": "main",
                "baseRefOid": self.origin.base_tip,
            }
        )

    def _pull(self) -> dict[str, Any]:
        head: dict[str, Any] = {"sha": self.origin.head}
        head["repo"] = None if self.head_repo is None else {"full_name": self.head_repo}
        return {
            "state": self.state,
            "draft": self.draft,
            "title": "Widgets",
            "user": {"login": "alice"},
            "author_association": self.associations.get("alice", "NONE"),
            "head": head,
            "base": {"ref": "main", "repo": {"id": 4242, "full_name": SLUG}},
            "html_url": f"https://github.com/{SLUG}/pull/{NUMBER}",
        }

    def _list(self, endpoint: str) -> list[dict[str, Any]]:
        if endpoint.startswith(f"{self.api}/files"):
            return self.origin.patches()
        if endpoint.startswith(f"{self.api}/reviews/"):
            review_id = int(endpoint.split("/reviews/")[1].split("/")[0])
            return [row for row in self.review_comments if row.get("pull_request_review_id") == review_id]
        if endpoint.startswith(f"{self.api}/reviews"):
            return self.reviews
        if endpoint.startswith(f"{self.api}/comments"):
            return self.review_comments
        if endpoint.startswith(f"repos/{SLUG}/issues/{NUMBER}/comments"):
            return self.issue_comments
        raise AssertionError(f"unexpected list: {endpoint}")

    def _write(self, method: str, path: str, body: dict[str, Any]) -> str:
        if method == "POST" and path == f"{self.api}/reviews":
            return self._post_review(body)
        if method == "POST" and path.startswith(f"{self.api}/comments/") and path.endswith("/replies"):
            parent = int(path.split("/comments/")[1].split("/")[0])
            reply = {
                "id": self._next(),
                "body": body["body"],
                "user": BOT,
                "in_reply_to_id": parent,
                "html_url": f"https://github.com/{SLUG}/pull/{NUMBER}#discussion_r{self._ids}",
            }
            self.review_comments.append(reply)
            return json.dumps(reply)
        if method == "POST" and path == f"repos/{SLUG}/issues/{NUMBER}/comments":
            comment = {
                "id": self._next(),
                "body": body["body"],
                "user": BOT,
                "html_url": f"https://github.com/{SLUG}/pull/{NUMBER}#issuecomment-{self._ids}",
            }
            self.issue_comments.append(comment)
            return json.dumps(comment)
        if method == "PATCH" and path.startswith(f"repos/{SLUG}/issues/comments/"):
            comment_id = int(path.rsplit("/", 1)[1])
            comment = next(row for row in self.issue_comments if row["id"] == comment_id)
            comment["body"] = body["body"]
            return json.dumps(comment)
        if method == "POST" and path.startswith(f"repos/{SLUG}/statuses/"):
            if self.deny_status:
                raise GitHubCliError("Resource not accessible by integration (HTTP 403)")
            self.statuses.append({"sha": path.rsplit("/", 1)[1], **body})
            return json.dumps(body)
        raise AssertionError(f"unexpected write: {method} {path}")

    def _post_review(self, body: dict[str, Any]) -> str:
        self.review_posts += 1
        self.posted_reviews.append(body)
        assert body["event"] == "COMMENT", body["event"]
        review_id = self._next()
        review = {
            "id": review_id,
            "user": BOT,
            "body": body["body"],
            "commit_id": body["commit_id"],
            "state": "COMMENTED",
            "html_url": f"https://github.com/{SLUG}/pull/{NUMBER}#pullrequestreview-{review_id}",
        }
        self.reviews.append(review)
        for row in body["comments"]:
            comment_id = self._next()
            self.review_comments.append(
                {
                    "id": comment_id,
                    "path": row["path"],
                    "line": row["line"],
                    # A multi-line comment also carries where it starts, on which side, exactly as sent.
                    **{key: row[key] for key in ("side", "start_line", "start_side") if key in row},
                    "body": row["body"],
                    "user": BOT,
                    "pull_request_review_id": review_id,
                    "html_url": f"https://github.com/{SLUG}/pull/{NUMBER}#discussion_r{comment_id}",
                }
            )
            self.threads[comment_id] = {"id": f"PRRT_{comment_id}", "resolved": False}
        return json.dumps(review)

    def _graphql(self, request: dict[str, Any]) -> str:
        query = request["query"]
        if "resolveReviewThread" in query:
            if self.deny_resolve:
                return json.dumps({"errors": [{"message": "Resource not accessible by integration"}]})
            node = request["variables"]["id"]
            for thread in self.threads.values():
                if thread["id"] == node:
                    thread.update(resolved=True, by=BOT["login"].removesuffix("[bot]"), by_kind="Bot")
            return json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}})
        if "reactions" in query:
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": self._feedback_page()}}}})
        nodes = [
            {"id": thread["id"], "isResolved": thread["resolved"], "comments": {"nodes": [{"databaseId": comment_id}]}}
            for comment_id, thread in self.threads.items()
        ]
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
        return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}})

    def _feedback_page(self) -> dict[str, Any]:
        """Every thread with its comments, their reactions and who resolved it: the feedback query's answer."""

        def comment(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "databaseId": row["id"],
                "body": row["body"],
                "author": {"login": row["user"]["login"], "__typename": row["user"]["type"]},
                "authorAssociation": row.get("author_association", "NONE"),
                "reactions": {
                    "nodes": [
                        {"content": r["content"], "user": {"login": r["login"]} if r["login"] else None}
                        for r in self.reactions.get(row["id"], [])
                    ]
                },
            }

        nodes = []
        for root, thread in self.threads.items():
            rows = [r for r in self.review_comments if r["id"] == root or r.get("in_reply_to_id") == root]
            nodes.append(
                {
                    "id": thread["id"],
                    "isResolved": thread["resolved"],
                    "resolvedBy": (
                        {"login": thread["by"], "__typename": thread.get("by_kind", "User")}
                        if thread.get("by")
                        else None
                    ),
                    "comments": {"nodes": [comment(r) for r in sorted(rows, key=lambda r: r["id"])]},
                }
            )
        return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


class FakeModel:
    """A structured executor: confirms a finding when the code it is shown says ``BUG: <title>``.

    A pull request review also asks for ``severity`` and ``replacement`` (``PR_INVESTIGATION_SCHEMA``); the fake
    answers them only for a title it was told about, so every other finding reads as the model gave no rating.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.severity: dict[str, str] = {}
        """``BUG`` title -> the impact rating the model returns for it."""
        self.cite_source: set[str] = set()
        """``BUG`` titles whose finding also cites the exact-source refs of its targets, not just the targets."""
        self.cite_context: set[str] = set()
        """``BUG`` titles whose finding also cites the repository-context excerpts (callers, tests, definitions) of its targets."""
        self.replacements: dict[str, dict[str, Any]] = {}
        """``BUG`` title -> the ``replacement`` object the model returns for it."""
        self.cite_lines: set[str] = set()
        """``BUG`` titles whose finding cites the line its ``BUG:`` marker is really on (a careful model)."""
        self.lines: dict[str, tuple[int, int]] = {}
        """``BUG`` title -> the (start, end) lines the model claims, right or wrong; wins over ``cite_lines``."""
        self.forget: set[str] = set()
        """``BUG`` titles the model fails to report even though the code still says so (it does not repeat itself)."""
        self.answer_prior = True
        """Whether the model answers the ``prior_findings`` it is shown: ``resolved`` once the ``BUG:`` line is gone."""
        self.prior: dict[str, str] = {}
        """Earlier finding title -> the status the model answers for it, instead of reading the code."""
        self.prior_seen: list[list[dict[str, Any]]] = []
        """The ``prior_findings`` rows each investigator call was shown (only calls that were shown any)."""
        self.evidence_seen: list[dict[str, Any]] = []
        """The evidence registry each investigator call was offered, with the path of its first target."""
        self.fail = False
        self.chats: list[dict[str, Any]] = []
        """Every chat call: the system prompt, the prompt as the model was shown it, and the other call arguments."""
        self.chat_answer: str | Any = "The guard is missing for a None principal; see src/auth.py:2."
        """What the model answers a chat question: text, or a function of the parsed prompt."""
        self.chat_input_tokens: int | None = 800
        self.input_tokens: int | None = 1000
        self.output_tokens: int | None = 200
        self.model = "gpt-4o-mini"

    def reset(self) -> None:
        self.calls.clear()

    def paths(self) -> list[str]:
        return sorted(call["path"] for call in self.calls)

    def _chat(self, kwargs: dict[str, Any]) -> StructuredExecution:
        prompt = json.loads(kwargs["prompt"])
        self.chats.append(
            {
                "system": kwargs["system"],
                "prompt": prompt,
                "args": {k: v for k, v in kwargs.items() if k not in {"system", "prompt"}},
            }
        )
        if self.fail:
            raise RuntimeError("provider unavailable")
        answer = self.chat_answer(prompt) if callable(self.chat_answer) else self.chat_answer
        usage = ModelCallUsage(calls=1, input_tokens=self.chat_input_tokens, output_tokens=120)
        return StructuredExecution(result={"answer": answer}, driver="openai", model=self.model, usage=usage)

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        if "answer" in kwargs["output_schema"].get("properties", {}):
            return self._chat(kwargs)
        prompt = json.loads(kwargs["prompt"])
        if "violated" in json.dumps(kwargs["output_schema"]):
            return self._rule_check(prompt)
        if "hypotheses" in kwargs["output_schema"].get("properties", {}):
            return self._hypotheses(prompt)
        hypothesis = prompt["hypothesis"]["value"]
        targets: list[str] = hypothesis["target_ids"]
        source = prompt["snapshot_source"]["value"]
        text = "\n".join(str(source[target].get("text", "")) for target in targets if target in source)
        path = str(source[targets[0]]["path"])
        self.calls.append({"path": path, "targets": targets, "category": hypothesis["category"]})
        self.evidence_seen.append({"path": path, "evidence": prompt["evidence_registry"]["value"]})
        if self.fail:
            raise RuntimeError("provider unavailable")
        usage = ModelCallUsage(calls=1, input_tokens=self.input_tokens, output_tokens=self.output_tokens)
        found = re.search(r"BUG: (.+)", text)
        if found is not None and found.group(1).strip() in self.forget:
            found = None
        asked = (prompt.get("prior_findings") or {}).get("value") or []
        if asked:
            self.prior_seen.append(asked)
        if found is None:
            result: dict[str, Any] = {
                "status": "not_confirmed",
                "category": hypothesis["category"],
                "target_ids": targets,
                "title": "",
                "explanation": "",
                "consequence": "",
                "evidence_refs": [],
                "rule_refs": [],
            }
        else:
            rules = prompt.get("trusted_repository_rules") or []
            title = found.group(1).strip()
            refs = [f"target:{target}" for target in targets]
            registry = prompt["evidence_registry"]["value"]
            if title in self.cite_source:
                refs += [
                    row["ref"] for row in registry if row["kind"] == "source" and set(row["target_ids"]) & set(targets)
                ]
            if title in self.cite_context:
                refs += [
                    row["ref"]
                    for row in registry
                    if row["kind"] == "repo_context" and set(row["target_ids"]) & set(targets)
                ]
            result = {
                "status": "confirmed",
                "category": hypothesis["category"],
                "target_ids": targets,
                "title": title,
                "explanation": f"The change in {path} {title}.",
                "consequence": "Callers can reach the guarded behavior without the check.",
                "evidence_refs": refs,
                "rule_refs": [rule["value"]["ref"] for rule in rules],
            }
            if "severity" in kwargs["output_schema"]["properties"]:
                if title in self.severity:
                    result["severity"] = self.severity[title]
                result["replacement"] = self.replacements.get(title)
                result["lines"] = self._claimed_lines(title, source[targets[0]])
        if "prior_findings" in kwargs["output_schema"]["properties"]:
            result["prior_findings"] = self._prior_answers(asked, text) if self.answer_prior else []
        return StructuredExecution(result=result, driver="openai", model=self.model, usage=usage)

    def _prior_answers(self, asked: list[dict[str, Any]], text: str) -> list[dict[str, str]]:
        """Per earlier finding: still_present while its ``BUG: <title>`` line is in the code shown, else resolved."""

        return [
            {
                "id": row["id"],
                "status": self.prior.get(
                    row["title"], "still_present" if f"BUG: {row['title']}" in text else "resolved"
                ),
            }
            for row in asked
        ]

    def _claimed_lines(self, title: str, excerpt: dict[str, Any]) -> dict[str, int] | None:
        if title in self.lines:
            start, end = self.lines[title]
            return {"start_line": start, "end_line": end}
        if title not in self.cite_lines:
            return None
        for offset, line in enumerate(str(excerpt.get("text", "")).splitlines()):
            if "BUG: " in line:
                number = int(excerpt["start_line"]) + offset
                return {"start_line": number, "end_line": number}
        return None

    def _rule_check(self, prompt: dict[str, Any]) -> StructuredExecution:
        """The per-rule question: violated when a shown target carries a ``BUG:`` line."""

        source = prompt["snapshot_source"]["value"]
        hits = [target for target, row in source.items() if "BUG: " in str(row.get("text", ""))]
        first = hits[0] if hits else next(iter(source))
        self.calls.append({"path": str(source[first]["path"]), "targets": list(source), "category": "rule_check"})
        if self.fail:
            raise RuntimeError("provider unavailable")
        usage = ModelCallUsage(calls=1, input_tokens=self.input_tokens, output_tokens=self.output_tokens)
        if not hits:
            result: dict[str, Any] = {
                "status": "satisfied",
                "target_ids": [],
                "title": "",
                "explanation": "",
                "consequence": "",
                "evidence_refs": [],
            }
        else:
            title = re.search(r"BUG: (.+)", str(source[first]["text"]))
            assert title is not None
            result = {
                "status": "violated",
                "target_ids": [first],
                "title": title.group(1).strip(),
                "explanation": f"The change in {source[first]['path']} {title.group(1).strip()}.",
                "consequence": "The rule no longer holds for this code.",
                "evidence_refs": [f"target:{first}"],
            }
        return StructuredExecution(result=result, driver="openai", model=self.model, usage=usage)

    def _hypotheses(self, prompt: dict[str, Any]) -> StructuredExecution:
        """The synthesis step (Thorough and Deep): one hypothesis per deterministic candidate."""

        self.calls.append({"path": "(synthesis)", "targets": [], "category": "hypotheses"})
        rows = [
            {
                "category": candidate["category"],
                "target_ids": candidate["target_ids"],
                "failure_mode": f"The changed {candidate['category']} path may be unsound.",
                "investigation_rationale": "The deterministic triage flagged it.",
                "priority_rationale": "It is on a consequential path.",
                "evidence_needed": "Trace the changed lines.",
                "rule_refs": candidate["rule_refs"],
            }
            for candidate in prompt["candidates"]["value"]
        ]
        usage = ModelCallUsage(calls=1, input_tokens=self.input_tokens, output_tokens=self.output_tokens)
        return StructuredExecution(result={"hypotheses": rows}, driver="openai", model=self.model, usage=usage)


# --------------------------------------------------------------------------- #
# a ready-made world: one repository, one pull request, a fake GitHub, a fake model
# --------------------------------------------------------------------------- #

BASE = {
    "src/auth.py": "def authorize(user):\n    return user.is_admin\n",
    "src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = value\n",
    "src/plain.py": "VALUE = 1\n",
}
AUTH_BUGGY = "def authorize(user):\n    if user is None:  # BUG: lets a missing principal through\n        return True\n    return user.is_admin\n"
AUTH_FIXED = "def authorize(user):\n    if user is None:\n        return False\n    return user.is_admin\n"
CACHE_BUGGY = "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE.setdefault(key, []).append(value)  # BUG: grows without a bound\n"
TOKEN_BUGGY = "def verify(token):\n    return token == 'x'  # BUG: compares secrets without a constant-time check\n"
FIRST_PUSH = {"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY, "src/plain.py": "VALUE = 2\n"}
RULE = "rules:\n  - stable_key: no-bug-markers\n    instruction: Source must not carry known defects.\n    paths: [src/auth.py]\n"


@dataclass
class World:
    origin: Origin
    github: FakeGitHub
    model: FakeModel
    root: Path
    monkeypatch: pytest.MonkeyPatch
    last_event: tuple[str, Path] = ("", Path())

    def review(self, **options: Any) -> RunReport:
        options.setdefault("publish", True)
        return run_pr_review(
            RunOptions(pr=str(NUMBER), repo_root=self.origin.clone, **options),
            runner=self.github,
            structured_executor=self.model,
        )

    def chat(
        self,
        body: str,
        *,
        thread: int | None = None,
        anchor: tuple[str, int] | None = None,
        author: str = "alice",
        user_type: str = "User",
        association: str | None = None,
        action: str = "created",
        fork: bool = False,
        env: dict[str, str] | None = None,
        **options: Any,
    ) -> RunReport:
        """A person comments *body* and the workflow runs on the comment event.

        ``thread`` is the id of the first comment of a review thread to reply in; ``anchor`` is ``(path, line)`` for a
        comment that starts a thread of its own; neither is a comment in the pull request's conversation.
        """

        user = {"login": author, "type": user_type}
        if thread is not None or anchor is not None:
            comment = self.github.add_review_comment(
                body,
                user=user,
                in_reply_to=thread or 0,
                path=anchor[0] if anchor else "",
                line=anchor[1] if anchor else 0,
                association=association,
            )
            name = "pull_request_review_comment"
            payload: dict[str, Any] = {
                "action": action,
                "comment": {
                    **comment,
                    "diff_hunk": "@@ -1,2 +1,4 @@\n def authorize(user):\n+    if user is None:",
                    "in_reply_to_id": thread,
                },
                "pull_request": {
                    "number": NUMBER,
                    "head": {"sha": self.origin.head, "repo": {"full_name": "mallory/widgets" if fork else SLUG}},
                    "base": {"repo": {"full_name": SLUG}},
                },
            }
            if thread is None:
                del payload["comment"]["in_reply_to_id"]
        else:
            comment = self.github.add_issue_comment(body, user=user, association=association)
            name = "issue_comment"
            payload = {
                "action": action,
                "issue": {
                    "number": NUMBER,
                    "pull_request": {"url": f"https://api.github.com/repos/{SLUG}/pulls/{NUMBER}"},
                },
                "comment": comment,
            }
        payload["sender"] = user
        path = self.root / f"event-{comment['id']}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.last_event = (name, path)
        return self.rerun_event(env=env, **options)

    def rerun_event(self, *, env: dict[str, str] | None = None, **options: Any) -> RunReport:
        """Run the job again for the last comment event, as "Re-run jobs" does."""

        name, path = self.last_event
        self.monkeypatch.setenv("GITHUB_EVENT_NAME", name)
        self.monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
        for key, value in (env or {}).items():
            self.monkeypatch.setenv(key, value)
        try:
            return self.review(**options)
        finally:
            self.monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
            self.monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> World:
    """Seed ``main`` with *files*; the environment is that of a GitHub Actions job with an OpenAI key."""

    for name in (
        "LEMONCROW_LLM_BACKEND",
        "LEMONCROW_OPENAI_MODEL",
        "LEMONCROW_LITELLM_MODEL",
        "LEMONCROW_REVIEW_MODEL",
        "ANTHROPIC_API_KEY",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_ACTOR",
        "LEMONCROW_REVIEW_MENTIONS",
        "LEMONCROW_REVIEW_CHAT_ASSOCIATIONS",
        "LEMONCROW_OPENGREP_BIN",
        "LEMONCROW_OPENGREP_NOTE",
    ):
        monkeypatch.delenv(name, raising=False)
    # The security scan is off unless a test turns it on: a developer's own `opengrep` on PATH must not change a result.
    monkeypatch.setenv("LEMONCROW_SECURITY_SCAN", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    origin = Origin(tmp_path)
    origin.seed_main(files)
    return World(origin, FakeGitHub(origin), FakeModel(), tmp_path, monkeypatch)
