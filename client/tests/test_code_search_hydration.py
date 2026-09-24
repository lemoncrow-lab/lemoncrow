from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import lemoncrow_client.search_markdown as markdown
import pytest
from lemoncrow_client.manifest import sha256_hex
from lemoncrow_client.search_markdown import render_code_search_markdown, render_local_code_search
from lemoncrow_client.session import RemoteSession


@pytest.mark.parametrize(
    "suffix,source",
    [
        (".py", b"def alpha():\n    return 1\n"),
        (".ts", b"function alpha() {\n  return 1;\n}\n"),
        (".go", b"func alpha() {\n  return\n}\n"),
        (".rs", b"fn alpha() {\n  return;\n}\n"),
    ],
)
def test_warm_navigation_skips_reads_with_identical_output(tmp_path, monkeypatch, suffix, source):
    paths = [f"file_{i}{suffix}" for i in range(10)]
    for path in paths:
        (tmp_path / path).write_bytes(source)
    hits = [{"path": path, "detail": {"definitions": [{"name": "alpha", "line": 1}]}} for path in paths]
    answer = {"hits": hits, "_hydration": {path: sha256_hex(source) for path in paths}}
    expected = render_code_search_markdown("alpha", answer, load_source=lambda hit: source)
    reads = []
    original = Path.read_bytes

    def counted(path):
        reads.append(path.name)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    assert render_local_code_search("alpha", answer, repo_root=tmp_path) == expected
    assert len(reads) == 10
    reads.clear()
    assert render_local_code_search("alpha", answer, repo_root=tmp_path) == expected
    assert reads == paths[:2]

    # A new server digest must miss even when the symbol's name/line is unchanged.
    changed = source + b"// changed\n"
    (tmp_path / paths[-1]).write_bytes(changed)
    answer["_hydration"][paths[-1]] = sha256_hex(changed)
    reads.clear()
    assert render_local_code_search("alpha", answer, repo_root=tmp_path) is not None
    assert reads == [*paths[:2], paths[-1]]

    # Inline source still requires a fresh digest check on every request.
    (tmp_path / paths[0]).write_bytes(b"stale local source")
    assert render_local_code_search("alpha", answer, repo_root=tmp_path) is None


def test_navigation_extent_cache_is_bounded(tmp_path, monkeypatch):
    from collections import OrderedDict

    monkeypatch.setattr(markdown, "_LOCAL_EXTENTS", OrderedDict())
    monkeypatch.setattr(markdown, "_MAX_LOCAL_EXTENTS", 2)
    source = b"def alpha():\n    pass\n"
    paths = [f"f{i}.py" for i in range(6)]
    for path in paths:
        (tmp_path / path).write_bytes(source)
    answer = {
        "hits": [{"path": path, "detail": {"definitions": [{"name": "alpha", "line": 1}]}} for path in paths],
        "_hydration": {path: sha256_hex(source) for path in paths},
    }
    render_local_code_search("alpha", answer, repo_root=tmp_path)
    assert len(markdown._LOCAL_EXTENTS) == 2
    assert [key[1] for key in markdown._LOCAL_EXTENTS] == paths[-2:]


def _structured(path: str, data: bytes, *, digest: str | None = None) -> dict[str, object]:
    return {
        "kind": "code_search",
        "hits": [
            {
                "path": path,
                "score": 1.0,
                "language": "python",
                "line_count": 2,
                "size": len(data),
                "detail": {
                    # Real index semantics: start/end identify only the symbol
                    # name, not the whole function body.
                    "definitions": [{"name": "alpha", "kind": "function", "line": 1, "start": 4, "end": 9}]
                },
            }
        ],
        "view_revision": 1,
        "searched_paths": 1,
        "degraded": False,
        "truncated": False,
        "remaining_hits": 0,
        "_hydration": {path: digest or sha256_hex(data)},
    }


def test_local_hydration_requires_exact_digest(tmp_path: Path) -> None:
    data = b"def alpha():\n    return 1\n"
    target = tmp_path / "pkg" / "alpha.py"
    target.parent.mkdir()
    target.write_bytes(data)
    structured = _structured("pkg/alpha.py", data)

    rendered = render_local_code_search("alpha", structured, repo_root=tmp_path)
    assert rendered is not None
    assert "= exact" in rendered
    assert "## pkg/alpha.py:L1-L2 · alpha" in rendered
    assert "def alpha():" in rendered
    assert "return 1" in rendered

    target.write_bytes(b"def alpha():\n    return 2\n")
    assert render_local_code_search("alpha", structured, repo_root=tmp_path) is None


def test_local_hydration_rejects_pointer_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.py"
    outside.write_text("def alpha(): pass\n", encoding="utf-8")
    data = outside.read_bytes()
    structured = _structured("../secret.py", data)
    assert render_local_code_search("alpha", structured, repo_root=tmp_path) is None


def test_path_only_hit_needs_no_hydration_pointer(tmp_path: Path) -> None:
    structured: dict[str, object] = {
        "hits": [{"path": "vendor/huge.bin", "detail": {"content_indexed": False}}],
        "truncated": False,
    }
    assert render_local_code_search("huge", structured, repo_root=tmp_path) == "~ ranked\n\n→ vendor/huge.bin"


def test_session_retries_once_with_server_hydration_on_digest_mismatch(
    monkeypatch: Any, bootstrapped: RemoteSession
) -> None:
    local = bootstrapped._config.repo_root / "pkg" / "alpha.py"
    local_data = local.read_bytes()
    structured = _structured("pkg/alpha.py", local_data, digest="0" * 64)
    calls: list[Mapping[str, Any]] = []

    def fake_dispatch(
        self: RemoteSession,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        reuse_validator: str = "",
    ) -> Mapping[str, Any]:
        del self, reuse_validator
        calls.append(dict(arguments))
        if arguments.get("_client_hydrate") is False:
            return {
                "tool": tool,
                "content": [{"type": "text", "text": "= exact\n\n## pkg/alpha.py:L1-L2 · alpha\n1 server canonical"}],
                "structured": structured,
                "is_error": False,
                "degraded": False,
                "view_revision": bootstrapped.view_revision,
            }
        return {
            "tool": tool,
            "content": [{"type": "text", "text": "→ pkg/alpha.py:L1 · alpha"}],
            "structured": structured,
            "is_error": False,
            "degraded": False,
            "view_revision": bootstrapped.view_revision,
        }

    monkeypatch.setattr(RemoteSession, "_dispatch", fake_dispatch)
    answer = bootstrapped.call_tool("code_search", {"query": "alpha"})
    assert [call.get("_client_hydrate") for call in calls] == [True, False]
    assert "server canonical" in str(answer.content[0]["text"])
