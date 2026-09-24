from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server


def _write_workspace(root: Path) -> Path:
    billing = root / "billing"
    (root / "src").mkdir(parents=True)
    (billing / "src").mkdir(parents=True)
    (root / "src" / "local.py").write_text("def shared_config():\n    return 'local'\n", encoding="utf-8")
    (billing / "src" / "billing.py").write_text("def shared_config():\n    return 'billing'\n", encoding="utf-8")
    (root / ".lemoncrow").mkdir()
    (root / ".lemoncrow" / "workspace.toml").write_text(
        "\n".join(
            [
                "[workspace]",
                'id = "combined"',
                "",
                "[[workspace.repos]]",
                'name = "app"',
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
    return billing


def test_code_search_routes_across_workspace_repos_and_keeps_paths_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    billing = _write_workspace(tmp_path)
    calls: list[Path] = []

    class FakeEngine:
        def __init__(self, repo_root: Path) -> None:
            self.repo_root = repo_root.resolve()

        def tool_explore(self, query: str, **_: object) -> dict[str, object]:
            calls.append(self.repo_root)
            if self.repo_root == billing.resolve():
                return {
                    "exact_match": False,
                    "entry_points": [
                        {
                            "qualified_name": "billing.shared_config",
                            "path": "src/billing.py",
                            "line": 1,
                            "end_line": 2,
                            "score": 8.0,
                        }
                    ],
                    "files": [
                        {
                            "path": "src/billing.py",
                            "source_sections": [
                                {
                                    "line": 1,
                                    "end_line": 2,
                                    "content": "1\tdef shared_config():\n2\t    return 'billing'\n",
                                }
                            ],
                        }
                    ],
                }
            return {
                "exact_match": False,
                "entry_points": [
                    {
                        "qualified_name": "app.shared_config",
                        "path": "src/local.py",
                        "line": 1,
                        "end_line": 2,
                        "score": 7.0,
                    }
                ],
                "files": [
                    {
                        "path": "src/local.py",
                        "source_sections": [
                            {"line": 1, "end_line": 2, "content": "1\tdef shared_config():\n2\t    return 'local'\n"}
                        ],
                    }
                ],
            }

    monkeypatch.setattr(mcp_server, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        mcp_server,
        "_code_context_engine",
        lambda repo_root=".": FakeEngine(Path(repo_root)),
    )
    monkeypatch.setattr(mcp_server, "_check_repeat_query", lambda query: False)

    handler = mcp_server.TOOLS["code_search"]["handler"]
    result = handler({"query": "shared_config"})

    assert calls == [tmp_path.resolve(), billing.resolve()]
    assert [entry["path"] for entry in result["files"]] == ["billing/src/billing.py", "src/local.py"]
    assert {entry["path"] for entry in result.get("related_symbols", [])} <= {
        "src/local.py",
        "billing/src/billing.py",
    }
