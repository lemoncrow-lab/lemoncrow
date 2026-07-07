from __future__ import annotations

import json
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from atelier.infra.code_intel.astgrep.adapter import AstGrepAdapter, _parse_json_output
from atelier.infra.code_intel.astgrep.binaries import discover_astgrep_binary


def test_discover_astgrep_binary_rejects_wrong_linux_sg_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATELIER_AST_GREP_BIN", "/usr/bin/sg")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/sg" if name == "ast-grep" else None)

    resolution = discover_astgrep_binary(tmp_path)

    assert resolution.available is False
    assert resolution.to_payload()["error"] == "tool_unavailable"
    assert resolution.to_payload()["expected_binary"] == "ast-grep"
    assert "/usr/bin/sg" in resolution.checked


def test_astgrep_search_preserves_captures_paths_and_truncation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = AstGrepAdapter(tmp_path, binary_path=tmp_path / "ast-grep")
    payload = {
        "matches": [
            {
                "file": "src/app.py",
                "text": "requests.get(url)",
                "range": {"start": {"line": 12, "column": 4}, "end": {"line": 12, "column": 21}},
                "metaVariables": {
                    "single": {
                        "URL": {
                            "text": "url",
                            "range": {"start": {"line": 12, "column": 17}, "end": {"line": 12, "column": 20}},
                        }
                    }
                },
            }
        ],
        "truncated": True,
        "total_matches": 41,
    }
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda args: CompletedProcess(args=args, returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    result = adapter.search(pattern="requests.get($URL)", language="python", file_glob="src/*.py", limit=10)

    assert result.truncated is True
    assert result.total_matches == 41
    assert result.matches[0].file_path == "src/app.py"
    assert result.matches[0].captures == {"URL": "url"}


def test_astgrep_rewrite_dry_run_returns_diff_and_apply_reports_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = repo_root / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("requests.get(url)\n", encoding="utf-8")

    adapter = AstGrepAdapter(repo_root, binary_path=repo_root / "ast-grep")
    # Real ast-grep --json emits one object per match with `replacement` and byte
    # `replacementOffsets`; the adapter reconstructs whole-file content from them.
    payload = {
        "matches": [
            {
                "file": "src/app.py",
                "text": "requests.get(url)",
                "replacement": "requests.get(url, timeout=30)",
                "replacementOffsets": {"start": 0, "end": 17},
            }
        ]
    }
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda args: CompletedProcess(args=args, returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    preview = adapter.rewrite(
        pattern="requests.get($URL)",
        rewrite="requests.get($URL, timeout=30)",
        language="python",
        dry_run=True,
    )
    applied = adapter.rewrite(
        pattern="requests.get($URL)",
        rewrite="requests.get($URL, timeout=30)",
        language="python",
        dry_run=False,
    )

    assert "--- a/src/app.py" in preview.diff
    assert target.read_text(encoding="utf-8") == "requests.get(url, timeout=30)\n"
    assert applied.files_changed == ["src/app.py"]


def test_astgrep_run_converts_subprocess_timeout_to_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H4: a stalled child hangs forever because a hang is not an exception. The
    # adapter must bound the call with a timeout and raise a domain RuntimeError
    # (so callers' `except Exception` fires) instead of blocking indefinitely.
    adapter = AstGrepAdapter(tmp_path, binary_path=tmp_path / "ast-grep", timeout=0.01)

    def _raise_timeout(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="ast-grep", timeout=0.01)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        adapter.scan(rules=[{"id": "x", "language": "python", "rule": {"pattern": "$X"}}])


def test_parse_json_output_skips_malformed_stream_lines() -> None:
    # M3: the newline-delimited fallback parses each line; one malformed line
    # among valid ones must be skipped, not crash the tolerant parser.
    stdout = '{"file": "a.py"}\nnot json at all\n{"file": "b.py"}\n'

    payload = _parse_json_output(stdout)

    assert payload == {"matches": [{"file": "a.py"}, {"file": "b.py"}]}
