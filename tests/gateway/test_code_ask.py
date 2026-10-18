"""Code Ask keeps live source scoped and streams the same chat lifecycle as Review Ask."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.service import code_ask, code_map
from lemoncrow.core.service.api import create_app
from lemoncrow.pro.capabilities.review import intelligence_drivers, intelligence_runtime

FastAPITestClient = pytest.importorskip("fastapi.testclient").TestClient


def test_code_ask_grounds_selected_lines_and_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "src" / "payment.py"
    source.parent.mkdir()
    source.write_text("def charge():\n    return retry(2)\n", encoding="utf-8")
    seen: list[dict[str, str]] = []
    deltas: list[str] = []

    routes: list[tuple[str, str]] = []

    def fake_chat(messages: list[dict[str, str]], **kwargs: Any) -> str:
        seen.extend(messages)
        routes.append((kwargs["backend"], kwargs["model"]))
        kwargs["on_delta"]("Retries ")
        kwargs["on_delta"]("twice.")
        return "Retries twice."

    # Code Ask resolves its route through the intelligence policy before any model call.
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "ollama")
    monkeypatch.setenv("LEMONCROW_OLLAMA_MODEL", "qwen-test")
    monkeypatch.setattr(code_ask, "chat_stream", fake_chat)
    result = code_ask.answer_code_ask(
        root=tmp_path,
        path="src/payment.py",
        question="Why retry?",
        line=2,
        end_line=2,
        symbol="charge",
        selected_text="return retry(2)",
        attachments=[{"name": "notes.txt", "content": "Retry budget is two."}],
        history=[{"role": "user", "body": "What is this?"}, {"role": "assistant", "body": "A payment helper."}],
        on_delta=deltas.append,
    )
    assert result["body"] == "Retries twice."
    assert routes == [("ollama", "qwen-test")]
    assert result["references"] == [{"path": "src/payment.py", "start_line": 2, "end_line": 2}]
    assert deltas == ["Retries ", "twice."]
    assert seen[1:3] == [
        {"role": "user", "content": "What is this?"},
        {"role": "assistant", "content": "A payment helper."},
    ]
    assert "src/payment.py:L2-L2" in seen[-1]["content"]
    assert "2:     return retry(2)" in seen[-1]["content"]
    assert "Exact selected text:\nreturn retry(2)" in seen[-1]["content"]
    assert "Attached file notes.txt:\nRetry budget is two." in seen[-1]["content"]
    assert str(tmp_path) not in seen[-1]["content"]

    with pytest.raises(ValueError, match="inside the selected project"):
        code_ask.answer_code_ask(root=tmp_path, path="../outside.py", question="Read it", on_delta=lambda _: None)


def test_code_ask_stream_hides_split_workspace_paths(tmp_path: Path) -> None:
    deltas: list[str] = []
    redactor = code_ask._RootRedactor(tmp_path, deltas.append)
    root = str(tmp_path)
    redactor.feed(f"See {root[:8]}")
    redactor.feed(f"{root[8:]}/src/payment.py")
    redactor.finish()
    assert "".join(deltas) == "See ./src/payment.py"


def _cli_code_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str, event: dict[str, Any], commands: list[list[str]]
) -> dict[str, Any]:
    """Code Ask over an agent CLI, through route resolution and the private executor."""

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "payment.py").write_text("def charge():\n    return retry(2)\n", encoding="utf-8")
    monkeypatch.setattr(intelligence_drivers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        intelligence_drivers.shutil, "copytree", lambda *args, **kwargs: pytest.fail("Code Ask copied the workspace")
    )

    def fake_stream(command: list[str], *, cwd: Path, env: Any, on_stdout: Any, timeout: float) -> tuple[int, str, str]:
        commands.append(list(command))
        line = json.dumps(event) + "\n"
        on_stdout(line)
        return 0, line, ""

    monkeypatch.setattr(intelligence_drivers, "_run_streaming_process", fake_stream)
    result = code_ask.answer_code_ask(
        root=workspace, path="payment.py", question="Inspect payment.py", backend=driver, on_delta=lambda _: None
    )
    return result


_ANSWER = {"intent": "question", "body": "Read-only answer", "handoff_instruction": ""}


def test_code_cli_ask_has_read_only_tools_without_workspace_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_ANSWER)}}
    commands: list[list[str]] = []
    result = _cli_code_ask(tmp_path, monkeypatch, "codex", event, commands)
    assert result["body"] == "Read-only answer"
    rendered = " ".join(commands[0])
    assert 'mcp_servers.lemoncrow.enabled_tools=["code_search","read","grep","web_fetch"]' in rendered
    assert "LEMONCROW_BASH_WORKSPACE_ROOT" not in rendered
    assert "Code context:" in rendered


def test_claude_code_ask_disables_builtin_write_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    result = _cli_code_ask(tmp_path, monkeypatch, "claude", {"type": "result", "structured_output": _ANSWER}, commands)
    assert result["body"] == "Read-only answer"
    command = commands[0]
    assert command[command.index("--tools") + 1] == ""
    disallowed = set(command[command.index("--disallowedTools") + 1].split(","))
    assert {
        "Bash",
        "Edit",
        "Write",
        "NotebookEdit",
        "Task",
        "WebFetch",
        "WebSearch",
        "mcp__lemoncrow__bash",
    } <= disallowed
    assert "mcp__lemoncrow__bash" not in command[command.index("--allowedTools") + 1]


def test_code_ask_on_a_host_that_forbids_agent_clis_starts_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.infra.internal_llm import InternalLLMError

    # Code Ask resolves its route like every Review Intelligence call; the host setting refuses it.
    monkeypatch.setattr(intelligence_runtime, "_agent_cli_drivers_allowed", False)
    commands: list[list[str]] = []
    with pytest.raises(InternalLLMError, match="agent CLI"):
        _cli_code_ask(tmp_path, monkeypatch, "claude", {"type": "result", "structured_output": _ANSWER}, commands)
    assert commands == []


def test_code_ask_stream_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr(code_map, "resolve_project_root", lambda value=None: project)
    monkeypatch.setattr(code_map, "get_engine", lambda value: object())

    def fake_answer(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["root"] == project
        assert kwargs["path"] == "src/payment.py"
        assert kwargs["attachments"] == [{"name": "notes.txt", "content": "Retry budget is two."}]
        kwargs["on_delta"]("The retry is bounded.")
        return {
            "body": "The retry is bounded.",
            "references": [{"path": "src/payment.py", "start_line": 2, "end_line": 2}],
        }

    monkeypatch.setattr(code_ask, "answer_code_ask", fake_answer)
    client = FastAPITestClient(create_app(store_root=tmp_path / ".lemoncrow"))
    response = client.post(
        "/v1/code-map/ask/stream",
        json={
            "project_root": str(project),
            "path": "src/payment.py",
            "line": 2,
            "question": "Why retry?",
            "attachments": [{"name": "notes.txt", "content": "Retry budget is two."}],
        },
    )
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "accepted"
    assert {event["type"] for event in events} >= {"delta", "result"}
    assert events[-1]["result"]["body"] == "The retry is bounded."
