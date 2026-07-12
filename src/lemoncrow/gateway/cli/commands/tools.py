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
        from lemoncrow.gateway.adapters.mcp_server import (
            TOOLS,
            _tool_description,
            _tool_visible_to_llm,
        )

        tools = [
            {
                "name": name,
                "description": _tool_description(spec),
                "inputSchema": spec.get("inputSchema", {}),
            }
            for name, spec in TOOLS.items()
            if _tool_visible_to_llm(name, spec)
        ]
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
            from lemoncrow.core.foundation.redaction import redact

            op = str(args.get("op") or "")
            if op == "block_upsert" and "value" in args:
                args["value"] = redact(str(args.get("value") or ""))
                if "description" in args:
                    args["description"] = redact(str(args.get("description") or ""))
            elif op == "archive" and "text" in args:
                args["text"] = redact(str(args.get("text") or ""))
        from lemoncrow.gateway.adapters.mcp_server import _Deferred, _handle

        response = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        if response is None:
            raise click.ClickException("tool call returned no response")
        if isinstance(response, _Deferred):
            # Deferral is only armed on the stdio server worker path
            # (_handle_and_write); the in-process CLI never sets that context, so a
            # deferred marker is unreachable here. Guard for type safety.
            raise click.ClickException("tool call returned a deferred result outside the server")
        if "error" in response:
            raise click.ClickException(str(response["error"].get("message") or response["error"]))
        result_payload = response.get("result", {})
        if result_payload.get("isError"):
            content = result_payload.get("content", [])
            text = str(content[0].get("text", "")) if content else "tool execution failed"
            raise click.ClickException(text)
        structured = result_payload.get("structuredContent")
        if structured is not None:
            payload = structured
        else:
            content = result_payload.get("content", [])
            text = str(content[0].get("text", "")) if content else ""
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text
        if as_json and not isinstance(payload, (dict, list)):
            # Tools whose host-facing content is rendered text (read, grep, search,
            # shell, ...) leave only a string here. For --json, recover the full
            # structured result the dispatcher stashed in-process so the caller gets
            # the real dict. CLI-side only -- the MCP host's main model never receives it.
            from lemoncrow.gateway.adapters.mcp_server import _tool_call_raw_result

            raw = getattr(_tool_call_raw_result, "value", None)
            if isinstance(raw, (dict, list)):
                payload = raw
        if as_json:
            _emit(payload, as_json=True)
            return
        if isinstance(payload, (dict, list)):
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return
        click.echo(payload)
    finally:
        restore()
