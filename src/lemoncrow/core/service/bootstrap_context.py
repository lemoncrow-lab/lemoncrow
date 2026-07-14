"""Deterministic bootstrap context planning and pinned-memory persistence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.infra.storage.memory_store import MemoryStore
from lemoncrow.infra.tree_sitter.tags import detect_language
from lemoncrow.pro.capabilities.code_context import CodeContextEngine
from lemoncrow.pro.capabilities.repo_map.graph import iter_source_files

BOOTSTRAP_BLOCK_TYPES = (
    "architecture-sketch",
    "entry-points",
    "hot-symbols-top",
    "language-mix",
)
_ENTRYPOINT_FILE_PARTS = ("main", "app", "cli", "server", "index")
_ENTRYPOINT_SYMBOL_NAMES = {
    "app",
    "bootstrap",
    "cli",
    "create_app",
    "main",
    "run",
    "serve",
    "server",
}
_BOOTSTRAP_IGNORED_PARTS = {
    ".claude",
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
}


class BootstrapBlockPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    block_type: str
    value: str
    description: str
    metadata: dict[str, Any]


class BootstrapPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    agent_id: str
    labels: list[str]
    plan_signature: str
    blocks: list[BootstrapBlockPlan]


class BootstrapPersistResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    agent_id: str
    written_labels: list[str]
    reused_labels: list[str]
    pending_labels: list[str]
    status: Literal["partial", "complete"]


def bootstrap_agent_id(repo_id: str) -> str:
    return f"bootstrap:{repo_id}"


def expected_bootstrap_labels(repo_id: str) -> list[str]:
    return [f"bootstrap/{repo_id}/{block_type}" for block_type in BOOTSTRAP_BLOCK_TYPES]


def list_bootstrap_blocks(memory_store: MemoryStore, repo_id: str) -> list[MemoryBlock]:
    agent_id = bootstrap_agent_id(repo_id)
    blocks: list[MemoryBlock] = []
    for label in expected_bootstrap_labels(repo_id):
        block = memory_store.get_block(agent_id, label)
        if block is not None:
            blocks.append(block)
    return blocks


def bootstrap_status(memory_store: MemoryStore, repo_id: str) -> Literal["cold", "partial", "warm"]:
    existing = {block.label for block in list_bootstrap_blocks(memory_store, repo_id)}
    labels = expected_bootstrap_labels(repo_id)
    if not existing:
        return "cold"
    if len(existing) < len(labels):
        return "partial"
    return "warm"


def missing_bootstrap_labels(memory_store: MemoryStore, repo_id: str) -> list[str]:
    existing = {block.label for block in list_bootstrap_blocks(memory_store, repo_id)}
    return [label for label in expected_bootstrap_labels(repo_id) if label not in existing]


def render_bootstrap_context(memory_store: MemoryStore, repo_id: str) -> tuple[str, list[dict[str, Any]]]:
    blocks = list_bootstrap_blocks(memory_store, repo_id)
    if not blocks:
        return "", []
    lines = ["# Repository bootstrap", ""]
    metadata: list[dict[str, Any]] = []
    for block in blocks:
        block_type = block.label.rsplit("/", 1)[-1]
        lines.extend([f"## {block_type}", block.value.strip(), ""])
        metadata.append(
            {
                "label": block.label,
                "block_type": block_type,
                "updated_at": block.updated_at.isoformat(),
            }
        )
    return "\n".join(lines).strip(), metadata


def build_bootstrap_plan(repo_root: str | Path) -> BootstrapPlan:
    root = Path(repo_root).resolve()
    # One-shot planning call (invoked from the background bootstrap job and
    # benchmark preseeding, never a long-lived interactive session): no need
    # for a live autosync file-watcher thread, and leaving one running past
    # this function's return leaks a watchdog.Observer that has been
    # observed thrashing (inotify watch-limit exhaustion -> polling fallback
    # -> native tree-sitter Node objects dropped cross-thread) for the rest
    # of the process's life. Same reasoning as the CLI's one-shot engine
    # (gateway/cli/commands/code.py's _code_context_engine).
    engine = CodeContextEngine(root, autosync_enabled=False)
    engine.index_repo()
    repo_map = engine.repo_map(budget_tokens=1200)
    outline_payload = engine.file_outline(limit=400, auto_index=False)
    files = {str(path): list(items) for path, items in dict(outline_payload.get("files", {})).items()}
    repo_id = engine.repo_id
    agent_id = bootstrap_agent_id(repo_id)
    labels = expected_bootstrap_labels(repo_id)

    _BLOCK_LIMIT = 7900  # leave headroom below MemoryBlock.limit_chars (8000)
    block_values = {
        labels[0]: _render_architecture_sketch(repo_map, files)[:_BLOCK_LIMIT],
        labels[1]: _render_entry_points(repo_map.get("ranked_files", []), files)[:_BLOCK_LIMIT],
        labels[2]: _render_hot_symbols(repo_map.get("ranked_files", []), files)[:_BLOCK_LIMIT],
        labels[3]: _render_language_mix(root, repo_id)[:_BLOCK_LIMIT],
    }
    signature = hashlib.sha256(
        json.dumps(block_values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    blocks = [
        BootstrapBlockPlan(
            label=label,
            block_type=label.rsplit("/", 1)[-1],
            value=block_values[label],
            description=f"Deterministic bootstrap block for {label.rsplit('/', 1)[-1]}",
            metadata={
                "bootstrap": {
                    "repo_id": repo_id,
                    "plan_signature": signature,
                    "block_type": label.rsplit("/", 1)[-1],
                    "summary_source": "deterministic-local",
                    "planned_labels": labels,
                }
            },
        )
        for label in labels
    ]
    return BootstrapPlan(
        repo_id=repo_id,
        agent_id=agent_id,
        labels=labels,
        plan_signature=signature,
        blocks=blocks,
    )


def persist_bootstrap_plan(
    repo_root: str | Path,
    memory_store: MemoryStore,
    *,
    actor: str = "system:bootstrap-context",
    labels: list[str] | None = None,
) -> BootstrapPersistResult:
    plan = build_bootstrap_plan(repo_root)
    selected_labels = set(labels or plan.labels)
    planned_by_label = {block.label: block for block in plan.blocks}
    existing = {block.label: block for block in list_bootstrap_blocks(memory_store, plan.repo_id)}
    reusable = {
        label
        for label, block in existing.items()
        if label in selected_labels
        and isinstance(block.metadata.get("bootstrap"), dict)
        and block.metadata["bootstrap"].get("plan_signature") == plan.plan_signature
    }
    to_write = [label for label in plan.labels if label in selected_labels and label not in reusable]
    final_completed = [label for label in plan.labels if label in reusable or label in to_write]
    pending_labels = [label for label in plan.labels if label not in final_completed]
    status: Literal["partial", "complete"] = "complete" if not pending_labels else "partial"

    for label in to_write:
        block_plan = planned_by_label[label]
        prior = existing.get(label)
        metadata = dict(block_plan.metadata)
        metadata["bootstrap"] = dict(metadata["bootstrap"])
        metadata["bootstrap"].update(
            {
                "completed_labels": final_completed,
                "pending_labels": pending_labels,
                "status": status,
            }
        )
        seed = prior or MemoryBlock(
            agent_id=plan.agent_id,
            label=label,
            value=block_plan.value,
            description=block_plan.description,
            pinned=True,
            metadata=metadata,
        )
        block = MemoryBlock(
            id=seed.id,
            agent_id=plan.agent_id,
            label=label,
            value=block_plan.value,
            description=block_plan.description,
            pinned=True,
            metadata=metadata,
            version=seed.version,
            current_history_id=seed.current_history_id,
            created_at=seed.created_at,
        )
        memory_store.upsert_block(block, actor=actor, reason="bootstrap-context")

    return BootstrapPersistResult(
        repo_id=plan.repo_id,
        agent_id=plan.agent_id,
        written_labels=to_write,
        reused_labels=[label for label in plan.labels if label in reusable],
        pending_labels=pending_labels,
        status=status,
    )


def _render_architecture_sketch(repo_map: dict[str, Any], files: dict[str, list[dict[str, Any]]]) -> str:
    ranked_files = [str(item) for item in repo_map.get("ranked_files", []) if _bootstrap_path_allowed(str(item))]
    lines = ["ranked files:"]
    for file_path in ranked_files[:8]:
        lines.append(f"- {file_path}")
    outline = str(repo_map.get("outline", "")).strip()
    if outline:
        lines.extend(["", "repo map:", outline])
    elif files:
        lines.extend(["", "outline:", *[f"- {path}" for path in sorted(files)[:8]]])
    else:
        lines.append("- no indexed symbols")
    return "\n".join(lines).strip()


def _render_entry_points(ranked_files: list[str], files: dict[str, list[dict[str, Any]]]) -> str:
    filtered_ranked = [path for path in ranked_files if _bootstrap_path_allowed(path)]
    filtered_files = {path: items for path, items in files.items() if _bootstrap_path_allowed(path)}
    order = {path: index for index, path in enumerate(filtered_ranked)}
    entries: list[tuple[int, str, int, str]] = []
    for file_path, items in filtered_files.items():
        basename = Path(file_path).stem.lower()
        for item in items:
            name = str(item.get("name", ""))
            qualified_name = str(item.get("qualified_name", name))
            if _is_entry_point(file_path=file_path, basename=basename, name=name):
                entries.append(
                    (
                        order.get(file_path, len(order) + 1),
                        file_path,
                        int(item.get("line_start", 0) or 0),
                        f"- {file_path}:{item.get('line_start', '?')} {qualified_name} :: {item.get('signature', '').strip()}",
                    )
                )
    if not entries:
        for file_path in sorted(filtered_files)[:3]:
            for item in filtered_files[file_path][:1]:
                entries.append(
                    (
                        order.get(file_path, len(order) + 1),
                        file_path,
                        int(item.get("line_start", 0) or 0),
                        f"- {file_path}:{item.get('line_start', '?')} {item.get('qualified_name', item.get('name', ''))}",
                    )
                )
    entries.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return "\n".join(entry[3] for entry in entries[:8])


def _render_hot_symbols(ranked_files: list[str], files: dict[str, list[dict[str, Any]]]) -> str:
    filtered_ranked = [path for path in ranked_files if _bootstrap_path_allowed(path)]
    filtered_files = {path: items for path, items in files.items() if _bootstrap_path_allowed(path)}
    order = {path: index for index, path in enumerate(filtered_ranked)}
    selected: list[tuple[int, str, int, str]] = []
    for file_path, items in filtered_files.items():
        for item in items:
            selected.append(
                (
                    order.get(file_path, len(order) + 1),
                    file_path,
                    int(item.get("line_start", 0) or 0),
                    f"- {item.get('qualified_name', item.get('name', ''))} :: {item.get('signature', '').strip()} ({file_path}:{item.get('line_start', '?')})",
                )
            )
    selected.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return "\n".join(entry[3] for entry in selected[:12])


def _render_language_mix(repo_root: Path, repo_id: str) -> str:
    files = iter_source_files(repo_root)
    languages = Counter((detect_language(path) or "unknown") for path in files)
    lines = ["languages:"]
    for language, count in sorted(languages.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {language}: {count} files")
    if not languages:
        lines.append("- none")
    return "\n".join(lines).strip()


def _is_entry_point(*, file_path: str, basename: str, name: str) -> bool:
    lowered_name = name.lower()
    if any(part == basename or basename.startswith(f"{part}.") for part in _ENTRYPOINT_FILE_PARTS):
        return True
    if any(part in file_path.lower().split("/")[-1] for part in _ENTRYPOINT_FILE_PARTS):
        return True
    return lowered_name in _ENTRYPOINT_SYMBOL_NAMES


def _bootstrap_path_allowed(path_text: str) -> bool:
    parts = [part for part in Path(path_text).parts if part not in {"", "."}]
    return all(part not in _BOOTSTRAP_IGNORED_PARTS for part in parts)


__all__ = [
    "BOOTSTRAP_BLOCK_TYPES",
    "BootstrapPersistResult",
    "BootstrapPlan",
    "bootstrap_agent_id",
    "bootstrap_status",
    "build_bootstrap_plan",
    "expected_bootstrap_labels",
    "list_bootstrap_blocks",
    "missing_bootstrap_labels",
    "persist_bootstrap_plan",
    "render_bootstrap_context",
]
