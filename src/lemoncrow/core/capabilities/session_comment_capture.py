"""Hook-only capture of all visible agent messages at Stop.

The session ledger owns retention. Its checkpoint and reply event commit together;
no Review is created, no network/model work runs, and no MCP tool is involved.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow_client.kit.redaction import redact

from lemoncrow.core.foundation.paths import confine_to_root, local_server_review_root, session_dir
from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json

_ACTIVE = re.compile(r"(?:You are operating as[\s\x60]*|^/)(?:\$?lemoncrow[:.])([a-z][a-z0-9_-]*)", re.MULTILINE)


def agent_role(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    name = re.sub(r"^lemoncrow[:.]", "", value.strip(), flags=re.IGNORECASE)
    if not name or name.lower() in {"default", "assistant", "agent", "human"} or re.search(r"[\x00-\x1f\x7f]", name):
        return "unknown"
    return name


def _texts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "output_text", "input_text"}
        )
    return ""


def review_findings(text: str) -> list[dict[str, Any]]:
    """Only explicit structured findings; never infer locations from prose."""
    candidates = [text.strip(), *re.findall(r"\x60\x60\x60(?:json)?\s*\n(.*?)\x60\x60\x60", text, re.DOTALL)]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("findings"), list):
            return [item for item in value["findings"] if isinstance(item, dict)]
    return []


def message_locations(text: str) -> list[dict[str, Any]]:
    """Use explicit agent locations, independent of producer role."""
    findings = review_findings(text)
    if findings:
        return findings
    # Codex/Claude prose commonly names files as path:line or path#Lline.
    # These are claims to verify, never instructions to relocate the comment.
    pattern = re.compile(
        r"(?<![\w/])(?P<path>/?(?:[\w.@+-]+/)*[\w.@+-]+\.[\w+-]+)"
        r"(?::(?P<line>[1-9]\d*)(?:-(?:L)?(?P<end>[1-9]\d*))?"
        r"|#L(?P<link_line>[1-9]\d*)(?:-L(?P<link_end>[1-9]\d*))?)(?!\d)"
    )
    seen: set[tuple[str, int, int]] = set()
    locations = []
    for match in pattern.finditer(text):
        # An external URL is not a local file reference.
        prefix = text[max(0, match.start() - 256) : match.start()]
        if re.search(r"(?:https?|app):[^\s\x60()]*$", prefix):
            continue
        line = int(match.group("line") or match.group("link_line"))
        end = int(match.group("end") or match.group("link_end") or line)
        target = (match.group("path"), line, max(line, end))
        if target in seen:
            continue
        seen.add(target)
        locations.append(
            {
                "path": target[0],
                "start_line": target[1],
                "end_line": target[2],
                "body": text,
                "_message_reference": True,
            }
        )
    return locations


def message_edit_scopes(events: list[Any]) -> dict[int, tuple[str, ...]]:
    """Consume this ledger's edits at each visible assistant output boundary.

    Ledgers append Stop-captured output after PostToolUse events, so event time,
    rather than append order, defines the interval. Missing times prove no scope.
    """
    timeline: list[tuple[datetime, int, int]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        edit = event.get("kind") == "file_edit"
        # A host's reported review is a tool result, not a reply: it does not end
        # the reply's edit interval.
        output = (
            event.get("kind") == "agent_message"
            and payload.get("role") == "assistant"
            and payload.get("event") != "ReportedFindings"
        )
        if not edit and not output:
            continue
        try:
            at = datetime.fromisoformat(str(event.get("at") or "").replace("Z", "+00:00"))
            if at.tzinfo is None:
                continue
        except ValueError:
            continue
        # Hooks have second precision; a same-second edit precedes the output.
        timeline.append((at, 0 if edit else 1, index))
    scopes: dict[int, tuple[str, ...]] = {}
    pending: set[str] = set()
    for _, kind, index in sorted(timeline):
        payload = events[index]["payload"]
        if kind == 0:
            path = str(payload.get("path") or "")
            path = re.sub(r":(?:minified:)?L[0-9]+(?:-L[0-9]+)?$", "", path)
            if path:
                pending.add(path)
        else:
            scopes[index] = tuple(sorted(pending))
            pending.clear()
    return scopes


def finding_location(finding: dict[str, Any]) -> tuple[str, int, int]:
    location = finding.get("code_location") or {}
    if not isinstance(location, dict):
        location = {}
    span = location.get("line_range") or {}
    if not isinstance(span, dict):
        span = {}
    path = str(location.get("absolute_file_path") or finding.get("path") or finding.get("file") or "")
    try:
        line = int(span.get("start") or finding.get("line") or finding.get("start_line") or 0)
        end = int(span.get("end") or finding.get("end_line") or line)
    except (ValueError, TypeError):
        return path, 0, 0
    return path, max(0, line), max(line, end)


def _server_review_binding(root: Path, checkout: Path) -> dict[str, str]:
    """Resolve the active local-server Review for one exact workspace."""

    review_root = local_server_review_root(root)
    if not review_root.is_dir():
        return {}
    matches: list[tuple[str, str, dict[str, str]]] = []
    for partition in review_root.iterdir():
        workspace_root = partition / ".workspace-roots"
        db = partition / "lemoncrow_reviews.db"
        if not workspace_root.is_dir() or not db.is_file():
            continue
        for marker in workspace_root.iterdir():
            if not marker.is_file():
                continue
            try:
                recorded = Path(marker.read_text(encoding="utf-8").strip()).expanduser().resolve()
            except (OSError, ValueError):
                continue
            if recorded != checkout:
                continue
            try:
                with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=0.1) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT review_id, repository_identity, subject_kind, subject_key, worktree_key, "
                        "created_at, updated_at FROM review_bindings WHERE review_id=? AND active=1",
                        (marker.name,),
                    ).fetchall()
            except sqlite3.Error:
                continue
            matches.extend(
                (
                    str(row["updated_at"] or ""),
                    str(row["created_at"] or ""),
                    {
                        "review_id": str(row["review_id"]),
                        "repository_identity": str(row["repository_identity"]),
                        "subject_kind": str(row["subject_kind"]),
                        "subject_key": str(row["subject_key"]),
                        "worktree_key": str(row["worktree_key"]),
                    },
                )
                for row in rows
            )
    return max(matches, key=lambda item: (item[0], item[1], item[2]["review_id"]))[2] if matches else {}


def _context(root: Path, workspace: str, messages: list[str]) -> dict[str, Any]:
    """Pin the actual checkout/branch and explicit finding bytes at Stop."""
    result: dict[str, Any] = {"workspace": workspace, "targets": {}, "captured_at": datetime.now(UTC).isoformat()}
    try:
        import pygit2

        git_dir = pygit2.discover_repository(workspace)
        if not git_dir:
            return result
        repo = pygit2.Repository(git_dir)
        checkout = Path(repo.workdir or workspace).resolve()
        common = Path(getattr(repo, "commondir", "") or repo.path).resolve()
        repository = hashlib.sha256(str(common).encode("utf-8", errors="surrogateescape")).hexdigest()
        branch = "" if repo.head_is_detached or repo.head_is_unborn else repo.head.name
        worktree = hashlib.sha256(str(checkout).encode("utf-8", errors="surrogateescape")).hexdigest()[:32]
        subject = branch or f"worktree:{worktree}"
        result.update(
            workspace=str(checkout),
            repository_identity=repository,
            subject_kind="branch" if branch else "detached",
            subject_key=subject,
            worktree_key=worktree,
        )
        # Prefer the server-owned binding when present. The legacy global Review
        # DB below remains as a compatibility fallback for older/local-only installs.
        result.update(_server_review_binding(root, checkout))
        db = root / "lemoncrow_reviews.db"
        if db.is_file():
            try:
                with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=0.1) as conn:
                    rows = conn.execute(
                        "SELECT review_id FROM review_bindings WHERE repository_identity=? AND subject_kind=? "
                        "AND subject_key=? AND worktree_key=? AND active=1",
                        (repository, result["subject_kind"], subject, worktree),
                    ).fetchall()
                    if len(rows) == 1 and not result.get("review_id"):
                        result["review_id"] = rows[0][0]
            except sqlite3.Error:
                pass
        for finding in (finding for text in messages for finding in message_locations(text)):
            raw_path, _, _ = finding_location(finding)
            if not raw_path:
                continue
            try:
                target = confine_to_root(checkout / raw_path, checkout)
                relative = target.relative_to(checkout).as_posix()
                result.setdefault("_target_paths", {})[raw_path] = relative
                if relative in result["targets"]:
                    continue
                content = target.read_text(encoding="utf-8", errors="replace")
                result["targets"][relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            except (ValueError, OSError):
                continue
    except Exception:
        # Context enrichment cannot prevent the completed visible reply from
        # being retained (including libgit2 errors on a disappearing checkout).
        pass
    return result


def capture_session_comment(root: str | Path, payload: dict[str, Any], *, host: str) -> int:
    """Read newly appended JSONL and retain every visible assistant message.

    An unfinished JSONL record is left for the next hook. Inode/header checks
    detect rotation or rewritten prefixes. Original provider IDs dedupe replay.
    """
    sid = str(payload.get("agent_id") or payload.get("subagent_id") or payload.get("session_id") or "").strip()
    raw_path = payload.get("agent_transcript_path") or payload.get("transcript_path") or payload.get("session_path")
    if not sid or not raw_path:
        return 0
    transcript = Path(str(raw_path)).expanduser()
    root_path = Path(root).expanduser().resolve()
    run_file = session_dir(root_path, host, sid) / "run.json"
    workspace = str(payload.get("cwd") or payload.get("workspace") or "")
    with RunFileLock(run_file):
        if run_file.is_file():
            data = json.loads(run_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return 0
        else:
            data = {
                "session_id": sid,
                "agent": host,
                "workspace_path": workspace,
                "created_at": datetime.now(UTC).isoformat(),
                "events": [],
                "files_touched": [],
            }
        events = data.setdefault("events", [])
        if not isinstance(events, list):
            return 0
        checkpoint = data.get("review_comment_capture") or {}
        stat = transcript.stat()
        with transcript.open("rb") as stream:
            prefix_length = int(checkpoint.get("prefix_length") or min(stat.st_size, 256))
            prefix = stream.read(min(stat.st_size, 256))
            same = (
                checkpoint.get("inode") == stat.st_ino
                and checkpoint.get("header") == hashlib.sha256(prefix[:prefix_length]).hexdigest()
            )
            header = hashlib.sha256(prefix).hexdigest()
            continuing = same and checkpoint.get("version") == 2
            offset = int(checkpoint.get("offset") or 0) if continuing else 0
            if offset > stat.st_size:
                offset = 0
            stream.seek(offset)
            role = agent_role(checkpoint.get("agent_role")) if continuing else "unknown"
            model = str(checkpoint.get("model") or "") if continuing else ""
            role_before_plan = agent_role(checkpoint.get("role_before_plan")) if continuing else "unknown"
            role_from_mode = bool(checkpoint.get("role_from_mode")) if continuing else False
            explicit_role = payload.get("agent_role") or payload.get("agent_type")
            if explicit_role:
                role = agent_role(explicit_role)
            model = str(payload.get("model") or payload.get("model_id") or model)
            candidates: list[dict[str, Any]] = []
            turn_key = str(checkpoint.get("turn_key") or "") if continuing else ""
            raw_last_visible = checkpoint.get("last_visible") if continuing else None
            last_visible: dict[str, Any] | None = raw_last_visible if isinstance(raw_last_visible, dict) else None
            while True:
                start = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    stream.seek(start)
                    break
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload_node = record.get("payload")
                node: dict[str, Any] = payload_node if isinstance(payload_node, dict) else record
                message_node = record.get("message")
                message: dict[str, Any] = message_node if isinstance(message_node, dict) else node
                kind = str(node.get("type") or record.get("type") or "")
                timestamp = str(record.get("timestamp") or node.get("timestamp") or "")
                metadata_role = node.get("agent_role") or node.get("agent_type")
                if metadata_role:
                    role = agent_role(metadata_role)
                if kind == "turn_context":
                    mode = node.get("collaboration_mode") or {}
                    if isinstance(mode, dict) and mode.get("mode") == "plan":
                        if not role_from_mode:
                            role_before_plan = role
                        role, role_from_mode = "plan", True
                    elif isinstance(mode, dict) and mode.get("mode") == "default" and role_from_mode:
                        role, role_from_mode = role_before_plan, False
                if kind in {"session_meta", "turn_context"}:
                    model = str(node.get("model") or model)
                    instructions = node.get("base_instructions") or node.get("developer_instructions") or ""
                    if isinstance(instructions, dict):
                        instructions = instructions.get("text") or ""
                    matches = list(_ACTIVE.finditer(str(instructions)))
                    if matches:
                        role = agent_role(matches[-1].group(1))
                if kind in {"session_meta", "turn_context"}:
                    workspace = str(node.get("cwd") or workspace)
                    turn_key = str(node.get("turn_id") or turn_key)
                content = message.get("content")
                text = _texts(content)
                message_role = message.get("role") or record.get("type")
                if message_role in {"system", "developer", "user"} or kind == "user_message":
                    matches = list(_ACTIVE.finditer(text or str(node.get("message") or "")))
                    if matches:
                        role = agent_role(matches[-1].group(1))
                    if (message_role == "user" and text) or kind == "user_message":
                        turn_key = str(
                            record.get("uuid")
                            or node.get("id")
                            or timestamp
                            or hashlib.sha256((text or str(node.get("message") or "")).encode()).hexdigest()
                        )
                        last_visible = None
                message_agent_role = role
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                            args = block.get("input") or {}
                            role = agent_role(args.get("skill") if isinstance(args, dict) else "")
                phase = str(
                    node.get("phase")
                    or message.get("phase")
                    or record.get("phase")
                    or message.get("channel")
                    or record.get("channel")
                    or ""
                )
                is_assistant = message_role == "assistant" or kind == "agent_message"
                if kind == "agent_message":
                    text = str(node.get("message") or "")
                has_tools = isinstance(content, list) and any(
                    isinstance(block, dict) and block.get("type") == "tool_use" for block in content
                )
                if kind in {"function_call", "function_call_output", "tool_result"}:
                    last_visible = None
                if (
                    is_assistant
                    and text
                    and phase not in {"analysis", "reasoning", "thinking"}
                    and kind not in {"analysis", "reasoning", "thinking"}
                ):
                    identity = str(
                        message.get("id")
                        or record.get("uuid")
                        or record.get("id")
                        or node.get("id")
                        or hashlib.sha256(f"{turn_key}\0{timestamp}\0{text}".encode()).hexdigest()
                    )
                    origin = str(record.get("type") or "")
                    text_sha = hashlib.sha256(text.encode()).hexdigest()
                    mirrored = (
                        host == "codex"
                        and isinstance(last_visible, dict)
                        and not last_visible.get("mirrored")
                        and {last_visible.get("kind"), origin} == {"response_item", "event_msg"}
                        and last_visible.get("message_sha") == text_sha
                        and last_visible.get("turn_key") == turn_key
                        and last_visible.get("agent_role") == message_agent_role
                        and (not phase or not last_visible.get("phase") or last_visible["phase"] == phase)
                    )
                    if mirrored and last_visible is not None:
                        last_visible["mirrored"] = True
                        if candidates and candidates[-1]["source_id"] == last_visible["source_id"]:
                            candidates[-1]["aliases"].append(identity)
                    else:
                        candidates.append(
                            {
                                "message": text,
                                "source_id": identity,
                                "aliases": [identity],
                                "agent_role": message_agent_role,
                                "model": str(message.get("model") or model),
                                "at": timestamp,
                                "phase": phase,
                                "workspace": workspace,
                            }
                        )
                        last_visible = {
                            "source_id": identity,
                            "kind": origin,
                            "message_sha": text_sha,
                            "turn_key": turn_key,
                            "agent_role": message_agent_role,
                            "phase": phase,
                        }
                if has_tools:
                    last_visible = None
            new_offset = stream.tell()
        # Small transcripts' first 256 bytes may grow; remember the post-read
        # prefix, so subsequent appends do not invalidate an otherwise valid cursor.
        seen = {str((e.get("payload") or {}).get("visible_turn_id") or "") for e in events if isinstance(e, dict)}
        added = 0
        # The terminal-only parser used content hashes when native IDs were
        # absent. Match its retained final once while replaying for this upgrade.
        if checkpoint and checkpoint.get("version") != 2:
            legacy = {
                str(event["payload"].get("message") or ""): str(event["payload"].get("source_id") or "")
                for event in events
                if isinstance(event, dict)
                and isinstance(event.get("payload"), dict)
                and event["payload"].get("event") == "CompletedTurn"
            }
            for candidate in reversed(candidates):
                if old_id := legacy.pop(candidate["message"], ""):
                    candidate["aliases"].append(old_id)
        pending: list[tuple[str, dict[str, Any]]] = []
        for candidate in candidates:
            identities = [
                hashlib.sha256(f"{host}\0{sid}\0{alias}\0{candidate['message']}".encode()).hexdigest()
                for alias in candidate["aliases"]
            ]
            if not any(identity in seen for identity in identities):
                pending.append((identities[0], candidate))
                seen.update(identities)
        grouped: dict[str, list[str]] = {}
        for _, candidate in pending:
            checkout = candidate["workspace"] or str(data.get("workspace_path") or "")
            grouped.setdefault(checkout, []).append(candidate["message"])
        contexts = {checkout: _context(root_path, checkout, messages) for checkout, messages in grouped.items()}
        for identity, candidate in pending:
            now = datetime.now(UTC).isoformat()
            shared = contexts[candidate["workspace"] or str(data.get("workspace_path") or "")]
            references = {
                shared.get("_target_paths", {}).get(finding_location(item)[0], "")
                for item in message_locations(candidate["message"])
            }
            context = {key: value for key, value in shared.items() if key != "_target_paths"}
            context["targets"] = {path: digest for path, digest in shared["targets"].items() if path in references}
            parent = str(
                payload.get("parent_session_id")
                or (payload.get("session_id") if payload.get("agent_id") or payload.get("subagent_id") else "")
                or data.get("parent_session_id")
                or ""
            )
            clean = redact(candidate["message"])
            events.append(
                {
                    "kind": "agent_message",
                    "at": candidate["at"] or now,
                    "summary": f"assistant: {clean[:120]}",
                    "payload": {
                        "role": "assistant",
                        "event": "VisibleOutput",
                        "phase": candidate["phase"],
                        "completed": True,
                        "visible_turn_id": identity,
                        "source_id": candidate["source_id"],
                        "message": clean,
                        "agent_role": candidate["agent_role"],
                        "model": candidate["model"],
                        "parent_session_id": parent,
                        "review_context": context,
                    },
                }
            )
            if parent:
                data["parent_session_id"] = parent
            added += 1
        if added:
            # Pin each output's own interval; never copy session-wide files_touched.
            for index, paths in message_edit_scopes(events).items():
                event_payload = events[index].get("payload") or {}
                review_context = event_payload.get("review_context")
                if (
                    isinstance(review_context, dict)
                    and event_payload.get("event") == "VisibleOutput"
                    and "edited_paths" not in review_context
                ):
                    review_context["edited_paths"] = list(paths)
        data["review_comment_capture"] = {
            "version": 2,
            "turn_key": turn_key,
            "last_visible": last_visible,
            "offset": new_offset,
            "inode": stat.st_ino,
            "header": header,
            "prefix_length": min(stat.st_size, 256),
            "agent_role": role,
            "model": model,
            "role_before_plan": role_before_plan,
            "role_from_mode": role_from_mode,
        }
        data["updated_at"] = datetime.now(UTC).isoformat()
        run_file.parent.mkdir(parents=True, exist_ok=True)
        # A duplicate Stop with no new bytes must perform no write.
        if added or checkpoint != data["review_comment_capture"]:
            atomic_write_json(run_file, data)
        return added


# ReportFindings carries at most 32 findings; these bound a malformed call only.
_MAX_REPORTED_FINDINGS = 64
_MAX_FINDING_TEXT = 8000


_TITLE_LIMIT = 120
_FIRST_SENTENCE = re.compile(r"(.+?[.!?])(?=\s|$)")


def _summary_title(summary: str) -> str:
    """A title for a finding reported without one: its first sentence, else its first words and an ellipsis."""

    line = summary.splitlines()[0].strip()
    sentence = _FIRST_SENTENCE.match(line)
    if sentence and 15 <= len(sentence.group(1)) <= _TITLE_LIMIT:
        return sentence.group(1)
    if len(line) <= _TITLE_LIMIT:
        return line
    cut = line[: _TITLE_LIMIT - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut}…"


def _reported_finding(item: dict[str, Any]) -> dict[str, Any] | None:
    """One host-reported finding in the ingestor's location shape, redacted and bounded."""

    def text(key: str, limit: int = _MAX_FINDING_TEXT) -> str:
        value = item.get(key)
        return redact(value.strip())[:limit] if isinstance(value, str) else ""

    summary = text("summary")
    if not summary:
        return None
    path = text("file", 1024)
    try:
        line = max(0, int(item.get("line") or 0))
    except (ValueError, TypeError):
        line = 0
    scenario = text("failure_scenario")
    labels = [
        f"{name}: {value}"
        for name, value in (
            ("Severity", text("severity", 40)),
            ("Category", text("category", 40)),
            ("Verdict", text("verdict", 40)),
        )
        if value
    ]
    body = "\n\n".join(
        part for part in (summary, f"Failure scenario: {scenario}" if scenario else "", " · ".join(labels)) if part
    )
    return {
        # The same finding re-reported (``--fix`` outcomes) keeps one identity.
        "key": hashlib.sha256(f"{path}\0{line}\0{summary}".encode()).hexdigest()[:32],
        "path": path,
        "line": line,
        "end_line": line,
        "title": text("short_summary", _TITLE_LIMIT) or _summary_title(summary),
        "body": body,
        "severity": text("severity", 40),
        "outcome": text("outcome", 40),
    }


def reported_findings_rows(raw: list[Any]) -> list[dict[str, Any]]:
    """A report's findings as the ledger keeps them, in report order: the order the chat numbers them in."""

    return [
        finding
        for item in raw[:_MAX_REPORTED_FINDINGS]
        if isinstance(item, dict) and (finding := _reported_finding(item)) is not None
    ]


def reported_source_id(payload: dict[str, Any]) -> str:
    """The identity one reported review is deduped by: its tool call id, else its findings' digest."""

    tool_input = payload.get("tool_input")
    raw = tool_input.get("findings") if isinstance(tool_input, dict) else None
    return (
        str(payload.get("tool_use_id") or "")
        or hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
    )


def _reported_message(host: str, findings: list[dict[str, Any]]) -> str:
    outcomes: dict[str, int] = {}
    for finding in findings:
        if finding["outcome"]:
            outcomes[finding["outcome"]] = outcomes.get(finding["outcome"], 0) + 1
    count = f"{len(findings)} finding{'' if len(findings) == 1 else 's'}"
    head = f"{host.capitalize()} code review: {count}" + (
        " (" + ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in sorted(outcomes.items())) + ")"
        if outcomes
        else ""
    )
    rows = [
        "- "
        + " · ".join(
            part
            for part in (
                finding["severity"],
                f"{finding['path']} line {finding['line']}" if finding["line"] else finding["path"],
                finding["title"],
                finding["outcome"].replace("_", " "),
            )
            if part
        )
        for finding in findings
    ]
    return "\n".join([head, *(["", *rows] if rows else [])])


def capture_reported_findings(
    root: str | Path, payload: dict[str, Any], *, host: str, review_id: str, revision_id: str
) -> int:
    """Retain a host's reported review (Claude ``ReportFindings``) bound to the Snapshot it reviewed.

    The capture already took that Snapshot, so the event names it: an unchanged
    tree reuses an older Snapshot, which the Stop rule (first Snapshot at or after
    capture) would never select. Replayed tool calls are deduped by id.
    """

    sid = str(payload.get("session_id") or "").strip()
    tool_input = payload.get("tool_input")
    raw = tool_input.get("findings") if isinstance(tool_input, dict) else None
    if not sid or not isinstance(raw, list):
        return 0
    findings = reported_findings_rows(raw)
    source_id = reported_source_id(payload)
    identity = hashlib.sha256(f"{host}\0{sid}\0reported\0{source_id}".encode()).hexdigest()
    root_path = Path(root).expanduser().resolve()
    run_file = session_dir(root_path, host, sid) / "run.json"
    workspace = str(payload.get("cwd") or "")
    with RunFileLock(run_file):
        if run_file.is_file():
            data = json.loads(run_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return 0
        else:
            data = {
                "session_id": sid,
                "agent": host,
                "workspace_path": workspace,
                "created_at": datetime.now(UTC).isoformat(),
                "events": [],
                "files_touched": [],
            }
        events = data.setdefault("events", [])
        if not isinstance(events, list):
            return 0
        if any(
            isinstance(e, dict)
            and isinstance(e.get("payload"), dict)
            and e["payload"].get("visible_turn_id") == identity
            for e in events
        ):
            return 0
        shared = _context(root_path, workspace, [json.dumps({"findings": findings})])
        context = {key: value for key, value in shared.items() if key != "_target_paths"}
        # The report names its own files; it never borrows this turn's edits.
        context.update(review_id=review_id, revision_id=revision_id, edited_paths=[])
        message = _reported_message(host, findings)
        events.append(
            {
                "kind": "agent_message",
                "at": context["captured_at"],
                "summary": f"assistant: {message[:120]}",
                "payload": {
                    "role": "assistant",
                    "event": "ReportedFindings",
                    "completed": True,
                    "visible_turn_id": identity,
                    "source_id": source_id,
                    "message": message,
                    "agent_role": "review",
                    "model": str(payload.get("model") or ""),
                    "parent_session_id": "",
                    "findings": findings,
                    "review_context": context,
                },
            }
        )
        data["updated_at"] = datetime.now(UTC).isoformat()
        run_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(run_file, data)
    return 1
