from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.tool_supervision import smart_search as smart_search_mod


def _configure(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))
    monkeypatch.setenv("LEMONCROW_CACHE_DISABLED", "1")


def test_smart_search_rejects_path_outside_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _configure(monkeypatch, workspace)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n", encoding="utf-8")

    with pytest.raises(ValueError):
        smart_search_mod.smart_search(query="secret", path=str(secret))


def test_smart_search_resolves_path_inside_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, tmp_path)
    target = tmp_path / "src" / "inside.py"
    target.parent.mkdir()
    target.write_text("x = 1\n", encoding="utf-8")

    resolved = smart_search_mod._resolve_path(tmp_path.resolve(), str(target))
    assert resolved == target.resolve()


def test_smart_search_relaxes_natural_language_query_after_empty_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, tmp_path)
    target = tmp_path / "benchmarks" / "codebench" / "run.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "parser.add_argument('--rate-limit-rpm', type=int)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        smart_search_mod,
        "_search_with_backend",
        lambda **_kwargs: {
            "matches": [],
            "backend": "zoekt",
            "index_age_seconds": 1,
            "total_tokens": 0,
        },
    )

    payload = smart_search_mod.smart_search(
        query="codebench CLI arguments rate-limit-rpm arms drivers model timeout command execution",
        path=str(target),
        max_files=8,
        budget_tokens=8000,
    )

    assert payload["backend"] == "ripgrep"
    assert payload["matches"][0]["path"] == str(target)
    assert payload["fallback"]["strategy"] == "query_terms"
    assert "rate-limit-rpm" in payload["fallback"]["terms"]


def test_smart_search_prefers_injected_index_before_relaxed_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, tmp_path)
    target = tmp_path / "src" / "commands.py"
    target.parent.mkdir()
    target.write_text("def configure_rate_limits() -> None:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(smart_search_mod, "_search_with_backend", lambda **_kwargs: None)

    def fail_relaxed_search(**_kwargs: object) -> None:
        raise AssertionError("term-based ripgrep should not run after indexed hits")

    monkeypatch.setattr(smart_search_mod, "_relaxed_search", fail_relaxed_search)

    payload = smart_search_mod.smart_search(
        query="configure command execution rate limits",
        path="src",
        indexed_search=lambda **_kwargs: {
            "items": [
                {
                    "file_path": "src/commands.py",
                    "language": "python",
                    "start_line": 1,
                    "end_line": 2,
                    "snippet": "def configure_rate_limits() -> None:\n    pass",
                }
            ],
            "total_tokens": 20,
        },
    )

    assert payload["backend"] == "code_index"
    assert payload["match_paths"] == [str(target)]
    assert payload["fallback"] == {
        "reason": "empty_primary_result",
        "strategy": "indexed_hybrid",
    }


def test_smart_search_returns_scoped_file_preview_when_all_queries_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, tmp_path)
    target = tmp_path / "run.py"
    target.write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(smart_search_mod, "_search_with_backend", lambda **_kwargs: None)

    payload = smart_search_mod.smart_search(
        query="completely absent vocabulary",
        path=str(target),
        max_chars_per_file=200,
        budget_tokens=50,
    )

    assert payload["backend"] == "file_preview"
    assert payload["matches"] == [{"path": str(target), "content": "def main() -> None:\n    pass\n", "snippets": []}]
    assert payload["fallback"] == {
        "reason": "empty_relaxed_result",
        "strategy": "scoped_file_preview",
    }


def test_smart_search_continues_when_indexed_fallback_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, tmp_path)
    target = tmp_path / "run.py"
    target.write_text("rate_limit_rpm = 10\n", encoding="utf-8")
    monkeypatch.setattr(smart_search_mod, "_search_with_backend", lambda **_kwargs: None)

    def locked_index(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("database is locked")

    payload = smart_search_mod.smart_search(
        query="rate limit rpm",
        path=str(target),
        indexed_search=locked_index,
    )

    assert payload["backend"] == "ripgrep"
    assert payload["fallback"]["strategy"] == "query_terms"
