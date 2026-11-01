"""Entire checkpoints and Agent Trace records as exact author provenance for the independence line."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import authorship, provenance
from lemoncrow.pro.capabilities.review.pr_bot.render import independence_line
from tests.gateway.pr_bot_support import BASE, SLUG, build_world

SHA_A = "a" * 40
SHA_B = "b" * 40
CP = "a3b2c4d5e6f7"
ULID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8"


def files(tree: dict[tuple[str, str], Any]) -> provenance.ReadFile:
    def read(ref: str, path: str) -> str | None:
        value = tree.get((ref, path))
        return value if value is None or isinstance(value, str) else json.dumps(value)

    return read


def entire_tree(checkpoint: str, *sessions: dict[str, Any]) -> dict[tuple[str, str], Any]:
    base = f"{checkpoint[:2]}/{checkpoint[2:]}"
    tree: dict[tuple[str, str], Any] = {
        (provenance.ENTIRE_BRANCH, f"{base}/metadata.json"): {
            "checkpoint_id": checkpoint,
            "sessions": [{"metadata": f"/{base}/{i}/metadata.json"} for i, _ in enumerate(sessions, 1)],
        }
    }
    for i, meta in enumerate(sessions, 1):
        tree[(provenance.ENTIRE_BRANCH, f"{base}/{i}/metadata.json")] = meta
    return tree


def test_checkpoint_trailers_are_counted_once_per_commit_and_only_as_whole_trailer_lines() -> None:
    messages = [
        f"feat: x\n\nEntire-Checkpoint: {CP}\n",
        f"fix: y\n\nEntire-Checkpoint: {CP}\nEntire-Checkpoint: {CP}\n",
        f"squash\n\nEntire-Checkpoint:\t{ULID}\n",
        f"prose mentions Entire-Checkpoint: {CP} inline",
        "Entire-Checkpoint: NOTANID\n",
    ]
    assert provenance.entire_checkpoints(messages) == {CP: 2, ULID: 1}


def test_entire_metadata_declares_agent_and_family() -> None:
    tree = entire_tree(CP, {"agent": "Claude Code", "model": "claude-sonnet-4-20250514"})
    [author] = provenance.entire_authors({CP: 3}, files(tree))
    assert author == authorship.DeclaredAuthor("Claude Code", "anthropic", provenance.ENTIRE_SOURCE, 3)


def test_entire_agent_name_alone_declares_a_family_only_for_single_vendor_agents() -> None:
    tree = {**entire_tree(CP, {"agent": "Codex", "model": ""}), **entire_tree(ULID, {"agent": "Cursor", "model": ""})}
    found = provenance.entire_authors({CP: 1, ULID: 1}, files(tree))
    assert {(a.agent, a.family) for a in found} == {("Codex", "openai"), ("Cursor", "")}


def test_unreadable_checkpoint_metadata_is_an_agent_of_undeclared_family() -> None:
    [author] = provenance.entire_authors({CP: 2}, files({}))
    assert (author.agent, author.family, author.commits) == (provenance.UNNAMED, "", 2)


def test_legacy_single_session_root_metadata_is_read() -> None:
    tree = {(provenance.ENTIRE_BRANCH, f"{CP[:2]}/{CP[2:]}/metadata.json"): {"agent": "Gemini CLI", "model": ""}}
    [author] = provenance.entire_authors({CP: 1}, files(tree))
    assert (author.agent, author.family) == ("Gemini CLI", "google")


def test_names_from_metadata_are_plain_words_never_markup() -> None:
    tree = entire_tree(CP, {"agent": "[click](https://evil.example)", "model": ""})
    [author] = provenance.entire_authors({CP: 1}, files(tree))
    assert author.agent == provenance.UNNAMED
    assert provenance.plain_name("  Claude   Code ") == "Claude Code"
    assert provenance.plain_name("x" * 41) == ""


def trace(revision: str, *contributors: dict[str, Any], tool: str = "cursor") -> dict[str, Any]:
    return {
        "version": "0.1.0",
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "timestamp": "2026-01-25T10:00:00Z",
        "vcs": {"type": "git", "revision": revision},
        "tool": {"name": tool, "version": "2.4.0"},
        "files": [
            {"path": "src/app.ts", "conversations": [{"contributor": c, "ranges": [{"start_line": 1, "end_line": 9}]}]}
            for c in contributors
        ],
    }


def test_agent_trace_counts_ai_and_mixed_contributors_of_the_pull_requests_commits_only() -> None:
    records = [
        trace(SHA_A, {"type": "ai", "model_id": "anthropic/claude-opus-4-5-20251101"}),
        trace(SHA_B[:12], {"type": "mixed", "model_id": "anthropic/claude-sonnet-4-20250514"}),
        trace(SHA_A, {"type": "human"}, {"type": "unknown"}),
        trace("c" * 40, {"type": "ai", "model_id": "openai/gpt-4o"}),
        {"version": "0.1.0", "files": [{"path": "x", "conversations": [{"contributor": {"type": "ai"}}]}]},
    ]
    [author] = provenance.trace_authors(records, [SHA_A, SHA_B])
    assert author == authorship.DeclaredAuthor("cursor", "anthropic", provenance.TRACE_SOURCE, 2)


def test_agent_trace_range_override_and_model_less_ai_contributor() -> None:
    record = trace(SHA_A, {"type": "human"})
    record["files"][0]["conversations"][0]["ranges"].append(
        {"start_line": 10, "end_line": 12, "contributor": {"type": "ai"}}
    )
    [author] = provenance.trace_authors([record], [SHA_A])
    assert (author.agent, author.family) == ("cursor", "")


def test_trace_files_accept_object_array_and_json_lines() -> None:
    one = trace(SHA_A, {"type": "ai"})
    assert provenance.trace_records(json.dumps(one)) == [one]
    assert provenance.trace_records(json.dumps([one, one, 3])) == [one, one]
    assert provenance.trace_records(json.dumps(one) + "\n\nnot json\n" + json.dumps(one)) == [one, one]


def test_declared_extra_reads_entire_and_agent_trace_into_the_independence_line() -> None:
    tree = entire_tree(CP, {"agent": "Claude Code", "model": "claude-opus-4-5"})
    tree[(SHA_B, ".agent-trace/traces.jsonl")] = json.dumps(trace(SHA_B, {"type": "ai", "model_id": "openai/gpt-4o"}))
    commits = [(SHA_A, f"feat\n\nEntire-Checkpoint: {CP}\n"), (SHA_B, "fix")]
    extra = provenance.declared_extra(
        commits,
        head=SHA_B,
        read_file=files(tree),
        list_dir=lambda ref, path: [".agent-trace/traces.jsonl", ".agent-trace/README.md"] if ref == SHA_B else None,
    )
    authors = authorship.declared_authors([message for _, message in commits], extra=extra)
    found = authorship.assess("litellm/google/gemini-2.5-pro", authors, commits=2)
    assert found.status == "independent"
    line = independence_line(found)
    assert "Claude Code (Anthropic, Entire checkpoint in 1 of 2 commits)" in line
    assert "cursor (OpenAI, Agent Trace record in 1 of 2 commits)" in line
    same = authorship.assess("litellm/anthropic/claude-sonnet-4-5", authors, commits=2)
    assert same.status == "same family"


def test_github_files_reads_contents_api_and_never_raises() -> None:
    calls: list[list[str]] = []
    payload = {"type": "file", "size": 5, "content": base64.b64encode(b"hello").decode()}

    def runner(args: Any, stdin: str | None = None) -> str:
        calls.append(list(args))
        if "missing" in args[1]:
            from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

            raise GitHubCliError("HTTP 404")
        if "dir" in args[1]:
            return json.dumps([{"type": "file", "path": "dir/a.json"}, {"type": "dir", "path": "dir/sub"}])
        return json.dumps(payload)

    gh = provenance.GitHubFiles(runner, "o/r")
    assert gh.read_file(provenance.ENTIRE_BRANCH, "a3/b2/metadata.json") == "hello"
    assert calls[0] == ["api", "repos/o/r/contents/a3/b2/metadata.json?ref=entire%2Fcheckpoints%2Fv1"]
    assert gh.read_file("main", "missing") is None
    assert gh.list_dir("main", "dir") == ["dir/a.json"]
    assert gh.list_dir("main", "missing") is None


def test_the_bot_reads_an_entire_checkpoint_into_the_independence_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    head = world.origin.push({"src/plain.py": "VALUE = 2\n"}, message=f"feat\n\nEntire-Checkpoint: {CP}")
    world.github.commit_messages = [f"feat\n\nEntire-Checkpoint: {CP}\n"]
    for (ref, path), meta in entire_tree(CP, {"agent": "Claude Code", "model": "claude-sonnet-4-5"}).items():
        world.github.contents[(ref, path)] = json.dumps(meta)
    report = world.review()
    assert report.independence is not None and report.independence.status == "same family"
    body = world.github.summary_comments()[0]["body"]
    assert "Claude Code (Anthropic, Entire checkpoint in 1 of 1 commits)" in body and "**same family**" in body
    assert ("GET", f"repos/{SLUG}/contents/.agent-trace?ref={head}") in world.github.calls
