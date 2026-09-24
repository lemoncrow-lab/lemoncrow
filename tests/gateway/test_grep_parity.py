"""Client ``grep`` and the main package's ``grep`` show the model the same text.

Both run ``lemoncrow_client.kit.search``. This pins the glue on each side --
argument aliases and coercion, mode names, rendering, the ``no matches``
footer, refusals and spills -- over seeded argument combinations. Main runs
its pure-Python backend on a repository with nothing ignored, so both sides
search the same files; call-graph badges come from the code index, which the
client does not have, and are switched off.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.errors import ClientError
from lemoncrow_client.localtools import LocalContext, executor_for

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp.framework import TOOLS
from lemoncrow.gateway.tools.presentation import assemble_response_text
from lemoncrow.gateway.tools.results import clean_tool_result
from lemoncrow.pro.capabilities.tool_supervision import native_search

_SPILL = re.compile(r"\[lc: spilled; [^\]]+\]")


def _module(name: str, functions: int) -> str:
    body = [f'"""Module {name}."""', "", "import os", "", "", "class OrderService:", "    def compute(self, order):"]
    body += ['        """Compute the total.'] + [f"        detail {i}" for i in range(12)] + ['        """']
    body += ["        total = 0  # TODO tax", "        return total", "", ""]
    for index in range(functions):
        body += [f"def helper_{index}(value):", f"    return value * {index}  # compute", "", ""]
    return "\n".join(body) + "\n"


_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": _module("core", 5),
    "pkg/big.py": _module("big", 140),
    "pkg/util.py": "from pkg.core import OrderService\n\n\ndef compute_all(orders):\n    return [OrderService().compute(o) for o in orders]\n",
    "web/app.ts": "import util from './lib';\n\nexport class OrderView {\n  compute(o: number[]): number {\n    return o.length; // TODO\n  }\n}\n",
    "docs/readme.md": "# Orders\n\nThe OrderService computes totals.\n",
    "notes.txt": "compute\nprint(order)\n",
}
_ARGS: dict[str, list[Any]] = {
    "regex": ["Order", "compute", "TODO", "def ", "print(", "helper_1", "nomatchzzz", None],
    "mode": [
        "with_content",
        "ranked_map",
        "paths_only",
        "count_only",
        "map",
        "counts",
        "files",
        "file_paths_with_content",
    ],
    "path": [".", ".", "pkg", "pkg/core.py", "pkg/core.py:L5-L25", "docs", "pkg/big.py"],
    "glob": [None, None, "**/*.py", ["pkg/*.py", "web/**/*.ts"], "*.md"],
    "type": [None, None, "python", "ts", "markdown", "go"],
}


def _cases(count: int) -> list[dict[str, Any]]:
    rng = random.Random(20260923)
    cases = []
    for _ in range(count):
        args = {key: rng.choice(values) for key, values in _ARGS.items()}
        args = {key: value for key, value in args.items() if value is not None}
        if rng.random() < 0.3:
            args["before"], args["after"] = rng.choice([(1, 1), (3, 0), (0, 5)])
        if rng.random() < 0.2:
            args["i"] = True
        if rng.random() < 0.2:
            args["summary"] = rng.choice([True, False])
        if rng.random() < 0.2:
            args["context_budget_tokens"] = rng.choice([200, 1000])
        if rng.random() < 0.15:
            args["file_limit"] = rng.choice([1, 2])
        if rng.random() < 0.15 and "regex" in args:
            args["pattern"] = args.pop("regex")  # an alias both sides accept
        cases.append(args)
    return cases


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    for relative, content in _FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / ".git").mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))
    monkeypatch.setattr(native_search, "_RG_BIN", None)
    monkeypatch.setattr(native_search, "_GREP_BIN", None)
    monkeypatch.setattr(mcp_server, "_grep_badge_provider", lambda _path, _symbols: None)
    return root


def _main_text(args: dict[str, Any]) -> str:
    mcp_server._search_history_sessions.clear()
    payload = TOOLS["grep"]["handler"](dict(args))
    if isinstance(payload, dict):
        payload = clean_tool_result(payload, "grep")
    # The main package's own presentation: rendered text, else compact JSON.
    return assemble_response_text(payload, mcp_server.render_tool_result_text("grep", payload))


def _client_text(context: LocalContext, args: dict[str, Any]) -> str:
    try:
        result = executor_for("grep")(context, dict(args))
    except ClientError as refused:
        return refused.message
    return "\n".join(block["text"] for block in result.content if block.get("type") == "text")


def test_client_and_main_grep_show_the_same_text(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "home" / "lemoncrow"
    state.mkdir(parents=True)
    config = load_config({"LEMONCROW_HOME": str(state), "HOME": str(state.parent)}, cwd=repo)
    context = LocalContext(config=config, repo_root=repo, sync=None, environment={})
    diverged = []
    spilled = no_matches = 0
    for index, args in enumerate(_cases(250)):
        main = _SPILL.sub("[lc: spilled; PATH]", _main_text(args))
        client = _SPILL.sub("[lc: spilled; PATH]", _client_text(context, args))
        spilled += "[lc: spilled;" in main
        no_matches += main.endswith("no matches")
        if main != client:
            diverged.append((index, args))
    assert not diverged, f"{len(diverged)} cases diverge, first: {diverged[:3]}"
    assert spilled >= 5 and no_matches >= 10, "the corpus must exercise spills and empty results"
