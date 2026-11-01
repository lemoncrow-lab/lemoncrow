"""LemonCrow's terminal look: one palette and glyph set for every human-facing screen.

It matches ``scripts/install.sh`` (brand purple 141, ``◆`` titles, ``✓ ⚠ ✗ ○``
states, ``→`` next steps) so the installer and the CLI read as one product.

Every helper returns a string. Print it with ``click.echo``: click drops the
styling when the stream is not a terminal, and ``NO_COLOR`` (no-color.org)
turns it off everywhere. Pad text *before* styling it; ANSI codes have no width.
"""

from __future__ import annotations

import functools
import os
import shutil
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Final, Literal

import click

BRAND: Final[int] = 141
"""256-colour brand purple, shared with ``scripts/install.sh``."""

State = Literal["ok", "warn", "fail", "off"]

_MARKS: Final[dict[str, tuple[str, dict[str, Any]]]] = {
    "ok": ("✓", {"fg": "green"}),
    "warn": ("⚠", {"fg": "yellow"}),
    "fail": ("✗", {"fg": "red"}),
    "off": ("○", {"dim": True}),
}


def color_enabled() -> bool:
    """False under ``NO_COLOR`` (no-color.org) or ``TERM=dumb``; click also strips styling off-terminal."""
    return not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(text: str, **styles: Any) -> str:
    return click.style(text, **styles) if color_enabled() and text else text


def accent(text: str) -> str:
    return paint(text, fg=BRAND)


def bold(text: str) -> str:
    return paint(text, bold=True)


def dim(text: str) -> str:
    return paint(text, dim=True)


def command(text: str) -> str:
    """A command the user can type."""
    return paint(text, fg="cyan")


def mark(state: State) -> str:
    glyph, styles = _MARKS[state]
    return paint(glyph, **styles)


def title(name: str, detail: str = "") -> str:
    """The first line of a screen: ``◆ LemonCrow 0.8.0  detail``."""
    line = f"{accent('◆')} {bold(name)}"
    return f"{line}  {dim(detail)}" if detail else line


def heading(text: str) -> str:
    return paint(text, fg=BRAND, bold=True)


def row(state: State | None, label: str, value: str = "", *, width: int = 12, indent: int = 2) -> str:
    """One aligned status row: ``  ✓ label       value``."""
    lead = " " * indent + (f"{mark(state)} " if state else "")
    if not value:
        return f"{lead}{label}"
    return f"{lead}{label.ljust(width)} {value}"


def next_step(text: str, cmd: str, *, indent: int = 2) -> str:
    """``  → text  cmd`` — what to run next."""
    return f"{' ' * indent}{accent('→')} {text} {command(cmd)}"


def tagged(lines: Iterable[tuple[str, str]]) -> list[str]:
    """Style report lines a core renderer tagged, so core code never imports the CLI.

    Tags: ``title`` (``name\tdetail``), ``heading``, ``total`` (bold), ``dim``,
    ``warn`` (a ``⚠`` line), ``next`` (``label\tcommand``); anything else is
    printed as it is.
    """
    out: list[str] = []
    for tag, text in lines:
        if tag == "title":
            name, _, detail = text.partition("\t")
            out.append(title(name, detail))
        elif tag == "next":
            label, _, cmd = text.partition("\t")
            out.append(next_step(label, cmd))
        elif tag == "warn":
            out.append(f"  {mark('warn')} {text}")
        else:
            styler = {"heading": heading, "total": bold, "dim": dim}.get(tag)
            out.append(styler(text) if styler and text.strip() else text)
    return out


def success(text: str) -> str:
    return f"{mark('ok')} {text}"


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def ago(seconds: float) -> str:
    """``3m ago``, ``2h ago``, ``5d ago``: how long ago, in its largest whole unit."""
    whole = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if whole >= size:
            return f"{whole // size}{unit} ago"
    return "just now"


def terminal_width(default: int) -> int:
    """The terminal's full width; ``default`` when stdout is a pipe or file, so scripted output stays stable."""
    if not _is_terminal(sys.stdout):
        return default
    return shutil.get_terminal_size((default, 24)).columns


def home_relative(path: str | Path) -> str:
    """``~/x`` for paths under the home directory; long absolute paths are noise."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


# ── waiting ────────────────────────────────────────────────────────────────────────

PROGRESS_DELAY_S: Final[float] = 0.2
"""Work that finishes sooner shows no indicator: a flash reads as a glitch, silence past this as a hang."""


@contextmanager
def after_delay(
    start: Callable[[], object], stop: Callable[[], object], delay: float = PROGRESS_DELAY_S
) -> Iterator[None]:
    """Call ``start`` only if the block outlasts ``delay``; then ``stop`` when it ends.

    A timer (not a progress callback) opens the indicator, so a block that
    waits silently, e.g. on a lock, still shows that it is alive.
    """
    guard = threading.Lock()
    state = {"open": False, "finished": False}

    def open_indicator() -> None:
        with guard:
            if not state["finished"]:
                start()
                state["open"] = True

    timer = threading.Timer(delay, open_indicator)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        with guard:
            state["finished"] = True
            if state["open"]:
                stop()
        timer.join()


@contextmanager
def working(message: str) -> Iterator[None]:
    """A transient spinner on stderr while a slow lookup runs; nothing off-terminal."""
    from rich.console import Console
    from rich.markup import escape

    console = Console(stderr=True)
    if not console.is_terminal:
        yield
        return
    status = console.status(f"[dim]{escape(message)}[/dim]", spinner="dots", spinner_style=f"color({BRAND})")
    with after_delay(status.start, status.stop):
        yield


# ── click integration ─────────────────────────────────────────────────────


class HelpFormatter(click.HelpFormatter):
    """Click help with brand headings and highlighted command/option names.

    Help text is built before anyone knows where it is printed, and callers
    such as tests read ``get_help()`` directly, so it is only styled when
    stdout is a terminal.
    """

    def __init__(self, indent_increment: int = 2, width: int | None = None, max_width: int | None = None) -> None:
        super().__init__(indent_increment, width, max_width)
        self._styled = color_enabled() and _is_terminal(sys.stdout)

    def write_usage(self, prog: str, args: str = "", prefix: str | None = None) -> None:
        if prefix is None and self._styled:
            prefix = bold("Usage: ")
        super().write_usage(prog, args, prefix=prefix)

    def write_heading(self, heading_text: str) -> None:
        if not self._styled:
            super().write_heading(heading_text)
            return
        self.write(f"{'':>{self.current_indent}}{heading(heading_text + ':')}\n")

    def write_dl(self, rows: Iterable[tuple[str, str]], col_max: int = 30, col_spacing: int = 2) -> None:
        # click measures the first column with term_len (ANSI-free), so
        # styling it keeps the alignment intact.
        if self._styled:
            rows = [(command(first), second) for first, second in rows]
        super().write_dl(rows, col_max, col_spacing)


def _is_terminal(stream: IO[Any]) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


class Context(click.Context):
    formatter_class = HelpFormatter


def adopt(root: click.Command) -> None:
    """Give ``root`` and every statically registered subcommand LemonCrow help styling."""
    stack = [root]
    while stack:
        current = stack.pop()
        current.context_class = Context
        if isinstance(current, click.Group):
            stack.extend(current.commands.values())


def short_help_rows(
    formatter: click.HelpFormatter, commands: Sequence[tuple[str, click.Command]]
) -> list[tuple[str, str]]:
    """Command-list rows whose one-liners use the full help width instead of click's 45 columns."""
    if not commands:
        return []
    limit = max(formatter.width - 6 - max(len(name) for name, _ in commands), 45)
    return [(name, cmd.get_short_help_str(limit)) for name, cmd in commands]


class Redirect(click.UsageError):
    """A usage error whose fix is one exact command: ``→ {text} {command}`` replaces the help hint."""

    def __init__(self, message: str, text: str, command: str, ctx: click.Context | None = None) -> None:
        super().__init__(message, ctx=ctx)
        self.text = text
        self.command = command


def show_error(error: click.ClickException, file: IO[Any] | None = None) -> None:
    """Render a click error: the problem first, then how to get unstuck."""
    stream = file if file is not None else sys.stderr
    ctx = error.ctx if isinstance(error, click.UsageError) else None
    color = ctx.color if ctx is not None else None
    click.echo(f"{paint('✗ Error:', fg='red', bold=True)} {error.format_message()}", file=stream, color=color)
    if isinstance(error, Redirect):
        click.echo(next_step(error.text, error.command), file=stream, color=color)
        return
    if ctx is None:
        return
    # A group's usage line ("lc [OPTIONS] COMMAND [ARGS]...") says nothing a
    # typo'd command name needs; a command's usage shows the arguments it wants.
    if not isinstance(ctx.command, click.Group):
        click.echo(f"\n  {dim(click.unstyle(ctx.get_usage()))}", file=stream, color=color)
    if ctx.command.get_help_option(ctx) is not None:
        names = ctx.command.get_help_option_names(ctx)
        click.echo(
            next_step("Try", f"{ctx.command_path} {max(names, key=len)}") + dim(" for help."),
            file=stream,
            color=color,
        )


@contextmanager
def styled_errors() -> Iterator[None]:
    """Re-raise click errors so that click's own exit handling prints them with :func:`show_error`."""
    try:
        yield
    except click.ClickException as error:
        # A bare group (`lc session`) raises its own help as a usage error; that is not a failure.
        if not isinstance(error, _NO_ARGS_IS_HELP):
            error.show = functools.partial(show_error, error)  # type: ignore[method-assign]
        raise


_NO_ARGS_IS_HELP: Final[tuple[type[Exception], ...]] = tuple(
    cls for cls in (getattr(click.exceptions, "NoArgsIsHelpError", None),) if cls is not None
)


__all__ = [
    "BRAND",
    "PROGRESS_DELAY_S",
    "Context",
    "HelpFormatter",
    "Redirect",
    "State",
    "accent",
    "adopt",
    "after_delay",
    "ago",
    "bold",
    "color_enabled",
    "command",
    "dim",
    "heading",
    "home_relative",
    "mark",
    "next_step",
    "paint",
    "plural",
    "row",
    "short_help_rows",
    "show_error",
    "styled_errors",
    "success",
    "tagged",
    "terminal_width",
    "title",
    "working",
]
