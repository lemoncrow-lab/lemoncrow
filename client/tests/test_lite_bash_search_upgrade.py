"""Bash source discovery upgrades once to code_search; fallback never cycles."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.lite import mcp as lite_mcp
from lemoncrow_client.lite.bash_search_upgrade import (
    bash_code_search_args,
    bash_code_search_batch_args,
    bash_source_search_redirect,
)
from lemoncrow_client.lite.index import LiteIndex
from lemoncrow_client.lite.mcp import LiteMcp
from lemoncrow_client.trace import summarize_code_search


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "src" / "module.py").write_text("def distinct_symbol():\n    pass\n")
    return root


def _env(repo: Path, tmp_path: Path) -> dict[str, str]:
    return {"LEMONCROW_WORKSPACE_ROOT": str(repo), "LEMONCROW_HOME": str(tmp_path / "state")}


@pytest.mark.parametrize(
    "command",
    [
        "rg distinct_symbol src",
        "rg -n distinct_symbol src",
        "grep -rn distinct_symbol src",
        "grep --recursive --line-number distinct_symbol src",
        "grep -n distinct_symbol src/module.py",
        "rg distinct_symbol src/module.py",
        "git grep -n distinct_symbol -- src",
        "git grep -n distinct_symbol -- src/module.py",
    ],
)
def test_literal_directory_source_search_is_eligible(repo: Path, command: str) -> None:
    scope = "src/module.py" if "module.py" in command else "src"
    assert bash_code_search_args({"command": command}, repo) == {"query": "distinct_symbol", "paths": str(repo / scope)}


@pytest.mark.parametrize(
    "command",
    [
        "rg distinct_symbol src | head -10",
        "rg -n distinct_symbol src | head -n 20",
        "grep -n distinct_symbol src/module.py | head -20",
        "rg distinct_symbol src/module.py | head -20",
    ],
)
def test_bounded_source_discovery_upgrades(repo: Path, command: str) -> None:
    scope = "src/module.py" if "module.py" in command else "src"
    assert bash_code_search_args({"command": command}, repo) == {
        "query": "distinct_symbol",
        "paths": str(repo / scope),
    }


def test_safe_alternation_upgrades(repo: Path) -> None:
    for command in (
        r"rg 'distinct_symbol|other_symbol' src",
        r"grep -rn 'distinct_symbol\|other_symbol' src",
        "git grep -n -E 'distinct_symbol|other_symbol' -- src",
    ):
        assert bash_code_search_args({"command": command}, repo) == {
            "query": "distinct_symbol other_symbol",
            "paths": str(repo / "src"),
        }


def test_explicit_cd_uses_the_destination_checkout(repo: Path, tmp_path: Path) -> None:
    assert bash_code_search_args({"command": f"cd {repo} && rg distinct_symbol src"}, tmp_path) == {
        "query": "distinct_symbol",
        "paths": str(repo / "src"),
    }


@pytest.mark.parametrize(
    "command",
    [
        "rg distinct_symbol src | wc -l",
        "rg distinct_symbol src && echo yes",
        "rg distinct_symbol src; echo done",
        "rg -i distinct_symbol src",
        "rg 'distinct.*' src",
        "rg distinct_symbol src > output.txt",
        "rg -l distinct_symbol src",
        "grep -n distinct_symbol src/module.py | wc -l",
        "git grep -n distinct_symbol -- src/module.py | wc -l",
        "git grep -n -i distinct_symbol -- src",
        "git grep -q distinct_symbol -- src",
        "git grep -n 'distinct.*' -- src",
        "git status --short | grep distinct_symbol",
        "grep -rn 'foo|bar' src",
        "grep -rn distinct_symbol src # lc:allow-shell-search",
        "cd src && rg distinct_symbol . | wc -l",
    ],
)
def test_exact_shell_semantics_are_never_rewritten(repo: Path, command: str) -> None:
    assert bash_code_search_args({"command": command}, repo) is None


def test_non_git_directory_and_special_bash_modes_are_not_upgraded(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "ordinary"
    outside.mkdir()
    other = tmp_path / "other-repo"
    other.mkdir()
    (other / ".git").mkdir()
    assert bash_code_search_args({"command": "rg distinct_symbol ."}, outside) is None
    assert bash_code_search_args({"command": f"rg distinct_symbol {other}"}, repo) is None
    assert bash_code_search_args({"command": f"cd {other} && rg distinct_symbol ."}, repo) is None
    for extra in ({"bg": True}, {"id": "running"}, {"action": "poll"}, {"interactive": True}):
        assert bash_code_search_args({"command": "rg distinct_symbol src", **extra}, repo) is None
    assert bash_code_search_args({"command": ["rg distinct_symbol src"]}, repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn distinct_symbol src; rg distinct_symbol src/module.py",
        "echo source; git grep -n distinct_symbol -- src; echo more; grep -rn distinct_symbol src",
    ],
)
def test_safe_source_only_batch_is_eligible(repo: Path, command: str) -> None:
    batch = bash_code_search_batch_args({"command": command}, repo)
    assert batch is not None
    assert len(batch) == 2
    assert all(item["query"] == "distinct_symbol" for _, item in batch)


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn distinct_symbol src; git status --short",
        "grep -rn distinct_symbol src; echo done",
        "grep -rn distinct_symbol src; grep -rn distinct_symbol src | wc -l",
        "grep -rn distinct_symbol src && rg distinct_symbol src",
        "grep -rn distinct_symbol src; touch src/newfile",
        "grep -rn distinct_symbol src; echo $HOME; grep -rn distinct_symbol src",
        "grep -rn distinct_symbol src; echo done > result.txt; rg distinct_symbol src",
    ],
)
def test_mixed_or_exact_shell_batch_is_not_rewritten(repo: Path, command: str) -> None:
    assert bash_code_search_batch_args({"command": command}, repo) is None


def test_source_batch_preserves_cd_between_commands(repo: Path) -> None:
    command = f"cd {repo} && grep -rn distinct_symbol src; rg distinct_symbol src/module.py"
    batch = bash_code_search_batch_args({"command": command}, repo)
    assert batch is not None
    assert len(batch) == 2
    assert batch[0][1]["paths"] == str(repo / "src")
    assert batch[1][1]["paths"] == str(repo / "src/module.py")


@pytest.mark.parametrize(
    "command",
    [
        "grep -n -E 'distinct.*' src/module.py | head -10",
        "echo source; sed -n '1,2p' src/module.py; grep -n 'distinct.*' src/module.py | head -10",
        "git grep -n -E 'distinct.*' -- src | head -10",
    ],
)
def test_read_only_source_search_requires_index_when_unconvertible(repo: Path, command: str) -> None:
    assert bash_source_search_redirect({"command": command}, repo)


@pytest.mark.parametrize(
    "command",
    [
        "grep -n -E 'distinct.*' src/module.py | wc -l",
        "grep -n -E 'distinct.*' src/module.py | grep -v skip",
        "grep -n -E 'distinct.*' src/module.py && echo done",
        "grep -n -E 'distinct.*' src/module.py; make test",
        "grep -n -E 'distinct.*' src/module.py; touch src/newfile",
        "sed -i 's/distinct/new/' src/module.py; grep -n -E 'distinct.*' src/module.py",
        "grep -n 'distinct.*' src/module.py | head -5 # lc:allow-shell-search",
        "grep -E 'PASS|FAIL' /tmp/tests.log | head -5",
        "ps aux | grep -E 'pytest|node' | head -10",
        "git log --oneline --grep 'distinct.*'",
    ],
)
def test_source_search_guard_preserves_exact_output_and_operations(repo: Path, command: str) -> None:
    assert not bash_source_search_redirect({"command": command}, repo)


def _text(answer: dict[str, Any]) -> str:
    return "\n".join(str(item.get("text", "")) for item in answer.get("content", []))


def test_real_lite_bash_uses_index_without_running_shell(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite = LiteMcp(environment=_env(repo, tmp_path))
    try:
        lite.prepare()

        def never_shell(*_args: object, **_kwargs: object) -> Any:
            pytest.fail("Bash was executed despite an eligible indexed search")

        monkeypatch.setitem(lite_mcp._LOCAL_EXECUTORS, "bash", never_shell)
        answer = lite.call("bash", {"command": "rg distinct_symbol src"})
        assert answer["isError"] is False
        assert _text(answer).startswith("[lc:auto_upgraded bash -> code_search]")
        assert "src/module.py" in _text(answer)
        assert answer["structuredContent"]["files"]
        for command in (
            "rg -n distinct_symbol src | head -10",
            "grep -n distinct_symbol src/module.py",
            "git grep -n distinct_symbol -- src",
        ):
            indexed = lite.call("bash", {"command": command})
            assert indexed["isError"] is False
            assert _text(indexed).startswith("[lc:auto_upgraded bash -> code_search]")
    finally:
        lite.close()


def test_real_lite_bash_batches_multiple_source_lookups_without_running_shell(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite = LiteMcp(environment=_env(repo, tmp_path))
    try:
        lite.prepare()

        def never_shell(*_args: object, **_kwargs: object) -> Any:
            pytest.fail("Bash executed despite a source-only lookup batch")

        monkeypatch.setitem(lite_mcp._LOCAL_EXECUTORS, "bash", never_shell)
        answer = lite.call(
            "bash", {"command": "echo source; grep -rn distinct_symbol src; git grep -n distinct_symbol -- src"}
        )
        assert answer["isError"] is False
        assert _text(answer).startswith("[lc:auto_upgraded bash batch -> code_search]")
        assert "[indexed source lookup 1] source" in _text(answer)
        assert len(answer["structuredContent"]["searches"]) == 2
    finally:
        lite.close()


def test_claude_mcp_advertises_search_first_without_disabling_bash(repo: Path, tmp_path: Path) -> None:
    lite = LiteMcp(environment=_env(repo, tmp_path))
    try:
        response = lite.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert response is not None
        instructions = response["result"]["instructions"]
        assert "code_search first" in instructions
        assert "Do not combine source grep/rg" in instructions
        assert "logs/processes" in instructions
        descriptions = {tool["name"]: tool["description"] for tool in lite.tools}
        assert "FIRST for Git source discovery" in descriptions["code_search"]
        assert "Use code_search for Git source discovery" in descriptions["bash"]
    finally:
        lite.close()


def test_trace_counts_indexed_upgrades_and_preserved_shell_searches(repo: Path, tmp_path: Path) -> None:
    lite = LiteMcp(environment={**_env(repo, tmp_path), "LEMONCROW_RUNTIME_TRACE": "redacted"})
    try:
        lite.prepare()
        assert "[lc:auto_upgraded" in _text(lite.call("bash", {"command": "rg distinct_symbol src"}))
        assert "1" in _text(lite.call("bash", {"command": "grep -n distinct_symbol src/module.py | wc -l"}))
        assert "ok" in _text(lite.call("bash", {"command": "printf ok"}))
        redirect = lite.call("bash", {"command": "grep -n 'distinct.*' src/module.py | head -10"})
        assert redirect["isError"] is True
        assert "[lc:code_search_required]" in _text(redirect)
        assert "distinct_symbol" in _text(lite.call("code_search", {"query": "distinct_symbol"}))
        stats = summarize_code_search(tmp_path / "state" / "runtime" / "traces")
        assert stats["indexed"] == 1
        assert stats["total"] == 1  # direct code_search only, no double counting
        assert stats["bash_calls"] == 4
        assert stats["bash_rg_grep"] == 3
        assert stats["bash_auto_upgraded"] == 1
        assert stats["bash_source_search_redirected"] == 1
        assert stats["bash_rg_grep_preserved"] == 1
        # Metrics count tool selection without leaking raw search queries.
        trace_text = "\n".join(str(path.read_text()) for path in (tmp_path / "state").rglob("client-*.jsonl"))
        assert "distinct_symbol" not in trace_text
    finally:
        lite.close()


def test_index_failure_runs_original_bash_once_without_loop(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite = LiteMcp(environment=_env(repo, tmp_path))
    try:
        shell_calls: list[dict[str, Any]] = []
        index_calls: list[str] = []
        real_shell = lite_mcp._LOCAL_EXECUTORS["bash"]

        def failed_index(_index: LiteIndex, query: str, **_kwargs: object) -> Any:
            index_calls.append(query)
            raise RuntimeError("index unavailable")

        def count_shell(context: Any, args: Any) -> Any:
            shell_calls.append(dict(args))
            return real_shell(context, args)

        monkeypatch.setattr(LiteIndex, "search", failed_index)
        monkeypatch.setitem(lite_mcp._LOCAL_EXECUTORS, "bash", count_shell)
        # Plain grep is available without a separate ripgrep installation.
        answer = lite.call("bash", {"command": "grep -rn distinct_symbol src"})
        assert answer["isError"] is False
        assert len(index_calls) == 1
        assert len(shell_calls) == 1
        assert "distinct_symbol" in _text(answer)
        assert "[lc:auto_upgraded" not in _text(answer)

        # Once the index failed, the next shell fallback is never re-upgraded.
        second = lite.call("bash", {"command": "grep -rn distinct_symbol src"})
        assert second["isError"] is False
        assert len(index_calls) == 1
        assert len(shell_calls) == 2
    finally:
        lite.close()


def test_failed_direct_code_search_does_not_reupgrade_followup_grep(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite = LiteMcp(environment=_env(repo, tmp_path))
    try:
        index_calls: list[str] = []

        def failed_index(_index: LiteIndex, query: str, **_kwargs: object) -> Any:
            index_calls.append(query)
            raise RuntimeError("index unavailable")

        monkeypatch.setattr(LiteIndex, "search", failed_index)
        first = lite.call("code_search", {"query": "distinct_symbol", "paths": "src"})
        # The local code_search fallback returns usable grep results, not an error.
        assert first["isError"] is False
        assert "[lc:index_fallback" in _text(first)
        # A different grep query after the failure stays in shell mode.
        second = lite.call("bash", {"command": "grep -rn def src"})
        assert second["isError"] is False
        assert "def" in _text(second)
        assert len(index_calls) == 1
    finally:
        lite.close()


def test_empty_index_falls_back_to_original_bash_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite = LiteMcp(environment=_env(repo, tmp_path))
    try:
        monkeypatch.setattr(
            LiteIndex,
            "search",
            lambda _self, query, **_kwargs: {
                "query": query,
                "files": [],
                "candidate_files": [],
                "related_symbols": [],
            },
        )
        answer = lite.call("bash", {"command": "grep -rn distinct_symbol src"})
        assert answer["isError"] is False
        assert "[lc:auto_upgraded" not in _text(answer)
        assert "distinct_symbol" in _text(answer)
    finally:
        lite.close()
