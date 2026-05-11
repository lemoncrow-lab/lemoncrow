"""Thin wrapper that runs every host's session importer.

Each host's importer (Claude, Codex, Copilot, OpenCode, Gemini) is now the
single source of truth for token metrics, including:
  - input_tokens / output_tokens / thinking_tokens
  - cached_input_tokens   (cache-read; discounted billing rate)
  - cache_creation_input_tokens (Anthropic cache-write; premium rate)
  - model                 (model name used by the session)
  - per-tool input_tokens / output_tokens

This script no longer post-processes traces — it just runs the importers and
reports any failures to stderr instead of swallowing them.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from atelier.core.foundation.store import ReasoningStore
from atelier.gateway.hosts.session_parsers.claude import ClaudeImporter
from atelier.gateway.hosts.session_parsers.cline import ClineImporter
from atelier.gateway.hosts.session_parsers.codex import CodexImporter
from atelier.gateway.hosts.session_parsers.copilot import CopilotImporter
from atelier.gateway.hosts.session_parsers.gemini import GeminiImporter
from atelier.gateway.hosts.session_parsers.opencode import OpenCodeImporter

_HOSTS = (
    ("claude", ClaudeImporter),
    ("cline", ClineImporter),
    ("codex", CodexImporter),
    ("copilot", CopilotImporter),
    ("opencode", OpenCodeImporter),
    ("gemini", GeminiImporter),
)


def import_all(
    force: bool = False,
    target_host: str | None = None,
    target_session_id: str | None = None,
    export_dir: Path | None = None,
) -> int:
    """Run every host importer once. Returns the count of imported sessions."""
    store_root = Path("~/.atelier").expanduser()
    store = ReasoningStore(store_root)
    store.init()

    if export_dir:
        export_dir.mkdir(parents=True, exist_ok=True)
        print(f"[atelier] exporting reconstructed sessions to {export_dir}")

    # One-time cleanup of legacy companion tables that were superseded by the
    # token fields now living on Trace itself.
    try:
        with sqlite3.connect(store.db_path) as cleanup_conn:
            cleanup_conn.execute("DROP TABLE IF EXISTS tool_usage_granular")
            cleanup_conn.execute("DROP TABLE IF EXISTS tool_usage_granular_staging")
    except sqlite3.Error as e:
        print(f"[atelier] cleanup of legacy tables failed: {e}", file=sys.stderr)

    total = 0
    reconstructable = 0
    all_imported_ids = []

    from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns

    with store.batch_mode():
        for name, importer_cls in _HOSTS:
            if target_host and name != target_host:
                continue

            try:
                importer = importer_cls(store)
                if target_session_id:
                    # Resolve ID to path for importers that expect Path
                    path = None
                    if name == "copilot":
                        path = Path("~/.copilot/session-state").expanduser() / target_session_id
                        if not path.exists():
                            # check transcripts?
                            pass
                    elif name == "cline":
                        path = (
                            Path("~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks").expanduser()
                            / target_session_id
                        )
                    elif name == "gemini":
                        # Gemini sessions are often deeper, but let's try a direct look
                        path = (
                            Path("~/.gemini/tmp").expanduser() / "atelier/chats" / f"session-{target_session_id}.jsonl"
                        )

                    if hasattr(importer, "import_session"):
                        sid = importer.import_session(path or target_session_id, force=force)
                        ids = [sid] if sid else []
                    elif name == "cline" and hasattr(importer, "import_task"):
                        # Cline uses import_task
                        sid = importer.import_task(path or target_session_id, {}, force=force)
                        ids = [sid] if sid else []
                    else:
                        print(f"[atelier] {name}: importer does not support single session import", file=sys.stderr)
                        continue
                else:
                    ids = importer.import_all(force=force)

                count = len(ids)
                total += count
                all_imported_ids.extend(ids)

                # Verify reconstruction for a sample or all new sessions
                for tid in ids:
                    trace = store.get_trace(tid)
                    if trace and trace.raw_artifact_ids:
                        art_id = trace.raw_artifact_ids[0]
                        artifact = store.get_raw_artifact(art_id)
                        if artifact:
                            try:
                                content = store.read_raw_artifact_content(artifact)
                                turns = parse_session_turns(content, name)
                                if turns:
                                    reconstructable += 1

                                    if export_dir:
                                        # Export the STANDARDIZED log (the RawArtifact content)
                                        # This is the "True Check" - it's reconstructed from the
                                        # source (DB/File) into our high-fidelity JSONL format.
                                        safe_tid = tid.replace("/", "_").replace("\\", "_")
                                        ext = "jsonl"
                                        export_file = export_dir / f"{name}-{safe_tid}.{ext}"
                                        export_file.write_text(content)
                                else:
                                    print(f"[atelier] WARNING: {tid} reconstructed 0 turns", file=sys.stderr)
                            except Exception as e:
                                print(f"[atelier] ERROR: Failed to reconstruct {tid}: {e}", file=sys.stderr)

                print(f"[atelier] {name}: imported {count} new sessions")
            except Exception as e:
                # Surface failures loudly instead of silently dropping them.
                print(f"[atelier] FATAL: {name} importer raised: {e!r}", file=sys.stderr)
                import traceback

                traceback.print_exc(file=sys.stderr)

    if total > 0:
        pct = (reconstructable / total) * 100
        print(f"\n[atelier] Audit: {reconstructable}/{total} sessions ({pct:.1f}%) 100% reconstructable.")

    # Report aggregated session counts to atelier.beseam.com
    try:
        from atelier.core.service.sync import sync_usage

        sync_usage(store_root, session_ids=all_imported_ids)
    except Exception as e:
        print(f"[atelier] sync to atelier.beseam.com failed: {e}", file=sys.stderr)

    return total


if __name__ == "__main__":
    force = "--force" in sys.argv
    target_host = None
    target_session_id = None
    export_dir = None

    for arg in sys.argv:
        if arg.startswith("--host="):
            target_host = arg.split("=")[1]
        if arg.startswith("--session-id="):
            target_session_id = arg.split("=")[1]
        if arg.startswith("--export-dir="):
            export_dir = Path(arg.split("=")[1])

    import_all(
        force=force,
        target_host=target_host,
        target_session_id=target_session_id,
        export_dir=export_dir,
    )
