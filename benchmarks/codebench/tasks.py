"""The seven CodeBench tasks.

Prompts and bundled workspaces are read from a local task-source checkout
(default ``../benchmarks/<repo>/codebench-tasks``; override with
``CODEBENCH_TASKS_DIR``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

TaskSource: TypeAlias = (  # noqa: UP040
    tuple[Literal["empty"]]
    | tuple[Literal["repo"], str, str | None]
    | tuple[Literal["workspace"], str]
    | tuple[Literal["path"], str]
)


def codebench_tasks_dir() -> Path:
    root = os.environ.get("CODEBENCH_TASKS_DIR")
    if root:
        return Path(root)
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root.parent / "benchmarks" / repo_root.name / "codebench-tasks"


# Portable (checkout-name-independent) path to this repo's own lemoncrow binary,
# used by every cg_* task's pre-index setup_cmds below. A prior hardcoded
# absolute path baked in a stale checkout name and silently no-op'd (`|| true`)
# on any machine where that name doesn't match -- no crash, just a permanently
# cold-started index for the lemoncrow arm, which quietly biases the whole
# cg_* cost/time comparison against it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEMONCROW_BIN = _REPO_ROOT / ".venv" / "bin" / "lemoncrow"
_INDEX_ON_LEMONCROW_REP: tuple[str, ...] = (
    f'case "$(pwd)" in *_lemoncrow_rep*) {_LEMONCROW_BIN} code index --repo-root . || true ;; esac',
)


@dataclass(frozen=True)
class Task:
    id: str
    language: str
    # source kinds: ("empty",) | ("repo", url, commit_or_None) | ("workspace", subdir)
    source: TaskSource
    # rough budget ordering for cheap-first runs
    weight: int  # 1=cheap (no clone) .. 3=heavy (large repo clone+build)
    task_dir: str  # folder name under codebench-tasks/tasks/
    # Shell commands run inside the prepared workspace before the agent starts.
    # Each string is passed to subprocess shell=True with the workspace as cwd.
    setup_cmds: tuple[str, ...] = field(default_factory=tuple)
    # Agent capability this task exercises; selects the per-arm persona
    # (built-in twin vs lemoncrow) and the grader. "code" -> objective verify
    # gate; "explore" -> answer-key overlap grader; "plan" -> overlap + judge.
    capability: str = "code"

    def prompt_path(self) -> Path:
        task_root = codebench_tasks_dir() / "tasks" / self.task_dir
        candidates = (
            "prompt.md",
            "prompt_hard.md",
            "prompt_medium.md",
            "prompt_trivial.md",
        )
        for name in candidates:
            path = task_root / name
            if path.exists():
                return path
        variant_prompts = sorted(task_root.glob("prompt_*.md"))
        if variant_prompts:
            return variant_prompts[0]
        return task_root / "prompt.md"

    def prompt(self) -> str:
        p = self.prompt_path()
        text = p.read_text(encoding="utf-8").strip() if p.exists() else ""
        return text

    def workspace_src(self) -> Path | None:
        if self.source[0] == "workspace":
            return codebench_tasks_dir() / "tasks" / self.task_dir / self.source[1]
        return None


TASKS: list[Task] = [
    # --- codegraph 7-repo A/B (efficiency-only) ---
    Task(
        "cg_vscode",
        "typescript",
        ("repo", "https://github.com/microsoft/vscode", "be441a4dc809ea2d98fe7903fcdead9eb0ec31e7"),
        3,
        "cg_vscode",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_excalidraw",
        "typescript",
        ("repo", "https://github.com/excalidraw/excalidraw", "28a9b1711dc0625b8ab5d643dc871810ee13642f"),
        2,
        "cg_excalidraw",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_django",
        "python",
        ("repo", "https://github.com/django/django", "cd385e6b8c16b51f68c1f220ff09a4cfd679af0c"),
        2,
        "cg_django",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_tokio",
        "rust",
        ("repo", "https://github.com/tokio-rs/tokio", "7892f6020d9c914a41d0c350693fb71937d43c03"),
        2,
        "cg_tokio",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_okhttp",
        "java",
        ("repo", "https://github.com/square/okhttp", "6abc678ad07aefe055cb1afb6fd897c34a988eb9"),
        2,
        "cg_okhttp",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_gin",
        "go",
        ("repo", "https://github.com/gin-gonic/gin", "d75fcd4c9ab260e5225de590f1f0f8c0e0e12d11"),
        1,
        "cg_gin",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_alamofire",
        "swift",
        ("repo", "https://github.com/Alamofire/Alamofire", "7595cbcf59809f9977c5f6378500de2ad73b7ddb"),
        1,
        "cg_alamofire",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    # --- cg q2-q5 (additional exploration questions, same repos) ---
    Task(
        "cg_vscode_2",
        "typescript",
        ("repo", "https://github.com/microsoft/vscode", "be441a4dc809ea2d98fe7903fcdead9eb0ec31e7"),
        3,
        "cg_vscode_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_vscode_3",
        "typescript",
        ("repo", "https://github.com/microsoft/vscode", "be441a4dc809ea2d98fe7903fcdead9eb0ec31e7"),
        3,
        "cg_vscode_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_vscode_4",
        "typescript",
        ("repo", "https://github.com/microsoft/vscode", "be441a4dc809ea2d98fe7903fcdead9eb0ec31e7"),
        3,
        "cg_vscode_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_vscode_5",
        "typescript",
        ("repo", "https://github.com/microsoft/vscode", "be441a4dc809ea2d98fe7903fcdead9eb0ec31e7"),
        3,
        "cg_vscode_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_excalidraw_2",
        "typescript",
        ("repo", "https://github.com/excalidraw/excalidraw", "28a9b1711dc0625b8ab5d643dc871810ee13642f"),
        2,
        "cg_excalidraw_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_excalidraw_3",
        "typescript",
        ("repo", "https://github.com/excalidraw/excalidraw", "28a9b1711dc0625b8ab5d643dc871810ee13642f"),
        2,
        "cg_excalidraw_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_excalidraw_4",
        "typescript",
        ("repo", "https://github.com/excalidraw/excalidraw", "28a9b1711dc0625b8ab5d643dc871810ee13642f"),
        2,
        "cg_excalidraw_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_excalidraw_5",
        "typescript",
        ("repo", "https://github.com/excalidraw/excalidraw", "28a9b1711dc0625b8ab5d643dc871810ee13642f"),
        2,
        "cg_excalidraw_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_django_2",
        "python",
        ("repo", "https://github.com/django/django", "cd385e6b8c16b51f68c1f220ff09a4cfd679af0c"),
        2,
        "cg_django_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_django_3",
        "python",
        ("repo", "https://github.com/django/django", "cd385e6b8c16b51f68c1f220ff09a4cfd679af0c"),
        2,
        "cg_django_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_django_4",
        "python",
        ("repo", "https://github.com/django/django", "cd385e6b8c16b51f68c1f220ff09a4cfd679af0c"),
        2,
        "cg_django_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_django_5",
        "python",
        ("repo", "https://github.com/django/django", "cd385e6b8c16b51f68c1f220ff09a4cfd679af0c"),
        2,
        "cg_django_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_tokio_2",
        "rust",
        ("repo", "https://github.com/tokio-rs/tokio", "7892f6020d9c914a41d0c350693fb71937d43c03"),
        2,
        "cg_tokio_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_tokio_3",
        "rust",
        ("repo", "https://github.com/tokio-rs/tokio", "7892f6020d9c914a41d0c350693fb71937d43c03"),
        2,
        "cg_tokio_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_tokio_4",
        "rust",
        ("repo", "https://github.com/tokio-rs/tokio", "7892f6020d9c914a41d0c350693fb71937d43c03"),
        2,
        "cg_tokio_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_tokio_5",
        "rust",
        ("repo", "https://github.com/tokio-rs/tokio", "7892f6020d9c914a41d0c350693fb71937d43c03"),
        2,
        "cg_tokio_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_okhttp_2",
        "java",
        ("repo", "https://github.com/square/okhttp", "6abc678ad07aefe055cb1afb6fd897c34a988eb9"),
        2,
        "cg_okhttp_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_okhttp_3",
        "java",
        ("repo", "https://github.com/square/okhttp", "6abc678ad07aefe055cb1afb6fd897c34a988eb9"),
        2,
        "cg_okhttp_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_okhttp_4",
        "java",
        ("repo", "https://github.com/square/okhttp", "6abc678ad07aefe055cb1afb6fd897c34a988eb9"),
        2,
        "cg_okhttp_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_okhttp_5",
        "java",
        ("repo", "https://github.com/square/okhttp", "6abc678ad07aefe055cb1afb6fd897c34a988eb9"),
        2,
        "cg_okhttp_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_gin_2",
        "go",
        ("repo", "https://github.com/gin-gonic/gin", "d75fcd4c9ab260e5225de590f1f0f8c0e0e12d11"),
        1,
        "cg_gin_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_gin_3",
        "go",
        ("repo", "https://github.com/gin-gonic/gin", "d75fcd4c9ab260e5225de590f1f0f8c0e0e12d11"),
        1,
        "cg_gin_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_gin_4",
        "go",
        ("repo", "https://github.com/gin-gonic/gin", "d75fcd4c9ab260e5225de590f1f0f8c0e0e12d11"),
        1,
        "cg_gin_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_gin_5",
        "go",
        ("repo", "https://github.com/gin-gonic/gin", "d75fcd4c9ab260e5225de590f1f0f8c0e0e12d11"),
        1,
        "cg_gin_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_alamofire_2",
        "swift",
        ("repo", "https://github.com/Alamofire/Alamofire", "7595cbcf59809f9977c5f6378500de2ad73b7ddb"),
        1,
        "cg_alamofire_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_alamofire_3",
        "swift",
        ("repo", "https://github.com/Alamofire/Alamofire", "7595cbcf59809f9977c5f6378500de2ad73b7ddb"),
        1,
        "cg_alamofire_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_alamofire_4",
        "swift",
        ("repo", "https://github.com/Alamofire/Alamofire", "7595cbcf59809f9977c5f6378500de2ad73b7ddb"),
        1,
        "cg_alamofire_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_alamofire_5",
        "swift",
        ("repo", "https://github.com/Alamofire/Alamofire", "7595cbcf59809f9977c5f6378500de2ad73b7ddb"),
        1,
        "cg_alamofire_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    # --- Linux kernel (5 exploration questions) ---
    Task(
        "cg_linux_1",
        "c",
        ("repo", "https://github.com/torvalds/linux", None),
        3,
        "cg_linux_1",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_linux_2",
        "c",
        ("repo", "https://github.com/torvalds/linux", None),
        3,
        "cg_linux_2",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_linux_3",
        "c",
        ("repo", "https://github.com/torvalds/linux", None),
        3,
        "cg_linux_3",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_linux_4",
        "c",
        ("repo", "https://github.com/torvalds/linux", None),
        3,
        "cg_linux_4",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    Task(
        "cg_linux_5",
        "c",
        ("repo", "https://github.com/torvalds/linux", None),
        3,
        "cg_linux_5",
        setup_cmds=_INDEX_ON_LEMONCROW_REP,
    ),
    # --- original task1-8 (coding capability) ---
    Task(
        "task1",
        "swift",
        ("empty",),
        1,
        "task1_LRUFileCacheSPec",
        setup_cmds=("swift package --version",),
    ),
    Task(
        "task2",
        "swift",
        ("repo", "https://github.com/maquannene/Track", None),
        2,
        "task2_AddLoggingToCache",
        setup_cmds=("swift package resolve",),
    ),
    Task(
        "task3",
        "rust",
        ("repo", "https://github.com/serde-rs/json", "4f6dbfac79647d032b0997b5ab73022340c6dab7"),
        2,
        "task3_FixJsonParsingBug",
        setup_cmds=("cargo fetch --quiet",),
    ),
    Task(
        "task4",
        "python",
        ("workspace", "workspace"),
        1,
        "task4_WriteTestsForExportFlows",
        setup_cmds=(
            "uv venv .venv --python 3.13 --quiet",
            "uv pip install --quiet mitmproxy pytest --python .venv/bin/python",
        ),
    ),
    Task(
        "task5",
        "python",
        ("workspace", "workspace"),
        1,
        "task5_RefactorBasedOnTests",
        setup_cmds=(
            "uv venv .venv --python 3.13 --quiet",
            "uv pip install --quiet mitmproxy pytest --python .venv/bin/python",
        ),
    ),
    Task(
        "task6",
        "typescript",
        (
            "repo",
            "https://github.com/openclaw/openclaw",
            "412811ec19c553a7c249f75d94a13a65b61ea2e6",
        ),
        3,
        "task6_AddFrenchSupportToOpenClaw",
        setup_cmds=("npm ci --prefer-offline --silent 2>/dev/null || npm install --silent",),
    ),
    Task(
        "task7",
        "rust",
        ("repo", "https://github.com/kirby88/codex", "7a393668185da6710425698885731b9af28ca0e0"),
        3,
        "task7_FixCompileBugCodex",
        setup_cmds=("cargo fetch --quiet 2>/dev/null || true",),
    ),
    Task(
        "task8",
        "rust",
        ("workspace", "workspace"),
        1,
        "task8_RenameAcrossCallSites",
    ),
]

BY_ID = {t.id: t for t in TASKS}
