"""All-sessions Recall — index past Claude, Codex, and OpenCode transcripts for semantic recall.

Indexes turns from every past session into the archival vector
store, then semantic-searches across ALL sessions (not just the current one).
Reuses LemonCrow's embedder + archival store via ``ArchivalRecallCapability`` and
indexes incrementally (sessions unchanged since the last run are skipped).
Improvement over a naive re-index: per-session mtime state + bounded caps so a
background run stays cheap.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_AGENT_ID = "session-recall"
_TAG = "session-recall"
# "agent:any" tags each indexed passage so it can be recalled under ANY agent_id
# *within the recall store* (see SqliteMemoryStore.list_passages). Cross-store
# visibility is handled by mcp_server._memory_recall, which reads this store
# (recall.db) in addition to memory.db, so past-session context surfaces through
# the memory(op=recall) tool, not just the `lc recall` CLI.
_SHARED_TAG = "agent:any"
_MAX_SESSIONS = 80
_MAX_SNIPPETS_PER_SESSION = 40
_MAX_SNIPPET_CHARS = 1500
_MIN_SNIPPET_CHARS = 16


def recall_dir(root: str | Path) -> Path:
    return Path(root) / "recall"


def _state_path(root: str | Path) -> Path:
    return recall_dir(root) / "index_state.json"


def _load_state(root: str | Path) -> dict[str, float]:
    try:
        data = json.loads(_state_path(root).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _save_state(root: str | Path, state: dict[str, float]) -> None:
    try:
        recall_dir(root).mkdir(parents=True, exist_ok=True)
        _state_path(root).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _iter_and_close(handle: Any) -> Iterator[str]:
    """Yield lines from *handle*, closing it when iteration stops (including on an
    early ``break``), so a large transcript streams line-by-line rather than being
    read whole into memory."""
    try:
        yield from handle
    finally:
        handle.close()


def _session_snippets(path: str | Path) -> list[str]:
    """Extract user/assistant text snippets from a transcript JSONL."""
    try:
        handle = Path(path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for line in _iter_and_close(handle):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        snippet = ""
        if isinstance(content, str):
            snippet = content
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            snippet = "\n".join(part for part in parts if part)
        snippet = snippet.strip()
        if len(snippet) >= _MIN_SNIPPET_CHARS:
            out.append(f"[{role}] {snippet[:_MAX_SNIPPET_CHARS]}")
        if len(out) >= _MAX_SNIPPETS_PER_SESSION:
            break
    return out


def _recall_embedder_choice(root: str | Path) -> tuple[str, str]:
    """Resolve (embedder, model) for recall: env overrides plugin_settings.json."""
    choice = (os.environ.get("LEMONCROW_RECALL_EMBEDDER") or "").strip().lower()
    model = (os.environ.get("LEMONCROW_RECALL_EMBED_MODEL") or "").strip()
    if not choice or not model:
        try:
            data = json.loads((Path(root) / "plugin_settings.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            choice = choice or str(data.get("recallEmbedder", "") or "").strip().lower()
            model = model or str(data.get("recallEmbedModel", "") or "").strip()
    return choice, model


def _make_recall_embedder(root: str | Path) -> Any:
    """Build the embedder for recall. Claude has no embeddings API, so it is not an
    option: codex maps to OpenAI, ollama runs locally, everything else falls back
    to FTS-only (the null embedder)."""
    from lemoncrow.infra.embeddings.factory import make_code_embedder, make_embedder

    choice, model = _recall_embedder_choice(root)
    if choice == "ollama":
        return make_code_embedder(pin="ollama", model=model or None)
    if choice in ("openai", "codex"):
        return make_embedder("openai")
    return make_embedder()


def _capability(root: str | Path) -> Any:
    # Recall indexes thousands of transcript passages; route them to a dedicated
    # global recall.db so the bulk writes never contend with the main lemoncrow.db.
    from lemoncrow.core.capabilities.archival_recall import ArchivalRecallCapability
    from lemoncrow.core.foundation.redaction import redact
    from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore

    store = SqliteMemoryStore(Path(root), db_name="recall.db")
    return ArchivalRecallCapability(store, _make_recall_embedder(root), redactor=redact)


_PROSE_KINDS = frozenset({"user_message", "agent_message"})


@dataclass(frozen=True)
class _Candidate:
    """A session eligible for recall indexing, normalized across hosts."""

    session_id: str
    change_key: float
    host: str
    project: str
    load: Callable[[], list[str]]


def _read_text(path: Path) -> str:
    try:
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return ""


def _snippets_from_turns(turns: list[dict[str, Any]]) -> list[str]:
    """Extract user/assistant prose snippets from normalized parser turns.

    Used for Codex and OpenCode, whose transcript formats differ from Claude's;
    the shared session parsers normalize both to ``user_message``/``agent_message``
    turns whose ``content`` holds the prose.
    """
    out: list[str] = []
    for turn in turns:
        kind = turn.get("kind")
        if kind not in _PROSE_KINDS:
            continue
        text = str(turn.get("content") or "").strip()
        if len(text) < _MIN_SNIPPET_CHARS:
            continue
        role = "user" if kind == "user_message" else "assistant"
        out.append(f"[{role}] {text[:_MAX_SNIPPET_CHARS]}")
        if len(out) >= _MAX_SNIPPETS_PER_SESSION:
            break
    return out


def _load_codex(path: Path) -> list[str]:
    from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns

    return _snippets_from_turns(parse_session_turns(_read_text(path), "codex"))


def _load_opencode(session_id: str, db_path: Path) -> list[str]:
    from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns
    from lemoncrow.gateway.hosts.session_parsers.opencode import serialize_opencode_session

    return _snippets_from_turns(parse_session_turns(serialize_opencode_session(session_id, db_path), "opencode"))


def _claude_transcript_paths_in_window(window_days: int) -> list[Path]:
    """Claude main-session transcript JSONLs modified within *window_days*.

    Main sessions only (``<project>/<uuid>.jsonl``); subagent sidechains are
    keyed by their parent session and skipped here.
    """
    claude_root = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME") or ""
    projects = Path(claude_root) / "projects" if claude_root else Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return []
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).timestamp()
    out: list[Path] = []
    try:
        for path in projects.glob("*/*.jsonl"):
            if "subagents" in path.parts:
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    out.append(path)
            except OSError:
                continue
    except OSError:
        return []
    return out


def _claude_candidates(window_days: int) -> list[_Candidate]:
    out: list[_Candidate] = []
    for path in _claude_transcript_paths_in_window(window_days):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append(
            _Candidate(
                session_id=path.stem,
                change_key=mtime,
                host="claude",
                project=path.parent.name,
                load=partial(_session_snippets, path),
            )
        )
    return out


def _codex_candidates(cutoff: float) -> list[_Candidate]:
    try:
        from lemoncrow.gateway.hosts.session_parsers.codex import find_codex_sessions
    except ImportError:
        return []
    out: list[_Candidate] = []
    for path in find_codex_sessions():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        out.append(
            _Candidate(
                session_id=path.stem,
                change_key=mtime,
                host="codex",
                project="codex",
                load=partial(_load_codex, path),
            )
        )
    return out


def _opencode_candidates(cutoff: float) -> list[_Candidate]:
    try:
        from lemoncrow.gateway.hosts.session_parsers.opencode import find_opencode_sessions
    except ImportError:
        return []
    db_path = Path.home() / ".local/share/opencode/opencode.db"
    out: list[_Candidate] = []
    for row in find_opencode_sessions(db_path):
        session_id = str(row.get("id") or "").strip()
        if not session_id:
            continue
        raw_time = row.get("time_updated") or row.get("time_created")
        if raw_time is None:
            continue
        try:
            change_key = float(raw_time)  # ms epoch
        except (TypeError, ValueError):
            continue
        if change_key / 1000.0 < cutoff:
            continue
        out.append(
            _Candidate(
                session_id=session_id,
                change_key=change_key,
                host="opencode",
                project="opencode",
                load=partial(_load_opencode, session_id, db_path),
            )
        )
    return out


def _load_copilot(path: Path) -> list[str]:
    from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns

    return _snippets_from_turns(parse_session_turns(_read_text(path), "copilot"))


def _copilot_candidates(cutoff: float) -> list[_Candidate]:
    try:
        from lemoncrow.gateway.hosts.session_parsers.copilot import find_copilot_sessions
    except ImportError:
        return []
    out: list[_Candidate] = []
    for session_dir in find_copilot_sessions():
        events = session_dir / "events.jsonl"
        try:
            mtime = events.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        out.append(
            _Candidate(
                session_id=session_dir.name,
                change_key=mtime,
                host="copilot",
                project="copilot",
                load=partial(_load_copilot, events),
            )
        )
    return out


def _load_cursor(session_id: str, db_path: Path) -> list[str]:
    """Prose snippets for one Cursor composer session, read from state.vscdb.

    Reuses the importer's text extraction (plain ``text`` first, then the
    ``richText`` tree) so recall sees the same prose the imported Trace does.
    """
    import sqlite3

    from lemoncrow.gateway.hosts.session_parsers.cursor import _extract_row_text

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT json_extract(value, '$.type'), "
                "json_extract(value, '$.text'), "
                "json_extract(value, '$.richText') "
                "FROM cursorDiskKV WHERE key LIKE ? "
                "AND ROWID IN (SELECT MAX(ROWID) FROM cursorDiskKV WHERE key LIKE ? GROUP BY key) "
                "ORDER BY ROWID ASC",
                (f"bubbleId:{session_id}:%", f"bubbleId:{session_id}:%"),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: list[str] = []
    for bubble_type, text, rich_text in rows:
        prose = _extract_row_text(text, rich_text)
        if len(prose) < _MIN_SNIPPET_CHARS:
            continue
        role = "user" if int(bubble_type or 0) == 1 else "assistant"
        out.append(f"[{role}] {prose[:_MAX_SNIPPET_CHARS]}")
        if len(out) >= _MAX_SNIPPETS_PER_SESSION:
            break
    return out


def _cursor_candidates(cutoff: float) -> list[_Candidate]:
    try:
        from lemoncrow.gateway.hosts.session_parsers.cursor import find_cursor_db
    except ImportError:
        return []
    import sqlite3

    db_path = find_cursor_db()
    if db_path is None:
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            # Newest createdAt per composer; createdAt is an ISO-8601 string
            # on every bubble the importer reads (see cursor.py import_all).
            rows = conn.execute(
                "SELECT substr(key, 10, instr(substr(key, 10), ':') - 1), "
                "MAX(json_extract(value, '$.createdAt')) "
                "FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' "
                "GROUP BY 1"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: list[_Candidate] = []
    for composer_id, created_at in rows:
        composer_id = str(composer_id or "").strip()
        if not composer_id or not created_at:
            continue
        # Strict parse: an unparseable createdAt must skip the session, not
        # default to "now" (which would always pass the cutoff).
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        change_key = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
        if change_key < cutoff:
            continue
        out.append(
            _Candidate(
                session_id=composer_id,
                change_key=change_key,
                host="cursor",
                project="cursor",
                load=partial(_load_cursor, composer_id, db_path),
            )
        )
    return out


def _load_hermes(session_row: dict[str, Any], db_path: Path) -> list[str]:
    from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns
    from lemoncrow.gateway.hosts.session_parsers.hermes import serialize_hermes_session

    return _snippets_from_turns(parse_session_turns(serialize_hermes_session(session_row, db_path), "hermes"))


def _hermes_candidates(cutoff: float) -> list[_Candidate]:
    try:
        from lemoncrow.gateway.hosts.session_parsers.hermes import find_hermes_db, find_hermes_sessions
    except ImportError:
        return []
    db_path = find_hermes_db()
    if db_path is None:
        return []
    out: list[_Candidate] = []
    for row in find_hermes_sessions(db_path):
        session_id = str(row.get("id") or "").strip()
        if not session_id:
            continue
        try:
            change_key = float(row.get("last_active") or row.get("started_at") or 0)
        except (TypeError, ValueError):
            continue
        if change_key < cutoff:
            continue
        out.append(
            _Candidate(
                session_id=session_id,
                change_key=change_key,
                host="hermes",
                project="hermes",
                load=partial(_load_hermes, row, db_path),
            )
        )
    return out


def _discover_candidates(window_days: int) -> list[_Candidate]:
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).timestamp()
    candidates: list[_Candidate] = []
    # Each host's discovery is isolated: one failing (missing dir, unreadable db)
    # must not block the others.
    try:
        candidates.extend(_claude_candidates(window_days))
    except Exception:  # noqa: BLE001
        _log.warning("recall discovery failed for host claude", exc_info=True)
    try:
        candidates.extend(_codex_candidates(cutoff))
    except Exception:  # noqa: BLE001
        _log.warning("recall discovery failed for host codex", exc_info=True)
    try:
        candidates.extend(_opencode_candidates(cutoff))
    except Exception:  # noqa: BLE001
        _log.warning("recall discovery failed for host opencode", exc_info=True)
    try:
        candidates.extend(_copilot_candidates(cutoff))
    except Exception:  # noqa: BLE001
        _log.warning("recall discovery failed for host copilot", exc_info=True)
    try:
        candidates.extend(_cursor_candidates(cutoff))
    except Exception:  # noqa: BLE001
        _log.warning("recall discovery failed for host cursor", exc_info=True)
    try:
        candidates.extend(_hermes_candidates(cutoff))
    except Exception:  # noqa: BLE001
        _log.warning("recall discovery failed for host hermes", exc_info=True)
    return candidates


def index_sessions(
    root: str | Path,
    *,
    window_days: int = 30,
    max_sessions: int = _MAX_SESSIONS,
    paths: list[Path] | None = None,
    capability: Any | None = None,
) -> dict[str, Any]:
    """Incrementally index recent session transcripts into the recall store.

    Covers Claude, Codex, OpenCode, Copilot, and Cursor. When *paths* is given
    they are treated as Claude transcript files (tests / manual runs); otherwise
    sessions are discovered across all hosts. The bounded ``max_sessions`` budget is
    spent newest-first across hosts, after dropping sessions already current in
    the index (so a backlog never starves never-indexed sessions).
    """
    cap = capability or _capability(root)
    if paths is None:
        candidates = _discover_candidates(window_days)
    else:
        candidates = []
        for path in paths:
            file_path = Path(path)
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                continue
            candidates.append(
                _Candidate(
                    session_id=file_path.stem,
                    change_key=mtime,
                    host="claude",
                    project=file_path.parent.name,
                    load=partial(_session_snippets, file_path),
                )
            )
    state = _load_state(root)
    indexed = 0
    sessions = 0
    pending = [c for c in candidates if state.get(c.session_id) != c.change_key]
    skipped = len(candidates) - len(pending)
    pending.sort(key=lambda c: c.change_key, reverse=True)
    for candidate in pending[:max_sessions]:
        snippets = candidate.load()
        if not snippets:
            state[candidate.session_id] = candidate.change_key
            continue
        tags = [_TAG, _SHARED_TAG, f"project:{candidate.project}", f"host:{candidate.host}"]
        for snippet in snippets:
            cap.archive(
                text=snippet,
                source="trace",
                agent_id=_AGENT_ID,
                source_ref=candidate.session_id,
                tags=tags,
            )
            indexed += 1
        state[candidate.session_id] = candidate.change_key
        sessions += 1
    _save_state(root, state)
    return {"indexed": indexed, "sessions": sessions, "skipped": skipped}


def recall(
    root: str | Path,
    query: str,
    *,
    top_k: int = 10,
    capability: Any | None = None,
) -> list[dict[str, Any]]:
    """Semantic search across all indexed past sessions."""
    cap = capability or _capability(root)
    try:
        passages, _ = cap.recall(agent_id=_AGENT_ID, query=query, top_k=top_k, tags=[_TAG])
    except Exception:  # noqa: BLE001 - recall is best-effort
        # Log so a backend failure is distinguishable from a genuine no-match.
        _log.warning("session recall failed for query %r; returning no matches", query, exc_info=True)
        return []
    return [
        {
            "text": passage.text,
            "session": passage.source_ref,
            "tags": list(passage.tags),
            "created_at": passage.created_at.isoformat(),
        }
        for passage in passages
    ]


def _main(argv: list[str] | None = None) -> int:
    """Detach target for the SessionStart background indexer."""
    import argparse

    parser = argparse.ArgumentParser(prog="session_recall")
    parser.add_argument("--root", required=True)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--max-sessions", type=int, default=_MAX_SESSIONS)
    namespace = parser.parse_args(argv)
    try:
        index_sessions(
            namespace.root,
            window_days=namespace.window_days,
            max_sessions=namespace.max_sessions,
        )
    except Exception:  # noqa: BLE001 - background indexing is best-effort
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    import sys

    sys.exit(_main())
