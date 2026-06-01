"""CLI for the Atelier reasoning runtime.

Designed to be readable when piped into another tool. All commands that
return data accept ``--json`` to emit machine-parseable output.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import click
import yaml

from atelier import __version__ as atelier_version
from atelier.bench import bootstrap as _bench_bootstrap
from atelier.core.capabilities.reporting.dashboard import _render_dashboard
from atelier.core.foundation.models import (
    ReasonBlock,
    Rubric,
    to_jsonable,
)
from atelier.core.foundation.paths import default_store_root
from atelier.core.foundation.renderer import (
    render_block_markdown,
)
from atelier.core.foundation.store import ContextStore

# Relocated CLI glue / dev-gating primitives (Phase 25 commands substrate).
# These live in cli/commands/* as a downward import target so future command
# modules avoid the app.py <-> commands/* circular-import risk. Re-imported here
# so every existing call site in app.py is unchanged.
from atelier.gateway.cli.commands import register as _register_command_modules
from atelier.gateway.cli.commands._dev import (
    MCP_TOOL_ONLY_COMMANDS,
    MCP_TOOL_ONLY_GROUPS,
    _check_dev_mode,
    _DummyGroup,
)
from atelier.gateway.cli.commands._shared import (
    _core_runtime,
    _emit,
    _load_store,
)
from atelier.gateway.hosts.session_parsers.registry import SUPPORTED_SESSION_IMPORT_HOSTS
from atelier.gateway.integrations.openmemory_lifecycle import (
    project_root as _project_root,
)

logger = logging.getLogger(__name__)

# Namespace covering every session-parser module logger (e.g.
# ``atelier.gateway.hosts.session_parsers.claude``). Progress records emitted
# by the parsers propagate up to this logger.
_IMPORT_PROGRESS_LOGGER = "atelier.gateway.hosts.session_parsers"
_IMPORT_PROGRESS_HANDLER_FLAG = "_atelier_import_progress_handler"


def _ensure_import_progress_logging() -> None:
    """Route session-parser import progress to stderr (never stdout).

    Parser progress is emitted via ``logger.info(...)`` on the
    ``atelier.gateway.hosts.session_parsers`` namespace. The CLI's root logger
    defaults to WARNING with no handler, so without this those records would
    vanish. Attach a single INFO-level stderr StreamHandler exactly once
    (idempotent across repeat import invocations). This is intentionally
    minimal — not a logging reconfiguration and not CLI decomposition.
    """
    progress_logger = logging.getLogger(_IMPORT_PROGRESS_LOGGER)
    for handler in progress_logger.handlers:
        if getattr(handler, _IMPORT_PROGRESS_HANDLER_FLAG, False):
            # Already attached; refresh the target stream (the active stderr may
            # differ between invocations, e.g. under test capture) without
            # adding a duplicate handler.
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


DEFAULT_ROOT = default_store_root()


# --------------------------------------------------------------------------- #
# Product telemetry helpers                                                   #
# --------------------------------------------------------------------------- #


def _atelier_version() -> str:
    try:
        return version("atelier")
    except PackageNotFoundError:
        return "0.1.0"


def _cli_command_name(argv: list[str]) -> str:
    skip_next = False
    options_with_values = {"--root"}
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in options_with_values:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token.replace("-", "_")
    return "root"


def _telemetry_session(ctx: click.Context) -> str | None:
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    value = obj.get("_telemetry_session_id")
    return value if isinstance(value, str) else None


def _begin_cli_telemetry(command_name: str) -> tuple[str, float]:
    from atelier.bench.mode import mode as _bench_mode
    from atelier.core.foundation.identity import (
        get_anon_id,
        new_session_id,
        platform_payload,
    )
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.banner import maybe_show_banner

    maybe_show_banner()
    # OTel is initialized lazily on first emit_product_log call.
    session_id = new_session_id()
    payload = platform_payload()
    emit_product(
        "session_start",
        agent_host="cli",
        atelier_version=_atelier_version(),
        anon_id=get_anon_id(),
        session_id=session_id,
        bench_mode=_bench_mode().value,
        **payload,
    )
    emit_product(
        "cli_command_invoked",
        command_name=command_name,
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    return session_id, time.perf_counter()


def _finish_cli_telemetry(
    *,
    command_name: str,
    session_id: str,
    started_at: float,
    ok: bool,
    exit_reason: str,
) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import bucket_duration_ms, bucket_duration_s

    elapsed = max(0.0, time.perf_counter() - started_at)
    emit_product(
        "cli_command_completed",
        command_name=command_name,
        session_id=session_id,
        duration_ms_bucket=bucket_duration_ms(elapsed * 1000),
        ok=ok,
    )
    emit_product(
        "session_end",
        session_id=session_id,
        duration_s_bucket=bucket_duration_s(elapsed),
        exit_reason=exit_reason,
    )


def _emit_cli_interrupted(
    *,
    session_id: str,
    started_at: float,
    signum: int,
    command_name: str,
) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import bucket_duration_s

    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    emit_product(
        "session_interrupted",
        session_id=session_id,
        signal=signal_name,
        elapsed_s_bucket=bucket_duration_s(max(0.0, time.perf_counter() - started_at)),
        last_phase=command_name,
    )


def _record_reasonblock_events(
    scored: list[Any],
    *,
    event_name: str,
    domain: str | None,
    session_id: str | None,
) -> None:
    if session_id is None:
        return
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import hash_identifier

    for rank, item in enumerate(scored, start=1):
        block = getattr(item, "block", None)
        block_id = getattr(block, "id", "")
        block_domain = getattr(block, "domain", domain or "")
        props: dict[str, Any] = {
            "block_id_hash": hash_identifier(str(block_id)),
            "domain": str(block_domain or domain or ""),
            "retrieval_score": float(getattr(item, "score", 0.0)),
            "session_id": session_id,
        }
        if event_name == "reasonblock_retrieved":
            props["rank"] = rank
        emit_product(event_name, **props)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _lesson_promoter(root: Path) -> Any:
    from atelier.core.capabilities.lesson_promotion import LessonPromoterCapability

    store = _load_store(root)
    return LessonPromoterCapability(store)


def _lesson_pr_bot(root: Path) -> Any:
    from atelier.core.capabilities.lesson_promotion import LessonPrBot

    store = _load_store(root)
    return LessonPrBot(store=store, root=root)


def _seed_resources() -> tuple[list[Path], list[Path]]:
    """Return (block_files, rubric_files) bundled with the package."""
    blocks_dir = resources.files("atelier") / "infra" / "seed_blocks"
    rubrics_dir = resources.files("atelier") / "core" / "rubrics"
    block_files = sorted(Path(str(p)) for p in blocks_dir.iterdir() if p.name.endswith(".yaml"))
    rubric_files = sorted(Path(str(p)) for p in rubrics_dir.iterdir() if p.name.endswith(".yaml"))
    return block_files, rubric_files


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_domain_manager(root: Path) -> Any:
    from atelier.core.domains import DomainManager

    return DomainManager(root)


def _parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dhm])", value.strip())
    if not match:
        raise click.ClickException("duration must look like 7d, 12h, or 30m")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=atelier_version, prog_name="atelier")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=DEFAULT_ROOT,
    show_default=True,
    help="Atelier runtime data directory.",
)
@click.pass_context
def cli(ctx: click.Context, root: Path) -> None:
    """Atelier - Agent Reasoning Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root


@cli.command("help", context_settings={"ignore_unknown_options": True})
@click.argument("command_path", nargs=-1)
@click.pass_context
def help_cmd(ctx: click.Context, command_path: tuple[str, ...]) -> None:
    """Show help for Atelier or a specific command path."""
    root_ctx = ctx.parent
    if root_ctx is None:
        click.echo(cli.get_help(ctx))
        return

    if not command_path:
        click.echo(root_ctx.get_help())
        return

    command: click.Command = cli
    command_ctx = root_ctx
    for token in command_path:
        if not isinstance(command, click.Group):
            raise click.ClickException(f"{command_ctx.command_path} has no subcommands")
        next_command = command.get_command(command_ctx, token)
        if next_command is None:
            raise click.ClickException(f"unknown command: {' '.join(command_path)}")
        command = next_command
        command_ctx = click.Context(command, info_name=token, parent=command_ctx)

    click.echo(command.get_help(command_ctx))


# ----- init ---------------------------------------------------------------- #


def _detect_git_root(search_path: Path) -> Path | None:
    """Return the git repo root containing search_path, or None if not in a repo."""
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(search_path),
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (OSError, _subprocess.SubprocessError):
        logger.debug("git root detection failed", exc_info=True)
    return None


@cli.command()
@click.option("--seed/--no-seed", default=True, help="Import bundled seed blocks and rubrics.")
@click.option("--stack", default=None, help="Copy starter ReasonBlock templates for a stack.")
@click.option("--list-stacks", "show_stacks", is_flag=True, help="List available starter stacks.")
@click.option(
    "--index/--no-index",
    default=True,
    help="Bootstrap the code index for the current git repo (default: on).",
)
@click.pass_context
def init(ctx: click.Context, seed: bool, stack: str | None, show_stacks: bool, index: bool) -> None:
    """Initialize the runtime store at --root."""
    if show_stacks:
        from atelier.core.capabilities.starter_packs import list_stacks

        stacks = list_stacks()
        if not stacks:
            click.echo("No starter stacks available.")
            return
        click.echo("Available starter stacks:")
        for item in stacks:
            click.echo(f"  {item.slug:20} {item.name} ({item.version}) - {item.description}")
        return

    root: Path = ctx.obj["root"]
    from atelier.infra.storage.factory import create_store

    try:
        store = create_store(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    store.init()
    click.echo(f"initialized atelier store at {store.root}")
    if seed:
        block_files, rubric_files = _seed_resources()
        seeded_blocks: dict[str, ReasonBlock] = {}
        for path in block_files:
            data = _load_yaml(path)
            if "id" not in data:
                data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
            block = ReasonBlock.model_validate(data)
            seeded_blocks[block.id] = block
        for block in _load_domain_manager(root).all_reasonblocks():
            seeded_blocks[block.id] = block
        n_b = 0
        for block in seeded_blocks.values():
            store.upsert_block(block)
            n_b += 1
        n_r = 0
        for path in rubric_files:
            data = _load_yaml(path)
            rubric = Rubric.model_validate(data)
            store.upsert_rubric(rubric)
            n_r += 1
        click.echo(f"seeded {n_b} reasonblocks and {n_r} rubrics")
    if stack:
        from atelier.core.capabilities.starter_packs import copy_stack_templates

        try:
            copied, skipped = copy_stack_templates(stack, store.blocks_dir)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        suffix = f", skipped {skipped} existing" if skipped else ""
        click.echo(f"copied {copied} starter reasonblocks for stack {stack}{suffix}")
    if index:
        git_root = _detect_git_root(Path.cwd())
        if git_root is not None:
            click.echo(f"bootstrapping code index for {git_root} ...")
            from atelier.gateway.cli.commands.code import _code_context_engine

            engine = _code_context_engine(str(git_root))
            stats = engine.index_repo().model_dump(mode="json")
            click.echo(
                f"indexed {stats['files_indexed']} files, "
                f"{stats['symbols_indexed']} symbols "
                f"({stats['imports_indexed']} imports)"
            )
        else:
            click.echo("code index skipped (no git repository detected in current directory)")


# ----- uninstall ----------------------------------------------------------- #


@cli.command("uninstall")
@click.option("--dry-run", is_flag=True, help="Print planned actions and exit.")
@click.option("--no-hosts", is_flag=True, help="Skip per-host uninstallation.")
@click.option(
    "--purge",
    is_flag=True,
    help="Also remove runtime state, install dirs, tool envs, and known host residue.",
)
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    help="Uninstall for a specific workspace.",
)
def uninstall(dry_run: bool, no_hosts: bool, purge: bool, workspace: Path | None) -> None:
    """Remove Atelier and all agent-host integrations."""
    root = _project_root()
    script = root / "scripts" / "uninstall.sh"
    if not script.exists():
        raise click.ClickException(f"uninstall script not found: {script}")

    cmd = ["bash", str(script)]
    if dry_run:
        cmd.append("--dry-run")
    if no_hosts:
        cmd.append("--no-hosts")
    if purge:
        cmd.append("--purge")
    if workspace:
        cmd.extend(["--workspace", str(workspace)])

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"uninstall failed with code {exc.returncode}") from exc


def _dev_command(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a dev command but gate execution at runtime."""
    if name in MCP_TOOL_ONLY_COMMANDS:
        return lambda f: f

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        command_name = name or func.__name__.replace("_", "-")

        @wraps(func)
        def guarded(*args: Any, **inner_kwargs: Any) -> Any:
            _check_dev_mode(command_name)
            return func(*args, **inner_kwargs)

        return cli.command(name, **kwargs)(guarded)

    return decorator


def _dev_group(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
    """Register a dev group but gate execution at runtime."""
    if name in MCP_TOOL_ONLY_GROUPS:
        return lambda f: _DummyGroup()

    def decorator(func: Callable[..., Any]) -> Any:
        group_name = name or func.__name__.replace("_", "-")

        @wraps(func)
        def guarded(*args: Any, **inner_kwargs: Any) -> Any:
            _check_dev_mode(group_name)
            return func(*args, **inner_kwargs)

        return cli.group(name, **kwargs)(guarded)

    return decorator


@_dev_command("reembed")
@click.option("--dry-run", is_flag=True, help="Count legacy rows without writing vectors.")
@click.option("--batch-size", default=100, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reembed(ctx: click.Context, dry_run: bool, batch_size: int, as_json: bool) -> None:
    """Back-fill legacy_stub embeddings for archival passages and lesson candidates."""
    from atelier.infra.embeddings.factory import make_embedder

    root: Path = ctx.obj["root"]
    store = ContextStore(root)
    store.init()
    embedder = make_embedder()
    counts = {"archival_passage": 0, "lesson_candidate": 0, "dry_run": dry_run}
    with store._connect() as conn:
        passages = conn.execute(
            """
            SELECT id, text FROM archival_passage
            WHERE embedding_provenance = 'legacy_stub'
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        lessons = conn.execute(
            """
            SELECT id, cluster_fingerprint, evidence_trace_ids, body FROM lesson_candidate
            WHERE embedding_provenance = 'legacy_stub'
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        counts["archival_passage"] = len(passages)
        counts["lesson_candidate"] = len(lessons)
        if not dry_run:
            for row in passages:
                vector = embedder.embed([str(row["text"])])[0]
                conn.execute(
                    """
                    UPDATE archival_passage
                    SET embedding = ?, embedding_provenance = ?
                    WHERE id = ?
                    """,
                    (json.dumps(vector).encode("utf-8"), embedder.__class__.__name__, row["id"]),
                )
            for row in lessons:
                text = "\n".join(
                    [
                        str(row["cluster_fingerprint"]),
                        str(row["evidence_trace_ids"]),
                        str(row["body"]),
                    ]
                )
                vector = embedder.embed([text])[0]
                conn.execute(
                    """
                    UPDATE lesson_candidate
                    SET embedding = ?, embedding_provenance = ?
                    WHERE id = ?
                    """,
                    (json.dumps(vector), embedder.__class__.__name__, row["id"]),
                )
    _emit(counts, as_json=as_json)


# ----- add-block ----------------------------------------------------------- #


@_dev_command("add-block")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def add_block(ctx: click.Context, path: Path) -> None:
    """Add or update a ReasonBlock from a YAML file."""
    store = _load_store(ctx.obj["root"])
    data = _load_yaml(path)
    if "id" not in data:
        data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
    block = ReasonBlock.model_validate(data)
    store.upsert_block(block)
    click.echo(f"upserted {block.id}")


@cli.group("domain")
def domain_group() -> None:
    """Manage Atelier internal domain bundles."""


@domain_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def domain_list(ctx: click.Context, as_json: bool) -> None:
    """List available domain bundles (built-in + user)."""
    manager = _load_domain_manager(ctx.obj["root"])
    refs = manager.list_bundles()
    payload = [r.model_dump(mode="json") for r in refs]
    if as_json:
        _emit(payload, as_json=True)
        return
    if not payload:
        click.echo("(no domain bundles)")
        return
    for item in payload:
        click.echo(f"{item['bundle_id']}\t{item['domain']}\t{item['description'][:60]}")


@domain_group.command("info")
@click.argument("bundle_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def domain_info(ctx: click.Context, bundle_id: str, as_json: bool) -> None:
    """Show details for a domain bundle."""
    manager = _load_domain_manager(ctx.obj["root"])
    result = manager.info(bundle_id)
    if result is None:
        raise click.ClickException(f"domain bundle not found: {bundle_id}")
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


# ----- telemetry ---------------------------------------------------------- #


@cli.group("telemetry")
def telemetry_group() -> None:
    """Product telemetry controls."""


@telemetry_group.command("status")
@click.option("--json", "as_json", is_flag=True)
def telemetry_status(as_json: bool) -> None:
    from atelier.core.foundation.identity import (
        get_anon_id,
        new_session_id,
        telemetry_id_path,
    )
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.banner import is_acknowledged
    from atelier.core.service.telemetry.config import config_path, load_telemetry_config
    from atelier.core.service.telemetry.local_store import default_db_path

    session_id = new_session_id()
    emit_product(
        "cli_command_invoked",
        command_name="telemetry_status",
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    cfg = load_telemetry_config()
    payload = {
        "remote_enabled": cfg.remote_enabled,
        "lexical_frustration_enabled": cfg.lexical_frustration_enabled,
        "config_path": str(config_path()),
        "telemetry_id_path": str(telemetry_id_path()),
        "local_db_path": str(default_db_path()),
        "acknowledged": is_acknowledged(),
        "anon_id": get_anon_id(),
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"remote telemetry: {'on' if cfg.remote_enabled else 'off'}")
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")
    click.echo(f"local database: {payload['local_db_path']}")


@telemetry_group.command("on")
def telemetry_on() -> None:
    from atelier.core.service.telemetry import set_remote_enabled

    set_remote_enabled(True)
    click.echo("remote telemetry: on")


@telemetry_group.command("off")
def telemetry_off() -> None:
    from atelier.core.service.telemetry import set_remote_enabled
    from atelier.core.service.telemetry.banner import mark_acknowledged

    set_remote_enabled(False)
    mark_acknowledged()
    click.echo("remote telemetry: off")


@telemetry_group.command("show")
@click.option("--limit", default=20, show_default=True, type=int)
def telemetry_show(limit: int) -> None:
    from atelier.core.service.telemetry.local_store import LocalTelemetryStore

    events = LocalTelemetryStore().list_events(limit=limit)
    _emit([{"event": item["event"], "props": item["props"]} for item in events], as_json=True)


@telemetry_group.command("reset-id")
def telemetry_reset_id() -> None:
    from atelier.core.foundation.identity import reset_anon_id

    click.echo(reset_anon_id())


@telemetry_group.group("lexical")
def telemetry_lexical_group() -> None:
    """Lexical frustration detection controls."""


@telemetry_lexical_group.command("on")
def telemetry_lexical_on() -> None:
    from atelier.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=True)
    click.echo("lexical frustration detection: on")


@telemetry_lexical_group.command("off")
def telemetry_lexical_off() -> None:
    from atelier.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=False)
    click.echo("lexical frustration detection: off")


@telemetry_lexical_group.command("status")
def telemetry_lexical_status() -> None:
    from atelier.core.service.telemetry.config import load_telemetry_config

    cfg = load_telemetry_config()
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")


# ----- report ------------------------------------------------------------- #


@cli.command("report")
@click.option("--since", default="7d", show_default=True, help="Lookback duration, e.g. 7d or 12h.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    show_default=True,
)
@click.option("--output", "output_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def report_cmd(ctx: click.Context, since: str, output_format: str, output_path: Path | None) -> None:
    """Generate an engineering-leader governance report."""
    from atelier.core.capabilities.reporting.weekly_report import generate_report, render_markdown

    store = _load_store(ctx.obj["root"])
    report = generate_report(_parse_duration(since), store=store, repo_root=Path.cwd())
    if output_format == "json":
        rendered = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)
    else:
        rendered = render_markdown(report)
    if output_path is not None:
        output_path.write_text(rendered, encoding="utf-8")
        return
    click.echo(rendered.rstrip())


# ----- import-style-guide ------------------------------------------------- #


@cli.command("import-style-guide")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path, exists=True))
@click.option("--domain", default="coding", show_default=True)
@click.option("--dry-run", is_flag=True, help="Print proposed candidates without writing.")
@click.option("--limit", default=25, show_default=True, type=int)
@click.pass_context
def import_style_guide_cmd(
    ctx: click.Context,
    paths: tuple[Path, ...],
    domain: str,
    dry_run: bool,
    limit: int,
) -> None:
    """Draft lesson candidates from Markdown style guides."""
    from atelier.core.capabilities.style_import import import_files
    from atelier.infra.internal_llm.ollama_client import OllamaUnavailable

    if not paths:
        raise click.ClickException("at least one Markdown file or directory is required")
    store = _load_store(ctx.obj["root"])
    try:
        candidates = import_files(paths, domain, store=store, write=not dry_run, limit=limit)
    except OllamaUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    payload = {
        "dry_run": dry_run,
        "written": 0 if dry_run else len(candidates),
        "candidates": [candidate.model_dump(mode="json", exclude={"embedding"}) for candidate in candidates],
    }
    if dry_run:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    click.echo(f"imported {len(candidates)} lesson candidates into inbox")
    for candidate in candidates:
        click.echo(candidate.id)


# --------------------------------------------------------------------------- #
# block                                                                       #
# --------------------------------------------------------------------------- #


@_dev_group("block")
def block_group() -> None:
    """ReasonBlock curation commands."""


@block_group.command("list")
@click.option("--domain", default=None)
@click.option("--include-deprecated", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_list(ctx: click.Context, domain: str | None, include_deprecated: bool, as_json: bool) -> None:  # type: ignore
    """List ReasonBlocks."""
    store = _load_store(ctx.obj["root"])
    blocks = store.list_blocks(domain=domain, include_deprecated=include_deprecated)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    if not blocks:
        click.echo("(no blocks)")
        return
    click.echo(f"{len(blocks)} blocks shown")
    for b in blocks:
        click.echo(f"{b.id}\t{b.domain}\t{b.title}")


@block_group.command("add")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def block_add(ctx: click.Context, path: Path) -> None:  # type: ignore
    """Import a ReasonBlock from a YAML file."""
    store = _load_store(ctx.obj["root"])
    data = _load_yaml(path)
    if "id" not in data:
        data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
    block = ReasonBlock.model_validate(data)
    store.upsert_block(block)
    click.echo(f"upserted {block.id}")


@block_group.command("extract")
@click.argument("trace_id")
@click.option("--save", is_flag=True, help="Persist the candidate block.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_extract(ctx: click.Context, trace_id: str, save: bool, as_json: bool) -> None:  # type: ignore
    """Extract a candidate ReasonBlock from a trace."""
    store = _load_store(ctx.obj["root"])
    trace = store.get_trace(trace_id)
    if trace is None:
        raise click.ClickException(f"trace not found: {trace_id}")
    from atelier.core.foundation.extractor import extract_candidate

    candidate = extract_candidate(trace)
    if save:
        store.upsert_block(candidate.block)
    payload = {
        "block": to_jsonable(candidate.block),
        "confidence": candidate.confidence,
        "reasons": candidate.reasons,
        "saved": save,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"candidate: {candidate.block.id} (confidence={candidate.confidence:.2f})")
    for r in candidate.reasons:
        click.echo(f"  - {r}")
    click.echo(render_block_markdown(candidate.block))


# ----- list-blocks --------------------------------------------------------- #


@cli.command("list-blocks")
@click.option("--domain", default=None)
@click.option("--include-deprecated", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_blocks_cmd(ctx: click.Context, domain: str | None, include_deprecated: bool, as_json: bool) -> None:
    """List ReasonBlocks."""
    store = _load_store(ctx.obj["root"])
    blocks = store.list_blocks(domain=domain, include_deprecated=include_deprecated)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    from atelier.core.foundation.metrics import summarize

    summary = summarize(store)
    click.echo(
        f"# {len(blocks)} blocks shown "
        f"(active={summary.blocks_active}, "
        f"deprecated={summary.blocks_deprecated}, "
        f"quarantined={summary.blocks_quarantined})"
    )
    for b in blocks:
        click.echo(f"{b.status[:1].upper()} {b.id}\t{b.domain}\t{b.title}")


# ----- env ----------------------------------------------------------------- #


@cli.group()
def env() -> None:
    """Validate named compatibility environments."""


@env.command("validate")
@click.argument("env_name")
@click.pass_context
def env_validate(ctx: click.Context, env_name: str) -> None:
    """Validate that a named environment contract exists."""
    store = _load_store(ctx.obj["root"])
    candidates = [env_name]
    suffix = env_name[4:] if env_name.startswith("env_") else env_name
    candidates.append(f"rubric_{suffix}")
    for rubric_id in candidates:
        if store.get_rubric(rubric_id) is not None:
            click.echo(f"ok: {env_name}")
            return
    raise click.ClickException(f"unknown environment: {env_name}")


# ----- deprecate / quarantine --------------------------------------------- #


@cli.command()
@click.argument("block_id")
@click.pass_context
def deprecate(ctx: click.Context, block_id: str) -> None:
    """Mark a block as deprecated."""
    store = _load_store(ctx.obj["root"])
    if not store.update_block_status(block_id, "deprecated"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"deprecated {block_id}")


@cli.command()
@click.argument("block_id")
@click.pass_context
def quarantine(ctx: click.Context, block_id: str) -> None:
    """Quarantine a block (will not be retrieved)."""
    store = _load_store(ctx.obj["root"])
    if not store.update_block_status(block_id, "quarantined"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"quarantined {block_id}")


# ----- agent host importers ------------------------------------------------- #
# Each sub-group follows the same pattern:
#   atelier <host> import [--path PATH]
#
# Data model (all three hosts):
#   - RawArtifact  : full redacted session file(s) stored under .atelier/raw/
#   - Trace        : compact curated summary with raw_artifact_ids linkback
#
# Nothing is thrown away except secrets/PII stripped by Atelier's redactor.
# --------------------------------------------------------------------------- #


@cli.group()
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
    """Import Copilot sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.copilot import CopilotImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = CopilotImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} copilot sessions")


# ----- claude --------------------------------------------------------------- #


@cli.group()
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
    """Import Claude Code sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.claude import ClaudeImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = ClaudeImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} claude sessions")


# ----- codex ---------------------------------------------------------------- #


@cli.group()
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
    """Import Codex sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.codex import CodexImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = CodexImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} codex sessions")


# ----- opencode ------------------------------------------------------------- #


@cli.group()
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
    """Import OpenCode sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.opencode import OpenCodeImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = OpenCodeImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} opencode sessions")


# ----- gemini --------------------------------------------------------------- #


@cli.group()
def gemini() -> None:
    """Gemini CLI session integration (~/.gemini/tmp/atelier/chats/)."""


@gemini.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def gemini_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Gemini sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.gemini import GeminiImporter

    _ensure_import_progress_logging()
    store = _load_store(ctx.obj["root"])
    importer = GeminiImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} gemini sessions")


# ----- global import -------------------------------------------------------- #


@cli.command("import")
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
    "--export-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Export reconstructed session logs (JSONL) to this directory.",
)
@click.pass_context
def global_import(ctx: click.Context, host: str | None, force: bool, export_dir: Path | None) -> None:
    """Unified import for ALL agent sessions (Claude, Gemini, Codex, etc.)."""
    from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns
    from atelier.gateway.hosts.session_parsers.registry import iter_importer_classes

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

    with store.batch_mode():
        for name, importer_cls in hosts:
            if host and name != host:
                continue

            try:
                ids = importer_cls(store).import_all(force=force)
                count = len(ids)
                total += count
                all_imported_ids.extend(ids)

                # Reconstruction audit
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

    if total > 0:
        pct = (reconstructable / total) * 100
        click.echo(f"\nAudit: {reconstructable}/{total} sessions ({pct:.1f}%) 100% reconstructable.")

    # Sync aggregated usage
    try:
        from atelier.core.service.sync import sync_usage

        sync_usage(ctx.obj["root"], session_ids=all_imported_ids)
    except Exception:
        logging.exception("sync_usage failed after global import")
        logger.warning(
            "Suppressed exception at cli.py:1827",
            exc_info=True,
        )


# --------------------------------------------------------------------------- #
# V2: Ledger / Watchdog / Compress / Env / Failure / Eval / Smart / Savings   #
# --------------------------------------------------------------------------- #


def _ledger_dir(root: Path) -> Path:
    return Path(root) / "runs"


def _latest_ledger_path(root: Path) -> Path | None:
    runs = _ledger_dir(root)
    if not runs.is_dir():
        return None
    paths = sorted(runs.glob("*.json"))
    return paths[-1] if paths else None


def _ledger_path(root: Path, session_id: str | None) -> Path:
    if session_id:
        return _ledger_dir(root) / f"{session_id}.json"
    latest = _latest_ledger_path(root)
    if latest is None:
        raise click.ClickException("no run ledger found. Pass --session-id or record one first.")
    return latest


# ----- ledger ------------------------------------------------------------- #


@cli.group()
def ledger() -> None:
    """Manage run ledgers."""


@ledger.command("show")
@click.option("--session-id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def ledger_show(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    if as_json:
        _emit(snap, as_json=True)
        return
    click.echo(f"session_id: {snap.get('session_id')}")
    click.echo(f"status: {snap.get('status')}")
    click.echo(f"task: {snap.get('task', '')}")
    click.echo(f"domain: {snap.get('domain', '')}")
    click.echo(f"events: {len(snap.get('events', []))}")
    click.echo(f"errors_seen: {len(snap.get('errors_seen', []))}")
    click.echo(f"current_blockers: {snap.get('current_blockers', [])}")


@ledger.command("reset")
@click.option("--session-id", default=None)
@click.confirmation_option(prompt="Delete this ledger snapshot?")
@click.pass_context
def ledger_reset(ctx: click.Context, session_id: str | None) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    path.unlink(missing_ok=True)
    click.echo(f"removed {path}")


@ledger.command("update")
@click.option("--session-id", default=None)
@click.option("--field", "field_name", required=True)
@click.option("--value", required=True, help="Value (use JSON literal for lists/dicts).")
@click.pass_context
def ledger_update(ctx: click.Context, session_id: str | None, field_name: str, value: str) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    snap[field_name] = parsed
    path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    click.echo(f"updated {field_name}")


@ledger.command("summarize")
@click.option("--session-id", default=None)
@click.pass_context
def ledger_summarize(ctx: click.Context, session_id: str | None) -> None:
    from atelier.infra.runtime.context_compressor import ContextCompressor
    from atelier.infra.runtime.run_ledger import RunLedger

    path = _ledger_path(ctx.obj["root"], session_id)
    led = RunLedger.load(path)
    state = ContextCompressor().compress(led)
    click.echo(state.to_prompt_block())


# ----- checkpoint --------------------------------------------------------- #


@cli.group()
def checkpoint() -> None:
    """Manage idempotent agent checkpoints for resumable execution."""


@checkpoint.command("create")
@click.option("--session-id", default=None, help="Session ID (defaults to latest ledger).")
@click.option("--tool", "tool_name", default="manual", show_default=True)
@click.option("--model-route", default="cheap_llm", show_default=True)
@click.option("--note", default="", help="Optional note stored as compact_state.")
@click.pass_context
def checkpoint_create(
    ctx: click.Context,
    session_id: str | None,
    tool_name: str,
    model_route: str,
    note: str,
) -> None:
    """Create a checkpoint at the current ledger step."""
    from atelier.infra.runtime.checkpoint import Checkpoint, CheckpointStore
    from atelier.infra.runtime.run_ledger import RunLedger

    root = ctx.obj["root"]
    path = _ledger_path(root, session_id)
    led = RunLedger.load(path)
    store = CheckpointStore(root)
    step_id = len(store.list_checkpoints(led.session_id))
    ckpt = Checkpoint.create(
        session_id=led.session_id,
        step_id=step_id,
        tool_name=tool_name,
        model_route=model_route,
        input_data=note,
        output_data=led.status,
        compact_state=note,
        cost_so_far_usd=led.cost_tracker.snapshot().get("total_cost_usd", 0.0) if led.cost_tracker else 0.0,
    )
    saved_path = store.save(ckpt)
    click.echo(f"checkpoint created: session={ckpt.session_id} step={ckpt.step_id} txn={ckpt.transaction_id}")
    click.echo(f"  saved to: {saved_path}")


@checkpoint.command("list")
@click.option("--session-id", default=None, help="Filter to a specific session.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def checkpoint_list(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """List available checkpoints."""
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)
    sessions = [session_id] if session_id else store.list_sessions()
    if not sessions:
        click.echo("no checkpoints found.")
        return
    rows = []
    for sid in sessions:
        for ckpt in store.list_checkpoints(sid):
            rows.append(ckpt.to_dict())
    if as_json:
        _emit(rows, as_json=True)
        return
    for row in rows:
        click.echo(
            f"  {row['session_id'][:12]}  step={row['step_id']:3d}"
            f"  tool={row['tool_name']:<18s}  route={row['model_route']:<14s}"
            f"  cost=${row['cost_so_far_usd']:.4f}  txn={row['transaction_id']}"
        )


@checkpoint.command("resume")
@click.argument("session_id")
@click.option(
    "--from-step",
    "from_step",
    type=int,
    default=None,
    help="Resume from this step (default: last).",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def checkpoint_resume(
    ctx: click.Context,
    session_id: str,
    from_step: int | None,
    as_json: bool,
) -> None:
    """Resume execution context from a saved checkpoint.

    Prints the compact_state from the checkpoint so the agent can restore
    context and continue from step N instead of restarting the full loop.
    """
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)

    if from_step is not None:
        ckpt = store.load(session_id, from_step)
        if ckpt is None:
            raise click.ClickException(f"no checkpoint found for session={session_id} step={from_step}")
    else:
        ckpt = store.latest_checkpoint(session_id)
        if ckpt is None:
            raise click.ClickException(f"no checkpoints found for session={session_id}")

    if as_json:
        _emit(ckpt.to_dict(), as_json=True)
        return

    click.echo(f"resuming from: session={ckpt.session_id}  step={ckpt.step_id}  txn={ckpt.transaction_id}")
    click.echo(f"  tool_name:    {ckpt.tool_name}")
    click.echo(f"  model_route:  {ckpt.model_route}")
    click.echo(f"  cost_so_far:  ${ckpt.cost_so_far_usd:.4f}")
    click.echo(f"  input_hash:   {ckpt.input_hash}")
    click.echo(f"  output_hash:  {ckpt.output_hash}")
    if ckpt.compact_state:
        click.echo("\ncompact_state:")
        click.echo(ckpt.compact_state)


@checkpoint.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="Delete all checkpoints for this session?")
@click.pass_context
def checkpoint_delete(ctx: click.Context, session_id: str) -> None:
    """Delete all checkpoints for a session."""
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)
    count = store.delete_session(session_id)
    click.echo(f"deleted {count} checkpoint(s) for session={session_id}")


# ----- failure ------------------------------------------------------------ #


@cli.group()
def failure() -> None:
    """Failure cluster management."""


def _failure_state_path(root: Path) -> Path:
    return Path(root) / "failure_clusters.json"


def _load_failure_state(root: Path) -> dict[str, dict[str, Any]]:
    path = _failure_state_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_failure_state(root: Path, state: dict[str, dict[str, Any]]) -> None:
    path = _failure_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


@failure.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def failure_list(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    runs = _ledger_dir(ctx.obj["root"])
    clusters = FailureAnalyzer(runs).analyze()
    state = _load_failure_state(ctx.obj["root"])
    if as_json:
        _emit(
            [{**to_jsonable(c), "status": state.get(c.id, {}).get("status", "open")} for c in clusters],
            as_json=True,
        )
        return
    if not clusters:
        click.echo("(no failure clusters)")
        return
    for c in clusters:
        st = state.get(c.id, {}).get("status", "open")
        click.echo(f"{c.id}\t{st}\t{c.severity}\t{c.domain}\t{c.fingerprint[:60]}")


@failure.command("show")
@click.argument("cluster_id")
@click.pass_context
def failure_show(ctx: click.Context, cluster_id: str) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    clusters = {c.id: c for c in FailureAnalyzer(_ledger_dir(ctx.obj["root"])).analyze()}
    if cluster_id not in clusters:
        raise click.ClickException(f"cluster not found: {cluster_id}")
    state = _load_failure_state(ctx.obj["root"])
    payload = to_jsonable(clusters[cluster_id])
    payload["status"] = state.get(cluster_id, {}).get("status", "open")
    _emit(payload, as_json=True)


@failure.command("accept")
@click.argument("cluster_id")
@click.pass_context
def failure_accept(ctx: click.Context, cluster_id: str) -> None:
    state = _load_failure_state(ctx.obj["root"])
    state.setdefault(cluster_id, {})["status"] = "accepted"
    _save_failure_state(ctx.obj["root"], state)
    click.echo(f"accepted {cluster_id}")


@failure.command("reject")
@click.argument("cluster_id")
@click.pass_context
def failure_reject(ctx: click.Context, cluster_id: str) -> None:
    state = _load_failure_state(ctx.obj["root"])
    state.setdefault(cluster_id, {})["status"] = "rejected"
    _save_failure_state(ctx.obj["root"], state)
    click.echo(f"rejected {cluster_id}")


# ----- lesson ------------------------------------------------------------- #


@cli.group()
def lesson() -> None:
    """Lesson candidate review workflow."""


def _emit_lesson_inbox(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    lessons = _lesson_promoter(ctx.obj["root"]).inbox(domain=domain, limit=limit)
    if as_json:
        _emit([item.model_dump(mode="json") for item in lessons], as_json=True)
        return
    if not lessons:
        click.echo("(no inbox lessons)")
        return
    for item in lessons:
        click.echo(f"{item.id}\t{item.domain}\t{item.kind}\t{item.confidence:.2f}\t{item.cluster_fingerprint[:48]}")


@lesson.command("list")
@click.option("--domain", default=None)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_list(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    _emit_lesson_inbox(ctx, domain, limit, as_json)


@lesson.command("inbox")
@click.option("--domain", default=None)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_inbox(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    """List lesson candidates currently waiting in the inbox."""
    _emit_lesson_inbox(ctx, domain, limit, as_json)


@lesson.command("approve")
@click.argument("lesson_id")
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_approve(
    ctx: click.Context,
    lesson_id: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision="approve",
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"approved {lesson_id}")


@lesson.command("reject")
@click.argument("lesson_id")
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_reject(
    ctx: click.Context,
    lesson_id: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision="reject",
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"rejected {lesson_id}")


@lesson.command("decide")
@click.argument("lesson_id")
@click.argument("decision", type=click.Choice(["approve", "reject"]))
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_decide(
    ctx: click.Context,
    lesson_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    """Approve or reject a lesson candidate."""
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision=decision,
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    verb = "approved" if decision == "approve" else "rejected"
    click.echo(f"{verb} {lesson_id}")


@lesson.group("active")
def lesson_active_group() -> None:
    """Inspect and manage active typed lessons."""


@lesson_active_group.command("list")
@click.option("--include-inactive", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_list(ctx: click.Context, include_inactive: bool, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lessons = TypedLessonStore(ctx.obj["root"], create=False).list_lessons()
    if not include_inactive:
        lessons = [lesson for lesson in lessons if lesson.enabled]
    if as_json:
        _emit([lesson.model_dump(mode="json") for lesson in lessons], as_json=True)
        return
    if not lessons:
        click.echo("(no active lessons)")
        return
    for lesson in lessons:
        last_applied = lesson.last_applied_at.isoformat() if lesson.last_applied_at else "-"
        click.echo(
            f"{lesson.id}\t{lesson.kind}\t{lesson.scope}\t{lesson.effective_confidence_at():.2f}\t"
            f"{'enabled' if lesson.enabled else 'disabled'}\t{last_applied}"
        )


@lesson_active_group.command("show")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_show(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"], create=False).get_lesson(lesson_id)
    if lesson is None:
        raise click.ClickException(f"typed lesson not found: {lesson_id}")
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(json.dumps(lesson.model_dump(mode="json"), indent=2, ensure_ascii=False, default=str))


@lesson_active_group.command("disable")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_disable(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"]).set_enabled(lesson_id, False)
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(f"disabled {lesson_id}")


@lesson_active_group.command("enable")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_enable(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"]).set_enabled(lesson_id, True)
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(f"enabled {lesson_id}")


@lesson.command("sync-pr")
@click.argument("lesson_id")
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_sync_pr(ctx: click.Context, lesson_id: str, dry_run: bool, as_json: bool) -> None:
    payload = _lesson_pr_bot(ctx.obj["root"]).sync_pr(lesson_id=lesson_id, dry_run=dry_run)
    if as_json:
        _emit(payload, as_json=True)
        return
    if payload.get("skipped"):
        click.echo(f"skipped: {payload.get('reason', 'unknown')}")
        return
    if dry_run:
        click.echo(payload.get("diff", ""))
        return
    click.echo(f"created {payload.get('pr_url', '').strip()}")


@cli.command("analyze-failures")
@click.option("--since", default=None, help="ISO timestamp or shorthand like '7d' (filter by mtime).")
@click.option("--trace", "trace_id", default=None, help="Single ledger run id to analyze.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def analyze_failures_cmd(ctx: click.Context, since: str | None, trace_id: str | None, as_json: bool) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    runs = _ledger_dir(ctx.obj["root"])
    fa = FailureAnalyzer(runs)
    snaps = fa.load_snapshots()

    if trace_id:
        snaps = [s for s in snaps if s.get("session_id") == trace_id]

    if since:
        from datetime import datetime, timedelta

        cutoff: datetime | None = None
        if since.endswith("d") and since[:-1].isdigit():
            cutoff = datetime.now(UTC) - timedelta(days=int(since[:-1]))
        else:
            try:
                cutoff = datetime.fromisoformat(since)
            except ValueError:
                cutoff = None
        if cutoff is not None:
            kept = []
            for s in snaps:
                ts = s.get("updated_at") or s.get("created_at")
                if not ts:
                    continue
                try:
                    if datetime.fromisoformat(ts) >= cutoff:
                        kept.append(s)
                except ValueError:
                    continue
            snaps = kept

    from atelier.core.improvement.failure_analyzer import analyze_failures

    clusters = analyze_failures(snaps)
    session_id = _telemetry_session(ctx)
    if session_id is not None:
        from atelier.core.service.telemetry import emit_product
        from atelier.core.service.telemetry.schema import hash_identifier

        for cluster in clusters:
            emit_product(
                "failure_cluster_matched",
                cluster_id_hash=hash_identifier(cluster.id),
                domain=cluster.domain,
                session_id=session_id,
            )
    if as_json:
        _emit([to_jsonable(c) for c in clusters], as_json=True)
        return
    for c in clusters:
        click.echo(f"{c.id}\t{c.severity}\t{c.domain}\t{c.fingerprint[:60]}")


# ----- eval --------------------------------------------------------------- #


def _eval_dir(root: Path) -> Path:
    return Path(root) / "evals"


def _load_eval(root: Path, case_id: str) -> dict[str, Any] | None:
    p = _eval_dir(root) / f"{case_id}.json"
    if not p.exists():
        return None
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def _save_eval(root: Path, case: dict[str, Any]) -> Path:
    d = _eval_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{case['id']}.json"
    p.write_text(json.dumps(case, indent=2), encoding="utf-8")
    return p


def _evaluate_eval_case(case: dict[str, Any]) -> dict[str, Any]:
    expected_status = str(case.get("expected_status", "pass"))
    actual_status = str(case.get("actual_status", expected_status))
    return {
        "case_id": str(case.get("id", "unknown")),
        "domain": str(case.get("domain", "unknown")),
        "description": str(case.get("description", "")),
        "expected_status": expected_status,
        "actual_status": actual_status,
        "passed": actual_status == expected_status,
    }


@cli.group(name="eval")
def eval_() -> None:  # name with trailing underscore to avoid python builtin
    """Evaluation case management."""


# Click v8 needs explicit name binding because eval is reserved-ish.
eval_.name = "eval"


@eval_.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def eval_list(ctx: click.Context, as_json: bool) -> None:
    d = _eval_dir(ctx.obj["root"])
    cases = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            cases.append(json.loads(p.read_text(encoding="utf-8")))
    if as_json:
        _emit(cases, as_json=True)
        return
    for c in cases:
        click.echo(f"{c.get('id')}\t{c.get('status', 'draft')}\t{c.get('domain', '')}\t{c.get('description', '')[:60]}")


@eval_.command("show")
@click.argument("case_id")
@click.pass_context
def eval_show(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    _emit(case, as_json=True)


@eval_.command("promote")
@click.argument("case_id")
@click.pass_context
def eval_promote(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    case["status"] = "active"
    _save_eval(ctx.obj["root"], case)
    click.echo(f"promoted {case_id}")


@eval_.command("deprecate")
@click.argument("case_id")
@click.pass_context
def eval_deprecate(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    case["status"] = "deprecated"
    _save_eval(ctx.obj["root"], case)
    click.echo(f"deprecated {case_id}")


@eval_.command("run")
@click.option("--domain", default=None)
@click.option("--case", "case_id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def eval_run(ctx: click.Context, domain: str | None, case_id: str | None, as_json: bool) -> None:
    """Run deterministic eval cases."""
    # Note: plan-check based evals have been deprecated.
    # This command now only lists the cases if not in JSON mode.
    d = _eval_dir(ctx.obj["root"])
    cases: list[dict[str, Any]] = []
    if case_id:
        c = _load_eval(ctx.obj["root"], case_id)
        if c is None:
            raise click.ClickException(f"eval case not found: {case_id}")
        cases = [c]
    elif d.is_dir():
        for p in sorted(d.glob("*.json")):
            cases.append(json.loads(p.read_text(encoding="utf-8")))
    if domain:
        cases = [c for c in cases if c.get("domain") == domain]
    results = [_evaluate_eval_case(case) for case in cases]

    if as_json:
        _emit(results, as_json=True)
    else:
        for result in results:
            click.echo(
                f"{result['case_id']}\t{result['domain']}\t{result['expected_status']}"
                f"\t{result['actual_status']}\t{'pass' if result['passed'] else 'fail'}"
            )


@cli.command("eval-from-cluster")
@click.argument("cluster_id")
@click.pass_context
def eval_from_cluster(ctx: click.Context, cluster_id: str) -> None:
    """Generate a draft eval from an accepted FailureCluster."""
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    state = _load_failure_state(ctx.obj["root"])
    if state.get(cluster_id, {}).get("status") != "accepted":
        raise click.ClickException(f"cluster not accepted: {cluster_id}")
    clusters = {c.id: c for c in FailureAnalyzer(_ledger_dir(ctx.obj["root"])).analyze()}
    if cluster_id not in clusters:
        raise click.ClickException(f"cluster not found: {cluster_id}")
    c = clusters[cluster_id]
    case = {
        "id": f"eval_from_{cluster_id}",
        "domain": c.domain,
        "description": f"Replay of {c.fingerprint[:60]}",
        "task": f"Replay failure cluster {cluster_id}",
        "plan": [c.suggested_rubric_check or "no-op"],
        "expected_status": "blocked",
        "expected_warnings_contain": [],
        "expected_dead_ends": [],
        "status": "draft",
        "source_trace_ids": list(c.trace_ids),
    }
    p = _save_eval(ctx.obj["root"], case)
    click.echo(f"saved draft eval at {p}")


# ----- savings + benchmark ----------------------------------------------- #


@cli.command("login")
@click.option("--token", default=None, help="Credentials JSON, base64 payload, or refresh token.")
@click.option("--anonymous", "anonymous", is_flag=True, help="Start a local anonymous trial.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def login_cmd(ctx: click.Context, token: str | None, anonymous: bool, as_json: bool) -> None:
    """Create local Atelier auth state for plugin operations."""
    from atelier.core.capabilities.plugin_runtime import (
        begin_browser_login,
        claim_anonymous_trial,
        parse_login_token,
        write_auth_state,
    )

    if anonymous:
        payload = {"auth": claim_anonymous_trial(ctx.obj["root"]), "mode": "anonymous"}
    elif token:
        payload = {
            "auth": write_auth_state(ctx.obj["root"], parse_login_token(token)),
            "mode": "token",
        }
    else:
        pending = begin_browser_login(ctx.obj["root"])
        payload = {"mode": "browser", "pending": pending}
    if as_json:
        _emit(payload, as_json=True)
        return
    if str(payload.get("mode")) == "browser":
        pending_payload = payload.get("pending")
        pending = pending_payload if isinstance(pending_payload, dict) else {}
        click.echo("Open this URL to finish login:")
        click.echo(pending.get("url", ""))
    else:
        auth_payload = payload.get("auth")
        auth = auth_payload if isinstance(auth_payload, dict) else {}
        label = "anonymous trial" if auth.get("isAnonymous") else auth.get("email") or auth.get("userId")
        click.echo(f"logged in: {label}")


@cli.command("logout")
@click.option("--no-trial", is_flag=True, help="Do not create a local anonymous trial after logout.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def logout_cmd(ctx: click.Context, no_trial: bool, as_json: bool) -> None:
    """Remove local auth and optionally activate an anonymous trial."""
    from atelier.core.capabilities.plugin_runtime import logout_local

    payload = logout_local(ctx.obj["root"], claim_trial=not no_trial)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("logged out" + ("; anonymous trial active" if payload.get("anonymous") else ""))


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON of runs data.")
@click.option("--line", "line_mode", is_flag=True, help="One-liner mode (good for status bars).")
@click.option("-n", type=int, default=5, show_default=True, help="Number of recent runs to show.")
@click.option("--session-id", default=None, help="Show detail for a specific run only.")
@click.option("--auth", "auth_mode", is_flag=True, help="Show auth/subscription status instead of runs.")
@click.pass_context
def status_cmd(
    ctx: click.Context,
    as_json: bool,
    line_mode: bool,
    n: int,
    session_id: str | None,
    auth_mode: bool,
) -> None:
    """Show runs dashboard or auth status.

    Default view: runs dashboard (overview of recent runs, totals, savings).

    Use --auth to show the old auth/subscription status.
    """
    root: Path = ctx.obj["root"]

    if auth_mode:
        from atelier.core.capabilities.plugin_runtime import auth_status, load_plugin_settings

        payload = auth_status(root)
        payload["settings"] = load_plugin_settings(root)
        if as_json:
            _emit(payload, as_json=True)
            return
        click.echo(f"authenticated: {payload['authenticated']}")
        click.echo(f"anonymous: {payload['isAnonymous']}")
        if payload.get("email"):
            click.echo(f"email: {payload['email']}")
        if payload.get("subscription"):
            click.echo(f"subscription: {payload['subscription']}")
        click.echo(f"root: {payload['root']}")
        return

    if as_json:
        runs_dir = root / "runs"
        if session_id:
            target = runs_dir / f"{session_id}.json"
        else:
            files = sorted(runs_dir.glob("*.json"), key=os.path.getmtime, reverse=True)
            target = files[0] if files else None
        if target and target.exists():
            click.echo(target.read_text().strip())
        else:
            click.echo("{}")
        return

    _render_dashboard(root, line_mode=line_mode, n_runs=n, session_id=session_id)


@cli.command("share")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def share_cmd(ctx: click.Context, as_json: bool) -> None:
    """Render local referral/share text."""
    from atelier.core.capabilities.plugin_runtime import share_referral

    payload = share_referral(ctx.obj["root"])
    if payload.get("is_error"):
        raise click.ClickException(str(payload["message"]))
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["text"])


@cli.group("settings")
def plugin_settings_group() -> None:
    """Manage local plugin settings."""


@plugin_settings_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_show(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.plugin_runtime import load_plugin_settings

    payload = load_plugin_settings(ctx.obj["root"])
    if as_json:
        _emit(payload, as_json=True)
        return
    for key, value in payload.items():
        click.echo(f"{key}: {str(value).lower()}")


@plugin_settings_group.command("set")
@click.argument("key")
@click.argument("value", type=click.Choice(["true", "false", "on", "off", "1", "0"]))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_set(ctx: click.Context, key: str, value: str, as_json: bool) -> None:
    from atelier.core.capabilities.plugin_runtime import write_plugin_setting

    enabled = value in {"true", "on", "1"}
    try:
        payload = write_plugin_setting(ctx.obj["root"], key, enabled)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"set {key}={str(enabled).lower()}")


# Register relocated command modules (Phase 25 commands substrate). Each
# extracted commands/*.py group is import-and-add_command'd onto ``cli`` here via
# the resilient ``register`` aggregator (try/except ModuleNotFoundError).
_register_command_modules(cli)


# --------------------------------------------------------------------------- #
# V3 capability commands                                                      #
# --------------------------------------------------------------------------- #


@_dev_command("detect-loop")
@click.option("--session-id", default=None, help="Specific session ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def detect_loop_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Detect loops, repeated failures, and dead-end trajectories in a run ledger."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(session_id=session_id)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"loop_detected: {payload['loop_detected']}")
    click.echo(f"severity: {payload['severity']}")
    click.echo(f"loop_types: {', '.join(payload['loop_types']) or 'none'}")
    click.echo(f"prior_attempts: {payload['prior_attempts']}")
    if payload["rescue_strategies"]:
        click.echo("rescue_strategies:")
        for s in payload["rescue_strategies"]:
            click.echo(f"  - {s}")


@cli.command("loop-report")
@click.option("--session-id", default=None, help="Specific session ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def loop_report_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Full loop analysis: signature, severity, alerts, rescue strategies."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(session_id=session_id)
    _emit(payload, as_json=True) if as_json else click.echo(json.dumps(payload, indent=2))


@cli.command("tool-report")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def tool_report_cmd(ctx: click.Context, as_json: bool) -> None:
    """Tool usage + savings summary including redundancy analysis."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.tool_report()
    if as_json:
        _emit(payload, as_json=True)
        return
    metrics = payload.get("metrics", {})
    click.echo(f"total_tool_calls: {metrics.get('total_tool_calls', 0)}")
    click.echo(f"avoided_tool_calls: {metrics.get('avoided_tool_calls', 0)}")
    click.echo(f"token_savings: {metrics.get('token_savings', 0)}")
    click.echo(f"cache_hit_rate: {metrics.get('cache_hit_rate', 0)}")
    recs = payload.get("recommendations", [])
    if recs:
        click.echo("recommendations:")
        for r in recs:
            click.echo(f"  - {r}")


def main() -> None:
    _bench_bootstrap()  # Freeze ATELIER_BENCH_MODE before any lazy init (MODE-05)
    command_name = _cli_command_name(sys.argv[1:])
    session_id, started_at = _begin_cli_telemetry(command_name)
    old_handlers: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        _emit_cli_interrupted(
            session_id=session_id,
            started_at=started_at,
            signum=signum,
            command_name=command_name,
        )
        previous = old_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)

    try:
        try:
            cli(obj={"_telemetry_session_id": session_id, "_telemetry_command_name": command_name})
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=code == 0,
                exit_reason="success" if code == 0 else "error",
            )
            raise
        except KeyboardInterrupt:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=False,
                exit_reason="interrupted",
            )
            raise
        except BaseException:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=False,
                exit_reason="error",
            )
            raise
        else:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=True,
                exit_reason="success",
            )
    finally:
        from atelier.core.service.telemetry import shutdown_otel

        shutdown_otel()


# --------------------------------------------------------------------------- #
# team                                                                         #
# --------------------------------------------------------------------------- #


@cli.group("team")
def team_group() -> None:
    """Manage local team workspace state."""


@team_group.command("init")
@click.option("--name", required=True, help="Workspace display name.")
@click.option("--admin-email", default="admin@local", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_init_cmd(ctx: click.Context, name: str, admin_email: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    workspace = TeamWorkspaceManager(ctx.obj["root"]).init_workspace(name=name, admin_email=admin_email)
    payload = workspace.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"initialized workspace {workspace.name} ({workspace.id})")


@team_group.command("invite")
@click.argument("emails", nargs=-1)
@click.option("--role", type=click.Choice(["member", "viewer", "admin"]), default="member", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_invite_cmd(ctx: click.Context, emails: tuple[str, ...], role: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    if not emails:
        raise click.ClickException("provide at least one email")
    invites = TeamWorkspaceManager(ctx.obj["root"]).invite_members(list(emails), role=role)  # type: ignore[arg-type]
    payload = [invite.model_dump(mode="json") for invite in invites]
    if as_json:
        _emit(payload, as_json=True)
        return
    for invite in invites:
        click.echo(f"{invite.email}\t{invite.role}\t{invite.code}")


@team_group.command("join")
@click.argument("invite_code")
@click.option("--user-id", default=None, help="Override the invite email as the local user id.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_join_cmd(ctx: click.Context, invite_code: str, user_id: str | None, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    member = TeamWorkspaceManager(ctx.obj["root"]).join_workspace(invite_code, user_id=user_id)
    payload = member.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"joined workspace as {member.user_id} ({member.role})")


@team_group.command("role")
@click.argument("user_id")
@click.argument("role", type=click.Choice(["admin", "member", "viewer"]))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_role_cmd(ctx: click.Context, user_id: str, role: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    member = TeamWorkspaceManager(ctx.obj["root"]).set_role(user_id, role)  # type: ignore[arg-type]
    payload = member.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{member.user_id}\t{member.role}")


@team_group.command("usage")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_usage_cmd(ctx: click.Context, since: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager, summarize_workspace_usage

    manager = TeamWorkspaceManager(ctx.obj["root"])
    manager.require_admin()
    payload = summarize_workspace_usage(ctx.obj["root"], manager=manager, since=_parse_since_arg(since))
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"workspace: {payload['workspace_id']}")
    click.echo(f"sessions: {payload['session_count']}")
    click.echo(f"total cost usd: {payload['total_cost_usd']:.6f}")
    for row in payload["users"]:
        click.echo(f"{row['user_id']}\t{row['role']}\t{row['session_count']}\t{row['total_cost_usd']:.6f}")


@team_group.command("audit")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_audit_cmd(ctx: click.Context, since: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    manager.require_admin()
    events = manager.list_audit_events(since=_parse_since_arg(since))
    payload = [event.model_dump(mode="json") for event in events]
    if as_json:
        _emit(payload, as_json=True)
        return
    if not events:
        click.echo("(no team audit events)")
        return
    for event in events:
        click.echo(f"{event.at.isoformat()}\t{event.action}\t{event.actor_user_id}")


# --------------------------------------------------------------------------- #
# governance                                                                   #
# --------------------------------------------------------------------------- #


@cli.group("governance")
def governance_group() -> None:
    """Inspect and apply workspace governance policy."""


@governance_group.command("show")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def governance_show_cmd(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.governance import load_policy

    policy = load_policy(ctx.obj["root"])
    payload = policy.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(yaml.safe_dump(payload, sort_keys=True).rstrip())


@governance_group.command("apply")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def governance_apply_cmd(ctx: click.Context, file_path: Path, as_json: bool) -> None:
    from atelier.core.capabilities.governance import GovernancePolicy, save_policy
    from atelier.core.capabilities.team import TeamAuditEvent, TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    member = manager.require_admin()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    policy = GovernancePolicy.model_validate(loaded)
    saved = save_policy(ctx.obj["root"], policy)
    manager.append_audit_event(
        TeamAuditEvent(
            action="governance.apply",
            actor_user_id=member.user_id,
            details={"source": str(file_path)},
        )
    )
    payload = saved.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"applied governance policy from {file_path}")


# --------------------------------------------------------------------------- #
# audit export                                                                 #
# --------------------------------------------------------------------------- #


@cli.group("audit")
def audit_group() -> None:
    """Export and verify workspace audit bundles."""


@audit_group.command("export")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--out", "out_dir", required=True, type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def audit_export_cmd(ctx: click.Context, since: str, out_dir: Path, as_json: bool) -> None:
    from atelier.core.capabilities.audit_export import export_audit_bundle
    from atelier.core.capabilities.team import TeamAuditEvent, TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    member = manager.require_admin()
    payload = export_audit_bundle(ctx.obj["root"], out_dir=out_dir, since=_parse_since_arg(since))
    manager.append_audit_event(
        TeamAuditEvent(
            action="audit.export",
            actor_user_id=member.user_id,
            details={"bundle_dir": payload["bundle_dir"]},
        )
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["bundle_dir"])


@audit_group.command("verify")
@click.argument("bundle_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def audit_verify_cmd(ctx: click.Context, bundle_dir: Path, as_json: bool) -> None:
    from atelier.core.capabilities.audit_export import verify_audit_bundle

    payload = verify_audit_bundle(ctx.obj["root"], bundle_dir=bundle_dir)
    if as_json:
        _emit(payload, as_json=True)
        return
    if payload["valid"]:
        click.echo(f"verified {bundle_dir}")
        return
    raise click.ClickException(
        f"bundle verification failed: {', '.join(payload['tampered_files']) or 'signature mismatch'}"
    )


# --------------------------------------------------------------------------- #
# insights                                                                     #
# --------------------------------------------------------------------------- #


def _parse_since_arg(value: str) -> datetime:
    """Parse ``--since`` argument.

    Accepts:
    * ``7d``, ``30d``, ``24h``, ``30m``  - duration relative to now
    * ``YYYY-MM-DD``                       - absolute date (start of day UTC)
    """
    import re
    from datetime import UTC, datetime, timedelta

    stripped = value.strip()
    # Relative duration (e.g. "7d", "24h", "30m")
    match = re.fullmatch(r"(\d+)([dhm])", stripped)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = (
            timedelta(days=amount)
            if unit == "d"
            else timedelta(hours=amount) if unit == "h" else timedelta(minutes=amount)
        )
        return datetime.now(UTC) - delta

    # Absolute date (YYYY-MM-DD)
    try:
        return datetime.strptime(stripped, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        pass

    raise click.ClickException(
        f"Cannot parse --since value {value!r}. " "Use a duration like '7d', '24h', or a date like '2026-05-01'."
    )


@cli.command("insights")
@click.option(
    "--since",
    default="7d",
    show_default=True,
    help="Time window: '7d', '30d', '24h', or a date like '2026-05-01'.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colours.")
@click.option(
    "--vendor",
    default=None,
    help="Filter output to a specific vendor (e.g. 'anthropic').",
)
@click.option(
    "--group-by",
    "group_by",
    default="tool",
    type=click.Choice(["tool", "vendor", "model", "session"]),
    show_default=True,
    help="Primary grouping for cost breakdown.",
)
@click.pass_context
def insights_cmd(
    ctx: click.Context,
    since: str,
    as_json: bool,
    no_color: bool,
    vendor: str | None,
    group_by: str,
) -> None:
    """Weekly AI-spend insights and savings opportunities."""
    from datetime import UTC, datetime

    from atelier.infra.runtime.insights import (
        InsightsWindow,
        build_insights,
        render_json,
        render_text,
    )

    root: Path = ctx.obj["root"]
    since_dt = _parse_since_arg(since)
    until_dt = datetime.now(UTC)

    window: InsightsWindow = build_insights(root, since=since_dt, until=until_dt)

    if window.session_count == 0:
        if as_json:
            click.echo(render_json(window))
        else:
            since_str = since_dt.strftime("%Y-%m-%d")
            click.echo(f"No sessions found since {since_str}.")
        return

    # Apply vendor filter to cost_by_vendor display (full window still computed).
    if vendor and not as_json:
        vendor_key = vendor.capitalize()
        filtered_cost = window.cost_by_vendor.get(vendor_key, 0.0)
        click.echo(f"Vendor filter: {vendor_key}  ${filtered_cost:.2f}" f" of ${window.total_cost_usd:.2f} total")

    # Apply group-by override for display (swap cost_by_* fields shown).
    display_window = window
    if group_by == "vendor" and not as_json:
        # Reorder: show vendor bars prominently (already first in default render).
        pass
    elif group_by == "model" and not as_json:
        # Swap cost_by_tool -> cost_by_model for the tool section.

        display_window = InsightsWindow(
            since=window.since,
            until=window.until,
            session_count=window.session_count,
            total_duration_seconds=window.total_duration_seconds,
            total_cost_usd=window.total_cost_usd,
            total_atelier_savings_usd=window.total_atelier_savings_usd,
            cost_by_vendor=window.cost_by_vendor,
            cost_by_tool=window.cost_by_model,
            cost_by_model=window.cost_by_model,
            top_sessions=window.top_sessions,
            outcomes_summary=window.outcomes_summary,
            opportunities=window.opportunities,
        )
    elif group_by == "session" and not as_json:
        # Replace cost_by_tool with per-session breakdown.
        session_costs = {s.session_id[:8]: s.cost_usd for s in window.top_sessions}
        display_window = InsightsWindow(
            since=window.since,
            until=window.until,
            session_count=window.session_count,
            total_duration_seconds=window.total_duration_seconds,
            total_cost_usd=window.total_cost_usd,
            total_atelier_savings_usd=window.total_atelier_savings_usd,
            cost_by_vendor=window.cost_by_vendor,
            cost_by_tool=session_costs,
            cost_by_model=window.cost_by_model,
            top_sessions=window.top_sessions,
            outcomes_summary=window.outcomes_summary,
            opportunities=window.opportunities,
        )

    if as_json:
        click.echo(render_json(window))
    else:
        click.echo(render_text(display_window, no_color=no_color))


if __name__ == "__main__":
    main()
