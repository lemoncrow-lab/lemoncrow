"""Permanent interactive coding CLI used by lc code and lemoncode."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.console import Console

from lemoncrow.gateway.cli.events import (
    AssistantMessage,
    LemonCrowEvent,
    PermissionRequested,
    RouteSelected,
    RuntimeErrorEvent,
)
from lemoncrow.gateway.cli.keybindings import make_keybindings
from lemoncrow.gateway.cli.render import EventRenderer
from lemoncrow.gateway.cli.runtime import InteractiveRuntime
from lemoncrow.gateway.cli.slash import parse_input
from lemoncrow.pro.capabilities.owned_agent_session import OwnedAgentSession
from lemoncrow.pro.capabilities.owned_agent_session.primer_cache import cached_task_primer

_SLASH_COMMANDS = [
    "/help",
    "/clear",
    "/compact",
    "/tools",
    "/mode",
    "/set-model",
    "/session",
    "/sessions",
    "/exit",
]


async def _consume_events(
    events: AsyncIterator[LemonCrowEvent],
    *,
    runtime: InteractiveRuntime,
    session_id: str,
    renderer: EventRenderer | None,
    owned: OwnedAgentSession,
) -> tuple[str, str]:
    final_text = ""
    last_error = ""
    async for event in events:
        if isinstance(event, RouteSelected):
            owned.provider = event.provider or owned.provider
            owned.model = event.model or owned.model
        if isinstance(event, AssistantMessage):
            final_text = event.text
        elif isinstance(event, RuntimeErrorEvent):
            last_error = event.message
        if renderer is not None:
            await renderer.render(event)
        if isinstance(event, PermissionRequested):
            approved = await asyncio.to_thread(
                click.confirm,
                f"Approve {event.action}?",
                default=False,
            )
            async for response in runtime.respond_to_permission(session_id, event.id, approved):
                if renderer is not None:
                    await renderer.render(response)
    return final_text, last_error


async def _run_code_async(
    *,
    store_root: Path,
    project_root: Path,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    yolo: bool,
    max_cost: float | None,
    prompt: str | None,
    resume: str | None,
    mcp_enabled: bool,
    mcp_schema_mode: str,
    optimization_mode: str = "shadow",
    local_retrieval: str = "auto",
    local_retrieval_model: str = "",
) -> str:
    store_root.mkdir(parents=True, exist_ok=True)
    if resume:
        try:
            owned = OwnedAgentSession.load(resume, root=store_root)
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc
        provider = provider or owned.provider
        model = model or owned.model
    else:
        owned = OwnedAgentSession.new(
            provider=provider,
            model=model,
            transport="litellm",
            cache_policy=cache_policy,
            phase_linear=False,
        )

    if provider and not model:
        raise click.ClickException("--provider requires --model")
    if model:
        from lemoncrow.gateway.cli.commands.run import _resolve_litellm_model

        model = _resolve_litellm_model(provider, model)
        owned.model = model

    runtime = InteractiveRuntime(
        root=store_root,
        yolo=yolo,
        model=model or None,
        provider=provider or None,
        budget_hint=budget,
        cache_policy=cache_policy,
        max_cost=max_cost,
        dynamic_routing=not bool(model),
        mcp_enabled=mcp_enabled,
        mcp_schema_mode=mcp_schema_mode,
        optimization_mode=optimization_mode,
    )
    await runtime.start_session(str(project_root), session_id=owned.session_id)
    if owned.messages:
        runtime.restore_session(owned.session_id, owned.messages)

    console = Console()
    renderer = None if prompt is not None else EventRenderer(console)
    if renderer is not None:
        renderer.print_welcome(session_id=owned.session_id, project_root=str(project_root))

    prompt_session: PromptSession[str] | None = None
    if prompt is None:
        prompt_session = PromptSession(
            history=FileHistory(str(store_root / "code_history")),
            key_bindings=make_keybindings(),
            completer=WordCompleter(_SLASH_COMMANDS, sentence=True),
        )

    first_task = not any(message.get("role") == "user" for message in owned.messages)
    final_text = ""
    pending_prompt = prompt
    try:
        while True:
            if pending_prompt is not None:
                raw = pending_prompt
                pending_prompt = None
            else:
                assert prompt_session is not None
                try:
                    raw = await prompt_session.prompt_async("lemoncode > ")
                except EOFError:
                    break
                except KeyboardInterrupt:
                    continue

            parsed = parse_input(raw)
            if parsed.kind == "empty":
                if prompt is not None:
                    break
                continue
            if parsed.kind == "exit":
                break
            if parsed.kind == "clear":
                console.clear()
                continue
            if parsed.kind == "slash":
                _, last_error = await _consume_events(
                    runtime.handle_slash_command(owned.session_id, parsed.name, parsed.args),
                    runtime=runtime,
                    session_id=owned.session_id,
                    renderer=renderer,
                    owned=owned,
                )
            else:
                primer = ""
                primer_metadata: dict[str, object] = {}
                if first_task:
                    primer_result = await asyncio.to_thread(
                        cached_task_primer,
                        parsed.text,
                        project_root,
                        store_root,
                        retrieval_mode=local_retrieval,
                        local_retrieval_model=local_retrieval_model,
                        optimization_mode=optimization_mode,
                    )
                    primer = primer_result.text
                    primer_metadata = primer_result.optimization_metadata()
                    first_task = False
                final_text, last_error = await _consume_events(
                    runtime.handle_user_message(
                        owned.session_id,
                        parsed.text,
                        budget_hint=budget,
                        context=primer,
                        primer_metadata=primer_metadata,
                    ),
                    runtime=runtime,
                    session_id=owned.session_id,
                    renderer=renderer,
                    owned=owned,
                )

            owned.messages = runtime.session_messages(owned.session_id)
            owned.cache_policy = cache_policy
            owned.save(root=store_root)
            if prompt is not None:
                if last_error and not final_text:
                    raise click.ClickException(last_error)
                break
    finally:
        owned.messages = runtime.session_messages(owned.session_id)
        owned.save(root=store_root)
        runtime.shutdown()
    return final_text


def run_code_cli(
    *,
    store_root: Path,
    project_root: Path,
    provider: str = "",
    model: str = "",
    budget: str = "balanced",
    cache_policy: str = "auto",
    yolo: bool = False,
    max_cost: float | None = None,
    prompt: str | None = None,
    resume: str | None = None,
    mcp_enabled: bool = True,
    mcp_schema_mode: str = "auto",
    optimization_mode: str = "shadow",
    local_retrieval: str = "auto",
    local_retrieval_model: str = "",
) -> None:
    """Run the interactive or one-shot owned coding client."""
    final_text = asyncio.run(
        _run_code_async(
            store_root=store_root,
            project_root=project_root.resolve(),
            provider=provider,
            model=model,
            budget=budget,
            cache_policy=cache_policy,
            yolo=yolo,
            max_cost=max_cost,
            prompt=prompt,
            resume=resume,
            mcp_enabled=mcp_enabled,
            mcp_schema_mode=mcp_schema_mode,
            optimization_mode=optimization_mode,
            local_retrieval=local_retrieval,
            local_retrieval_model=local_retrieval_model,
        )
    )
    if prompt is not None and final_text:
        click.echo(final_text)


__all__ = ["run_code_cli"]
