from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.core.foundation.runtime_trace import TraceStore
from lemoncrow.gateway.cli.app import cli
from tests.core.test_trace_replay import _records


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    store = TraceStore(tmp_path)
    for record in _records():
        store.append(record)
    return tmp_path


def _run(root: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, ["--root", str(root), *args], catch_exceptions=False)
    return result.exit_code, result.output


def test_trace_show_list_replay_compare(root: Path) -> None:
    code, out = _run(root, "trace", "show", "lt_0000000000000001")
    assert code == 0 and "layers    client, server" in out and "→ src/util.py" in out

    code, out = _run(root, "trace", "list", "--json")
    assert code == 0 and next(r["trace_id"] for r in json.loads(out)) == "lt_0000000000000003"

    code, out = _run(root, "trace", "replay", "lt_0000000000000001", "--ranker", "feature-v1", "--json")
    payload = json.loads(out)
    assert code == 0 and payload["replay"]["selected"][0] == "src/review/auth.py"

    code, out = _run(root, "trace", "compare", "lt_0000000000000001", "lt_0000000000000002")
    assert code == 0 and json.loads(out)["tool"] == ["code_search", "read"]


def test_trace_show_unknown_id_fails(root: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(root), "trace", "show", "lt_ffffffffffffffff"])
    assert result.exit_code != 0 and "trace not found" in result.output


def test_eval_replay_reports_and_gates_on_evidence(root: Path) -> None:
    code, out = _run(root, "eval", "replay", "--candidate-ranker", "feature-v1", "--json")
    payload = json.loads(out)
    assert payload["cases_labeled"] == 1 and payload["candidate"]["mrr"] == 1.0
    assert payload["verdict"] == "insufficient_evidence" and code == 1
    code, out = _run(root, "eval", "replay", "--candidate-ranker", "feature-v1", "--min-labeled", "1", "--json")
    assert json.loads(out)["verdict"] == "no_regression" and code == 0
