from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from lemoncrow.gateway.cli.commands._shared import _load_store
from lemoncrow.gateway.hosts.session_parsers.registry import SUPPORTED_SESSION_IMPORT_HOSTS

logger = logging.getLogger(__name__)

_IMPORT_PROGRESS_LOGGER = "lemoncrow.gateway.hosts.session_parsers"
_IMPORT_PROGRESS_HANDLER_FLAG = "_lemoncrow_import_progress_handler"


def _ensure_import_progress_logging() -> None:
    """Route session-parser import progress to stderr (never stdout).

    Parser progress is emitted via ``logger.info(...)`` on the
    ``lemoncrow.gateway.hosts.session_parsers`` namespace. The CLI's root logger
    defaults to WARNING with no handler, so without this those records would
    vanish. Attach a single INFO-level stderr StreamHandler exactly once
    (idempotent across repeat import invocations). This is intentionally
    minimal — not a logging reconfiguration and not CLI decomposition.
    """
    progress_logger = logging.getLogger(_IMPORT_PROGRESS_LOGGER)
    for handler in progress_logger.handlers:
        if getattr(handler, _IMPORT_PROGRESS_HANDLER_FLAG, False):
            if isinstance(handler, logging.StreamHandler):
                handler.setStream(sys.stderr)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _IMPORT_PROGRESS_HANDLER_FLAG, True)
    progress_logger.addHandler(handler)
    progress_logger.propagate = False
    if progress_logger.level == logging.NOTSET or progress_logger.level > logging.INFO:
        progress_logger.setLevel(logging.INFO)


@click.group()
def copilot() -> None:
    """Copilot session-state integration (~/.copilot/session-state/)."""


@copilot.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root (default: ~/.copilot/session-state).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def copilot_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Copilot sessions into the LemonCrow store (loss-preserving)."""
    from lemoncrow.gateway.hosts.session_parsers.copilot import CopilotImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = CopilotImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} copilot sessions")


@click.group()
def claude() -> None:
    """Claude Code session integration (~/.claude/projects/)."""


@claude.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root (default: ~/.claude/projects/).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def claude_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Claude Code sessions into the LemonCrow store (loss-preserving)."""
    from lemoncrow.gateway.hosts.session_parsers.claude import ClaudeImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = ClaudeImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} claude sessions")


@click.group()
def codex() -> None:
    """Codex session integration (~/.codex/sessions/)."""


@codex.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root (default: ~/.codex/sessions/).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def codex_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Codex sessions into the LemonCrow store (loss-preserving)."""
    from lemoncrow.gateway.hosts.session_parsers.codex import CodexImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = CodexImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} codex sessions")


@click.group()
def opencode() -> None:
    """OpenCode session integration (~/.local/share/opencode/opencode.db)."""


@opencode.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override DB path (default: ~/.local/share/opencode/opencode.db/).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def opencode_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import OpenCode sessions into the LemonCrow store (loss-preserving)."""
    from lemoncrow.gateway.hosts.session_parsers.opencode import OpenCodeImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = OpenCodeImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} opencode sessions")


@click.command("import")
@click.option(
    "--host",
    type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)),
    default=None,
    help="Import from only one specific host.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the selected host's sessions root. Requires --host.",
)
@click.option(
    "--export-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Export reconstructed session logs (JSONL) to this directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Output per-host counts as JSON (machine-readable).")
@click.pass_context
def global_import(
    ctx: click.Context, host: str | None, force: bool, path: Path | None, export_dir: Path | None, as_json: bool
) -> None:
    """Unified import for ALL agent sessions (Claude, Codex, etc.)."""
    from lemoncrow.gateway.cli.commands._shared import _emit
    from lemoncrow.gateway.hosts.session_parsers._session_parser import parse_session_turns
    from lemoncrow.gateway.hosts.session_parsers.registry import iter_importer_classes

    if path is not None and host is None:
        raise click.ClickException("--path requires --host")

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    store.init()

    if export_dir:
        export_dir.mkdir(parents=True, exist_ok=True)
        click.echo(f"exporting reconstructed sessions to {export_dir}")

    hosts = iter_importer_classes()

    total = 0
    reconstructable = 0
    all_imported_ids = []
    per_host_counts: dict[str, int] = {}

    with store.history.batch_mode():
        for name, importer_cls in hosts:
            if host and name != host:
                continue

            try:
                importer = importer_cls(store)
                ids = importer.import_all(path, force=force) if path is not None else importer.import_all(force=force)
                count = len(ids)
                per_host_counts[name] = count
                total += count
                all_imported_ids.extend(ids)

                for tid in ids:
                    trace = store.history.get_trace(tid)
                    if trace and trace.raw_artifact_ids:
                        art_id = trace.raw_artifact_ids[0]
                        artifact = store.history.get_raw_artifact(art_id)
                        if artifact:
                            try:
                                content = store.history.read_raw_artifact_content(artifact)
                                turns = parse_session_turns(content, name)
                                if turns:
                                    reconstructable += 1
                                    if export_dir:
                                        safe_tid = tid.replace("/", "_").replace("\\", "_")
                                        export_file = export_dir / f"{name}-{safe_tid}.jsonl"
                                        export_file.write_text(content)
                            except Exception:
                                logging.exception("global import reconstruction audit failed")
                                logger.warning(
                                    "Suppressed exception at cli.py:1812",
                                    exc_info=True,
                                )

            except Exception as e:
                logging.exception("global importer failed for host %s", name)
                click.secho(f"FATAL: {name} importer raised: {e!r}", fg="red", err=True)

    try:
        from lemoncrow.core.service.sync import sync_usage

        sync_usage(ctx.obj["root"], session_ids=all_imported_ids)
    except Exception:
        logging.exception("sync_usage failed after global import")
        logger.warning(
            "Suppressed exception at cli.py:1827",
            exc_info=True,
        )

    if as_json:
        _emit(per_host_counts, as_json=True)
        return

    click.echo(f"imported {total} sessions")
    if total > 0:
        pct = (reconstructable / total) * 100
        click.echo(f"\nAudit: {reconstructable}/{total} sessions ({pct:.1f}%) 100% reconstructable.")


__all__ = [
    "_IMPORT_PROGRESS_HANDLER_FLAG",
    "_IMPORT_PROGRESS_LOGGER",
    "_ensure_import_progress_logging",
    "claude",
    "codex",
    "copilot",
    "global_import",
    "opencode",
]
