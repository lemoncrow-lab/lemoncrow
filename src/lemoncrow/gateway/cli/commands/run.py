from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from lemoncrow.core.capabilities.providers.config import LITELLM_PREFIX
from lemoncrow.gateway.cli.commands._shared import _emit

if TYPE_CHECKING:
    from lemoncrow.core.capabilities.owned_agent_session import OwnedAgentSession
    from lemoncrow.core.capabilities.owned_agent_session.receipt import SessionReceipt


def _resolve_litellm_model(provider: str, model: str) -> str:
    """Auto-prefix *model* with the litellm provider prefix when needed.

    Examples::

        _resolve_litellm_model("openai", "gpt-4o")          # "openai/gpt-4o"
        _resolve_litellm_model("anthropic", "claude-opus-4-8")  # "anthropic/claude-opus-4-8"
        _resolve_litellm_model("openai", "openai/gpt-4o")   # "openai/gpt-4o" (already prefixed)
        _resolve_litellm_model("", "openai/gpt-4o")         # "openai/gpt-4o" (already prefixed)
    """
    if not model:
        return model
    if "/" in model:
        return model  # already provider-prefixed
    prefix = LITELLM_PREFIX.get(provider.lower(), "")
    return f"{prefix}{model}" if prefix else model


def _root_from_obj(obj: dict[str, Any]) -> Path:
    if isinstance(obj, dict):
        root = obj.get("root")
        if isinstance(root, Path):
            return root
    return Path.home() / ".lemoncrow"


def _execute_owned_tool_session(
    task: str,
    *,
    provider: str,
    model: str,
    yolo: bool,
    root: Path,
) -> tuple[OwnedAgentSession, SessionReceipt, str]:
    """Execute one real model/tool loop shared with the TUI and HTTP gateway."""
    import asyncio

    from lemoncrow.core.capabilities.owned_agent_session import OwnedAgentSession
    from lemoncrow.core.capabilities.owned_agent_session.receipt import PhaseTokens, SessionReceipt
    from lemoncrow.core.capabilities.owned_agent_session.task_primer import build_task_primer
    from lemoncrow.gateway.cli.events import AssistantMessage, ContextUsageUpdated, RuntimeErrorEvent
    from lemoncrow.gateway.cli.runtime import InteractiveRuntime

    session = OwnedAgentSession.new(
        provider=provider,
        model=model,
        transport="litellm",
        phase_linear=False,
    )
    runtime = InteractiveRuntime(root=root, yolo=yolo, model=model, provider=provider)

    primer = build_task_primer(task, Path(os.getcwd()))
    prompt = f"{task}\n\n{primer}" if primer else task

    async def _run() -> tuple[str, ContextUsageUpdated | None]:
        await runtime.start_session(os.getcwd(), session_id=session.session_id)
        final_text = ""
        usage: ContextUsageUpdated | None = None
        try:
            async for event in runtime.handle_user_message(session.session_id, prompt):
                if isinstance(event, AssistantMessage):
                    final_text = event.text
                elif isinstance(event, ContextUsageUpdated):
                    usage = event
                elif isinstance(event, RuntimeErrorEvent):
                    raise RuntimeError(event.message)
        finally:
            session.messages = runtime.session_messages(session.session_id)
            runtime.shutdown()
        return final_text, usage

    try:
        final_text, usage = asyncio.run(_run())
    except Exception:
        session.save(root=root)
        raise
    receipt = SessionReceipt(
        session_id=session.session_id,
        provider=provider,
        model=model,
        turn_count=sum(1 for message in session.messages if message.get("role") == "assistant"),
    )
    receipt.phases.append(
        PhaseTokens(
            phase="agent",
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
        )
    )
    return session, receipt, final_text


def _run_owned_session(
    task: str,
    *,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    phase_linear: bool,
    max_cost: float | None,
    yolo: bool,
    dry_run: bool,
    root: Path,
) -> None:
    from lemoncrow.core.capabilities.owned_agent_session import (
        OwnedAgentSession,
        run_phase_linear,
        run_single_shot,
    )
    from lemoncrow.core.capabilities.owned_execution_routing import (
        OwnedCachePolicy,
        OwnedRouteBudget,
        OwnedRouteRequest,
        select_owned_route,
    )
    from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import (
        detect_api_key_vendors,
    )
    from lemoncrow.pro.capabilities.cross_vendor_routing.router import NoFeasibleRouteError

    # Credential check — fail fast with actionable message
    vendors = detect_api_key_vendors()
    if not vendors:
        click.echo(
            "Error: No API key found.\n\n"
            "Set one of the following environment variables (or add to .env):\n"
            "  ANTHROPIC_API_KEY   — Anthropic / Claude models\n"
            "  OPENAI_API_KEY      — OpenAI / ChatGPT models\n"
            "  GOOGLE_API_KEY      — Google / Gemini models\n"
            "  AWS_ACCESS_KEY_ID   — AWS Bedrock (+ AWS_SECRET_ACCESS_KEY)\n"
            "  AWS_PROFILE         — AWS Bedrock (named profile)\n"
            "  VERTEXAI_PROJECT    — GCP Vertex AI\n"
            "  AZURE_API_KEY       — Azure OpenAI (+ AZURE_API_BASE)\n"
            "  (any other litellm-compatible key, e.g. GROQ_API_KEY, MISTRAL_API_KEY)\n",
            err=True,
        )
        sys.exit(1)

    budget_cast: OwnedRouteBudget = (
        budget if budget in ("cheap", "balanced", "best") else "balanced"  # type: ignore[assignment]
    )
    cache_policy_cast: OwnedCachePolicy = "fresh" if cache_policy == "fresh" else "inherit"

    # Auto-prefix bare model names with litellm provider prefix
    resolved_model = _resolve_litellm_model(provider, model)

    try:
        decision = select_owned_route(
            root,
            OwnedRouteRequest(
                tool_name="run",
                task_text=task,
                mode="explicit" if (provider or resolved_model) else "auto",
                budget=budget_cast,
                provider=provider,
                model=resolved_model,
                cache_policy=cache_policy_cast,
            ),
        )
    except NoFeasibleRouteError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Ensure the final model is litellm-prefixed
    litellm_model = _resolve_litellm_model(decision.provider, decision.model)

    final_text = ""
    if dry_run:
        session = OwnedAgentSession.new(
            provider=decision.provider,
            model=litellm_model,
            transport=decision.transport,
            cache_policy=cache_policy_cast,
            phase_linear=phase_linear,
        )
        click.echo(f"[lc run] session={session.session_id}  provider={decision.provider}  model={litellm_model}")
        click.echo("[lc run] --dry-run: planning only, no edits will be applied")
        receipt = (
            run_phase_linear(session, task, dry_run=True)
            if phase_linear
            else run_single_shot(session, task, dry_run=True)
        )
    else:
        session, receipt, final_text = _execute_owned_tool_session(
            task,
            provider=decision.provider,
            model=litellm_model,
            yolo=yolo,
            root=root,
        )
        session.cache_policy = cache_policy_cast
        click.echo(f"[lc run] session={session.session_id}  provider={decision.provider}  model={litellm_model}")

    if max_cost is not None and receipt.cost_usd() > max_cost:
        click.echo(
            f"\nWarning: session cost ${receipt.cost_usd():.4f} exceeded --max-cost ${max_cost:.4f}",
            err=True,
        )

    session_path = session.save()
    if final_text:
        click.echo(f"\n{final_text}")
    click.echo(f"\nSession saved: {session_path}")
    click.echo("")
    click.echo(receipt.format_receipt())


def _select_print_route(
    task: str,
    *,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    root: Path,
) -> tuple[str, str, str, str]:
    """Return provider, litellm model, transport, and normalized cache policy for one-shot runs."""
    from lemoncrow.core.capabilities.owned_execution_routing import (
        OwnedCachePolicy,
        OwnedRouteBudget,
        OwnedRouteRequest,
        select_owned_route,
    )
    from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import (
        detect_api_key_vendors,
    )
    from lemoncrow.pro.capabilities.cross_vendor_routing.router import (
        NoFeasibleRouteError,
    )

    vendors = detect_api_key_vendors()
    if not vendors:
        raise click.ClickException(
            "No API key configured. Set one of ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, "
            "AWS_ACCESS_KEY_ID/AWS_PROFILE, VERTEXAI_PROJECT, AZURE_API_KEY, or another LiteLLM-compatible "
            "provider key."
        )

    budget_cast: OwnedRouteBudget = (
        budget if budget in ("cheap", "balanced", "best") else "balanced"  # type: ignore[assignment]
    )
    cache_policy_cast: OwnedCachePolicy = "fresh" if cache_policy == "fresh" else "inherit"

    try:
        decision = select_owned_route(
            root,
            OwnedRouteRequest(
                tool_name="run",
                task_text=task,
                mode="explicit" if (provider or model) else "auto",
                budget=budget_cast,
                provider=provider,
                model=model,
                cache_policy=cache_policy_cast,
            ),
        )
    except NoFeasibleRouteError as exc:
        raise click.ClickException(str(exc)) from exc

    litellm_model = _resolve_litellm_model(decision.provider, decision.model)
    return decision.provider, litellm_model, decision.transport, cache_policy_cast


def _run_print_session(
    task: str,
    *,
    provider: str = "",
    model: str = "",
    budget: str = "balanced",
    cache_policy: str = "inherit",
    root: Path,
) -> str:
    """Run one prompt and return only the final assistant text."""
    selected_provider, litellm_model, _transport, cache_policy_cast = _select_print_route(
        task,
        provider=provider,
        model=model,
        budget=budget,
        cache_policy=cache_policy,
        root=root,
    )
    session, _receipt, final_text = _execute_owned_tool_session(
        task,
        provider=selected_provider,
        model=litellm_model,
        yolo=True,
        root=root,
    )
    session.cache_policy = cache_policy_cast
    session.save(root=root)
    return final_text


def _run_ci_session(
    task: str,
    *,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    phase_linear: bool,
    output_format: str,
    root: Path,
) -> None:
    """Non-interactive CI session — outputs JSON."""
    try:
        selected_provider, litellm_model, _transport, cache_policy_cast = _select_print_route(
            task,
            provider=provider,
            model=model,
            budget=budget,
            cache_policy=cache_policy,
            root=root,
        )
    except click.ClickException as exc:
        error = "no_api_key" if str(exc).startswith("No API key configured") else "no_route"
        sys.stdout.write(json.dumps({"error": error, "message": str(exc)}) + "\n")
        raise SystemExit(1) from exc

    session, receipt, final_text = _execute_owned_tool_session(
        task,
        provider=selected_provider,
        model=litellm_model,
        yolo=True,
        root=root,
    )
    session.cache_policy = cache_policy_cast
    session_path = session.save()

    output = {
        "session_id": session.session_id,
        "model": litellm_model,
        "provider": selected_provider,
        "phases": [p.phase for p in receipt.phases],
        "receipt": receipt.to_dict(),
        "session_path": str(session_path),
        "result": final_text,
        "exit_code": 0,
    }
    sys.stdout.write(json.dumps(output) + "\n")


@click.group("run")
def run_group() -> None:
    """Run an owned coding session on your own API credentials."""


@run_group.command("start", context_settings={"ignore_unknown_options": False})
@click.argument("task")
@click.option("--provider", default="", help="Provider: anthropic, openai, google, groq, mistral, …")
@click.option(
    "--model",
    default="",
    help="Model name (bare or litellm-prefixed, e.g. gpt-4o or openai/gpt-4o)",
)
@click.option(
    "--budget",
    type=click.Choice(["cheap", "balanced", "best"]),
    default="balanced",
    show_default=True,
)
@click.option(
    "--cache-policy",
    type=click.Choice(["inherit", "fresh"]),
    default="inherit",
    show_default=True,
)
@click.option(
    "--phase-linear/--no-phase-linear",
    default=True,
    show_default=True,
    help="Survey→Plan→Implement in one conversation",
)
@click.option("--max-cost", type=float, default=None, help="Abort if cost exceeds this USD amount")
@click.option("--yolo", is_flag=True, default=False, help="Skip edit-approval prompts")
@click.option("--dry-run", is_flag=True, default=False, help="Preview plan without applying edits")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output structured JSON (for CI pipelines)",
)
@click.option(
    "--output-format",
    type=click.Choice(["text", "json", "stream-json"]),
    default="text",
)
@click.pass_obj
def run_start(
    obj: dict[str, Any],
    task: str,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    phase_linear: bool,
    max_cost: float | None,
    yolo: bool,
    dry_run: bool,
    output_json: bool,
    output_format: str,
) -> None:
    """Run an owned coding session. TASK is the coding task description."""
    root = _root_from_obj(obj)
    if output_json or output_format in ("json", "stream-json"):
        _run_ci_session(
            task,
            provider=provider,
            model=model,
            budget=budget,
            cache_policy=cache_policy,
            phase_linear=phase_linear,
            output_format="json" if output_json else output_format,
            root=root,
        )
        return
    _run_owned_session(
        task,
        provider=provider,
        model=model,
        budget=budget,
        cache_policy=cache_policy,
        phase_linear=phase_linear,
        max_cost=max_cost,
        yolo=yolo,
        dry_run=dry_run,
        root=root,
    )


@run_group.command("resume")
@click.argument("session_id")
@click.option("--task", default="", help="Additional task to continue with")
@click.pass_obj
def run_resume(obj: dict[str, Any], session_id: str, task: str) -> None:
    """Resume a session with its warm prefix intact."""
    from lemoncrow.core.capabilities.owned_agent_session import OwnedAgentSession, run_phase_linear

    root = _root_from_obj(obj)

    try:
        session = OwnedAgentSession.load(session_id, root=root)
    except FileNotFoundError:
        click.echo(f"Error: session {session_id!r} not found in {root / 'runs'}", err=True)
        sys.exit(1)

    click.echo(f"[lc run resume] session={session.session_id}  provider={session.provider}  model={session.model}")
    click.echo(f"  Restoring {len(session.messages)} turns from previous session")

    if not task:
        click.echo("No --task provided; displaying saved receipt.")
        _print_receipt_from_session(session)
        return

    receipt = run_phase_linear(session, task)
    session.save(root=root)
    click.echo(receipt.format_receipt())


@run_group.command("report")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def run_report(obj: dict[str, Any], session_id: str, as_json: bool) -> None:
    """Display the cache-economics receipt for a past session."""
    from lemoncrow.core.capabilities.owned_agent_session import OwnedAgentSession
    from lemoncrow.core.capabilities.owned_agent_session.receipt import SessionReceipt

    root = _root_from_obj(obj)

    try:
        session = OwnedAgentSession.load(session_id, root=root)
    except FileNotFoundError:
        click.echo(f"Error: session {session_id!r} not found in {root / 'runs'}", err=True)
        sys.exit(1)

    receipt = SessionReceipt(
        session_id=session.session_id,
        provider=session.provider,
        model=session.model,
    )

    if as_json:
        _emit(receipt.to_dict(), as_json=True)
    else:
        click.echo(receipt.format_receipt())


def _print_receipt_from_session(session: OwnedAgentSession) -> None:
    from lemoncrow.core.capabilities.owned_agent_session.receipt import SessionReceipt

    receipt = SessionReceipt(
        session_id=session.session_id,
        provider=session.provider,
        model=session.model,
    )
    click.echo(receipt.format_receipt())


__all__ = ["run_group"]
