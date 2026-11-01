"""Exact agent provenance beyond ``Co-authored-by``: Entire checkpoints and Agent Trace records.

Both feed :func:`authorship.declared_authors` through its *extra* argument, so the independence line can name the
author's model family when a tool recorded it, never when it is only plausible.

* **Entire** (`entireio/cli`) writes one ``Entire-Checkpoint: <id>`` trailer on each commit an agent session produced
  (a 12-hex id, or a 26-character ULID). The agent and model are not in the trailer: they are in the checkpoint's
  session metadata on the ``entire/checkpoints/v1`` branch, at ``<id[:2]>/<id[2:]>/metadata.json`` (the summary, whose
  ``sessions[].metadata`` names each session's ``metadata.json`` with ``agent`` and ``model``). A trailer whose metadata
  cannot be read still declares that an agent wrote the commit, of a family not declared.
* **Agent Trace** (agent-trace.dev, v0.1) is a JSON trace record linking file ranges to a contributor of type ``ai``,
  ``mixed``, ``human`` or ``unknown``, with an optional ``model_id`` (``anthropic/claude-...``). The spec leaves storage
  to the tool; LemonCrow reads the records committed under ``.agent-trace/`` at the pull request's head (JSON, a JSON
  array or JSON Lines). Only a record whose ``vcs`` is git and whose ``revision`` is one of the pull request's commits
  counts; ``ai`` and ``mixed`` contributors declare an agent, ``human`` and ``unknown`` declare none.

Every name read from either source is reduced to plain words before it reaches the summary: both are written by
whoever can push, so they are declarations, as trailers are, and never markup.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any, Final
from urllib.parse import quote

from lemoncrow.pro.capabilities.review.model_identity import clean_model_name, model_family
from lemoncrow.pro.capabilities.review.pr_bot.authorship import FAMILY_NAMES, DeclaredAuthor
from lemoncrow.pro.capabilities.review.sources.github import GhRunner, GitHubCliError, gh_json

ENTIRE_BRANCH: Final = "entire/checkpoints/v1"
ENTIRE_SOURCE: Final = "Entire checkpoint"
TRACE_DIR: Final = ".agent-trace"
TRACE_SOURCE: Final = "Agent Trace record"
# lc-debt: at most 20 checkpoints x 5 sessions and 20 trace files of 1 MB each are read per review, in API calls one by
# one; a pull request past that reports the rest as an agent of undeclared family. Upgrade: one git tree read per ref.
MAX_CHECKPOINTS: Final = 20
MAX_SESSIONS: Final = 5
MAX_TRACE_FILES: Final = 20
MAX_TRACE_BYTES: Final = 1_000_000

#: ``(ref, path) -> text``, ``None`` when absent or unreadable. Never raises.
ReadFile = Callable[[str, str], str | None]
#: ``(ref, directory) -> file paths``, ``None`` when the directory is absent or unreadable. Never raises.
ListDir = Callable[[str, str], list[str] | None]

_CHECKPOINT: Final = re.compile(r"^Entire-Checkpoint:[ \t]*([0-9a-f]{12}|[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26})[ \t]*$", re.M)
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}")
#: The family an agent's name declares when its metadata names no model: an agent that runs one vendor's models only.
_AGENT_FAMILY: Final[dict[str, str]] = {"claude code": "anthropic", "codex": "openai", "gemini cli": "google"}
UNNAMED: Final = "an agent"


def plain_name(value: object) -> str:
    """*value* as at most 40 plain characters (letters, digits, space, ``._+-``), or ``""``."""

    if not isinstance(value, str):
        return ""
    found = _NAME.fullmatch(" ".join(value.split()))
    return found[0].strip() if found else ""


def _family(model: str, agent: str = "") -> str:
    """The family a model id declares (``anthropic/claude-x``, ``claude-x``), else the one the agent's name declares."""

    if model:
        provider, _, name = model.partition("/")
        if name and provider.lower() in FAMILY_NAMES:
            return provider.lower()
        family = model_family(model.rsplit("/", 1)[-1])
        if family:
            return family
    return _AGENT_FAMILY.get(agent.lower(), "")


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _count(found: dict[tuple[str, str], int], agent: str, family: str, commits: int) -> None:
    key = (agent or UNNAMED, family)
    found[key] = found.get(key, 0) + max(commits, 1)


# --------------------------------------------------------------------------- #
# Entire
# --------------------------------------------------------------------------- #


def entire_checkpoints(messages: Iterable[str]) -> dict[str, int]:
    """Each checkpoint id the commit *messages* carry as an ``Entire-Checkpoint`` trailer, with how many commits do."""

    found: dict[str, int] = {}
    for message in messages:
        for checkpoint in dict.fromkeys(_CHECKPOINT.findall(message or "")):
            found[checkpoint] = found.get(checkpoint, 0) + 1
    return found


def _sessions(summary: object) -> list[str]:
    rows = summary.get("sessions") if isinstance(summary, dict) else None
    paths = [str(row.get("metadata") or "").lstrip("/") for row in rows or () if isinstance(row, dict)]
    return [path for path in paths if path.endswith("metadata.json") and ".." not in path][:MAX_SESSIONS]


def entire_authors(checkpoints: Mapping[str, int], read_file: ReadFile) -> list[DeclaredAuthor]:
    """The agents the *checkpoints*' session metadata declares, each counted by the commits citing its checkpoint."""

    found: dict[tuple[str, str], int] = {}
    for index, (checkpoint, commits) in enumerate(checkpoints.items()):
        summary = (
            _json(read_file(ENTIRE_BRANCH, f"{checkpoint[:2]}/{checkpoint[2:]}/metadata.json"))
            if index < MAX_CHECKPOINTS
            else None
        )
        sessions = _sessions(summary)
        # An older single-session checkpoint keeps the session's own metadata at the root.
        metas = [_json(read_file(ENTIRE_BRANCH, path)) for path in sessions] if sessions else [summary]
        agents: dict[tuple[str, str], None] = {}
        for meta in metas:
            if not isinstance(meta, dict) or ("agent" not in meta and "model" not in meta):
                continue
            agent = plain_name(meta.get("agent"))
            agents[(agent or UNNAMED, _family(clean_model_name(meta.get("model")), agent))] = None
        for agent, family in agents or {(UNNAMED, ""): None}:
            _count(found, agent, family, commits)
    return [DeclaredAuthor(agent, family, ENTIRE_SOURCE, n) for (agent, family), n in found.items()]


# --------------------------------------------------------------------------- #
# Agent Trace
# --------------------------------------------------------------------------- #


def trace_records(text: str) -> list[dict[str, Any]]:
    """The trace records in one file: a JSON object, a JSON array of them, or one per line (JSON Lines)."""

    whole = _json(text)
    if isinstance(whole, dict):
        return [whole]
    if isinstance(whole, list):
        return [row for row in whole if isinstance(row, dict)]
    rows = (_json(line) for line in text.splitlines() if line.strip())
    return [row for row in rows if isinstance(row, dict)]


def _rows(value: object) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _contributors(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for file in _rows(record.get("files")):
        for conversation in _rows(file.get("conversations")):
            if isinstance(conversation.get("contributor"), dict):
                yield conversation["contributor"]
            for row in _rows(conversation.get("ranges")):
                if isinstance(row.get("contributor"), dict):
                    yield row["contributor"]


def _revision(record: Mapping[str, Any], revisions: Collection[str]) -> str:
    """The pull request commit *record* was recorded at, or ``""``: no git ``vcs``, or a commit not in the PR."""

    vcs = record.get("vcs")
    if not isinstance(vcs, dict) or vcs.get("type") != "git":
        return ""
    revision = str(vcs.get("revision") or "").strip().lower()
    if len(revision) < 12:
        return ""
    return next((sha for sha in revisions if sha.lower().startswith(revision)), "")


def trace_authors(records: Iterable[Mapping[str, Any]], revisions: Collection[str]) -> list[DeclaredAuthor]:
    """The agents Agent Trace *records* of the pull request's commits (*revisions*) declare, per commit they cover."""

    per_commit: dict[tuple[str, str], set[str]] = {}
    for record in records:
        revision = _revision(record, revisions)
        if not revision:
            continue
        tool = record.get("tool")
        agent = plain_name(tool.get("name")) if isinstance(tool, dict) else ""
        for contributor in _contributors(record):
            if contributor.get("type") not in {"ai", "mixed"}:
                continue
            family = _family(clean_model_name(contributor.get("model_id")), agent)
            per_commit.setdefault((agent or UNNAMED, family), set()).add(revision)
    return [DeclaredAuthor(agent, family, TRACE_SOURCE, len(shas)) for (agent, family), shas in per_commit.items()]


def read_traces(head: str, list_dir: ListDir, read_file: ReadFile) -> list[dict[str, Any]]:
    """Every trace record committed under ``.agent-trace/`` at *head*, within the file and size bounds."""

    paths = sorted(path for path in list_dir(head, TRACE_DIR) or () if path.endswith((".json", ".jsonl")))
    records: list[dict[str, Any]] = []
    for path in paths[:MAX_TRACE_FILES]:
        text = read_file(head, path)
        if text and len(text) <= MAX_TRACE_BYTES:
            records.extend(trace_records(text))
    return records


# --------------------------------------------------------------------------- #
# both, over the GitHub API
# --------------------------------------------------------------------------- #


def declared_extra(
    commits: Sequence[tuple[str, str]], *, head: str, read_file: ReadFile, list_dir: ListDir
) -> list[DeclaredAuthor]:
    """Entire and Agent Trace authors of a pull request whose *commits* are ``(sha, message)`` pairs. Never raises."""

    authors = entire_authors(entire_checkpoints(message for _, message in commits), read_file)
    revisions = [sha for sha, _ in commits if sha]
    if revisions:
        authors.extend(trace_authors(read_traces(head, list_dir, read_file), revisions))
    return authors


class GitHubFiles:
    """Read-only file access on one repository through the contents API (``contents: read``)."""

    def __init__(self, runner: GhRunner, slug: str) -> None:
        self.runner = runner
        self.slug = slug

    def _get(self, ref: str, path: str) -> Any:
        try:
            return gh_json(self.runner, ["api", f"repos/{self.slug}/contents/{quote(path)}?ref={quote(ref, safe='')}"])
        except GitHubCliError:
            return None

    def read_file(self, ref: str, path: str) -> str | None:
        row = self._get(ref, path)
        if not isinstance(row, dict) or row.get("type") != "file":
            return None
        size = row.get("size")
        if not isinstance(size, int) or size > MAX_TRACE_BYTES:
            return None
        try:
            return base64.b64decode(str(row.get("content") or "")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None

    def list_dir(self, ref: str, path: str) -> list[str] | None:
        rows = self._get(ref, path)
        if not isinstance(rows, list):
            return None
        return [
            str(row["path"]) for row in rows if isinstance(row, dict) and row.get("type") == "file" and row.get("path")
        ]


__all__ = [
    "ENTIRE_BRANCH",
    "ENTIRE_SOURCE",
    "TRACE_DIR",
    "TRACE_SOURCE",
    "GitHubFiles",
    "declared_extra",
    "entire_authors",
    "entire_checkpoints",
    "plain_name",
    "read_traces",
    "trace_authors",
    "trace_records",
]
