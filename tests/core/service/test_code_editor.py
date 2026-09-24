from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lemoncrow.core.service import code_editor


def test_editor_capability_prefers_explicit_allowlisted_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEMONCROW_CODE_EDITOR", "code")
    monkeypatch.setattr(
        code_editor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"cursor", "code"} else None,
    )

    capability = code_editor.editor_capability()

    assert capability["available"] is True
    assert capability["preferred"] == {"id": "vscode", "label": "VS Code"}
    assert capability["editors"] == [
        {"id": "cursor", "label": "Cursor"},
        {"id": "vscode", "label": "VS Code"},
    ]


def test_open_in_editor_uses_fixed_argv_and_exact_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_CODE_EDITOR", "cursor")
    monkeypatch.setattr(code_editor.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 4242

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)
    path = tmp_path / "src" / "payment.py"
    path.parent.mkdir()
    path.write_text("pass\n", encoding="utf-8")

    result = code_editor.open_in_editor(path, line=12, column=3, repo_root=tmp_path)

    assert captured["args"] == ["/usr/bin/cursor", "--goto", f"{path}:12:3"]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert result == {
        "opened": True,
        "editor": "cursor",
        "label": "Cursor",
        "line": 12,
        "column": 3,
        "pid": 4242,
    }
