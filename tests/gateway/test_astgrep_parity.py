"""Client ``scan``/``codemod`` and the main package's show the same text and write the same files.

Both run ``lemoncrow_client.kit.astgrep`` and ``kit.security_scan``. What the
main package adds on its own is left out on purpose: its index-backed Python
matcher (patterns such as ``def $F($$$)`` or ``f($$$)``, answered from the code
index) and the index-status keys it attaches while the index warms.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.localtools import LocalContext, executor_for

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp.framework import TOOLS
from lemoncrow.gateway.tools.presentation import assemble_response_text
from lemoncrow.gateway.tools.results import clean_tool_result

pytestmark = pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is not installed")

_MAIN_ONLY_KEYS = ("hint", "index_status", "bootstrap_note")
_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "import os\n\n\ndef check(x):\n    if x == None:\n        return print('none')\n"
        "    if x == None and x == None:\n        pass\n    return isinstance(x, int)\n\n\n"
        "class Service:\n    def run(self, cmd):\n        eval(cmd)\n        return os.system('ls ' + cmd)\n"
    ),
    "scripts/run.py": "#!/usr/bin/env python3\nprint('hi')\nif y == None: print(y)\n",
    "web/app.ts": "export function f(x: any) { if (x == null) { console.log(x); } }\n",
    "sec/vuln.py": (
        "import os\nimport subprocess\n\nfrom flask import request\n\n\ndef view():\n"
        "    cmd = request.args.get('q')\n    subprocess.run('ls ' + cmd, shell=True)\n    os.system(cmd)\n"
        "    password = 'hunter2hunter2'\n    eval(cmd); exec(cmd)\n"
    ),
    "sec/safe.py": "import subprocess\n\n\ndef ok():\n    subprocess.run(['ls'], check=True)\n",
}
_SCANS: list[dict[str, Any]] = [
    {},
    {"path": "sec"},
    {"path": "sec/vuln.py"},
    {"path": "pkg"},
    {"include_taint": False},
    {"include_rules": False},
    {"include_taint": False, "include_rules": False},
    {"repo_root": "sec"},
    {"path": "vuln.py", "repo_root": "sec"},
    {"include_taint": "false"},
]
_SEARCHES: list[dict[str, Any]] = [
    {"pattern": pattern, **extra}
    for pattern in ("$X == None", "eval($X)", "isinstance($A, $B)", "os.system($X)", "nomatch_zzz($X)")
    for extra in (
        {},
        {"language": "python"},
        {"glob": "pkg/*.py"},
        {"file_glob": "sec/*.py"},
        {"limit": 1},
        {"limit": "2"},
    )
] + [
    {"pattern": "console.log($X)", "language": "ts"},
    {"pattern": "$X == null", "language": "ts"},
    {"pattern": "eval($X)", "language": "bogus"},
]
_REWRITES: list[dict[str, Any]] = [
    {"pattern": pattern, "rewrite": rewrite, **extra}
    for pattern, rewrite, extra in (
        ("$X == None", "$X is None", {}),
        ("$X == None", "$X is None", {"glob": "pkg/*.py"}),
        ("eval($X)", "safe_eval($X)", {"language": "python"}),
        ("print($$$A)", "log($$$A)", {"language": "python"}),
        ("nomatch($X)", "y", {}),
    )
]


def _repo(base: Path) -> Path:
    for relative, content in _FILES.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (base / ".git").mkdir()
    return base


def _state(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts and ".lemoncrow" not in path.parts
    }


def _main_text(tool: str, args: dict[str, Any], root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(root))
    monkeypatch.chdir(root)
    try:
        payload = TOOLS[tool]["handler"](dict(args))
    except RuntimeError as exc:
        return str(exc)
    payload = clean_tool_result(payload, tool)
    for key in _MAIN_ONLY_KEYS:
        payload.pop(key, None)
    return assemble_response_text(payload, mcp_server.render_tool_result_text(tool, payload))


def _client_text(tool: str, args: dict[str, Any], root: Path, state: Path) -> str:
    config = load_config({"LEMONCROW_HOME": str(state), "HOME": str(state.parent)}, cwd=root)
    context = LocalContext(config=config, repo_root=root, sync=None, environment=dict(os.environ))
    result = executor_for(tool)(context, dict(args))
    return "\n".join(str(block.get("text", "")) for block in result.content)


def _run_both(
    tool: str, args: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int
) -> tuple[tuple[str, dict[str, str]], tuple[str, dict[str, str]]]:
    state = tmp_path / "home" / "lemoncrow"
    state.mkdir(parents=True, exist_ok=True)
    main_root = _repo(tmp_path / f"{index}-main")
    client_root = _repo(tmp_path / f"{index}-client")
    main = _main_text(tool, args, main_root, monkeypatch)
    client = _client_text(tool, args, client_root, state)
    return (main, _state(main_root)), (client, _state(client_root))


def test_client_and_main_scan_show_the_same_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diverged = []
    for index, args in enumerate(_SCANS):
        main, client = _run_both("scan", args, tmp_path, monkeypatch, index)
        if main != client:
            diverged.append((args, main[0][:300], client[0][:300]))
    assert not diverged, diverged[:2]


def test_client_and_main_codemod_show_the_same_matches_and_write_the_same_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diverged = []
    seen = {"matches": 0, "previews": 0, "writes": 0, "errors": 0}
    original = _state(_repo(tmp_path / "original"))
    cases = _SEARCHES + [{**case, "dry_run": dry_run} for case in _REWRITES for dry_run in (True, False)]
    for index, args in enumerate(cases):
        main, client = _run_both("codemod", args, tmp_path, monkeypatch, index)
        if main != client:
            diverged.append((args, main[0][:300], client[0][:300]))
        text, files = main
        seen["matches"] += text.startswith("- ") and text != "- no matches"
        seen["previews"] += text.startswith("{") and bool(json.loads(text).get("files_changed")) and files == original
        seen["writes"] += files != original
        seen["errors"] += "bogus" in text
    assert not diverged, diverged[:2]
    assert seen["matches"] >= 20 and seen["previews"] >= 3 and seen["writes"] >= 3 and seen["errors"] == 1, seen
