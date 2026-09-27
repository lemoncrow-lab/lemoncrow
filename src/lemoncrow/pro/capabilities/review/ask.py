"""Context assembly and answering for Review Ask.

Ask is deliberately read-only.  It consumes immutable Review artifacts and the
bounded observable author journal; it never reads the mutable checkout and it
never resumes the coding agent.  Explicit change requests are returned as a
preview instruction for the existing exact-session handoff path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from lemoncrow.infra.internal_llm import InternalLLMError, chat
from lemoncrow.pro.capabilities.review.session_models import ReviewAskMessage, ReviewRevision
from lemoncrow.pro.capabilities.review.store import ReviewStore

_MAX_SOURCE_FILES = 8
_MAX_SOURCE_CHARS = 36_000
_MAX_HISTORY_MESSAGES = 16
_MAX_JOURNAL_ROWS = 24
_MAX_EVIDENCE_ROWS = 12
_MAX_SYMBOL_ROWS = 16
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass(frozen=True)
class AskContextSelection:
    """Typed context seam for the single Change-scoped Ask conversation."""

    kind: str = "change"
    context_key: str = ""
    target_unit_key: str = ""
    path: str = ""
    symbol: str = ""
    side: str = "new"
    start_line: int = 0
    end_line: int = 0
    surface_context: Mapping[str, Any] | None = None


def wants_previous_snapshot_comparison(question: str) -> bool:
    """Return whether a natural question explicitly asks about the prior Snapshot."""

    lowered = " ".join(question.lower().split())
    return any(
        marker in lowered
        for marker in (
            "previous snapshot",
            "previous revision",
            "prior snapshot",
            "prior revision",
            "last snapshot",
            "last revision",
            "since previous",
            "since the previous",
            "since prior",
            "since the prior",
        )
    )


def _terms(question: str) -> tuple[str, ...]:
    stop = {
        "about",
        "after",
        "again",
        "also",
        "because",
        "could",
        "change",
        "did",
        "does",
        "from",
        "have",
        "into",
        "need",
        "should",
        "that",
        "this",
        "what",
        "when",
        "where",
        "which",
        "why",
        "with",
        "would",
        "there",
        "these",
        "those",
        "review",
        "revision",
        "code",
    }
    rows: list[str] = []
    for value in _WORD_RE.findall(question):
        term = value.lower()
        if term in stop or term in rows:
            continue
        rows.append(term)
    return tuple(rows[:12])


def _decode_source(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def exact_revision_sources(
    store: ReviewStore,
    revision: ReviewRevision,
    *,
    paths: Sequence[str],
    side: Literal["new", "old"] = "new",
) -> dict[str, str]:
    """Return exact persisted source for one side, never today's checkout bytes."""

    requested = tuple(dict.fromkeys(path for path in paths if path))
    if not requested:
        return {}

    tree = store.read_source_tree_artifact(revision.review_id, revision.id, side=side)
    if tree is not None:
        out: dict[str, str] = {}
        for path in requested:
            entry = tree.get(path)
            if entry is None:
                continue
            digest = str(entry.get("content_digest") or "")
            payload = store.read_source_content(digest) if digest else None
            if payload is None or hashlib.sha256(payload).hexdigest() != digest:
                continue
            out[path] = _decode_source(payload)
        return out

    # Legacy local captures persisted exact changed-file *new-side* blobs only.
    # Old/deletion-side questions must never silently receive those newer bytes.
    if side == "old":
        return {}
    blobs = store.read_blob_artifact(revision.review_id, revision.id) or {}
    return {path: blobs[path] for path in requested if path in blobs}


def _packet_rows(packet: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    raw = packet.get(name)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _candidate_paths(packet: Mapping[str, Any], question: str, selected_path: str) -> list[str]:
    files = _packet_rows(packet, "files")
    file_paths = [str(item.get("path") or "") for item in files if str(item.get("path") or "")]
    if selected_path:
        file_paths.insert(0, selected_path)

    terms = _terms(question)
    symbol_paths: list[str] = []
    for item in _packet_rows(packet, "symbols"):
        name = " ".join(str(item.get(key) or "") for key in ("symbol_name", "qualified_name")).lower()
        if terms and not any(term in name for term in terms):
            continue
        path = str(item.get("file_path") or "")
        if path:
            symbol_paths.append(path)

    scored: list[tuple[int, int, str]] = []
    for index, path in enumerate(dict.fromkeys([*symbol_paths, *file_paths])):
        lowered = path.lower()
        score = (100 if path == selected_path else 0) + sum(8 for term in terms if term in lowered)
        if path in symbol_paths:
            score += 24
        scored.append((-score, index, path))
    scored.sort()
    return [path for _, _, path in scored[:_MAX_SOURCE_FILES]]


def _source_snippet(
    text: str,
    *,
    start_line: int = 0,
    end_line: int = 0,
    question_terms: Sequence[str] = (),
) -> tuple[int, int, str]:
    lines = text.splitlines()
    if not lines:
        return 0, 0, ""

    if start_line > 0:
        lo = max(1, start_line - 18)
        hi = min(len(lines), max(end_line or start_line, start_line) + 18)
    else:
        hit = 1
        lowered_terms = tuple(term.lower() for term in question_terms)
        if lowered_terms:
            for index, line in enumerate(lines, start=1):
                lowered = line.lower()
                if any(term in lowered for term in lowered_terms):
                    hit = index
                    break
        lo = max(1, hit - 12)
        hi = min(len(lines), hit + 28)
    return lo, hi, "\n".join(lines[lo - 1 : hi])


def build_ask_context(
    *,
    store: ReviewStore,
    revision: ReviewRevision,
    packet: Mapping[str, Any],
    journal: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    previous_messages: Sequence[ReviewAskMessage],
    question: str,
    comparison: Mapping[str, Any] | None = None,
    selected_path: str = "",
    start_line: int = 0,
    end_line: int = 0,
    target_unit_key: str = "",
    symbol: str = "",
    side: str = "new",
    selection: AskContextSelection | None = None,
    code_intelligence_readiness: Mapping[str, Any] | None = None,
    semantic_retrieval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if selection is not None:
        selected_path = selection.path
        start_line = selection.start_line
        end_line = selection.end_line
        target_unit_key = selection.target_unit_key
        symbol = selection.symbol
        side = selection.side

    terms = _terms(" ".join(part for part in (question, symbol) if part))
    surface_context = dict(selection.surface_context or {}) if selection is not None else {}
    paths = _candidate_paths(packet, question + " " + symbol, selected_path)
    snapshot_paths = surface_context.get("snapshot_paths")
    if isinstance(snapshot_paths, list):
        exact_context_paths = [str(item) for item in snapshot_paths if isinstance(item, str) and item]
        paths = list(dict.fromkeys([*exact_context_paths[:4], *paths]))[:_MAX_SOURCE_FILES]
    sources = exact_revision_sources(store, revision, paths=paths, side="new")
    selected_side: Literal["new", "old"] = "old" if side in {"old", "deletions"} else "new"
    selected_side_sources = (
        exact_revision_sources(store, revision, paths=(selected_path,), side=selected_side) if selected_path else {}
    )
    if selected_path:
        if selected_path in selected_side_sources:
            sources[selected_path] = selected_side_sources[selected_path]
        elif selected_side == "old":
            sources.pop(selected_path, None)
    source_rows: list[dict[str, Any]] = []
    char_budget = _MAX_SOURCE_CHARS
    for path in paths:
        text = sources.get(path)
        if text is None:
            continue
        lo, hi, snippet = _source_snippet(
            text,
            start_line=start_line if path == selected_path else 0,
            end_line=end_line if path == selected_path else 0,
            question_terms=terms,
        )
        if not snippet:
            continue
        snippet = snippet[:char_budget]
        if not snippet:
            break
        char_budget -= len(snippet)
        source_rows.append(
            {
                "path": path,
                "side": selected_side if path == selected_path else "new",
                "start_line": lo,
                "end_line": hi,
                "text": snippet,
                "selected": path == selected_path,
                "target_unit_key": target_unit_key if path == selected_path else "",
                "symbol": symbol if path == selected_path else "",
            }
        )
        if char_budget <= 0:
            break

    changed_rows: list[dict[str, Any]] = []
    for item in _packet_rows(packet, "files")[:12]:
        changed_rows.append(
            {
                "path": str(item.get("path") or ""),
                "status": str(item.get("status") or ""),
                "additions": int(item.get("additions") or 0),
                "deletions": int(item.get("deletions") or 0),
                "category": str(item.get("category") or ""),
            }
        )

    impact_rows: list[dict[str, Any]] = []
    for item in _packet_rows(packet, "impact")[:12]:
        impact_rows.append(
            {
                "kind": str(item.get("kind") or ""),
                "path": str(item.get("path") or ""),
                "source_path": str(item.get("source_path") or ""),
                "snippet": str(item.get("snippet") or "")[:600],
                "in_patch": bool(item.get("in_patch")),
                "uncertainty": str(item.get("uncertainty") or ""),
            }
        )

    symbol_rows: list[dict[str, Any]] = []
    for item in _packet_rows(packet, "symbols"):
        item_path = str(item.get("file_path") or "")
        item_name = str(item.get("qualified_name") or item.get("symbol_name") or "")
        relevant = (
            (selected_path and item_path == selected_path)
            or (symbol and symbol.lower() in item_name.lower())
            or any(term in item_name.lower() for term in terms)
        )
        if not relevant and (selected_path or symbol or terms):
            continue
        symbol_rows.append(
            {
                "symbol_name": str(item.get("symbol_name") or ""),
                "qualified_name": str(item.get("qualified_name") or ""),
                "kind": str(item.get("kind") or ""),
                "file_path": item_path,
                "start_line": int(item.get("start_line") or 0),
                "end_line": int(item.get("end_line") or 0),
                "change": str(item.get("change") or ""),
                "caller_count": (
                    int(cast(Any, item.get("caller_count"))) if item.get("caller_count") is not None else -1
                ),
                "usage_count": (int(cast(Any, item.get("usage_count"))) if item.get("usage_count") is not None else -1),
                "source": str(item.get("source") or ""),
            }
        )
        if len(symbol_rows) >= _MAX_SYMBOL_ROWS:
            break

    selected_diff: list[dict[str, Any]] = []
    if selected_path:
        selected_file = next(
            (item for item in _packet_rows(packet, "files") if str(item.get("path") or "") == selected_path),
            None,
        )
        hunks = selected_file.get("hunks") if isinstance(selected_file, Mapping) else None
        if isinstance(hunks, list):
            remaining = 24_000
            for raw in hunks[:8]:
                if not isinstance(raw, Mapping) or remaining <= 0:
                    continue
                patch = str(raw.get("patch") or "")[:remaining]
                remaining -= len(patch)
                selected_diff.append(
                    {
                        "path": selected_path,
                        "side": selected_side,
                        "header": str(raw.get("header") or ""),
                        "patch": patch,
                    }
                )

    journal_rows = []
    for raw in list(journal.get("activity") or [])[-_MAX_JOURNAL_ROWS:]:
        if not isinstance(raw, Mapping):
            continue
        journal_rows.append(
            {
                "kind": str(raw.get("kind") or ""),
                "summary": str(raw.get("summary") or "")[:800],
                "detail": str(raw.get("detail") or "")[:1000],
                "path": str(raw.get("path") or ""),
                "status": str(raw.get("status") or ""),
            }
        )

    comparison_context: dict[str, Any] = {}
    if comparison:
        from_revision = comparison.get("from_revision")
        to_revision = comparison.get("to_revision")
        summary = comparison.get("code_summary")
        files = comparison.get("code_files")
        comparison_files: list[dict[str, Any]] = []
        remaining = 32_000
        if isinstance(files, list):
            preferred_paths = {selected_path} if selected_path else set()
            preferred_paths.update(
                str(item)
                for item in list(surface_context.get("snapshot_paths") or [])
                if isinstance(item, str) and item
            )
            ordered_files = [raw for raw in files if isinstance(raw, Mapping)]
            ordered_files.sort(
                key=lambda raw: (
                    0 if str(raw.get("path") or "") in preferred_paths else 1,
                    str(raw.get("path") or ""),
                )
            )
            for raw in ordered_files[:8]:
                patch = str(raw.get("patch") or "")[:remaining]
                if remaining <= 0:
                    break
                remaining -= len(patch)
                comparison_files.append(
                    {
                        "path": str(raw.get("path") or ""),
                        "status": str(raw.get("status") or ""),
                        "added_lines": int(raw.get("added_lines") or 0),
                        "removed_lines": int(raw.get("removed_lines") or 0),
                        "patch": patch,
                    }
                )
        from_revision_number = from_revision.get("revision_number") if isinstance(from_revision, Mapping) else None
        to_revision_number = to_revision.get("revision_number") if isinstance(to_revision, Mapping) else None
        comparison_context = {
            "from_revision_number": int(from_revision_number) if from_revision_number is not None else None,
            "to_revision_number": (
                int(to_revision_number) if to_revision_number is not None else max(0, revision.revision_number - 1)
            ),
            "available": bool(comparison.get("code_available", bool(comparison_files))),
            "unavailable_reason": str(comparison.get("code_unavailable_reason") or ""),
            "summary": dict(summary) if isinstance(summary, Mapping) else {},
            "files": comparison_files,
        }

    history = []
    for message in list(previous_messages)[-_MAX_HISTORY_MESSAGES:]:
        try:
            stored_context = json.loads(message.context_json or "{}")
        except (TypeError, json.JSONDecodeError):
            stored_context = {}
        history.append(
            {
                "role": message.role,
                "intent": message.intent,
                "body": message.body[:4000],
                "revision_id": message.revision_id,
                "path": message.path,
                "symbol": message.symbol,
                "side": message.side,
                "start_line": message.start_line,
                "end_line": message.end_line,
                "context": stored_context if isinstance(stored_context, dict) else {},
            }
        )

    selected_source_available = not selected_path or selected_path in selected_side_sources
    return {
        "revision": {
            "id": revision.id,
            "number": max(0, revision.revision_number - 1),
            "tree_fingerprint": revision.tree_fingerprint,
            "source_fingerprint": revision.source_fingerprint,
        },
        "question": question,
        "scope": {
            "kind": (
                selection.kind
                if selection is not None
                else ("source" if selected_path or target_unit_key else "change")
            ),
            "context_key": selection.context_key if selection is not None else (target_unit_key or selected_path),
            "path": selected_path,
            "side": side,
            "start_line": start_line,
            "end_line": end_line,
            "target_unit_key": target_unit_key,
            "symbol": symbol,
        },
        "grounding": {
            "selected_source_available": selected_source_available,
            "selected_source_reason": (
                ""
                if selected_source_available
                else f"exact persisted {selected_side}-side source is unavailable; use persisted diff evidence only"
            ),
        },
        "surface_context": surface_context,
        "changed_files": changed_rows,
        "selected_diff": selected_diff,
        "comparison": comparison_context,
        "source": source_rows,
        "impact": impact_rows,
        "code_intelligence": {
            "index_status": str(packet.get("index_status") or "absent"),
            "degraded": [str(item) for item in list(packet.get("degraded") or [])[:32]],
            "readiness": dict(code_intelligence_readiness or {}),
            "semantic_retrieval": dict(semantic_retrieval or {}),
            "symbols": symbol_rows,
        },
        "author_context": {
            "available": bool(journal.get("available")),
            "reason": str(journal.get("reason") or ""),
            "task": str(journal.get("task") or "")[:1000],
            "plan": [str(item)[:800] for item in list(journal.get("plan") or [])[:12]],
            "verified_facts": [str(item)[:800] for item in list(journal.get("verified_facts") or [])[:12]],
            "tests_run": [str(item)[:800] for item in list(journal.get("tests_run") or [])[:12]],
            "activity": journal_rows,
        },
        "evidence": [dict(item) for item in list(evidence)[:_MAX_EVIDENCE_ROWS]],
        "conversation": history,
    }


_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["question", "change_request"]},
        "body": {"type": "string"},
        "handoff_instruction": {"type": "string"},
    },
    "required": ["intent", "body", "handoff_instruction"],
}


def _parse_agent_json(output: str, *, driver: str) -> dict[str, Any]:
    text = output.strip()
    if not text:
        raise InternalLLMError(f"{driver} returned an empty Ask response")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise InternalLLMError(f"{driver} returned a non-JSON Ask response") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise InternalLLMError(f"{driver} returned invalid Ask JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InternalLLMError(f"{driver} returned a non-object Ask response")
    structured = parsed.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result_text = parsed.get("result")
    if isinstance(result_text, str) and result_text.strip().startswith("{"):
        return _parse_agent_json(result_text, driver=driver)
    return parsed


def discover_opencode_models() -> list[str]:
    """Return the model catalog exposed by the installed OpenCode/LemonCode host."""
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.gateway.cli.lemoncode_host import resolve_host_binary

    executable = resolve_host_binary(default_store_root())
    if not executable:
        return []
    env = dict(os.environ)
    env.update(
        {
            "LEMONCODE_CLI_NAME": "opencode",
            "LEMONCODE_PRODUCT_NAME": "OpenCode",
            "LEMONCODE_STORAGE_NAME": "opencode",
        }
    )
    try:
        completed = subprocess.run(
            [executable, "models"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    models: list[str] = []
    for raw_line in completed.stdout.splitlines():
        model_id = raw_line.strip()
        if "/" not in model_id or model_id.startswith(("{", "[")):
            continue
        if model_id not in models:
            models.append(model_id)
    return models


def _parse_opencode_events(output: str) -> dict[str, Any]:
    last_text = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            last_text = str(part["text"])
    if not last_text:
        raise InternalLLMError("opencode returned no final text event")
    return _parse_agent_json(last_text, driver="opencode")


def _agent_cli_answer(
    *,
    driver: Literal["claude", "codex", "opencode"],
    model: str | None,
    system: str,
    prompt: str,
    project_root: Path,
) -> dict[str, Any]:
    if driver == "opencode":
        from lemoncrow.core.foundation.paths import default_store_root
        from lemoncrow.gateway.cli.lemoncode_host import resolve_host_binary

        executable = resolve_host_binary(default_store_root())
    else:
        executable = shutil.which(driver)
    if not executable:
        raise InternalLLMError(f"{driver} CLI is not installed")
    cwd = project_root.resolve()
    instruction = (
        system
        + "\n\nYou are running inside the exact immutable Review snapshot. "
        + "Use the attached LemonCrow MCP tools (`code_search` first, then `read`/`grep` for exact source) "
        + "for code investigation. `bash` runs in a disposable writable execution copy of this Snapshot, "
        + "so use it for tests/builds/inspection when useful; writes there are discarded. "
        + "Use `web_fetch` when external documentation is needed. Do not infer from any checkout outside this workspace."
        + "\n\nReturn ONLY one JSON object with keys intent, body, handoff_instruction. "
        + "Do not wrap it in markdown.\n\nReview context:\n"
        + prompt
    )
    from lemoncrow.core.foundation.paths import pin_workspace_env

    mcp_env: dict[str, str] = {}
    pin_workspace_env(cwd, mcp_env)
    mcp_env.update(
        {
            "LEMONCROW_MCP_TOOL_PROFILE": "review",
            "LEMONCROW_CODE_AUTOSYNC": "0",
            "LEMONCROW_SERVICE_URL": "",
        }
    )
    mcp_command = sys.executable
    mcp_args = [
        "-c",
        "from lemoncrow.gateway.adapters.mcp_server import main; main()",
        "--host",
        driver,
    ]
    with tempfile.TemporaryDirectory(
        prefix=f"lemoncrow-review-ask-{driver}-",
        dir=str(cwd.parent),
    ) as tmp:
        control_root = Path(tmp)
        execution_root = control_root / "execution"
        shutil.copytree(
            cwd,
            execution_root,
            ignore=shutil.ignore_patterns(".lemoncrow"),
            dirs_exist_ok=True,
        )
        mcp_env["LEMONCROW_BASH_WORKSPACE_ROOT"] = str(execution_root)
        mcp_env["LEMONCROW_REVIEW_SNAPSHOT_ROOT"] = str(cwd)
        launch_env: dict[str, str] | None = None
        if driver == "claude":
            mcp_config = control_root / "mcp.json"
            mcp_config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "lemoncrow": {
                                "command": mcp_command,
                                "args": mcp_args,
                                "env": mcp_env,
                            }
                        }
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            allowed_tools = ",".join(
                [
                    "mcp__lemoncrow__code_search",
                    "mcp__lemoncrow__read",
                    "mcp__lemoncrow__grep",
                    "mcp__lemoncrow__bash",
                    "mcp__lemoncrow__web_fetch",
                ]
            )
            command = [
                executable,
                "--strict-mcp-config",
                "--mcp-config",
                str(mcp_config),
                "--setting-sources",
                "",
                "--allowedTools",
                allowed_tools,
                "--no-session-persistence",
                "--json-schema",
                json.dumps(_ANSWER_SCHEMA, separators=(",", ":")),
            ]
            if model:
                command.extend(["--model", model])
            command.extend(["-p", instruction, "--output-format", "json"])
        elif driver == "codex":
            env_toml = "{" + ",".join(f"{key}={json.dumps(value)}" for key, value in mcp_env.items()) + "}"
            command = [
                executable,
                "-C",
                str(cwd),
                "-a",
                "never",
                "-s",
                "read-only",
                "-c",
                f"mcp_servers.lemoncrow.command={json.dumps(mcp_command)}",
                "-c",
                "mcp_servers.lemoncrow.args=" + json.dumps(mcp_args, separators=(",", ":")),
                "-c",
                f"mcp_servers.lemoncrow.env={env_toml}",
                "-c",
                'mcp_servers.lemoncrow.default_tools_approval_mode="approve"',
                "-c",
                'mcp_servers.lemoncrow.enabled_tools=["code_search","read","grep","bash","web_fetch"]',
            ]
            if model:
                command.extend(["-m", model])
            command.extend(["exec", "--skip-git-repo-check", instruction])
        else:
            chosen_model = model or ""
            if not (chosen_model.startswith("opencode/") or chosen_model.startswith("ollama/")):
                raise InternalLLMError("opencode Review Ask requires an opencode/<model> or ollama/<model> selection")
            provider_id = chosen_model.split("/", 1)[0]
            opencode_config: dict[str, Any] = {
                "model": chosen_model,
                "default_agent": "review",
                "enabled_providers": [provider_id],
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "compaction": {"auto": False, "prune": True},
                "mcp": {
                    "lemoncrow": {
                        "type": "local",
                        "command": [mcp_command, *mcp_args],
                        "environment": mcp_env,
                    }
                },
                "agent": {
                    "review": {
                        "mode": "primary",
                        "tools": {
                            "write": False,
                            "edit": False,
                            "patch": False,
                            "bash": False,
                            "read": False,
                            "grep": False,
                            "glob": False,
                            "webfetch": False,
                        },
                    }
                },
            }
            if provider_id == "ollama":
                ollama_model = chosen_model.removeprefix("ollama/")
                from lemoncrow.core.capabilities.providers.config import load_providers_config
                from lemoncrow.core.foundation.paths import default_store_root

                configured_base = (load_providers_config(default_store_root()).get("ollama", "base_url") or "").strip()
                ollama_base = configured_base or os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
                ollama_base = ollama_base.rstrip("/")
                if not ollama_base.endswith("/v1"):
                    ollama_base += "/v1"
                opencode_config["provider"] = {
                    "ollama": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Ollama",
                        "options": {"baseURL": ollama_base, "apiKey": "ollama"},
                        "models": {
                            ollama_model: {
                                "name": ollama_model,
                                "limit": {"context": 131072, "output": 8192},
                            }
                        },
                    }
                }

            data_root = control_root / "data"
            auth_source = (
                Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode" / "auth.json"
            )
            if auth_source.is_file():
                auth_target = data_root / "opencode" / "auth.json"
                auth_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(auth_source, auth_target)

            launch_env = dict(os.environ)
            launch_env.update(
                {
                    "LEMONCODE_CLI_NAME": "opencode",
                    "LEMONCODE_PRODUCT_NAME": "OpenCode",
                    "LEMONCODE_STORAGE_NAME": "opencode",
                    "OPENCODE_CONFIG_CONTENT": json.dumps(opencode_config, separators=(",", ":")),
                    "OPENCODE_DISABLE_AUTOUPDATE": "true",
                    "OPENCODE_DISABLE_AUTOCOMPACT": "true",
                    "OPENCODE_DISABLE_MODELS_FETCH": "true",
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                    "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "true",
                    "XDG_CONFIG_HOME": str(control_root / "config"),
                    "XDG_DATA_HOME": str(data_root),
                    "XDG_CACHE_HOME": str(control_root / "cache"),
                }
            )
            command = [
                executable,
                "--pure",
                "--model",
                chosen_model,
                "--agent",
                "review",
                "run",
                "--format",
                "json",
                instruction,
            ]
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=launch_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InternalLLMError(f"{driver} Ask execution failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
            raise InternalLLMError(f"{driver} Ask execution failed: {detail or f'exit {completed.returncode}'}")
        if driver == "opencode":
            return _parse_opencode_events(completed.stdout)
        return _parse_agent_json(completed.stdout, driver=driver)


def _sanitize_snapshot_paths(text: str, project_root: Path | None) -> str:
    """Remove server-local immutable Snapshot paths from model-authored prose."""

    if project_root is None or not text:
        return text
    root = str(project_root.resolve())
    # CLI agents sometimes turn a source citation into a Markdown link to the
    # private materialized Snapshot path. The API already returns structured,
    # repo-relative references, so keep only the human label and redact any
    # remaining bare occurrence of the server-local root.
    linked = re.sub(rf"\[([^\]]+)\]\({re.escape(root)}/[^)\n]+\)", r"\1", text)
    return linked.replace(root, ".")


def answer_ask(
    context: Mapping[str, Any],
    *,
    backend: str | None = None,
    model: str | None = None,
    project_root: Path | None = None,
) -> dict[str, str]:
    """Answer one Ask turn with the selected read-only AI driver."""

    system = """You are LemonCrow Review Ask, a read-only engineering review assistant.

Answer like another engineer explaining the change, in concise conversational prose.
Ground claims in the exact Review revision context supplied below.

Important distinctions:
- author_context contains observable recorded author-session facts only; hidden model reasoning is neither captured nor available here.
- Recorded author context is evidence of what the author/agent actually stated or did.
- If rationale is not recorded, say that you do not see a recorded reason and describe what the implementation appears to do. Never invent author intent.
- If code_intelligence.index_status is absent/stale or degraded says relation/caller intelligence is missing, disclose that limitation rather than guessing around it. Treat caller/usage count -1 as unknown, never zero.
- code_intelligence.readiness is the live provider state, not Snapshot evidence. If graph is building/degraded/unavailable, do not claim exhaustive callers or impact. If semantic is degraded/unavailable, source and graph evidence remain usable when their own states permit it. Never collapse layer readiness into a generic confidence score.
- code_intelligence.semantic_retrieval is bounded current-index discovery context, never Snapshot-exact source. Use it to find potentially relevant surrounding paths/symbols, but ground claims about the reviewed Change in persisted Snapshot source/diff/evidence.
- If grounding says exact selected-side source is unavailable, use only persisted diff/source evidence and disclose the source gap when it matters.
- surface_context may describe a Review Map node or edge. Snapshot facts in it are frozen to this Review revision; facts labeled current_index are surrounding present-day Code Map context and must never be presented as Snapshot-exact.
- A current_index Map edge proves only that the surrounding relationship exists in the current index. Never claim the Change introduced, removed, or altered that relationship unless Snapshot/comparison evidence supports it.
- Use graph/impact/caller evidence in surface_context when the question is about dependencies, callers, impact, or why a mapped area was touched. If current graph context is unavailable/stale, say so instead of guessing.
- Whether the authoring agent inspected important callers must come from recorded author_context activity/facts/tests; caller presence in the Map alone is not evidence that the agent inspected it.
- A normal question is read-only and must never be converted into an instruction to the coding agent.
- Classify as change_request only when the reviewer explicitly asks for code to be changed, removed, simplified, renamed, fixed, implemented, or otherwise modified.
- For a change_request, answer conversationally and provide a concrete handoff_instruction that can be previewed verbatim before the reviewer sends it to the authoring agent.
- For a question, handoff_instruction must be empty.
- Do not use rigid headings such as INTENT/OBSERVATION/EVIDENCE/CONFIDENCE.
- Cite reviewed source with repo-relative path:line text only. Never include absolute filesystem paths, Snapshot cache paths, or file:// links.
"""
    prompt = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if backend in {"claude", "codex", "opencode"}:
        if project_root is None:
            raise InternalLLMError(f"{backend} Ask requires an immutable Review snapshot")
        result: dict[str, Any] | str = _agent_cli_answer(
            driver=cast(Literal["claude", "codex", "opencode"], backend),
            model=model,
            system=system,
            prompt=prompt,
            project_root=project_root,
        )
    else:
        result = chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            json_schema=_ANSWER_SCHEMA,
            backend=backend,
            model=model,
        )
    if not isinstance(result, Mapping):
        raise InternalLLMError("Review Ask model returned a non-object response")
    intent = str(result.get("intent") or "question")
    if intent not in {"question", "change_request"}:
        intent = "question"
    body = _sanitize_snapshot_paths(str(result.get("body") or "").strip(), project_root)
    instruction = _sanitize_snapshot_paths(str(result.get("handoff_instruction") or "").strip(), project_root)
    if not body:
        raise InternalLLMError("Review Ask model returned an empty answer")
    if intent != "change_request":
        instruction = ""
    elif not instruction:
        instruction = str(context.get("question") or "").strip()
    return {"intent": intent, "body": body, "handoff_instruction": instruction}


__all__ = [
    "AskContextSelection",
    "answer_ask",
    "build_ask_context",
    "exact_revision_sources",
    "wants_previous_snapshot_comparison",
]
