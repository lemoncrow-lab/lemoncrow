from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.live_reviewer.agentic import _read_file, run_agentic_review


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, tc_id: str, name: str, arguments: str) -> None:
        self.id = tc_id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content: str | None = None, tool_calls: list[_TC] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Msg) -> None:
        self.message = message


class _Resp:
    def __init__(self, message: _Msg) -> None:
        self.choices = [_Choice(message)]


def test_agentic_reads_then_finishes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    def completion(*, model: str, messages: list, tools: list) -> _Resp:
        if len(messages) == 2:  # first call: investigate
            return _Resp(_Msg(tool_calls=[_TC("c1", "read_file", '{"path": "a.py"}')]))
        return _Resp(_Msg(tool_calls=[_TC("c2", "finish", '{"verdict": "NEEDS_FIX", "findings": []}')]))

    v = run_agentic_review(repo_root=tmp_path, diffs={"a.py": "diff"}, contract="C", completion=completion)
    assert v is not None and v["verdict"] == "NEEDS_FIX" and v["findings"] == []


def test_agentic_no_toolcalls_falls_back(tmp_path: Path) -> None:
    v = run_agentic_review(
        repo_root=tmp_path, diffs={"a.py": "d"}, contract="C", completion=lambda **k: _Resp(_Msg(content="text"))
    )
    assert v is None


def test_agentic_error_falls_back(tmp_path: Path) -> None:
    def boom(**_kwargs: object) -> object:
        raise RuntimeError("transport down")

    assert run_agentic_review(repo_root=tmp_path, diffs={"a.py": "d"}, contract="C", completion=boom) is None


def test_agentic_max_turns_falls_back(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

    def always_read(**_kwargs: object) -> _Resp:
        return _Resp(_Msg(tool_calls=[_TC("c", "read_file", '{"path": "a.py"}')]))

    assert (
        run_agentic_review(repo_root=tmp_path, diffs={"a.py": "d"}, contract="C", max_turns=3, completion=always_read)
        is None
    )


def test_read_file_containment(tmp_path: Path) -> None:
    assert "refused" in _read_file(tmp_path, "../../etc/passwd", None, None)


def test_read_file_range(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    assert _read_file(tmp_path, "f.py", 2, 3) == "b\nc"
