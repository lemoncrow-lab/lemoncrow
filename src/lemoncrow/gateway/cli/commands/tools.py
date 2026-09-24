"""Thin ``lc tool-mode`` and ``lc tools`` command groups (QBL-CLI-02).

``tool-mode`` reads/writes the smart-tool shadow-mode state; ``tools`` inspects
and calls the LemonCrow MCP tool surface. The MCP CLI plumbing (``_mcp_cli_args``,
``_prepare_mcp_cli``) is module-private here -- it is used only by ``tools``.
Bodies are copied verbatim from ``app.py``; the groups are standalone
``click.Group``s so ``commands/__init__.py`` can ``add_command`` them without an
import cycle (RESEARCH Pattern 1).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from lemoncrow.gateway.cli.commands._shared import (
    _emit,
    _load_smart_state,
    _save_smart_state,
)


@click.group("tool-mode")
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


def _mcp_cli_args(raw: str) -> dict[str, Any]:
    text = raw
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid JSON args: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException("--args must decode to a JSON object")
    return payload


def _prepare_mcp_cli(ctx: click.Context, *, dev: bool, workspace: Path | None = None) -> Callable[[], None]:
    old_root = os.environ.get("LEMONCROW_ROOT")
    old_workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    old_service_url = os.environ.get("LEMONCROW_SERVICE_URL")
    os.environ["LEMONCROW_ROOT"] = str(ctx.obj["root"])
    if workspace is not None:
        os.environ["CLAUDE_WORKSPACE_ROOT"] = str(workspace)
    if dev:
        # --dev runs MCP tools against the LOCAL handlers. Drop any configured
        # remote service URL for the duration of the call so a configured-but-
        # unreachable LEMONCROW_SERVICE_URL can't turn remote-routed tools
        # (verify/rescue/context/memory/trace) into a 'service unavailable' error.
        os.environ.pop("LEMONCROW_SERVICE_URL", None)

    def restore() -> None:
        if old_root is None:
            os.environ.pop("LEMONCROW_ROOT", None)
        else:
            os.environ["LEMONCROW_ROOT"] = old_root
        if old_workspace is None:
            os.environ.pop("CLAUDE_WORKSPACE_ROOT", None)
        else:
            os.environ["CLAUDE_WORKSPACE_ROOT"] = old_workspace
        if old_service_url is None:
            os.environ.pop("LEMONCROW_SERVICE_URL", None)
        else:
            os.environ["LEMONCROW_SERVICE_URL"] = old_service_url

    return restore


@click.group("tools")
def tools_group() -> None:
    """Inspect and call LemonCrow MCP tools."""


@tools_group.command("list")
@click.option("--dev", is_flag=True, hidden=True, expose_value=False)
@click.option("--json", "as_json", is_flag=True, help="Emit tool metadata as JSON.")
@click.pass_context
def tools_list_cmd(ctx: click.Context, as_json: bool) -> None:
    """List tools visible through MCP tools/list."""
    restore = _prepare_mcp_cli(ctx, dev=False)
    try:
        from lemoncrow.gateway.tools.registry import advertised_tools

        tools = advertised_tools()
        if as_json:
            _emit({"tools": tools}, as_json=True)
            return
        for tool in tools:
            click.echo(tool["name"])
    finally:
        restore()


@tools_group.command("call")
@click.argument("name")
@click.option("--args", "args_json", default="{}", show_default=True, help="JSON object or @path.")
@click.option(
    "--dev",
    "dev",
    is_flag=True,
    hidden=True,
    help="Run against local handlers, bypassing any configured remote LEMONCROW_SERVICE_URL.",
)
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Workspace root for path-scoped MCP tools.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the decoded MCP payload as JSON.")
@click.pass_context
def tools_call_cmd(
    ctx: click.Context, name: str, args_json: str, dev: bool, workspace: Path | None, as_json: bool
) -> None:
    """Call one MCP tool by name."""
    restore = _prepare_mcp_cli(ctx, dev=dev, workspace=workspace)
    try:
        args = _mcp_cli_args(args_json)
        if name == "memory" and isinstance(args, dict):
            from lemoncrow_client.kit.redaction import redact

            op = str(args.get("op") or "")
            if op == "block_upsert" and "value" in args:
                args["value"] = redact(str(args.get("value") or ""))
                if "description" in args:
                    args["description"] = redact(str(args.get("description") or ""))
            elif op == "archive" and "text" in args:
                args["text"] = redact(str(args.get("text") or ""))
        from lemoncrow.gateway.tools.registry import call_registered_tool
        from lemoncrow.gateway.tools.rendering import render_tool_result_text

        try:
            payload = call_registered_tool(name, args)
        except KeyError:
            raise click.ClickException(f"unknown tool: {name}") from None
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

        if not as_json:
            rendered = render_tool_result_text(name, payload)
            if rendered is not None:
                payload = rendered
        if as_json:
            _emit(payload, as_json=True)
            return
        if isinstance(payload, (dict, list)):
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return
        click.echo(payload)
    finally:
        restore()
