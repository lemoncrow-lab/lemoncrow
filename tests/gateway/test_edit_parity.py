"""The client's ``edit`` executor and the main ``edit`` handler share one engine.

Both run :func:`lemoncrow_client.kit.edit.apply_edits`, so the same batch on the
same tree must give the same outcome and the same bytes. The main handler's
served-range freshness guard needs this server's read history, which the
client never has, so it is switched off here; the client does not run post-edit
lint hooks, so neither does the main handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.errors import ClientError
from lemoncrow_client.localtools import LocalContext, executor_for

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp_server import tool_smart_edit
from tests.helpers import init_store_at

SEED = {
    "pkg/alpha.py": "def alpha():\n    return 'alpha'\n",
    "pkg/lines.txt": "a\nb\nc\nd\ne\n",
    "pkg/dup.txt": "x\nx\n",
    "tests/test_alpha.py": "def test_a():\n    assert 1\n    assert 2\n",
}

CASES: dict[str, list[dict[str, Any]]] = {
    "exact": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'A'"}],
    "batched_ranges": [
        {"path": "pkg/lines.txt:L1-L1", "new": "A1\nA2\nA3\n"},
        {"path": "pkg/lines.txt:L4-L4", "new": "D\n"},
    ],
    "range_with_anchor": [{"path": "pkg/lines.txt:L2-L4", "old": "c\n", "new": "C\n"}],
    "create": [{"path": "pkg/new.txt", "new": "hello\n", "replace": True}],
    "ambiguous": [{"path": "pkg/dup.txt", "old": "x", "new": "y"}],
    "miss": [{"path": "pkg/alpha.py", "old": "nowhere to be found", "new": "x"}],
    "parse_error": [{"path": "pkg/alpha.py", "old": "return 'alpha'", "new": "return ("}],
    "test_weakening": [{"path": "tests/test_alpha.py", "old": "    assert 1\n    assert 2\n", "new": "    assert 2\n"}],
    "failing_batch": [
        {"path": "pkg/lines.txt", "old": "a\n", "new": "A\n"},
        {"path": "pkg/alpha.py", "old": "nowhere to be found", "new": "x"},
    ],
}


SUCCEEDS = {"exact", "batched_ranges", "range_with_anchor", "create"}


def _seed(root: Path) -> None:
    for relative, text in SEED.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".lemoncrow" not in path.relative_to(root).parts
    }


def _run_client(root: Path, state: Path, edits: list[dict[str, Any]]) -> bool:
    config = load_config({"LEMONCROW_HOME": str(state), "HOME": str(state.parent)}, cwd=root)
    context = LocalContext(config=config, repo_root=root, sync=None, environment={})
    try:
        executor_for("edit")(context, {"edits": [dict(edit) for edit in edits]})
    except ClientError:
        return False
    return True


def _run_main(root: Path, edits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.chdir(root)
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(root))
    payload = tool_smart_edit({"post_edit_hooks": False, "edits": [dict(edit) for edit in edits]})
    return not payload.get("failed") and not payload.get("rolled_back")


@pytest.fixture()
def main_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store" / ".lemoncrow"
    init_store_at(str(store))
    monkeypatch.setenv("LEMONCROW_ROOT", str(store))
    monkeypatch.setenv("LEMONCROW_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("LEMONCROW_RANGE_EDIT_GUARD", "0")
    monkeypatch.delenv("LEMONCROW_SERVICE_URL", raising=False)
    mcp_server._ledger._current_ledger = None


@pytest.mark.parametrize("case", sorted(CASES))
def test_client_and_main_handler_agree(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_store: None
) -> None:
    client_root, main_root, state = tmp_path / "client", tmp_path / "main", tmp_path / "state"
    for directory in (client_root, main_root, state):
        directory.mkdir()
    _seed(client_root)
    _seed(main_root)

    client_ok = _run_client(client_root, state, CASES[case])
    main_ok = _run_main(main_root, CASES[case], monkeypatch)

    assert client_ok == main_ok == (case in SUCCEEDS), case
    assert _tree(client_root) == _tree(main_root), case
    if case not in SUCCEEDS:
        seeded = {relative: text.encode() for relative, text in SEED.items()}
        assert _tree(client_root) == seeded, f"{case}: a refused batch must change nothing"
