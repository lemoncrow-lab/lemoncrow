"""Rich-based renderer for LemonCrow interactive CLI events."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from lemoncrow.gateway.cli.events import (
    AssistantDelta,
    AssistantMessage,
    LemonCrowEvent,
    MemoryHit,
    PatchProposed,
    PermissionRequested,
    RouteSelected,
    RuntimeErrorEvent,
    SessionStarted,
    ToolFinished,
    ToolOutput,
    ToolRequested,
    ToolStarted,
    VerificationResult,
)


class EventRenderer:
    """Render LemonCrow runtime events to a Rich console."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._streaming = False
        self._stream_buffer = ""

    def print_welcome(self, *, session_id: str, project_root: str | None) -> None:
        body = Text()
        body.append("LemonCrow interactive runtime\n", style="bold cyan")
        body.append(f"Project: {project_root or '(none)'}\n", style="dim")
        body.append(f"Session: {session_id}", style="dim")
        self._console.print(Panel(body, border_style="cyan", expand=False))

    async def render(self, event: LemonCrowEvent) -> None:
        if isinstance(event, AssistantDelta):
            self.append_delta(event.text)
            return

        # Any non-delta event ends an in-progress stream first.
        if self._streaming:
            self.end_stream()

        if isinstance(event, SessionStarted):
            self._console.print(
                Panel(
                    Text(f"session {event.session_id} started", style="green"),
                    border_style="green",
                    expand=False,
                )
            )
        elif isinstance(event, RouteSelected):
            body = Text()
            body.append(f"provider: {event.provider}\n", style="cyan")
            body.append(f"model: {event.model}\n", style="cyan")
            if event.reason:
                body.append(f"reason: {event.reason}", style="dim")
            self._console.print(Panel(body, title="route", border_style="magenta", expand=False))
        elif isinstance(event, MemoryHit):
            self._console.print(Text(f"• memory: {event.key}", style="yellow"))
            if event.summary:
                self._console.print(Text(f"  {event.summary}", style="dim"))
        elif isinstance(event, AssistantMessage):
            self._console.print(Markdown(event.text))
        elif isinstance(event, ToolRequested):
            self._console.print(Text(f"[tool] {event.name} {_short_args(event.args)}", style="dim"))
        elif isinstance(event, ToolStarted):
            self._console.print(Text(f"… running {event.name}", style="dim"))
        elif isinstance(event, ToolOutput):
            self._console.print(
                Panel(
                    Text(event.chunk, style="dim"),
                    title=f"output [{event.stream}]",
                    border_style="grey50",
                    expand=False,
                )
            )
        elif isinstance(event, ToolFinished):
            mark = "✓" if event.ok else "✗"
            style = "green" if event.ok else "red"
            suffix = "" if event.ok else " (failed)"
            self._console.print(Text(f"{mark} {event.name}{suffix}", style=style))
        elif isinstance(event, PatchProposed):
            self._console.print(
                Panel(
                    Syntax(event.diff, "diff", theme="ansi_dark"),
                    title=f"patch: {', '.join(event.files)}",
                    border_style="blue",
                    expand=False,
                )
            )
        elif isinstance(event, PermissionRequested):
            body = Text()
            body.append(f"{event.action}\n", style="bold")
            body.append(f"risk: {event.risk}", style="dim")
            if event.reason:
                body.append(f"\n{event.reason}", style="dim")
            body.append("\n\nApprove? [/approve | /deny]", style="yellow")
            self._console.print(Panel(body, title="permission requested", border_style="yellow", expand=False))
        elif isinstance(event, VerificationResult):
            style = "green" if event.ok else "red"
            body = Text(f"verification: {'passed' if event.ok else 'failed'}", style=style)
            if event.rubric:
                body.append(f"\nrubric: {event.rubric}", style="dim")
            if event.details:
                body.append(f"\n{event.details}", style="dim")
            self._console.print(Panel(body, border_style=style, expand=False))
        elif isinstance(event, RuntimeErrorEvent):
            body = Text(event.message, style="red")
            if event.details:
                body.append(f"\n{event.details}", style="dim")
            self._console.print(Panel(body, title="error", border_style="red", expand=False))

    def render_exception(self, exc: BaseException) -> None:
        if self._streaming:
            self.end_stream()
        self._console.print(
            Panel(
                Text(f"{type(exc).__name__}: {exc}", style="red"),
                title="error",
                border_style="red",
                expand=False,
            )
        )

    def start_stream(self) -> None:
        self._streaming = True
        self._stream_buffer = ""

    def append_delta(self, text: str) -> None:
        if not self._streaming:
            self.start_stream()
        self._stream_buffer += text
        self._console.print(text, end="", soft_wrap=True, highlight=False, markup=False)

    def end_stream(self) -> None:
        if self._streaming and self._stream_buffer:
            self._console.print()
        self._streaming = False
        self._stream_buffer = ""


def _short_args(args: dict[str, object]) -> str:
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return rendered[:100] + ("…" if len(rendered) > 100 else "")
