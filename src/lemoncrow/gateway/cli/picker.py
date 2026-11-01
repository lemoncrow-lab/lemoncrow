"""An inline terminal picker: pages of rows you move through, open, search and edit.

``lc settings``, ``lc rt map`` and ``lc session`` share it. A :class:`Source`
supplies the pages and decides what Enter and any extra keys do;
:class:`Picker` is the plain-Python state machine every key press drives, so
behaviour is tested without a terminal; :func:`run` is the prompt_toolkit
shell that draws a picker inline (no alternate screen) and erases itself when
done, leaving the shell's scrollback as it was.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from prompt_toolkit.utils import get_cwidth

from lemoncrow.gateway.cli import style

Fragment = tuple[str, str]
Line = list[Fragment]
Mode = Literal["browse", "search", "edit"]

POINTER: Final[str] = "\u276f"  # heavy right angle: the selected row
CRUMB: Final[str] = "\u203a"  # single right angle: breadcrumb separator
_GLYPHS: Final[dict[str, str]] = {"ok": "✓", "warn": "⚠", "fail": "✗", "off": "○"}


@dataclass(frozen=True)
class Row:
    label: str = ""
    value: str = ""
    note: str = ""
    """Dim text after the value, cut to the line."""
    flag: Literal["", "changed", "env"] = ""
    """``•`` before a value changed from its default, ``⚠`` before one the environment overrides."""
    kind: Literal["item", "heading", "note", "gap"] = "item"
    """``heading``, ``note`` (a dim line) and ``gap`` rows can't be selected."""
    ref: Any = None
    """What the row stands for, for the source."""
    style: str = ""
    """A text page's line style (``class:dim``, ``class:heading`` …)."""
    spans: tuple[Fragment, ...] = ()
    """A text page's line in styled pieces (highlighted code); ``label`` keeps its plain text."""

    @property
    def selectable(self) -> bool:
        return self.kind == "item"


def heading(label: str) -> Row:
    return Row(label, kind="heading")


def note(text: str) -> Row:
    return Row(text, kind="note")


GAP: Final[Row] = Row(kind="gap")


@dataclass(frozen=True)
class Edit:
    """A request to type a value, shown after the pointer as ``{prompt}``."""

    ref: Any
    prompt: str
    initial: str = ""
    secret: bool = False


@dataclass(frozen=True)
class Outcome:
    page: Page | None = None
    """Open this page on top."""
    edit: Edit | None = None
    message: tuple[style.State, str] | None = None
    close: bool = False
    """Go back one page (after picking from a list)."""
    run: Callable[[], None] | None = None
    """Hand the terminal to this (an editor) and come back to the picker."""


@dataclass(frozen=True)
class Key:
    """An extra single-key action on a page, e.g. ``r`` to reset."""

    key: str
    hint: str
    act: Callable[[Page, Row], Outcome | None]
    when: Callable[[Row], bool] | None = None
    """Rows the key applies to (its hint shows only on them); None: every row."""

    def applies(self, row: Row | None) -> bool:
        return row is not None and (self.when is None or self.when(row))


@dataclass
class Page:
    title: str
    load: Callable[[str], list[Row]]
    """Rows for the current query (``""`` unless the page is searchable); re-run after every change."""
    searchable: bool = False
    text: bool = False
    """A page of text lines: arrows scroll, nothing is selected."""
    enter: str = "open"
    keys: tuple[Key, ...] = ()
    empty: str = "Nothing here."
    ref: Any = None
    rows: list[Row] = field(default_factory=list)
    cursor: int = 0
    offset: int = 0
    query: str = ""

    def reload(self) -> None:
        self.rows = self.load(self.query)
        if self.text:
            return
        self.cursor = min(self.cursor, max(len(self.rows) - 1, 0))
        if self.rows and not self.rows[self.cursor].selectable:
            self.cursor = next((i for i, row in enumerate(self.rows) if row.selectable), 0)


class Source(Protocol):
    title: str

    def home(self) -> Page: ...

    def open(self, page: Page, row: Row) -> Outcome | None: ...

    def describe(self, page: Page, row: Row | None, width: int) -> list[Line]:
        """Footer lines about the selected row (description, facts)."""
        ...

    def search(self) -> Page | None:
        """The page ``/`` opens, or None for no search."""
        ...

    def submit(self, edit: Edit, text: str) -> Outcome:
        """Save a typed value; a ``fail`` message keeps the edit open."""
        ...


class Picker:
    """Where the picker is and what it shows; every key press is one method call."""

    def __init__(self, source: Source) -> None:
        self.source = source
        home = source.home()
        home.reload()
        self.stack = [home]
        self.editing: Edit | None = None
        self.message: tuple[style.State, str] | None = None
        self.done = False
        self.pending: Callable[[], None] | None = None
        """Work that needs the terminal (an editor); the shell runs it."""

    @property
    def page(self) -> Page:
        return self.stack[-1]

    @property
    def mode(self) -> Mode:
        if self.editing is not None:
            return "edit"
        return "search" if self.page.searchable else "browse"

    def selected(self) -> Row | None:
        page = self.page
        if page.text or not page.rows or not page.rows[page.cursor].selectable:
            return None
        return page.rows[page.cursor]

    # ── keys ──

    def move(self, delta: int) -> None:
        page = self.page
        self.message = None
        if page.text:
            page.offset = max(min(page.offset + delta, len(page.rows) - 1), 0)
            return
        rows, cursor = page.rows, page.cursor
        step = 1 if delta > 0 else -1
        for _ in range(abs(delta)):
            nxt = cursor + step
            while 0 <= nxt < len(rows) and not rows[nxt].selectable:
                nxt += step
            if not 0 <= nxt < len(rows):
                break
            cursor = nxt
        page.cursor = cursor

    def activate(self) -> None:
        row = self.selected()
        if row is not None:
            self.message = None
            self._apply(self.source.open(self.page, row))

    def press(self, key: str) -> bool:
        """Run the current page's action bound to ``key``; False when there is none."""
        page = self.page
        row = self.selected() or (Row(ref=page.ref) if page.text and page.ref is not None else None)
        for action in self.page.keys:
            if action.key == key and action.applies(row):
                assert row is not None
                self.message = None
                self._apply(action.act(self.page, row))
                return True
        return False

    def submit(self, text: str) -> bool:
        if self.editing is None:
            return False
        outcome = self.source.submit(self.editing, text)
        if outcome.message is not None and outcome.message[0] == "fail":
            self.message = outcome.message
            return False
        self.editing = None
        self._apply(outcome)
        return True

    def start_search(self) -> None:
        page = self.source.search()
        if page is not None:
            self.message = None
            self._push(page)

    def set_query(self, query: str) -> None:
        page = self.page
        if page.searchable:
            page.query = query
            page.cursor = page.offset = 0
            page.reload()

    def back(self) -> None:
        self.message = None
        if self.editing is not None:
            self.editing = None
        elif len(self.stack) > 1:
            self.stack.pop()
            self.page.reload()
        else:
            self.done = True

    def _push(self, page: Page) -> None:
        page.reload()
        self.stack.append(page)

    def _apply(self, outcome: Outcome | None) -> None:
        if outcome is None:
            return
        if outcome.message is not None:
            self.message = outcome.message
        if outcome.run is not None:
            self.pending = outcome.run
        if outcome.close and len(self.stack) > 1:
            self.stack.pop()
        if outcome.edit is not None:
            self.editing = outcome.edit
        if outcome.page is not None:
            self.page.reload()  # what is underneath may have changed
            self._push(outcome.page)
        else:
            self.page.reload()

    # ── drawing ──

    def header(self, width: int) -> Line:
        crumbs = "".join(f"  {CRUMB}  {page.title}" for page in self.stack[1:])
        name = self.source.title
        return [
            ("class:brand", "◆ "),
            ("class:title", name),
            ("", fit(crumbs, width - cells(name) - 2)),
            ("", "\n"),
        ]

    def body(self, width: int, height: int) -> list[Fragment]:
        page = self.page
        rows = page.rows
        if not rows:
            return [("class:dim", fit(f"    {page.empty}", width))]
        height = max(height, 3)
        if page.text:
            page.offset = max(min(page.offset, len(rows) - height), 0)
            return _join(
                [
                    (
                        fit_spans([("", "  "), *row.spans], width)
                        if row.spans
                        else [(row.style, fit(f"  {row.label}", width))]
                    )
                    for row in rows[page.offset : page.offset + height]
                ]
            )
        if page.cursor < page.offset:
            page.offset = page.cursor
        elif page.cursor >= page.offset + height:
            page.offset = page.cursor - height + 1
        page.offset = max(min(page.offset, len(rows) - height), 0)
        if page.offset and page.offset == page.cursor and not rows[page.offset - 1].selectable:
            page.offset -= 1  # keep a section heading with its first row

        shown = rows[page.offset : page.offset + height]
        label_max = max((cells(r.label) for r in rows if r.selectable), default=0)
        value_max = min(max((cells(r.value) for r in shown if r.selectable), default=0), 24)
        label_w, value_w = min(label_max, max(width // 3, 12)), value_max
        note_w = width - (4 + label_w + 2 + 2 + value_w + 2)
        if note_w < 12:  # no room for notes (the footer describes the row): names and values get the line
            value_w = min(value_max, max(width // 4, 6))
            label_w = min(label_max, max(width - 8 - value_w, 6))

        lines: list[Line] = []
        for index, row in enumerate(shown):
            if row.kind == "heading":
                lines.append([("class:heading", fit(f"  {row.label}", width))])
                continue
            if row.kind == "note":
                lines.append([("class:dim", fit(f"  {row.label}", width))])
                continue
            if row.kind == "gap":
                lines.append([])
                continue
            here = page.offset + index == page.cursor
            line: Line = [("class:pointer", f"  {POINTER} ") if here else ("", "    ")]
            line.append(("class:selected" if here else "", pad(fit(row.label, label_w), label_w)))
            if value_w:
                mark, mark_style = {"changed": ("• ", "class:brand"), "env": ("⚠ ", "class:warn")}.get(
                    row.flag, ("  ", "")
                )
                line += [
                    ("", "  "),
                    (mark_style, mark),
                    ("class:changed" if row.flag else "", pad(fit(row.value, value_w), value_w)),
                ]
            if row.note and note_w >= 12:
                line += [("", "  "), ("class:dim", fit(row.note, note_w))]
            lines.append(line)
        return _join(lines)

    def footer(self, width: int, height: int) -> list[Fragment]:
        lines: list[Line] = []
        if not self.page.text:
            lines += self.source.describe(self.page, self.selected(), width)
        if self.message is not None:
            state, text = self.message
            lines.append([(f"class:{state}", f"  {_GLYPHS[state]} "), ("", fit(text, width - 4))])
        lines.append([("class:dim", fit("  " + self.hints(height), width))])
        return [("", "\n"), *_join(lines)]

    def hints(self, height: int) -> str:
        page = self.page
        if self.mode == "edit":
            return "⏎ save · esc cancel"
        if page.text:
            here = Row(ref=page.ref) if page.ref is not None else None
            parts = ["↑↓ scroll", *(action.hint for action in page.keys if action.applies(here)), "esc back"]
            if len(page.rows) > height:
                parts.append(f"line {page.offset + 1} of {len(page.rows)}")
            return " · ".join(parts)
        parts = ["↑↓ move", f"⏎ {page.enter}"]
        if self.mode == "search":
            parts = ["type to filter", *parts, "esc done"]
        else:
            parts += [action.hint for action in page.keys if action.applies(self.selected())]
            if len(self.stack) == 1 and self.source.search() is not None:
                parts.append("/ search")
            parts.append("esc back" if len(self.stack) > 1 else "q quit")
        selectable = [i for i, row in enumerate(page.rows) if row.selectable]
        if len(page.rows) > height and page.cursor in selectable:
            parts.append(f"{selectable.index(page.cursor) + 1} of {len(selectable)}")
        return " · ".join(parts)

    def text(self, width: int = 100, height: int = 40) -> str:
        """The whole screen as plain text, laid out as the shell stacks it (tests, debugging)."""
        sections = [self.header(width), self.body(width, height), self.footer(width, height)]
        return "\n".join("".join(text for _, text in fragments) for fragments in sections)


def _join(lines: Sequence[Line]) -> list[Fragment]:
    out: list[Fragment] = []
    for index, line in enumerate(lines):
        if index:
            out.append(("", "\n"))
        out += line
    return out


def describe_lines(text: str, facts: Sequence[str], width: int, *, max_lines: int = 2) -> list[Line]:
    """A footer: ``text`` wrapped to ``max_lines``, then the ``facts`` joined on one dim line."""
    lines: list[Line] = []
    if text:
        wrapped = textwrap.wrap(text, width=max(width - 4, 20), max_lines=max_lines, placeholder=" …")
        lines += [[("", f"    {chunk}")] for chunk in wrapped]
    if facts:
        lines.append([("class:dim", fit("    " + " · ".join(facts), width))])
    return lines


def cells(text: str) -> int:
    """Terminal cells ``text`` takes (wide characters count two)."""
    return sum(get_cwidth(char) for char in text)


def fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` terminal cells, ending in ``…`` when cut."""
    if cells(text) <= width:
        return text
    if width <= 0:
        return ""
    out, used = [], 0
    for char in text:
        size = get_cwidth(char)
        if used + size > width - 1:
            break
        out.append(char)
        used += size
    return "".join(out) + "…"


def fit_spans(spans: Sequence[Fragment], width: int) -> Line:
    """Styled pieces cut to ``width`` terminal cells, ending in ``…`` when cut."""
    if sum(cells(text) for _, text in spans) <= width:
        return list(spans)
    out: Line = []
    room = width
    for style_class, text in spans:
        if cells(text) < room:
            out.append((style_class, text))
            room -= cells(text)
            continue
        # This piece reaches the edge and more follows (or it overflows): cut it and mark the cut.
        out.append((style_class, fit(text + "\u2026", room) if room > 0 else ""))
        break
    return out


def pad(text: str, width: int) -> str:
    return text + " " * max(width - cells(text), 0)


def can_open() -> bool:
    """A picker needs a real terminal on both ends and a user who wants prompts."""
    import os
    import sys

    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    if os.environ.get("LEMONCROW_NON_INTERACTIVE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# ── the terminal shell ───────────────────────────────────────────────────────────────────

_PALETTE: Final[dict[str, str]] = {
    "brand": "fg:#af87ff",
    "title": "bold",
    "heading": "fg:#af87ff bold",
    "pointer": "fg:#af87ff bold",
    "selected": "bold",
    "changed": "bold",
    "dim": "fg:ansibrightblack",
    "ok": "fg:ansigreen",
    "warn": "fg:ansiyellow",
    "fail": "fg:ansired",
    "off": "fg:ansibrightblack",
    # Highlighted source (prompt_toolkit's names for pygments tokens), in the terminal's own ANSI colours.
    "gutter": "fg:ansibrightblack",
    "target": "fg:#af87ff bold",
    "pygments.keyword": "fg:ansimagenta",
    "pygments.operator.word": "fg:ansimagenta",
    "pygments.name.builtin": "fg:ansicyan",
    "pygments.name.class": "fg:ansicyan bold",
    "pygments.name.decorator": "fg:ansicyan",
    "pygments.name.function": "fg:ansiblue",
    "pygments.literal.string": "fg:ansigreen",
    "pygments.literal.number": "fg:ansiyellow",
    "pygments.comment": "fg:ansibrightblack italic",
}

_CHROME_ROWS: Final[int] = 10
"""Title, inputs, description, facts, message and hint lines around the list."""


def run(picker: Picker) -> None:
    """Run ``picker`` inline until the user quits."""
    from prompt_toolkit.application import Application, run_in_terminal
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor, PasswordProcessor
    from prompt_toolkit.output.color_depth import ColorDepth
    from prompt_toolkit.styles import Style

    def size() -> tuple[int, int]:
        dims = app.output.get_size()
        return dims.columns, max(dims.rows - _CHROME_ROWS, 5)

    def text(fragments: Line) -> StyleAndTextTuples:
        # prompt_toolkit's fragment list also admits mouse handlers; list is invariant, so widen it here.
        return list(fragments)

    search_box = Buffer(multiline=False, on_text_changed=lambda buffer: picker.set_query(buffer.text))
    edit_box = Buffer(multiline=False)
    listing = Window(
        FormattedTextControl(lambda: text(picker.body(*size())), focusable=True, show_cursor=False),
        dont_extend_height=True,
    )
    search_input = Window(
        BufferControl(search_box, input_processors=[BeforeInput("  / ", style="class:brand")]), height=1
    )
    secret = Condition(lambda: picker.editing is not None and picker.editing.secret)
    edit_input = Window(
        BufferControl(
            edit_box,
            input_processors=[
                BeforeInput(
                    lambda: [("class:brand", f"  {POINTER} "), ("", picker.editing.prompt if picker.editing else "")]
                ),
                ConditionalProcessor(PasswordProcessor(), filter=secret),
            ],
        ),
        height=1,
    )

    def sync() -> None:
        if picker.done:
            app.exit()
            return
        if picker.pending is not None:
            work, picker.pending = picker.pending, None
            run_in_terminal(work)
        mode = picker.mode
        if mode == "edit" and picker.editing is not None and app.layout.current_window is not edit_input:
            edit_box.text = picker.editing.initial
            edit_box.cursor_position = len(edit_box.text)
        if mode == "search" and app.layout.current_window is not search_input:
            search_box.reset()
        app.layout.focus(edit_input if mode == "edit" else search_input if mode == "search" else listing)

    browsing = Condition(lambda: picker.mode == "browse")
    listing_keys = Condition(lambda: picker.mode != "edit")
    editing = Condition(lambda: picker.mode == "edit")
    keys = KeyBindings()

    @keys.add("up", filter=listing_keys)
    @keys.add("c-p", filter=listing_keys)
    @keys.add("k", filter=browsing)
    def _up(event: KeyPressEvent) -> None:
        picker.move(-1)

    @keys.add("down", filter=listing_keys)
    @keys.add("c-n", filter=listing_keys)
    @keys.add("j", filter=browsing)
    def _down(event: KeyPressEvent) -> None:
        picker.move(1)

    @keys.add("pageup", filter=listing_keys)
    def _page_up(event: KeyPressEvent) -> None:
        picker.move(-size()[1])

    @keys.add("pagedown", filter=listing_keys)
    def _page_down(event: KeyPressEvent) -> None:
        picker.move(size()[1])

    @keys.add("home", filter=browsing)
    def _first(event: KeyPressEvent) -> None:
        picker.move(-(10**9))

    @keys.add("end", filter=browsing)
    def _last(event: KeyPressEvent) -> None:
        picker.move(10**9)

    @keys.add("enter", filter=listing_keys)
    @keys.add("space", filter=browsing)
    @keys.add("right", filter=browsing)
    def _activate(event: KeyPressEvent) -> None:
        picker.activate()
        sync()

    @keys.add("enter", filter=editing)
    def _save(event: KeyPressEvent) -> None:
        picker.submit(edit_box.text)
        sync()

    @keys.add("escape", eager=True)
    @keys.add("left", filter=browsing)
    def _back(event: KeyPressEvent) -> None:
        picker.back()
        sync()

    @keys.add("/", filter=browsing)
    def _search(event: KeyPressEvent) -> None:
        picker.start_search()
        sync()

    @keys.add("q", filter=browsing)
    @keys.add("c-c")
    def _quit(event: KeyPressEvent) -> None:
        event.app.exit()

    @keys.add(Keys.Any, filter=browsing)
    def _page_key(event: KeyPressEvent) -> None:
        if picker.press(event.data):
            sync()

    layout = HSplit(
        [
            Window(FormattedTextControl(lambda: text(picker.header(size()[0]))), height=2),
            ConditionalContainer(search_input, filter=Condition(lambda: picker.page.searchable)),
            listing,
            ConditionalContainer(edit_input, filter=editing),
            Window(FormattedTextControl(lambda: text(picker.footer(*size()))), dont_extend_height=True),
        ]
    )
    app: Application[None] = Application(
        layout=Layout(layout, focused_element=listing),
        key_bindings=keys,
        style=Style.from_dict(_PALETTE),
        color_depth=None if style.color_enabled() else ColorDepth.DEPTH_1_BIT,
        full_screen=False,
        erase_when_done=True,
    )
    app.ttimeoutlen = 0.05  # Esc answers at once instead of waiting half a second for an Alt chord
    if picker.mode != "browse":
        app.layout.focus(search_input if picker.mode == "search" else edit_input)
    app.run()


__all__ = [
    "GAP",
    "Edit",
    "Key",
    "Outcome",
    "Page",
    "Picker",
    "Row",
    "Source",
    "can_open",
    "cells",
    "describe_lines",
    "fit",
    "fit_spans",
    "heading",
    "note",
    "pad",
    "run",
]
