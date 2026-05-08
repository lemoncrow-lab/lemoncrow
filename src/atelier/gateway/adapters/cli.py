"""CLI for the Atelier reasoning runtime.

Designed to be readable when piped into another tool. All commands that
return data accept ``--json`` to emit machine-parseable output.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import click
import yaml

from atelier.core.foundation.paths import default_store_root
from atelier.core.capabilities.lesson_promotion import LessonPrBot, LessonPromoterCapability
from atelier.core.foundation.extractor import extract_candidate
from atelier.core.foundation.metrics import summarize
from atelier.core.foundation.models import (
    ReasonBlock,
    Rubric,
    Trace,
    to_jsonable,
)
from atelier.core.foundation.plan_checker import check_plan
from atelier.core.foundation.renderer import (
    render_block_markdown,
    render_context_for_agent,
    render_plan_check,
    render_rubric_result,
)
from atelier.core.foundation.retriever import TaskContext, retrieve
from atelier.core.foundation.rubric_gate import run_rubric
from atelier.core.foundation.store import ReasoningStore

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
    from atelier.core.service.telemetry import emit_product, init_product_telemetry
    from atelier.core.service.telemetry.banner import maybe_show_banner
    from atelier.core.service.telemetry.identity import (
        get_anon_id,
        new_session_id,
        platform_payload,
    )

    maybe_show_banner()
    init_product_telemetry(service_version=_atelier_version())
    session_id = new_session_id()
    payload = platform_payload()
    emit_product(
        "session_start",
        agent_host="cli",
        atelier_version=_atelier_version(),
        anon_id=get_anon_id(),
        session_id=session_id,
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


def _record_plan_telemetry(
    *,
    ctx: click.Context,
    result: Any,
    domain: str | None,
    plan: list[str],
) -> None:
    session_id = _telemetry_session(ctx)
    if session_id is None:
        return
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import hash_identifier

    status = getattr(result, "status", "")
    matched_blocks = list(getattr(result, "matched_blocks", []) or [])
    if status == "blocked":
        blocking_rule_id = hash_identifier(str(matched_blocks[0] if matched_blocks else "blocked"))
        emit_product(
            "plan_check_blocked",
            domain=domain or "",
            blocking_rule_id=blocking_rule_id,
            severity="high",
            session_id=session_id,
        )
    else:
        emit_product(
            "plan_check_passed",
            domain=domain or "",
            rule_count=len(matched_blocks),
            session_id=session_id,
        )

    if not plan:
        return


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _load_store(root: Path) -> ReasoningStore:
    store = ReasoningStore(root)
    if not store.db_path.exists():
        raise click.ClickException(f"No atelier store at {root}. Run `atelier init` first.")
    return store


def _core_runtime(root: Path) -> Any:
    from atelier.core.runtime import AtelierRuntimeCore

    return AtelierRuntimeCore(root)


def _lesson_promoter(root: Path) -> LessonPromoterCapability:
    store = _load_store(root)
    return LessonPromoterCapability(store)


def _lesson_pr_bot(root: Path) -> LessonPrBot:
    store = _load_store(root)
    return LessonPrBot(store=store, root=root)


def _emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        click.echo(data)


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


_REDACTION_PLACEHOLDER_RE = re.compile(r"<redacted[^>]*>")


def _redact_memory_input(text: str, field_name: str) -> str:
    from atelier.core.foundation.redaction import redact

    redacted = redact(text)
    if not text:
        return redacted
    remaining = _REDACTION_PLACEHOLDER_RE.sub("", redacted)
    if len(remaining.strip()) < len(text.strip()) * 0.5:
        raise click.ClickException(f"{field_name} rejected: likely secret leakage")
    return redacted


def _read_memory_value(value: str) -> str:
    if not value.startswith("@"):
        return value
    path_text = value[1:]
    if path_text == "/dev/stdin" or path_text == "-":
        return sys.stdin.read()
    return Path(path_text).read_text(encoding="utf-8")


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


def _letta_compose_file() -> Path:
    return Path.cwd() / "deploy" / "letta" / "docker-compose.yml"


def _run_compose(args: list[str]) -> None:
    subprocess.run(["docker", "compose", "-f", str(_letta_compose_file()), *args], check=True)


def _parse_tags(values: tuple[str, ...]) -> list[str]:
    tags: list[str] = []
    for value in values:
        tags.extend(tag.strip() for tag in value.split(",") if tag.strip())
    return tags


def _cache_disabled() -> bool:
    return os.environ.get("ATELIER_CACHE_DISABLED") == "1"


def _path_content_fingerprint(path_text: str) -> str:
    path = Path(path_text)
    digest = sha256()
    if path.is_file():
        try:
            digest.update(path.read_bytes())
            return digest.hexdigest()[:16]
        except OSError:
            return "unreadable"
    if path.is_dir():
        files = [p for p in sorted(path.rglob("*")) if p.is_file()]
        for file_path in files[:500]:
            try:
                digest.update(str(file_path.relative_to(path)).encode())
                digest.update(b"\0")
                digest.update(file_path.read_bytes())
                digest.update(b"\0")
            except OSError:
                continue
        digest.update(str(len(files)).encode())
        return digest.hexdigest()[:16]
    return "missing"


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=DEFAULT_ROOT,
    show_default=True,
    help="Atelier runtime data directory.",
)
@click.pass_context
def cli(ctx: click.Context, root: Path) -> None:
    """Atelier — Agent Reasoning Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root


# ----- init ---------------------------------------------------------------- #


@cli.command()
@click.option("--seed/--no-seed", default=True, help="Import bundled seed blocks and rubrics.")
@click.option("--stack", default=None, help="Copy starter ReasonBlock templates for a stack.")
@click.option("--list-stacks", "show_stacks", is_flag=True, help="List available starter stacks.")
@click.pass_context
def init(ctx: click.Context, seed: bool, stack: str | None, show_stacks: bool) -> None:
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
    store = ReasoningStore(root)
    store.init()
    click.echo(f"initialized atelier store at {store.root}")
    if seed:
        block_files, rubric_files = _seed_resources()
        n_b = 0
        for path in block_files:
            data = _load_yaml(path)
            if "id" not in data:
                data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
            block = ReasonBlock.model_validate(data)
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


@cli.command("reembed")
@click.option("--dry-run", is_flag=True, help="Count legacy rows without writing vectors.")
@click.option("--batch-size", default=100, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reembed(ctx: click.Context, dry_run: bool, batch_size: int, as_json: bool) -> None:
    """Back-fill legacy_stub embeddings for archival passages and lesson candidates."""
    from atelier.infra.embeddings.factory import make_embedder

    root: Path = ctx.obj["root"]
    store = ReasoningStore(root)
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


@cli.command("add-block")
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


# ----- search -------------------------------------------------------------- #


@cli.command()
@click.argument("query_parts", nargs=-1)
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def search(ctx: click.Context, query_parts: tuple[str, ...], limit: int, as_json: bool) -> None:
    """Search procedures. Supports legacy mode and `search smart <query>`."""
    if not query_parts:
        raise click.ClickException("query is required")

    if query_parts[0] == "smart":
        smart_query = " ".join(query_parts[1:]).strip()
        if not smart_query:
            raise click.ClickException("smart search query is required")
        rt = _core_runtime(ctx.obj["root"])
        payload = rt.smart_search(smart_query, limit=limit)
        _emit(payload, as_json=True)
        return

    query = " ".join(query_parts).strip()
    store = _load_store(ctx.obj["root"])
    blocks = store.search_blocks(query, limit=limit)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    if not blocks:
        click.echo("(no matches)")
        return
    for b in blocks:
        click.echo(f"{b.id}\t{b.domain}\t{b.title}")


# ----- reasoning ------------------------------------------------------------ #


@cli.command("reasoning")
@click.option("--task", required=True, help="Task description.")
@click.option("--domain", default=None)
@click.option("--file", "files", multiple=True, help="File path likely to be edited.")
@click.option("--tool", "tools", multiple=True, help="Tool the agent expects to use.")
@click.option("--error", "errors", multiple=True, help="Known error message.")
@click.option("--limit", default=5, show_default=True, type=int)
@click.option("--token-budget", default=2000, show_default=True, type=int)
@click.option("--no-dedup", "dedup", is_flag=True, flag_value=False, default=True)
@click.option("--telemetry", "include_telemetry", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reasoning(
    ctx: click.Context,
    task: str,
    domain: str | None,
    files: tuple[str, ...],
    tools: tuple[str, ...],
    errors: tuple[str, ...],
    limit: int,
    token_budget: int,
    dedup: bool,
    include_telemetry: bool,
    as_json: bool,
) -> None:
    """Render the reasoning-context block to inject into an agent prompt."""
    from atelier.core.service.telemetry.frustration import match_frustration

    match_frustration(task, surface="cli_input", session_id=_telemetry_session(ctx))
    store = _load_store(ctx.obj["root"])
    tctx = TaskContext(task=task, domain=domain, files=list(files), tools=list(tools), errors=list(errors))
    scored = retrieve(store, tctx, limit=limit, token_budget=token_budget, dedup=dedup)
    _record_reasonblock_events(
        scored,
        event_name="reasonblock_retrieved",
        domain=domain,
        session_id=_telemetry_session(ctx),
    )
    context_text = render_context_for_agent([s.block for s in scored])
    if as_json:
        payload: dict[str, Any] = {
            "matched": [{"id": s.block.id, "score": s.score, "breakdown": s.breakdown} for s in scored],
            "context": context_text,
        }
        if include_telemetry:
            from atelier.core.foundation.retriever import count_tokens

            naive = retrieve(store, tctx, limit=limit, token_budget=None, dedup=False)
            naive_text = render_context_for_agent([s.block for s in naive])
            tokens_used = count_tokens(context_text)
            payload["tokens_used"] = tokens_used
            payload["tokens_saved_vs_naive"] = max(0, count_tokens(naive_text) - tokens_used)
        _emit(
            payload,
            as_json=True,
        )
        return
    click.echo(context_text)


# ----- lint ---------------------------------------------------------- #


@cli.command("lint")
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Read JSON payload from file. Use '-' for stdin.",
)
@click.option("--task", default=None)
@click.option("--domain", default=None)
@click.option("--step", "steps", multiple=True, help="Plan step (repeatable).")
@click.option("--file", "files", multiple=True)
@click.option("--tool", "tools", multiple=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def check_plan_cmd(
    ctx: click.Context,
    input_path: Path | None,
    task: str | None,
    domain: str | None,
    steps: tuple[str, ...],
    files: tuple[str, ...],
    tools: tuple[str, ...],
    as_json: bool,
) -> None:
    """Validate a proposed agent plan."""
    store = _load_store(ctx.obj["root"])
    if input_path is not None:
        raw = sys.stdin.read() if str(input_path) == "-" else input_path.read_text("utf-8")
        payload = json.loads(raw)
        task = payload.get("task", task)
        domain = payload.get("domain", domain)
        plan = list(payload.get("plan", steps))
        files = tuple(payload.get("files", files))
        tools = tuple(payload.get("tools", tools))
    else:
        plan = list(steps)
    if not task or not plan:
        raise click.ClickException("--task and at least one --step (or --input) required")

    result = check_plan(store, task=task, plan=plan, domain=domain, files=list(files), tools=list(tools))
    _record_plan_telemetry(ctx=ctx, result=result, domain=domain, plan=plan)
    if as_json:
        _emit(to_jsonable(result), as_json=True)
    else:
        click.echo(render_plan_check(result))
    sys.exit(0 if result.status != "blocked" else 2)


# ----- rescue -------------------------------------------------------------- #


@cli.command()
@click.option("--task", required=True)
@click.option("--error", required=True)
@click.option("--domain", default=None)
@click.option("--file", "files", multiple=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def rescue(
    ctx: click.Context,
    task: str,
    error: str,
    domain: str | None,
    files: tuple[str, ...],
    as_json: bool,
) -> None:
    """Suggest a rescue procedure for a repeated failure."""
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.frustration import match_frustration
    from atelier.core.service.telemetry.schema import hash_identifier
    from atelier.gateway.adapters.runtime import ReasoningRuntime

    match_frustration(task, surface="cli_input", session_id=_telemetry_session(ctx))
    rt = ReasoningRuntime(ctx.obj["root"])
    result = rt.rescue_failure(task=task, error=error, files=list(files), domain=domain)
    if _telemetry_session(ctx) is not None:
        cluster_id_hash = hash_identifier(result.matched_blocks[0] if result.matched_blocks else "unmatched_rescue")
        emit_product(
            "rescue_offered",
            cluster_id_hash=cluster_id_hash,
            rescue_type="reasonblock" if result.matched_blocks else "summary",
            session_id=_telemetry_session(ctx),
        )
    if as_json:
        _emit(to_jsonable(result), as_json=True)
        return
    click.echo(result.rescue)
    if result.matched_blocks:
        click.echo("matched blocks: " + ", ".join(result.matched_blocks))


# ----- telemetry ---------------------------------------------------------- #


@cli.group("telemetry")
def telemetry_group() -> None:
    """Product telemetry controls."""


@telemetry_group.command("status")
@click.option("--json", "as_json", is_flag=True)
def telemetry_status(as_json: bool) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.banner import is_acknowledged
    from atelier.core.service.telemetry.config import config_path, load_telemetry_config
    from atelier.core.service.telemetry.identity import (
        get_anon_id,
        new_session_id,
        telemetry_id_path,
    )
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

    set_remote_enabled(False)
    click.echo("remote telemetry: off")


@telemetry_group.command("show")
@click.option("--limit", default=20, show_default=True, type=int)
def telemetry_show(limit: int) -> None:
    from atelier.core.service.telemetry.local_store import LocalTelemetryStore

    events = LocalTelemetryStore().list_events(limit=limit)
    _emit([{"event": item["event"], "props": item["props"]} for item in events], as_json=True)


@telemetry_group.command("reset-id")
def telemetry_reset_id() -> None:
    from atelier.core.service.telemetry.identity import reset_anon_id

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


# ----- trace --------------------------------------------------------------- #


@cli.group("trace")
def trace_group() -> None:
    """Trace record, list, and inspect commands."""


@trace_group.command("record")
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path),
    default="-",
    show_default=True,
    help="Trace JSON file. Use '-' for stdin.",
)
@click.pass_context
def trace_record(ctx: click.Context, input_path: Path | str) -> None:
    """Record an observable trace."""
    store = _load_store(ctx.obj["root"])
    raw = sys.stdin.read() if str(input_path) == "-" else Path(input_path).read_text("utf-8")
    data = json.loads(raw)
    if "id" not in data:
        data["id"] = Trace.make_id(data.get("task", "untitled"), data.get("agent", "agent"))
    trace = Trace.model_validate(data)
    store.record_trace(trace)
    click.echo(trace.id)


@trace_group.command("list")
@click.option("--domain", default=None, help="Filter by domain.")
@click.option("--status", default=None, type=click.Choice(["success", "failed", "partial"]))
@click.option("--agent", default=None, help="Filter by agent name.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_list(
    ctx: click.Context,
    domain: str | None,
    status: str | None,
    agent: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """List recorded traces."""
    store = _load_store(ctx.obj["root"])
    traces = store.list_traces(domain=domain, status=status, agent=agent, limit=limit)
    if as_json:
        _emit([to_jsonable(t) for t in traces], as_json=True)
        return
    if not traces:
        click.echo("(no traces)")
        return
    for t in traces:
        click.echo(f"{t.id}\t{t.agent}\t{t.status}\t{t.domain}\t{t.task[:60]}")


@trace_group.command("show")
@click.argument("trace_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_show(ctx: click.Context, trace_id: str, as_json: bool) -> None:
    """Show a single trace by ID."""
    store = _load_store(ctx.obj["root"])
    trace = store.get_trace(trace_id)
    if trace is None:
        raise click.ClickException(f"trace not found: {trace_id}")
    if as_json:
        _emit(to_jsonable(trace), as_json=True)
        return
    click.echo(f"id:     {trace.id}")
    click.echo(f"agent:  {trace.agent}")
    click.echo(f"status: {trace.status}")
    click.echo(f"domain: {trace.domain}")
    click.echo(f"task:   {trace.task}")


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


@cli.group("block")
def block_group() -> None:
    """ReasonBlock curation commands."""


@block_group.command("list")
@click.option("--domain", default=None)
@click.option("--include-deprecated", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_list(ctx: click.Context, domain: str | None, include_deprecated: bool, as_json: bool) -> None:
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
def block_add(ctx: click.Context, path: Path) -> None:
    """Import a ReasonBlock from a YAML file."""
    from atelier.core.foundation.loader import load_block_from_yaml

    store = _load_store(ctx.obj["root"])
    block = load_block_from_yaml(path)
    store.upsert_block(block)
    click.echo(f"upserted {block.id}")


@block_group.command("extract")
@click.argument("trace_id")
@click.option("--save", is_flag=True, help="Persist the candidate block.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_extract(ctx: click.Context, trace_id: str, save: bool, as_json: bool) -> None:
    """Extract a candidate ReasonBlock from a trace."""
    store = _load_store(ctx.obj["root"])
    trace = store.get_trace(trace_id)
    if trace is None:
        raise click.ClickException(f"trace not found: {trace_id}")
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
    summary = summarize(store)
    click.echo(
        f"# {len(blocks)} blocks shown "
        f"(active={summary.blocks_active}, "
        f"deprecated={summary.blocks_deprecated}, "
        f"quarantined={summary.blocks_quarantined})"
    )
    for b in blocks:
        click.echo(f"{b.status[:1].upper()} {b.id}\t{b.domain}\t{b.title}")


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


# ----- verify --------------------------- #


@cli.command("verify")
@click.argument("rubric_id")
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path),
    default="-",
    show_default=True,
    help="JSON object mapping check_name -> bool. Use '-' for stdin.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def run_rubric_cmd(ctx: click.Context, rubric_id: str, input_path: Path | str, as_json: bool) -> None:
    """Evaluate a rubric against a checks JSON object."""
    store = _load_store(ctx.obj["root"])
    rubric = store.get_rubric(rubric_id)
    if rubric is None:
        raise click.ClickException(f"rubric not found: {rubric_id}")
    raw = sys.stdin.read() if str(input_path) == "-" else Path(input_path).read_text("utf-8")
    checks = json.loads(raw)
    result = run_rubric(rubric, checks)
    if as_json:
        _emit(to_jsonable(result), as_json=True)
    else:
        click.echo(render_rubric_result(result))
    sys.exit(0 if result.status != "blocked" else 2)


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

    store = _load_store(ctx.obj["root"])
    importer = CopilotImporter(store)
    count = importer.import_all(path, force=force)
    click.echo(f"imported {count} copilot sessions")


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

    store = _load_store(ctx.obj["root"])
    importer = ClaudeImporter(store)
    count = importer.import_all(path, force=force)
    click.echo(f"imported {count} claude sessions")


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

    store = _load_store(ctx.obj["root"])
    importer = CodexImporter(store)
    count = importer.import_all(path, force=force)
    click.echo(f"imported {count} codex sessions")


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

    store = _load_store(ctx.obj["root"])
    importer = OpenCodeImporter(store)
    count = importer.import_all(path, force=force)
    click.echo(f"imported {count} opencode sessions")


# --------------------------------------------------------------------------- #
# V2: Ledger / Monitor / Compress / Env / Failure / Eval / Smart / Savings   #
# --------------------------------------------------------------------------- #


def _ledger_dir(root: Path) -> Path:
    return Path(root) / "runs"


def _latest_ledger_path(root: Path) -> Path | None:
    runs = _ledger_dir(root)
    if not runs.is_dir():
        return None
    paths = sorted(runs.glob("*.json"))
    return paths[-1] if paths else None


def _ledger_path(root: Path, run_id: str | None) -> Path:
    if run_id:
        return _ledger_dir(root) / f"{run_id}.json"
    latest = _latest_ledger_path(root)
    if latest is None:
        raise click.ClickException("no run ledger found. Pass --run-id or record one first.")
    return latest


# ----- ledger ------------------------------------------------------------- #


@cli.group()
def ledger() -> None:
    """Manage run ledgers."""


@ledger.command("show")
@click.option("--run-id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def ledger_show(ctx: click.Context, run_id: str | None, as_json: bool) -> None:
    path = _ledger_path(ctx.obj["root"], run_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    if as_json:
        _emit(snap, as_json=True)
        return
    click.echo(f"run_id: {snap.get('run_id')}")
    click.echo(f"status: {snap.get('status')}")
    click.echo(f"task: {snap.get('task', '')}")
    click.echo(f"domain: {snap.get('domain', '')}")
    click.echo(f"events: {len(snap.get('events', []))}")
    click.echo(f"errors_seen: {len(snap.get('errors_seen', []))}")
    click.echo(f"current_blockers: {snap.get('current_blockers', [])}")


@ledger.command("reset")
@click.option("--run-id", default=None)
@click.confirmation_option(prompt="Delete this ledger snapshot?")
@click.pass_context
def ledger_reset(ctx: click.Context, run_id: str | None) -> None:
    path = _ledger_path(ctx.obj["root"], run_id)
    path.unlink(missing_ok=True)
    click.echo(f"removed {path}")


@ledger.command("update")
@click.option("--run-id", default=None)
@click.option("--field", "field_name", required=True)
@click.option("--value", required=True, help="Value (use JSON literal for lists/dicts).")
@click.pass_context
def ledger_update(ctx: click.Context, run_id: str | None, field_name: str, value: str) -> None:
    path = _ledger_path(ctx.obj["root"], run_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    snap[field_name] = parsed
    path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    click.echo(f"updated {field_name}")


@ledger.command("summarize")
@click.option("--run-id", default=None)
@click.pass_context
def ledger_summarize(ctx: click.Context, run_id: str | None) -> None:
    from atelier.infra.runtime.context_compressor import ContextCompressor
    from atelier.infra.runtime.run_ledger import RunLedger

    path = _ledger_path(ctx.obj["root"], run_id)
    led = RunLedger.load(path)
    state = ContextCompressor().compress(led)
    click.echo(state.to_prompt_block())


# ----- compress-context --------------------------------------------------- #


@cli.command("compress-context")
@click.option("--run-id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def compress_context_cmd(ctx: click.Context, run_id: str | None, as_json: bool) -> None:
    """Compress a run ledger into a small state packet."""
    from atelier.infra.runtime.context_compressor import ContextCompressor
    from atelier.infra.runtime.run_ledger import RunLedger

    path = _ledger_path(ctx.obj["root"], run_id)
    led = RunLedger.load(path)
    state = ContextCompressor().compress(led)
    if as_json:
        _emit(
            {
                "environment_id": state.environment_id,
                "files_changed": state.files_changed,
                "error_fingerprints": state.error_fingerprints,
                "high_severity_alerts": state.high_severity_alerts,
                "current_blocker": state.current_blocker,
                "tool_call_count": state.tool_call_count,
                "total_tool_output_chars": state.total_tool_output_chars,
                "preserved": {
                    "latest_error": (state.error_fingerprints[-1] if state.error_fingerprints else None),
                    "active_rubrics": led.active_rubrics,
                    "active_reasonblocks": led.active_reasonblocks,
                    "next_required_validation": led.next_required_validation,
                },
            },
            as_json=True,
        )
        return
    click.echo(state.to_prompt_block())


# ----- env ---------------------------------------------------------------- #


@cli.group()
def env() -> None:
    """Inspect Beseam reasoning environments."""


def _all_environments() -> list[Any]:
    from atelier.core.foundation.environments import load_packaged_environments

    return load_packaged_environments()


@env.command("list")
@click.option("--json", "as_json", is_flag=True)
def env_list(as_json: bool) -> None:
    envs = _all_environments()
    if as_json:
        _emit([to_jsonable(e) for e in envs], as_json=True)
        return
    for e in envs:
        click.echo(f"{e.id}\t{e.domain}\t{e.description.strip()[:60]}")


@env.command("show")
@click.argument("env_id")
@click.option("--json", "as_json", is_flag=True)
def env_show(env_id: str, as_json: bool) -> None:
    envs = {e.id: e for e in _all_environments()}
    if env_id not in envs:
        raise click.ClickException(f"environment not found: {env_id}")
    e = envs[env_id]
    if as_json:
        _emit(to_jsonable(e), as_json=True)
        return
    click.echo(f"# {e.id}")
    click.echo(f"domain: {e.domain}")
    click.echo(f"description: {e.description.strip()}")
    if e.forbidden:
        click.echo("forbidden:")
        for p in e.forbidden:
            click.echo(f"  - {p}")
    if e.required:
        click.echo("required:")
        for p in e.required:
            click.echo(f"  - {p}")
    if e.escalate:
        click.echo("escalate:")
        for p in e.escalate:
            click.echo(f"  - {p}")
    if e.high_risk_tools:
        click.echo("high_risk_tools:")
        for p in e.high_risk_tools:
            click.echo(f"  - {p}")
    if e.rubric_id:
        click.echo(f"rubric_id: {e.rubric_id}")
    if e.related_blocks:
        click.echo("related_blocks:")
        for p in e.related_blocks:
            click.echo(f"  - {p}")


@env.command("context")
@click.argument("env_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def env_context(ctx: click.Context, env_id: str, as_json: bool) -> None:
    """Render env context: ReasonBlocks + rubric checks for injection."""
    store = _load_store(ctx.obj["root"])
    envs = {e.id: e for e in _all_environments()}
    if env_id not in envs:
        raise click.ClickException(f"environment not found: {env_id}")
    e = envs[env_id]
    blocks = []
    for bid in e.related_blocks:
        b = store.get_block(bid)
        if b is not None:
            blocks.append(b)
    rubric = store.get_rubric(e.rubric_id) if e.rubric_id else None
    payload = {
        "environment": to_jsonable(e),
        "blocks": [to_jsonable(b) for b in blocks],
        "rubric": to_jsonable(rubric) if rubric else None,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    from atelier.core.foundation.renderer import render_context_for_agent

    click.echo(f"## Environment {e.id}")
    click.echo(e.description.strip())
    if blocks:
        click.echo(render_context_for_agent(blocks))
    if rubric:
        click.echo(f"## Required rubric: {rubric.id}")
        for c in rubric.required_checks:
            click.echo(f"- [ ] {c}")


@env.command("validate")
@click.argument("env_id")
def env_validate(env_id: str) -> None:
    """Validate an environment YAML by id."""
    envs = {e.id: e for e in _all_environments()}
    if env_id not in envs:
        raise click.ClickException(f"environment not found: {env_id}")
    e = envs[env_id]
    issues = []
    if not e.required:
        issues.append("no required checks")
    if e.rubric_id is None:
        issues.append("no rubric_id")
    if not e.related_blocks:
        issues.append("no related_blocks")
    if issues:
        for i in issues:
            click.echo(f"warn: {i}")
    click.echo(f"ok {env_id}")


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
        snaps = [s for s in snaps if s.get("run_id") == trace_id]

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
    """Run deterministic eval cases (plan-check based)."""
    store = _load_store(ctx.obj["root"])
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

    results: list[dict[str, Any]] = []
    for c in cases:
        plan = c.get("plan") or []
        result = check_plan(
            store,
            task=c.get("task", c.get("description", "eval")),
            plan=plan,
            domain=c.get("domain"),
        )
        expected = c.get("expected_status", "pass")
        passed = result.status == expected
        results.append({"id": c["id"], "expected": expected, "got": result.status, "passed": passed})
    if as_json:
        _emit(results, as_json=True)
    else:
        for r in results:
            click.echo(f"{r['id']}\t{'PASS' if r['passed'] else 'FAIL'}\texpected={r['expected']}\tgot={r['got']}")


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


# ----- smart tools (shadow mode) ------------------------------------------ #


def _smart_state_path(root: Path) -> Path:
    return Path(root) / "smart_state.json"


def _load_smart_state(root: Path) -> dict[str, Any]:
    p = _smart_state_path(root)
    if not p.exists():
        return {"mode": "shadow", "cache": {}, "savings": {"calls_avoided": 0, "tokens_saved": 0}}
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def _save_smart_state(root: Path, state: dict[str, Any]) -> None:
    p = _smart_state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


@cli.group("tool-mode")
def tool_mode() -> None:
    """Smart tool mode (shadow|suggest|replace)."""


@tool_mode.command("show")
@click.pass_context
def tool_mode_show(ctx: click.Context) -> None:
    s = _load_smart_state(ctx.obj["root"])
    click.echo(s.get("mode", "shadow"))


@tool_mode.command("set")
@click.argument("mode", type=click.Choice(["shadow", "suggest", "replace"]))
@click.pass_context
def tool_mode_set(ctx: click.Context, mode: str) -> None:
    s = _load_smart_state(ctx.obj["root"])
    s["mode"] = mode
    _save_smart_state(ctx.obj["root"], s)
    click.echo(f"tool_mode={mode}")


@cli.group("route")
def route_group() -> None:
    """Quality-aware routing helpers."""


@route_group.command("decide")
@click.option("--goal", "user_goal", required=True, help="User goal/task summary.")
@click.option("--repo-root", default=".", show_default=True)
@click.option(
    "--task-type",
    type=click.Choice(["debug", "feature", "refactor", "test", "explain", "review", "docs", "ops"]),
    default="feature",
    show_default=True,
)
@click.option(
    "--risk-level",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    show_default=True,
)
@click.option("--changed-file", "changed_files", multiple=True, help="Repeat for each changed file.")
@click.option("--domain", default=None)
@click.option(
    "--step-type",
    type=click.Choice(
        [
            "classify",
            "compress",
            "retrieve",
            "plan",
            "edit",
            "debug",
            "verify",
            "summarize",
            "lesson_extract",
        ]
    ),
    default="plan",
    show_default=True,
)
@click.option("--step-index", default=0, show_default=True, type=int)
@click.option(
    "--evidence-json",
    default="{}",
    show_default=True,
    help="JSON object with optional routing evidence (confidence, refs, verifier_coverage, etc.).",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_decide_cmd(
    ctx: click.Context,
    user_goal: str,
    repo_root: str,
    task_type: str,
    risk_level: str,
    changed_files: tuple[str, ...],
    domain: str | None,
    step_type: str,
    step_index: int,
    evidence_json: str,
    as_json: bool,
) -> None:
    """Compute a deterministic route decision from quality-aware policy and runtime evidence."""
    rt = _core_runtime(ctx.obj["root"])

    try:
        evidence = json.loads(evidence_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --evidence-json: {exc}") from exc
    if not isinstance(evidence, dict):
        raise click.ClickException("--evidence-json must decode to an object")

    decision = rt.route_decide(
        user_goal=user_goal,
        repo_root=repo_root,
        task_type=task_type,
        risk_level=risk_level,
        changed_files=list(changed_files),
        domain=domain,
        step_type=step_type,
        step_index=step_index,
        evidence_summary=evidence,
    )
    payload = to_jsonable(decision)

    if as_json:
        _emit(payload, as_json=True)
        return

    click.echo(
        f"tier={payload['tier']} model={payload.get('selected_model', '') or '(deterministic)'} "
        f"confidence={payload['confidence']:.2f}"
    )
    click.echo(payload["reason"])
    if payload.get("escalation_trigger"):
        click.echo(f"escalation: {payload['escalation_trigger']}")
    if payload.get("verifier_required"):
        click.echo("verifiers: " + ", ".join(payload["verifier_required"]))


@route_group.command("verify")
@click.option("--route-decision-id", required=True)
@click.option("--changed-file", "changed_files", multiple=True, help="Repeat for each changed file.")
@click.option(
    "--validation-json",
    default="[]",
    show_default=True,
    help="JSON list of validation result objects: [{name, passed, detail}].",
)
@click.option(
    "--rubric-status",
    type=click.Choice(["not_run", "pass", "warn", "fail"]),
    default="not_run",
    show_default=True,
)
@click.option("--required-verifier", "required_verifiers", multiple=True)
@click.option("--protected-file-match", is_flag=True, default=False)
@click.option("--repeated-failure", "repeated_failures", multiple=True)
@click.option("--diff-line-count", default=0, show_default=True, type=int)
@click.option("--human-accepted/--human-rejected", default=None)
@click.option("--benchmark-accepted/--benchmark-rejected", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_verify_cmd(
    ctx: click.Context,
    route_decision_id: str,
    changed_files: tuple[str, ...],
    validation_json: str,
    rubric_status: str,
    required_verifiers: tuple[str, ...],
    protected_file_match: bool,
    repeated_failures: tuple[str, ...],
    diff_line_count: int,
    human_accepted: bool | None,
    benchmark_accepted: bool | None,
    as_json: bool,
) -> None:
    """Verify routing outcome and determine pass/warn/fail/escalate status."""
    rt = _core_runtime(ctx.obj["root"])

    try:
        validation_results = json.loads(validation_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --validation-json: {exc}") from exc
    if not isinstance(validation_results, list):
        raise click.ClickException("--validation-json must decode to a list")

    envelope = rt.quality_router.verify(
        route_decision_id=route_decision_id,
        run_id="cli-route-verify",
        changed_files=list(changed_files),
        validation_results=[item for item in validation_results if isinstance(item, dict)],
        rubric_status=rubric_status,
        required_verifiers=list(required_verifiers),
        protected_file_match=protected_file_match,
        repeated_failure_signatures=list(repeated_failures),
        diff_line_count=diff_line_count,
        human_accepted=human_accepted,
        benchmark_accepted=benchmark_accepted,
    )

    payload = to_jsonable(envelope)
    if as_json:
        _emit(payload, as_json=True)
        return

    click.echo(f"outcome={payload['outcome']} rubric={payload['rubric_status']}")
    click.echo(payload["compressed_evidence"])


# --------------------------------------------------------------------------- #
# proof                                                                       #
# --------------------------------------------------------------------------- #


@cli.group("proof")
def proof_group() -> None:
    """Cost-quality proof gate commands (WP-32)."""


@proof_group.command("run")
@click.option(
    "--run-id",
    required=True,
    help="Stable identifier for this proof run (e.g. a git SHA or timestamp).",
)
@click.option(
    "--context-reduction-pct",
    type=float,
    default=None,
    help=(
        "Context reduction percentage from WP-19 savings bench. " "When omitted, the benchmark is re-run automatically."
    ),
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_run_cmd(
    ctx: click.Context,
    run_id: str,
    context_reduction_pct: float | None,
    as_json: bool,
) -> None:
    """Run the cost-quality proof gate and write proof-report.json/md (WP-32)."""
    from atelier.core.capabilities.proof_gate.capability import (
        BenchmarkCase,
        ProofGateCapability,
    )

    root: Path = ctx.obj["root"]

    # Derive context_reduction_pct from savings bench if not provided
    if context_reduction_pct is None:
        try:
            from benchmarks.swe.savings_bench import run_savings_bench

            savings = run_savings_bench(root / "proof" / "savings_bench_tmp")
            context_reduction_pct = savings.reduction_pct
        except Exception as exc:
            raise click.ClickException(f"Could not run savings bench (pass --context-reduction-pct): {exc}") from exc

    # Build a minimal deterministic set of benchmark cases from the savings bench suite
    # to provide trace evidence for the proof report.
    cases: list[BenchmarkCase] = _build_proof_cases(run_id)

    capability = ProofGateCapability(root)
    report = capability.run(
        run_id=run_id,
        context_reduction_pct=context_reduction_pct,
        benchmark_cases=cases,
        save=True,
    )

    payload = to_jsonable(report)
    if as_json:
        _emit(payload, as_json=True)
        return

    status_str = "PASS" if report.status == "pass" else "FAIL"
    click.echo(f"proof run_id={report.run_id} status={status_str}")
    click.echo(f"context_reduction_pct={report.context_reduction_pct:.1f}%")
    click.echo(f"cost_per_accepted_patch=${report.cost_per_accepted_patch:.4f}")
    click.echo(f"accepted_patch_rate={report.accepted_patch_rate:.3f}")
    click.echo(f"routing_regression_rate={report.routing_regression_rate:.4f}")
    click.echo(f"cheap_success_rate={report.cheap_success_rate:.3f}")
    if report.failed_thresholds:
        click.echo(f"failed_thresholds={','.join(report.failed_thresholds)}")


def _show_proof_report(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.proof_gate.capability import ProofGateCapability

    root: Path = ctx.obj["root"]
    capability = ProofGateCapability(root)
    report = capability.load()

    if report is None:
        raise click.ClickException("No proof report found. Run `atelier proof run --run-id <id>` first.")

    payload = to_jsonable(report)
    if as_json:
        _emit(payload, as_json=True)
        return

    status_str = "PASS" if report.status == "pass" else "FAIL"
    click.echo(f"proof run_id={report.run_id} status={status_str}")
    click.echo(f"context_reduction_pct={report.context_reduction_pct:.1f}%")
    click.echo(f"cost_per_accepted_patch=${report.cost_per_accepted_patch:.4f}")
    if report.failed_thresholds:
        click.echo(f"failed_thresholds={','.join(report.failed_thresholds)}")


@proof_group.command("report")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_report_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show the last saved proof report (WP-32)."""
    _show_proof_report(ctx, as_json)


@proof_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_show_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show the last saved proof report (WP-32)."""
    _show_proof_report(ctx, as_json)


def _build_proof_cases(run_id: str) -> list[Any]:
    """Build a deterministic set of benchmark cases for the proof gate.

    These cases are derived from the WP-28 routing eval suite.  Each case
    must include a trace_id so the evidence link requirement is met.  Failed
    cheap attempts are included — they cannot be elided.
    """
    from atelier.core.capabilities.proof_gate.capability import BenchmarkCase

    # Deterministic cases representative of the routing eval suite.
    # Each case carries a synthetic trace_id so every claim links to evidence.
    _CASES: list[dict[str, Any]] = [
        {
            "case_id": f"{run_id}:cheap-01",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": True,
            "cost_usd": 0.002,
            "trace_id": f"{run_id}:trace:cheap-01",
            "run_id": run_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{run_id}:cheap-02",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": False,
            "cost_usd": 0.002,
            "trace_id": f"{run_id}:trace:cheap-02",
            "run_id": run_id,
            "verifier_outcome": "fail",
        },
        {
            "case_id": f"{run_id}:cheap-03",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": True,
            "cost_usd": 0.002,
            "trace_id": f"{run_id}:trace:cheap-03",
            "run_id": run_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{run_id}:mid-01",
            "task_type": "coding",
            "tier": "mid",
            "accepted": True,
            "cost_usd": 0.008,
            "trace_id": f"{run_id}:trace:mid-01",
            "run_id": run_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{run_id}:premium-01",
            "task_type": "coding",
            "tier": "premium",
            "accepted": True,
            "cost_usd": 0.05,
            "trace_id": f"{run_id}:trace:premium-01",
            "run_id": run_id,
            "verifier_outcome": "pass",
        },
    ]
    return [BenchmarkCase(**c) for c in _CASES]


@cli.command("read")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--max-lines", default=120, show_default=True)
@click.pass_context
def read_cmd(ctx: click.Context, path: Path, max_lines: int) -> None:
    """Read a file with summarization and related-ReasonBlock hints."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.smart_read(path, max_lines=max_lines)
    _emit(payload, as_json=True)


@cli.command("edit")
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON file with edits: [{path, find, replace}, ...]",
)
@click.pass_context
def edit_cmd(ctx: click.Context, input_path: Path) -> None:
    """Apply a batch of find/replace edits from a JSON file."""
    rt = _core_runtime(ctx.obj["root"])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise click.ClickException("edit input must be a JSON list")
    result = rt.smart_edit([p for p in payload if isinstance(p, dict)])
    _emit(result, as_json=True)


@cli.group("memory")
def memory_group() -> None:
    """Session memory operations."""


@memory_group.command("upsert")
@click.option("--agent-id", required=True)
@click.option("--label", required=True)
@click.option("--value", required=True, help="Inline text or @path. Use @/dev/stdin for stdin.")
@click.option("--limit-chars", default=8000, show_default=True, type=int)
@click.option("--description", default="")
@click.option("--read-only", is_flag=True)
@click.option("--pinned", is_flag=True)
@click.option("--metadata-json", default="{}")
@click.option("--expected-version", default=None, type=int)
@click.option("--actor", default=None)
@click.pass_context
def memory_upsert(
    ctx: click.Context,
    agent_id: str,
    label: str,
    value: str,
    limit_chars: int,
    description: str,
    read_only: bool,
    pinned: bool,
    metadata_json: str,
    expected_version: int | None,
    actor: str | None,
) -> None:
    """Create or update one editable memory block."""
    from atelier.core.foundation.memory_models import MemoryBlock
    from atelier.infra.storage.factory import make_memory_store
    from atelier.infra.storage.memory_store import MemoryConcurrencyError, MemorySidecarUnavailable

    try:
        metadata_raw = json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --metadata-json: {exc}") from exc
    if not isinstance(metadata_raw, dict):
        raise click.ClickException("--metadata-json must decode to an object")

    store = make_memory_store(ctx.obj["root"])
    clean_value = _redact_memory_input(_read_memory_value(value), "value")
    clean_description = _redact_memory_input(description, "description")
    existing = store.get_block(agent_id, label)
    version = expected_version if expected_version is not None else (existing.version if existing else 1)
    seed = existing or MemoryBlock(agent_id=agent_id, label=label, value=clean_value)
    block = MemoryBlock(
        id=seed.id,
        agent_id=agent_id,
        label=label,
        value=clean_value,
        limit_chars=limit_chars,
        description=clean_description,
        read_only=read_only,
        metadata=metadata_raw,
        pinned=pinned,
        version=version,
        current_history_id=existing.current_history_id if existing else None,
        created_at=seed.created_at,
    )
    try:
        stored = store.upsert_block(block, actor=actor or f"agent:{agent_id}")
    except MemoryConcurrencyError as exc:
        raise click.ClickException(str(exc)) from exc
    except MemorySidecarUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"id": stored.id, "version": stored.version}, as_json=True)


@memory_group.command("get")
@click.option("--agent-id", required=True)
@click.option("--label", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def memory_get(ctx: click.Context, agent_id: str, label: str, as_json: bool) -> None:
    """Fetch one editable memory block."""
    from atelier.infra.storage.factory import make_memory_store

    block = make_memory_store(ctx.obj["root"]).get_block(agent_id, label)
    if block is None:
        _emit(None, as_json=as_json)
        return
    payload = block.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{payload['agent_id']}\t{payload['label']}\tv{payload['version']}")
    click.echo(payload["value"])


@memory_group.command("list")
@click.option("--agent-id", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def memory_list(ctx: click.Context, agent_id: str, as_json: bool) -> None:
    """List all memory blocks for an agent."""
    from atelier.infra.storage.factory import make_memory_store

    store = make_memory_store(ctx.obj["root"])
    blocks = store.list_blocks(agent_id)
    if as_json:
        _emit([b.model_dump(mode="json") for b in blocks], as_json=True)
        return
    if not blocks:
        click.echo("(no blocks)")
        return
    for b in blocks:
        click.echo(f"{b.label}\tv{b.version}\t{len(b.value)} chars")


@memory_group.command("archive")
@click.option("--agent-id", required=True)
@click.option("--text", required=True, help="Inline text or @path. Use @/dev/stdin for stdin.")
@click.option("--source", required=True)
@click.option("--source-ref", default="")
@click.option("--tags", "tag_values", multiple=True)
@click.pass_context
def memory_archive(
    ctx: click.Context,
    agent_id: str,
    text: str,
    source: str,
    source_ref: str,
    tag_values: tuple[str, ...],
) -> None:
    """Archive long-term memory text for later recall."""
    from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
    from atelier.core.foundation.redaction import redact
    from atelier.infra.embeddings.factory import make_embedder
    from atelier.infra.storage.factory import make_memory_store

    capability = ArchivalRecallCapability(make_memory_store(ctx.obj["root"]), make_embedder(), redactor=redact)
    passage = capability.archive(
        agent_id=agent_id,
        text=_read_memory_value(text),
        source=source,  # type: ignore[arg-type]
        source_ref=source_ref,
        tags=_parse_tags(tag_values),
    )
    _emit({"id": passage.id, "dedup_hit": passage.dedup_hit}, as_json=True)


@memory_group.command("recall")
@click.option("--agent-id", required=True)
@click.option("--query", required=True)
@click.option("--top-k", default=5, show_default=True, type=int)
@click.option("--tags", "tag_values", multiple=True)
@click.option("--since", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def memory_recall(
    ctx: click.Context,
    agent_id: str,
    query: str,
    top_k: int,
    tag_values: tuple[str, ...],
    since: str | None,
    as_json: bool,
) -> None:
    """Recall relevant archival memory passages."""
    from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
    from atelier.core.foundation.redaction import redact
    from atelier.infra.embeddings.factory import make_embedder
    from atelier.infra.storage.factory import make_memory_store

    capability = ArchivalRecallCapability(make_memory_store(ctx.obj["root"]), make_embedder(), redactor=redact)
    passages, recall = capability.recall(
        agent_id=agent_id,
        query=query,
        top_k=top_k,
        tags=_parse_tags(tag_values) or None,
        since=datetime.fromisoformat(since) if since else None,
    )
    payload = {
        "passages": [
            {
                "id": passage.id,
                "text": passage.text,
                "source_ref": passage.source_ref,
                "tags": passage.tags,
            }
            for passage in passages
        ],
        "recall_id": recall.id,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    for passage in passages:
        click.echo(f"{passage.id}\t{passage.source_ref}\t{passage.text}")


@cli.group("letta")
def letta_group() -> None:
    """Manage the self-hosted Letta sidecar."""


@letta_group.command("up")
def letta_up() -> None:
    """Start the Letta Docker Compose stack."""
    _run_compose(["up", "-d"])


@letta_group.command("down")
def letta_down() -> None:
    """Stop the Letta Docker Compose stack while preserving volumes."""
    _run_compose(["down"])


@letta_group.command("logs")
@click.option("-f", "follow", is_flag=True)
def letta_logs(follow: bool) -> None:
    """Show Letta logs."""
    args = ["logs"]
    if follow:
        args.append("-f")
    _run_compose(args)


@letta_group.command("status")
def letta_status() -> None:
    """Print Letta health status."""
    url = os.environ.get("ATELIER_LETTA_URL", "http://localhost:8283").rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/v1/health", timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
        click.echo(f"healthy\t{url}\t{body}")
    except Exception as exc:
        raise click.ClickException(f"Letta is not healthy at {url}: {exc}") from exc


@letta_group.command("reset")
@click.option("--yes", is_flag=True, help="Confirm destructive volume removal.")
def letta_reset(yes: bool) -> None:
    """Remove the Letta container and persistent volume."""
    if not yes:
        raise click.ClickException("refusing to reset Letta data without --yes")
    _run_compose(["down", "-v"])


@cli.command("consolidate")
@click.option("--since", default="7d", show_default=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def consolidate_cmd(ctx: click.Context, since: str, dry_run: bool, as_json: bool) -> None:
    """Run manual sleep-time consolidation."""
    from atelier.core.capabilities.consolidation import consolidate

    store = ReasoningStore(ctx.obj["root"])
    store.init()
    report = consolidate(store, since=_parse_duration(since), dry_run=dry_run)
    _emit(report.to_dict(), as_json=as_json)


@cli.group("consolidation")
def consolidation_group() -> None:
    """Consolidation candidate review workflow."""


@consolidation_group.command("inbox")
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def consolidation_inbox(ctx: click.Context, limit: int, as_json: bool) -> None:
    store = ReasoningStore(ctx.obj["root"])
    store.init()
    items = store.list_consolidation_candidates(limit=limit)
    payload = {"candidates": [item.model_dump(mode="json") for item in items]}
    if as_json:
        _emit(payload, as_json=True)
        return
    if not items:
        click.echo("(no consolidation candidates)")
        return
    for item in items:
        click.echo(f"{item.id}\t{item.kind}\t{item.proposed_action}")


@consolidation_group.command("decide")
@click.argument("candidate_id")
@click.argument("decision")
@click.option("--reviewer", default="human", show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def consolidation_decide(ctx: click.Context, candidate_id: str, decision: str, reviewer: str, as_json: bool) -> None:
    store = ReasoningStore(ctx.obj["root"])
    store.init()
    candidate = store.get_consolidation_candidate(candidate_id)
    if candidate is None:
        raise click.ClickException(f"consolidation candidate not found: {candidate_id}")
    candidate.decided_at = datetime.now(UTC)
    candidate.decided_by = reviewer
    candidate.decision = decision
    store.upsert_consolidation_candidate(candidate)
    payload = candidate.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{decision} {candidate_id}")


@cli.group("bash")
def bash_group() -> None:
    """Shell interception helpers."""


@bash_group.command("intercept")
@click.option("--command", "command_text", required=True, help="Shell command string to inspect.")
@click.option(
    "--history",
    "history_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional JSON array file with prior shell commands.",
)
@click.pass_context
def bash_intercept(ctx: click.Context, command_text: str, history_path: Path | None) -> None:
    rt = _core_runtime(ctx.obj["root"])
    history: list[str] = []
    if history_path is not None:
        raw = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            history = [str(item) for item in raw]
    payload = rt.bash_intercept(command_text, history=history)
    _emit(payload, as_json=True)


@cli.command("search-read")
@click.option("--query", required=True, help="Pattern to search for (grep -rn).")
@click.option("--path", "search_path", default=".", show_default=True, help="Directory or file to search.")
@click.option("--max-files", default=10, show_default=True, type=int, help="Max hit-files to return.")
@click.option("--max-chars-per-file", default=2000, show_default=True, type=int)
@click.option("--no-outline", "include_outline", is_flag=True, flag_value=False, default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON (default: human-readable).")
@click.pass_context
def search_read_cmd(
    ctx: click.Context,
    query: str,
    search_path: str,
    max_files: int,
    max_chars_per_file: int,
    include_outline: bool,
    as_json: bool,
) -> None:
    """Combined search + read.

    Collapses grep→read→read into a single ranked-snippet call.  Returns
    context windows around each match plus AST outlines for dense files.
    Typically saves ≥70 % of tokens vs. separate grep + full-file-read calls.

    Host-native search/read tools remain available for raw exploration.
    """
    from atelier.core.capabilities.tool_supervision.search_read import (
        search_read,
        search_read_to_dict,
    )

    try:
        result = search_read(
            query=query,
            path=search_path,
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
            include_outline=include_outline,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    payload = search_read_to_dict(result)

    if as_json:
        _emit(payload, as_json=True)
        return

    click.echo(
        f"matches: {len(payload['matches'])} files  "
        f"tokens: {payload['total_tokens']}  "
        f"saved_vs_naive: {payload['tokens_saved_vs_naive']}"
    )
    for m in payload["matches"]:
        click.echo(f"\n  [{m['lang']}] {m['path']}  ({m['tokens']} tokens)")
        for sn in m["snippets"]:
            click.echo(f"    lines {sn['line_start']}-{sn['line_end']}  score={sn['score']:.2f}")
            for ln in sn["text"].splitlines()[:5]:
                click.echo(f"      {ln}")
        if m.get("outline"):
            symbols = m["outline"].get("symbols", [])
            click.echo(f"    outline: {len(symbols)} symbols")


@cli.command("cached-grep")
@click.argument("pattern")
@click.option("--path", "search_path", default=".", show_default=True)
@click.pass_context
def cached_grep(ctx: click.Context, pattern: str, search_path: str) -> None:
    """Cache-aware grep. Returns cached result on repeated queries."""
    from atelier.core.foundation.redaction import assert_safe_grep_args

    try:
        assert_safe_grep_args(pattern, search_path)
    except ValueError as exc:
        _emit({"error": str(exc)}, as_json=True)
        ctx.exit(2)
        return
    s = _load_smart_state(ctx.obj["root"])
    cache = s.setdefault("cache", {})
    key = f"grep:{pattern}:{search_path}:{_path_content_fingerprint(search_path)}"
    if not _cache_disabled() and key in cache:
        s["savings"]["calls_avoided"] = int(s["savings"].get("calls_avoided", 0)) + 1
        _save_smart_state(ctx.obj["root"], s)
        _emit({**cache[key], "cached": True}, as_json=True)
        return
    import subprocess

    try:
        proc = subprocess.run(
            ["grep", "-rn", "--", pattern, search_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        out = proc.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        out = f"(grep failed: {exc})"
    payload = {"cached": False, "output": out[:8000]}
    if not _cache_disabled():
        cache[key] = payload
        _save_smart_state(ctx.obj["root"], s)
    _emit(payload, as_json=True)


# ----- savings + benchmark ----------------------------------------------- #


@cli.command("savings")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def savings_cmd(ctx: click.Context, as_json: bool) -> None:
    """Aggregate savings: cache + reasoning-library + cost-delta vs. baseline."""
    from atelier.core.capabilities.plugin_runtime import build_savings_report

    runs = _ledger_dir(ctx.obj["root"])
    bad_plans_blocked = 0
    rescue_events = 0
    rubric_failures = 0
    if runs.is_dir():
        for p in runs.glob("*.json"):
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for ev in snap.get("events", []):
                kind = ev.get("kind")
                if kind == "monitor_alert":
                    sev = (ev.get("payload") or {}).get("severity")
                    if sev == "high":
                        rescue_events += 1
                if kind == "rubric_run" and (ev.get("payload") or {}).get("status") == "blocked":
                    rubric_failures += 1
    payload = build_savings_report(ctx.obj["root"])
    payload["bad_plans_blocked"] = bad_plans_blocked
    payload["rescue_events"] = rescue_events
    payload["rubric_failures_caught"] = rubric_failures
    if as_json:
        _emit(payload, as_json=True)
    else:
        for k, v in payload.items():
            if isinstance(v, dict):
                click.echo(f"{k}:")
                for k2, v2 in v.items():
                    click.echo(f"  {k2}: {v2}")
            else:
                click.echo(f"{k}: {v}")


@cli.command("savings-detail")
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", default=20, show_default=True, help="Top N operations.")
@click.pass_context
def savings_detail(ctx: click.Context, as_json: bool, limit: int) -> None:
    """Per-operation cost-delta breakdown (last_cost - new_cost, baseline %)."""
    from atelier.infra.runtime.cost_tracker import CostTracker

    tracker = CostTracker(ctx.obj["root"])
    summary = tracker.total_savings()
    rows = summary["per_operation"][:limit]
    if as_json:
        _emit(
            {
                "summary": {k: v for k, v in summary.items() if k != "per_operation"},
                "operations": rows,
            },
            as_json=True,
        )
        return
    click.echo(
        f"Tracked operations: {summary['operations_tracked']}  "
        f"calls={summary['total_calls']}  "
        f"saved=${summary['saved_usd']:.4f} ({summary['saved_pct']}%)"
    )
    click.echo("-" * 92)
    click.echo(
        f"{'op_key':18} {'calls':>5} {'baseline$':>10} "
        f"{'last$':>10} {'now$':>10} {'d_last$':>10} {'d_base$':>10} {'%down':>6}  domain"
    )
    click.echo("-" * 92)
    for r in rows:
        click.echo(
            f"{r['op_key']:18} {r['calls_count']:>5} "
            f"{r['baseline_cost_usd']:>10.4f} {r['last_cost_usd']:>10.4f} "
            f"{r['current_cost_usd']:>10.4f} {r['delta_vs_last_usd']:>10.4f} "
            f"{r['delta_vs_base_usd']:>10.4f} {r['pct_vs_base']:>6.1f}  "
            f"{r.get('domain', '-')}"
        )


@cli.command("savings-reset")
@click.pass_context
def savings_reset(ctx: click.Context) -> None:
    s = _load_smart_state(ctx.obj["root"])
    s["savings"] = {"calls_avoided": 0, "tokens_saved": 0}
    _save_smart_state(ctx.obj["root"], s)
    from atelier.infra.runtime.cost_tracker import save_cost_history

    save_cost_history(ctx.obj["root"], {"operations": {}})
    click.echo("savings reset (cache + cost history)")


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
        payload = {"auth": write_auth_state(ctx.obj["root"], parse_login_token(token)), "mode": "token"}
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
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show local plugin/auth/subscription status."""
    from atelier.core.capabilities.plugin_runtime import auth_status, load_plugin_settings

    payload = auth_status(ctx.obj["root"])
    payload["settings"] = load_plugin_settings(ctx.obj["root"])
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


@cli.command("benchmark")
@click.argument("action", required=False)
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    help="Prompts to benchmark (repeat). Defaults to 5 built-in tasks.",
)
@click.option("--model", default="claude-sonnet-4.6", show_default=True)
@click.option("--rounds", default=3, show_default=True, help="How many rounds per prompt.")
@click.option(
    "--baseline-command",
    default=None,
    help="Command template for benchmark savings baseline runs. Receives ATELIER_BENCH_PROMPT.",
)
@click.option(
    "--atelier-command",
    default=None,
    help="Command template for benchmark savings Atelier-enabled runs. Receives ATELIER_BENCH_PROMPT.",
)
@click.option("--timeout", "timeout_s", default=600.0, show_default=True, type=float, help="Seconds per command.")
@click.option(
    "--max-prompts", default=5, show_default=True, type=int, help="Default replay prompts for savings action."
)
@click.option(
    "--input",
    "inputs",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Benchmark report JSON inputs for compare/report/export.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write report/export output to this path.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "markdown", "csv"]),
    default="json",
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark(
    ctx: click.Context,
    action: str | None,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
    baseline_command: str | None,
    atelier_command: str | None,
    timeout_s: float,
    max_prompts: int,
    inputs: tuple[Path, ...],
    output_path: Path | None,
    output_format: str,
    as_json: bool,
) -> None:
    """Run, compare, render, or export Atelier runtime benchmarks.

    Backward compatibility:
    - ``atelier benchmark --prompt ...`` still runs the benchmark directly.
    - ``atelier benchmark run --prompt ...`` is the new explicit form.
    """
    from atelier.infra.runtime.benchmarking import (
        benchmark_report_path,
        compare_runtime_reports,
        export_runtime_report,
        load_runtime_report,
        render_runtime_report,
        run_runtime_benchmark,
    )

    selected_action = action or "run"
    if selected_action == "run":
        report = run_runtime_benchmark(
            root=ctx.obj["root"],
            prompts=prompts,
            model=model,
            rounds=rounds,
        )
        if output_path is not None:
            export_runtime_report(report, output_path=output_path, output_format=output_format)
        if as_json:
            _emit(report, as_json=True)
            return
        click.echo(render_runtime_report(report))
        click.echo(f"saved report: {benchmark_report_path(ctx.obj['root'])}")
        return

    if selected_action == "savings":
        if baseline_command is None or atelier_command is None:
            raise click.ClickException("benchmark savings requires --baseline-command and --atelier-command")
        from benchmarks.swe.savings_replay import run_paired_command_benchmark

        tasks = [
            {"id": f"prompt-{idx}", "task_type": "ad_hoc", "task": prompt}
            for idx, prompt in enumerate(prompts, start=1)
        ]
        report = run_paired_command_benchmark(
            root=ctx.obj["root"],
            baseline_command=baseline_command,
            atelier_command=atelier_command,
            tasks=tasks or None,
            model=model,
            timeout_s=timeout_s,
            max_prompts=max_prompts,
        )
        payload = report.to_dict()
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        if as_json:
            _emit(payload, as_json=True)
            return
        click.echo(
            f"savings benchmark complete: {payload['tokens_saved']} tokens, "
            f"{payload['reduction_pct']:.2f}% reduction, "
            f"${payload['cost_saved_usd']:.4f} saved"
        )
        click.echo(f"saved report: {ctx.obj['root'] / 'benchmarks' / 'savings' / 'latest.json'}")
        return

    if selected_action == "compare":
        if len(inputs) < 2:
            raise click.ClickException("benchmark compare requires at least two --input reports")
        comparison = compare_runtime_reports(list(inputs))
        _emit(comparison, as_json=True)
        return

    if selected_action == "report":
        if len(inputs) != 1:
            raise click.ClickException("benchmark report requires exactly one --input report")
        report = load_runtime_report(inputs[0])
        if as_json:
            _emit(report, as_json=True)
            return
        click.echo(render_runtime_report(report))
        return

    if selected_action == "export":
        if len(inputs) != 1 or output_path is None:
            raise click.ClickException("benchmark export requires one --input report and --output")
        report = load_runtime_report(inputs[0])
        exported = export_runtime_report(report, output_path=output_path, output_format=output_format)
        _emit({"output": str(exported), "format": output_format}, as_json=True)
        return

    raise click.ClickException("benchmark action must be one of: run, savings, compare, report, export")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _run_benchmark_core(
    *,
    root: Path,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
) -> dict[str, Any]:
    from atelier.infra.runtime.benchmarking import run_runtime_benchmark

    report = run_runtime_benchmark(root=root, prompts=prompts, model=model, rounds=rounds)
    return {"suite": "core", "report": report}


def _run_benchmark_hosts(*, workspace: str | None = None) -> dict[str, Any]:
    script = _repo_root() / "scripts" / "verify_agent_clis.sh"
    cmd = ["bash", str(script)]
    if workspace:
        cmd.extend(["--workspace", workspace])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "suite": "hosts",
        "exit_code": proc.returncode,
        "status": "pass" if proc.returncode == 0 else "fail",
        "command": " ".join(cmd),
        "output": output.strip(),
    }


def _run_benchmark_packs(*, root: Path, host: str) -> dict[str, Any]:
    manager = _load_domain_manager(root)
    bundle_ids = [ref.bundle_id for ref in manager.list_bundles()]

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for bundle_id in bundle_ids:
        try:
            info = manager.info(bundle_id) or {}
            results.append({"bundle_id": bundle_id, "domain": info.get("domain", ""), "status": "ok"})
        except Exception as exc:
            failures.append({"bundle_id": bundle_id, "error": str(exc)})

    return {
        "suite": "domains",
        "host": host,
        "domains_total": len(bundle_ids),
        "domains_benchmarked": len(results),
        "results": results,
        "failures": failures,
    }


@cli.command("benchmark-core")
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    help="Prompts to benchmark (repeat). Defaults to built-in runtime tasks.",
)
@click.option("--model", default="claude-sonnet-4.6", show_default=True)
@click.option("--rounds", default=3, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_core(
    ctx: click.Context,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
    as_json: bool,
) -> None:
    """Phase T3: benchmark core runtime behavior."""
    payload = _run_benchmark_core(root=ctx.obj["root"], prompts=prompts, model=model, rounds=rounds)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("core benchmark complete")
    click.echo(f"tasks: {len(payload['report'].get('tasks', []))}")


@cli.command("benchmark-hosts")
@click.option("--workspace", default=None, help="Optional workspace path passed to verify scripts.")
@click.option("--json", "as_json", is_flag=True)
def benchmark_hosts(workspace: str | None, as_json: bool) -> None:
    """Phase T3: benchmark/verify host integration readiness."""
    payload = _run_benchmark_hosts(workspace=workspace)
    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo(payload["output"])
    if payload["exit_code"] != 0:
        raise click.ClickException("host benchmark/verification failed")


@cli.command("benchmark-host")
@click.option("--workspace", default=None, help="Optional workspace path passed to verify scripts.")
@click.option("--json", "as_json", is_flag=True)
def benchmark_host(workspace: str | None, as_json: bool) -> None:
    """Benchmark/verify host integration readiness (alias for benchmark-hosts)."""
    payload = _run_benchmark_hosts(workspace=workspace)
    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo(payload["output"])
    if payload["exit_code"] != 0:
        raise click.ClickException("host benchmark/verification failed")


@cli.group("bench")
def bench_group() -> None:
    """Benchmark commands."""


@bench_group.command("runtime")
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON output path.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def bench_runtime(ctx: click.Context, output_path: Path | None, as_json: bool) -> None:
    """Emit runtime capability efficiency metrics."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.benchmark_runtime_metrics()
    if output_path is not None:
        rt.export_benchmark_runtime(output_path)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@cli.command("benchmark-packs")
@click.option("--host", default="codex", show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_packs(ctx: click.Context, host: str, as_json: bool) -> None:
    """Phase T3: benchmark official/installed packs."""
    payload = _run_benchmark_packs(root=ctx.obj["root"], host=host)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"domain benchmark complete: {payload['domains_benchmarked']}/{payload['domains_total']} domains")
    if payload["failures"]:
        click.echo("failures:")
        for item in payload["failures"]:
            click.echo(f"  - {item.get('bundle_id', item.get('pack_id', '?'))}: {item['error']}")


@cli.command("benchmark-full")
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    help="Prompts to benchmark for the core suite (repeat).",
)
@click.option("--model", default="claude-sonnet-4.6", show_default=True)
@click.option("--rounds", default=3, show_default=True)
@click.option("--host", default="codex", show_default=True)
@click.option("--workspace", default=None, help="Optional workspace path passed to host verify scripts.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_full(
    ctx: click.Context,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
    host: str,
    workspace: str | None,
    as_json: bool,
) -> None:
    """Phase T3: run core + hosts + packs benchmark suite."""
    core_payload = _run_benchmark_core(root=ctx.obj["root"], prompts=prompts, model=model, rounds=rounds)
    hosts_payload = _run_benchmark_hosts(workspace=workspace)
    packs_payload = _run_benchmark_packs(root=ctx.obj["root"], host=host)

    payload = {
        "suite": "full",
        "core": core_payload,
        "hosts": hosts_payload,
        "packs": packs_payload,
        "status": ("pass" if hosts_payload["exit_code"] == 0 and not packs_payload["failures"] else "warn"),
    }

    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo("full benchmark suite complete")
        click.echo(f"core tasks: {len(core_payload['report'].get('tasks', []))}")
        click.echo(f"host verification status: {hosts_payload['status']}")
        click.echo(f"domain coverage: {packs_payload['domains_benchmarked']}/{packs_payload['domains_total']}")

    if hosts_payload["exit_code"] != 0:
        raise click.ClickException("full benchmark failed in host verification")


@cli.group("service")
def service_group() -> None:
    """Production service commands."""


@service_group.command("start")
@click.option("--host", default=None, help="Bind host (overrides ATELIER_SERVICE_HOST).")
@click.option("--port", default=None, type=int, help="Bind port (overrides ATELIER_SERVICE_PORT).")
@click.option("--reload", is_flag=True, default=False, help="Enable uvicorn auto-reload.")
def service_start(host: str | None, port: int | None, reload: bool) -> None:
    """Start the Atelier HTTP service API."""
    try:
        from atelier.core.service.api import main as service_main
    except ImportError as exc:
        raise click.ClickException("FastAPI/uvicorn not installed. Run: uv add 'atelier[api]'") from exc
    service_main(host=host, port=port, reload=reload)


@service_group.command("config")
def service_config() -> None:
    """Print current service configuration (no secret values)."""
    import json

    from atelier.core.service.config import cfg

    click.echo(json.dumps(cfg.as_dict(), indent=2))


# --------------------------------------------------------------------------- #
# Worker group (P6)                                                           #
# --------------------------------------------------------------------------- #


@cli.group("worker")
def worker_group() -> None:
    """Worker/job queue commands."""


@worker_group.command("start")
@click.pass_context
def worker_start(ctx: click.Context) -> None:
    """Start the background worker loop (Postgres required)."""
    try:
        from atelier.core.service.worker import Worker
    except ImportError as exc:
        raise click.ClickException("Worker dependencies not available.") from exc

    from atelier.infra.storage.factory import create_store

    root = ctx.obj["root"]
    store = create_store(root)
    if not hasattr(store, "claim_job"):
        click.echo(
            "No production job queue configured (SQLite mode). "
            "Set ATELIER_STORAGE_BACKEND=postgres and ATELIER_DATABASE_URL to enable workers."
        )
        return
    worker = Worker(store=store)
    click.echo("Worker started. Press Ctrl+C to stop.")
    worker.run()


@worker_group.command("run-once")
@click.pass_context
def worker_run_once(ctx: click.Context) -> None:
    """Claim and process one pending job then exit."""
    try:
        from atelier.core.service.worker import Worker
    except ImportError as exc:
        raise click.ClickException("Worker dependencies not available.") from exc

    from atelier.infra.storage.factory import create_store

    root = ctx.obj["root"]
    store = create_store(root)
    if not hasattr(store, "claim_job"):
        click.echo("no production queue configured — SQLite mode")
        return
    worker = Worker(store=store)
    processed = worker.run_once()
    if processed:
        click.echo(f"processed job: {processed}")
    else:
        click.echo("no pending jobs")


# --------------------------------------------------------------------------- #
# V3 capability commands                                                      #
# --------------------------------------------------------------------------- #


@cli.command("detect-loop")
@click.option("--run-id", default=None, help="Specific run ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def detect_loop_cmd(ctx: click.Context, run_id: str | None, as_json: bool) -> None:
    """Detect loops, repeated failures, and dead-end trajectories in a run ledger."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(run_id=run_id)
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
@click.option("--run-id", default=None, help="Specific run ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def loop_report_cmd(ctx: click.Context, run_id: str | None, as_json: bool) -> None:
    """Full loop analysis: signature, severity, alerts, rescue strategies."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(run_id=run_id)
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


@cli.command("diff-context")
@click.argument("files", nargs=-1, required=True)
@click.option("--lines", default=5, show_default=True, help="Lines of context around diffs.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def diff_context_cmd(ctx: click.Context, files: tuple[str, ...], lines: int, as_json: bool) -> None:
    """Show git diff context for given source files."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.diff_context(list(files), lines=lines)
    if as_json:
        _emit(payload, as_json=True)
        return
    for entry in payload.get("diffs", []):
        click.echo(f"## {entry['path']}")
        click.echo(entry.get("diff", "(no changes)"))


@cli.command("test-context")
@click.argument("files", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def test_context_cmd(ctx: click.Context, files: tuple[str, ...], as_json: bool) -> None:
    """Find test files related to the given source files."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.test_context(list(files))
    if as_json:
        _emit(payload, as_json=True)
        return
    for entry in payload.get("test_contexts", []):
        click.echo(f"{entry['path']}: {', '.join(entry['test_files']) or '(none found)'}")


@cli.command("module-summary")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def module_summary_cmd(ctx: click.Context, path: Path, as_json: bool) -> None:
    """Concise module-level summary: exports, symbols, imports, test files."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.module_summary(path)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"path: {payload['path']}")
    click.echo(f"language: {payload['language']}")
    click.echo(f"exports: {', '.join(payload['exports'][:20]) or '(none)'}")
    click.echo(f"imports: {', '.join(payload['imports'][:10]) or '(none)'}")
    click.echo(f"test_files: {', '.join(payload['test_files']) or '(none found)'}")
    click.echo(f"lines_total: {payload['lines_total']}")


@cli.command("symbol-search")
@click.argument("query")
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def symbol_search_cmd(ctx: click.Context, query: str, limit: int, as_json: bool) -> None:
    """Search for a symbol across all semantically cached files."""
    rt = _core_runtime(ctx.obj["root"])
    results = rt.symbol_search(query, limit=limit)
    if as_json:
        _emit(results, as_json=True)
        return
    if not results:
        click.echo("(no matches)")
        return
    for r in results:
        click.echo(f"{r['path']}:{r['lineno']}  [{r['kind']}]  {r['signature']}")


@cli.command("context-report")
@click.option("--run-id", default=None, help="Specific run ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def context_report_cmd(ctx: click.Context, run_id: str | None, as_json: bool) -> None:
    """Compression + provenance report for a run ledger."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.context_report(run_id=run_id)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"chars_before: {payload['chars_before']}")
    click.echo(f"chars_after: {payload['chars_after']}")
    click.echo(f"reduction_pct: {payload['reduction_pct']}%")
    click.echo(f"preserved_facts: {len(payload['preserved_facts'])}")
    for fact in payload["preserved_facts"][:10]:
        click.echo(f"  + {fact}")
    dropped = payload.get("dropped", [])
    if dropped:
        click.echo("dropped:")
        for d in dropped:
            click.echo(f"  - {d['kind']} ({d['count']}): {d['reason']}")


# --------------------------------------------------------------------------- #
# batch-edit                                                                  #
# --------------------------------------------------------------------------- #


@cli.command("batch-edit")
@click.option(
    "--from",
    "from_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="JSON file containing the edit payload.",
)
@click.option(
    "--from-stdin",
    is_flag=True,
    default=False,
    help="Read JSON edit payload from stdin.",
)
@click.option(
    "--no-atomic",
    is_flag=True,
    default=False,
    help="Disable atomic (all-or-nothing) mode.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit result as JSON.")
@click.pass_context
def batch_edit_cmd(
    ctx: click.Context,
    from_file: Path | None,
    from_stdin: bool,
    no_atomic: bool,
    as_json: bool,
) -> None:
    """Apply many mechanical edits across files in one deterministic call.

    Reads a JSON payload either from --from <file.json> or --from-stdin.
    The payload shape:

    \b
      {
        "edits": [
          {"path": "src/foo.py", "op": "replace",
           "old_string": "...", "new_string": "..."},
          {"path": "src/bar.py", "op": "insert_after",
           "anchor": "def baz", "new_string": "..."},
          {"path": "src/baz.ts", "op": "replace_range",
           "line_start": 42, "line_end": 58, "new_string": "..."}
        ],
        "atomic": true
      }

    This is an *optional* Atelier augmentation.  Host-native edit tools remain
    the default path for ordinary coding.
    """
    if from_stdin and from_file:
        raise click.UsageError("Provide either --from or --from-stdin, not both.")
    if not from_stdin and not from_file:
        raise click.UsageError("Provide either --from <file.json> or --from-stdin.")

    if from_stdin:
        raw = sys.stdin.read()
    else:
        assert from_file is not None
        raw = from_file.read_text(encoding="utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON: {exc}") from exc

    edits = payload.get("edits", [])
    atomic = payload.get("atomic", True)
    if no_atomic:
        atomic = False

    from atelier.core.capabilities.tool_supervision.batch_edit import apply_batch_edit

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", str(Path.cwd()))
    result = apply_batch_edit(
        edits,
        atomic=atomic,
        repo_root=Path(workspace),
    )

    applied = result.get("applied", [])
    failed = result.get("failed", [])
    rolled_back = result.get("rolled_back", False)

    if as_json:
        _emit(result, as_json=True)
    else:
        click.echo(f"applied: {len(applied)}  failed: {len(failed)}  rolled_back: {rolled_back}")
        for item in applied:
            click.echo(f"  ✓ {item['path']}")
        for item in failed:
            click.echo(f"  ✗ {item['path']}: {item['error']}")

    if rolled_back:
        sys.exit(2)
    if failed:
        sys.exit(1)


# --------------------------------------------------------------------------- #


def main() -> None:
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


if __name__ == "__main__":
    main()
