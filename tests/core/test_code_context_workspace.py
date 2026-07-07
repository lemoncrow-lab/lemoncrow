from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.code_context.workspace_config import load_workspace_config
from atelier.core.capabilities.code_context.workspace_router import WorkspaceCodeRouter


def _write_workspace_config(workspace_root: Path) -> Path:
    sibling_repo = workspace_root / "billing"
    sibling_repo.mkdir(parents=True)
    (workspace_root / ".atelier").mkdir(parents=True)
    (workspace_root / ".atelier" / "workspace.toml").write_text(
        "\n".join(
            [
                "[workspace]",
                'id = "leanchain-main"',
                "",
                "[[workspace.repos]]",
                'name = "atelier"',
                'path = "."',
                "",
                "[[workspace.repos]]",
                'name = "billing"',
                'path = "billing"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return sibling_repo


def test_load_workspace_config_parses_workspace_id_and_repo_roots(tmp_path: Path) -> None:
    billing_root = _write_workspace_config(tmp_path)

    config = load_workspace_config(tmp_path)

    assert config is not None
    assert config.workspace_id == "leanchain-main"
    assert [repo.name for repo in config.repos] == ["atelier", "billing"]
    assert config.repos[0].repo_root == tmp_path.resolve()
    assert config.repos[1].repo_root == billing_root.resolve()


def test_load_workspace_config_returns_none_when_workspace_file_is_missing(tmp_path: Path) -> None:
    assert load_workspace_config(tmp_path) is None


def test_workspace_router_unions_search_results_and_allows_repo_filter(tmp_path: Path) -> None:
    _write_workspace_config(tmp_path)
    responses = {
        tmp_path.resolve(): {
            "items": [{"symbol_name": "SharedConfig", "file_path": "src/local.py", "repo_id": "repo-1"}],
            "cache_hit": False,
            "provenance": "local",
            "tokens_saved": 0,
            "total_tokens": 40,
        },
        (tmp_path / "billing").resolve(): {
            "items": [{"symbol_name": "SharedConfig", "file_path": "src/billing.py", "repo_id": "repo-2"}],
            "cache_hit": False,
            "provenance": "local",
            "tokens_saved": 0,
            "total_tokens": 45,
        },
    }
    calls: list[tuple[Path, str]] = []

    class FakeEngine:
        def __init__(self, repo_root: Path) -> None:
            self.repo_root = repo_root

        def tool_search(self, query: str, **_: object) -> dict[str, object]:
            calls.append((self.repo_root, query))
            return responses[self.repo_root]

    router = WorkspaceCodeRouter(
        repo_root=tmp_path,
        engine_factory=lambda repo_root: FakeEngine(Path(repo_root).resolve()),
    )

    merged = router.route("search", query="SharedConfig", limit=5)
    filtered = router.route("search", query="SharedConfig", repo="billing", limit=5)

    assert [item["file_path"] for item in merged["items"]] == ["src/local.py", "src/billing.py"]
    assert [item["file_path"] for item in filtered["items"]] == ["src/billing.py"]
    assert calls == [
        (tmp_path.resolve(), "SharedConfig"),
        ((tmp_path / "billing").resolve(), "SharedConfig"),
        ((tmp_path / "billing").resolve(), "SharedConfig"),
    ]


def test_workspace_router_rejects_unknown_repo_filter(tmp_path: Path) -> None:
    _write_workspace_config(tmp_path)
    router = WorkspaceCodeRouter(repo_root=tmp_path, engine_factory=lambda repo_root: object())

    with pytest.raises(ValueError, match="Unknown workspace repo"):
        router.route("search", query="SharedConfig", repo="missing")


def test_workspace_router_adds_repo_name_and_preserves_origin_on_merged_search_results(tmp_path: Path) -> None:
    _write_workspace_config(tmp_path)
    billing_root = (tmp_path / "billing").resolve()

    class FakeEngine:
        def __init__(self, repo_root: Path) -> None:
            self.repo_root = repo_root

        def tool_search(self, query: str, **_: object) -> dict[str, object]:
            if self.repo_root == billing_root:
                return {
                    "items": [
                        {
                            "symbol_name": query,
                            "qualified_name": "requests.get",
                            "file_path": "external/requests/api.py",
                            "repo_id": "repo-2",
                            "origin": "external",
                        }
                    ],
                    "cache_hit": False,
                    "provenance": "local",
                    "tokens_saved": 0,
                    "total_tokens": 30,
                }
            return {
                "items": [
                    {
                        "symbol_name": query,
                        "qualified_name": query,
                        "file_path": "src/config.py",
                        "repo_id": "repo-1",
                        "origin": "internal",
                    }
                ],
                "cache_hit": False,
                "provenance": "local",
                "tokens_saved": 0,
                "total_tokens": 20,
            }

    router = WorkspaceCodeRouter(
        repo_root=tmp_path,
        engine_factory=lambda repo_root: FakeEngine(Path(repo_root).resolve()),
    )

    merged = router.route("search", query="SharedConfig", limit=5)

    assert [(item["repo_name"], item["origin"]) for item in merged["items"]] == [
        ("atelier", "internal"),
        ("billing", "external"),
    ]


def test_workspace_router_symbol_defaults_to_first_repo_and_respects_repo_filter(tmp_path: Path) -> None:
    _write_workspace_config(tmp_path)
    billing_root = (tmp_path / "billing").resolve()

    class FakeEngine:
        def __init__(self, repo_root: Path) -> None:
            self.repo_root = repo_root

        def tool_symbol(self, **_: object) -> dict[str, object]:
            if self.repo_root == billing_root:
                return {
                    "symbol_name": "SharedConfig",
                    "qualified_name": "billing.SharedConfig",
                    "file_path": "src/config.py",
                    "repo_id": "repo-2",
                }
            return {
                "symbol_name": "SharedConfig",
                "qualified_name": "atelier.SharedConfig",
                "file_path": "src/config.py",
                "repo_id": "repo-1",
            }

    router = WorkspaceCodeRouter(
        repo_root=tmp_path,
        engine_factory=lambda repo_root: FakeEngine(Path(repo_root).resolve()),
    )

    default_symbol = router.route("symbol", symbol_name="SharedConfig")
    billing_symbol = router.route("symbol", symbol_name="SharedConfig", repo="billing")

    assert default_symbol["repo_name"] == "atelier"
    assert default_symbol["qualified_name"] == "atelier.SharedConfig"
    assert billing_symbol["repo_name"] == "billing"
    assert billing_symbol["qualified_name"] == "billing.SharedConfig"
