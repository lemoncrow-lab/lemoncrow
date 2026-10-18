"""``lc settings``: the essentials up front, every setting one search away.

:class:`SettingsStore` reads and writes the settings file; :class:`SettingsSource`
gives the shared terminal picker (:mod:`lemoncrow.gateway.cli.picker`) its pages.
The ``*_lines`` helpers render the same model as plain output for ``show`` and
``get``, scripts, and terminals that can't host the picker.
"""

from __future__ import annotations

import difflib
import re
import textwrap
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from lemoncrow.core.reply_register import REPLY_REGISTER_LEVELS, TELEGRAPHIC_SETTING_KEY
from lemoncrow.core.settings import (
    CATEGORIES,
    SettingSpec,
    all_settings,
    coerce,
    env_override,
    find_by_key,
    load_raw,
    unset_setting,
    write_setting,
)
from lemoncrow.gateway.cli import style
from lemoncrow.gateway.cli.picker import (
    GAP,
    Edit,
    Key,
    Line,
    Outcome,
    Page,
    Picker,
    Row,
    cells,
    describe_lines,
    fit,
    heading,
    pad,
)

# ── what comes first ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Essential:
    label: str
    keys: tuple[str, ...]
    """One key is a setting; several open as a group."""
    blurb: str


APPROVAL_PREFIX: Final[str] = "review.approval."
APPROVAL_KEYS: Final[tuple[str, ...]] = tuple(s.key for s in all_settings() if s.key.startswith(APPROVAL_PREFIX))

ESSENTIALS: Final[tuple[Essential, ...]] = (
    Essential("Reply style", (TELEGRAPHIC_SETTING_KEY,), "How terse your agents' replies are"),
    Essential("Commit attribution", ("attribution",), "Co-author trailer on commits and PRs"),
    Essential("Status line", ("statusLine",), "LemonCrow savings in your agent's status bar"),
    Essential("Update checks", ("cli.update_check",), "Check once a day for a newer lc"),
    Essential("Auto-update", ("cli.auto_update",), "Install updates instead of only telling you"),
    Essential("Offline mode", ("cli.offline",), "Skip network calls that aren't strictly needed"),
    Essential("Review fix agent", ("review.fix_agent",), "Who fixes Review findings after the author's session"),
    Essential("Approval gates", APPROVAL_KEYS, "Pause the agent for your OK before risky actions"),
    Essential(
        "Verified work", ("verified_work.mode",), "Local verified repairs: off, shadow or enforce (opt in per repo)"
    ),
)

_LABELS: Final[dict[str, str]] = {e.keys[0]: e.label for e in ESSENTIALS if len(e.keys) == 1}


def friendly_name(key: str) -> str:
    return _LABELS.get(key, key)


def _gate_name(key: str) -> str:
    return key.removeprefix(APPROVAL_PREFIX).replace("_", " ")


# ── values ────────────────────────────────────────────────────────────────────────


_SECRET = re.compile(r"(?:api_key|_token|secret|password)$")


def is_secret(spec: SettingSpec) -> bool:
    """API keys and tokens: never echoed back to the screen."""
    return spec.type == "str" and bool(_SECRET.search(spec.key))


def display_value(spec: SettingSpec, value: Any) -> str:
    if is_secret(spec) and value:
        return "hidden"
    if spec.type == "bool":
        return "on" if value else "off"
    if value is None:
        return "auto"  # LemonCrow picks an internal default
    if value == "":
        return "none" if spec.choices else "not set"
    return str(value)


def options(spec: SettingSpec) -> tuple[str, ...]:
    """The values a choice setting offers; an empty default adds "none" (``""``)."""
    if not spec.choices:
        return ()
    return spec.choices if spec.default not in ("", None) else ("", *spec.choices)


@dataclass(frozen=True)
class Current:
    spec: SettingSpec
    value: Any
    """What LemonCrow uses: an exported env var, else the stored value, else the default."""
    changed: bool
    """A stored value that differs from the default."""
    env: str | None
    """An exported env var whose value wins over the stored one."""

    @property
    def shown(self) -> str:
        return display_value(self.spec, self.value)

    @property
    def env_shown(self) -> str:
        """The overriding ``VAR=value`` as exported, with secrets hidden."""
        return f"{self.spec.env_var}={'hidden' if is_secret(self.spec) else self.env}"

    @property
    def flag(self) -> Literal["", "changed", "env"]:
        return "env" if self.env is not None else "changed" if self.changed else ""


def require(key: str) -> SettingSpec:
    """The setting called ``key``; a ValueError that suggests near misses otherwise."""
    spec = find_by_key(key)
    if spec is not None:
        return spec
    keys = [s.key for s in all_settings()]
    close = difflib.get_close_matches(key, keys, n=3, cutoff=0.6)
    if not close:
        close = [k for k in keys if key.lower() in k.lower()][:3]
    hint = f". Did you mean {' or '.join(close)}?" if close else ""
    raise ValueError(f"unknown setting: {key}{hint}")


class SettingsStore:
    """The settings file under ``root``, read once and re-read after each write."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._raw = load_raw(self.root)

    def get(self, key: str) -> Current:
        spec = require(key)
        value = spec.default
        stored = key in self._raw
        if stored:
            try:
                value = coerce(spec, self._raw[key])
            except (TypeError, ValueError):
                stored = False
        changed = stored and value != spec.default
        exported = env_override(spec)
        if exported is not None:
            try:
                from_env: Any = coerce(spec, exported)
            except (TypeError, ValueError):
                from_env = exported
            if not spec.settable:  # the environment is the only way to set it: nothing overridden
                return Current(spec, from_env, False, None)
            if from_env != value:
                return Current(spec, from_env, changed, exported)
        return Current(spec, value, changed, None)

    def set(self, key: str, raw: Any) -> Current:
        spec = require(key)
        if key == TELEGRAPHIC_SETTING_KEY and str(raw) not in REPLY_REGISTER_LEVELS:
            raise ValueError(f"invalid value for {key}: {raw!r} (choices: {', '.join(REPLY_REGISTER_LEVELS)})")
        write_setting(self.root, spec.key, raw)
        self._raw = load_raw(self.root)
        return self.get(key)

    def reset(self, key: str) -> Current:
        spec = require(key)
        if not spec.settable:
            raise ValueError(_read_only(spec))
        unset_setting(self.root, key)
        self._raw = load_raw(self.root)
        return self.get(key)


def _read_only(spec: SettingSpec) -> str:
    hint = f"set {spec.env_var} instead" if spec.env_var else "LemonCrow manages it"
    return f"{spec.key} is read-only ({hint})"


def search(query: str) -> list[SettingSpec]:
    """Settings matching every word of ``query`` (key matches first); all settings for an empty query."""
    terms = query.lower().split()
    ranked: list[tuple[int, str, SettingSpec]] = []
    for spec in all_settings():
        key = spec.key.lower()
        text = f"{key} {(spec.env_var or '').lower()} {friendly_name(spec.key).lower()} {spec.description.lower()}"
        if all(term in text for term in terms):
            ranked.append((0 if all(term in key for term in terms) else 1, spec.key, spec))
    return [spec for _, _, spec in sorted(ranked, key=lambda item: (item[0], item[1]))]


# ── the picker ─────────────────────────────────────────────────────────────────────


def _shared_lead(texts: Sequence[str]) -> str:
    """The longest opening of 4+ words that 3+ descriptions repeat: boilerplate, cut from the list.

    "Pause the coding agent for a human decision in the Review before git push"
    reads as "Git push or force-push." next to its siblings; the footer keeps the full text.
    """
    counts: Counter[str] = Counter()
    for text in texts:
        words = text.split()
        for size in range(4, len(words)):
            counts[" ".join(words[:size])] += 1
    return max((lead for lead, count in counts.items() if count >= 3), key=len, default="")


class SettingsSource:
    """The settings picker's pages: essentials, areas, search, choices, typed values."""

    title = "LemonCrow settings"

    def __init__(self, store: SettingsStore) -> None:
        self.store = store
        self.changes: dict[str, str] = {}
        """Settings saved in this session, key → shown value, in order."""
        self._reset = Key("r", "r reset", self._reset_row, when=lambda row: row.ref[0] == "setting")

    # ── pages ──

    def home(self) -> Page:
        return Page("", self._home_rows, enter="change", keys=(self._reset,))

    def search(self) -> Page:
        return Page(
            "Search",
            lambda query: [self._row(spec.key, spec.key, spec.description) for spec in search(query)],
            searchable=True,
            enter="change",
            empty="No setting matches that. Esc clears the search.",
        )

    def _home_rows(self, _query: str) -> list[Row]:
        rows = [heading("Essentials")]
        for item in ESSENTIALS:
            if len(item.keys) == 1:
                rows.append(self._row(item.keys[0], item.label, item.blurb))
                continue
            on = [_gate_name(key) for key in item.keys if self.store.get(key).value is True]
            value = "off" if not on else ", ".join(on) if len(on) <= 2 else f"{len(on)} on"
            rows.append(Row(item.label, value, item.blurb, "changed" if on else "", ref=("group", item.keys)))
        rows += [
            GAP,
            Row(
                "Browse all settings",
                style.plural(len(_areas()), "area"),
                f"{len(all_settings())} settings, grouped by area",
                ref=("areas",),
            ),
            Row("Search settings", "/", "Find any setting by name or description", ref=("search",)),
        ]
        return rows

    def _area_rows(self, _query: str) -> list[Row]:
        rows = []
        for area, keys in _areas().items():
            changed = sum(1 for key in keys if self.store.get(key).flag)
            note = f"{changed} changed" if changed else ""
            rows.append(Row(area, style.plural(len(keys), "setting"), note, ref=("group", keys)))
        return rows

    def _group_rows(self, keys: Sequence[str], strip: str) -> list[Row]:
        specs = [require(key) for key in keys]
        lead = _shared_lead([spec.description for spec in specs])
        rows = []
        for spec in specs:
            note = spec.description
            if lead and note.startswith(lead + " "):
                rest = note[len(lead) + 1 :]
                note = rest[:1].upper() + rest[1:]
            rows.append(self._row(spec.key, spec.key.removeprefix(strip) or spec.key, note))
        return rows

    def _choice_rows(self, spec: SettingSpec) -> list[Row]:
        current = self.store.get(spec.key).value
        rows = []
        for value in options(spec):
            notes = ["current"] if value == current else []
            if value == (spec.default or ""):
                notes.append("default")
            rows.append(Row(display_value(spec, value), note=" · ".join(notes), ref=("choice", spec.key, value)))
        return rows

    def _row(self, key: str, label: str, note: str) -> Row:
        current = self.store.get(key)
        return Row(label, current.shown, note, current.flag, ref=("setting", key))

    # ── actions ──

    def open(self, page: Page, row: Row) -> Outcome | None:
        kind = row.ref[0]
        if kind == "group":
            keys = row.ref[1]
            strip = APPROVAL_PREFIX if keys == APPROVAL_KEYS else f"{row.label}."
            return Outcome(
                page=Page(row.label, lambda _q: self._group_rows(keys, strip), enter="change", keys=(self._reset,))
            )
        if kind == "areas":
            return Outcome(page=Page("All settings", self._area_rows))
        if kind == "search":
            return Outcome(page=self.search())
        if kind == "choice":
            _, key, value = row.ref
            return self._save(key, lambda: self.store.set(key, value), close=True)
        key = row.ref[1]
        current = self.store.get(key)
        spec = current.spec
        if not spec.settable:
            return Outcome(message=("warn", _read_only(spec)))
        if spec.type == "bool":
            return self._save(key, lambda: self.store.set(key, not current.value))
        if spec.choices:
            values = options(spec)
            at = values.index(current.value) if current.value in values else 0
            return Outcome(page=Page(row.label, lambda _q: self._choice_rows(spec), enter="choose", cursor=at))
        initial = "" if current.value is None or is_secret(spec) else str(current.value)
        return Outcome(edit=Edit(key, f"{key} = ", initial, secret=is_secret(spec)))

    def submit(self, edit: Edit, text: str) -> Outcome:
        key = str(edit.ref)
        return self._save(key, lambda: self.store.set(key, text.strip()))

    def _reset_row(self, page: Page, row: Row) -> Outcome | None:
        if row.ref[0] != "setting":
            return None
        key = row.ref[1]
        return self._save(key, lambda: self.store.reset(key), verb="reset to")

    def _save(self, key: str, write: Callable[[], Current], *, verb: str = "set to", close: bool = False) -> Outcome:
        try:
            current = write()
        except ValueError as exc:
            return Outcome(message=("fail", str(exc)))
        self.changes[key] = current.shown
        if current.env is not None:
            message: tuple[style.State, str] = (
                "warn",
                f"Saved, but {current.spec.env_var} in your environment still overrides it",
            )
        else:
            message = ("ok", f"{friendly_name(key)} {verb} {current.shown}")
        return Outcome(message=message, close=close)

    # ── footer ──

    def describe(self, page: Page, row: Row | None, width: int) -> list[Line]:
        if row is None or row.ref[0] not in ("setting", "choice"):
            return []
        current = self.store.get(row.ref[1])
        spec = current.spec
        facts = [spec.key, *([spec.env_var] if spec.env_var else []), f"default {display_value(spec, spec.default)}"]
        if not spec.settable:
            facts.append("read-only")
        lines = describe_lines(spec.description, facts, width)
        if current.env is not None:
            text = f"    ⚠ {current.env_shown} in your environment overrides this"
            lines.append([("class:warn", fit(text, width))])
        return lines


def _areas() -> dict[str, tuple[str, ...]]:
    areas: dict[str, tuple[str, ...]] = {}
    for category in CATEGORIES:
        keys = tuple(s.key for s in all_settings() if s.category == category)
        if keys:
            areas[category] = keys
    return areas


def settings_picker(store: SettingsStore) -> Picker:
    return Picker(SettingsSource(store))


# ── plain output (show, get, scripts) ────────────────────────────────────────────


@dataclass(frozen=True)
class _Line:
    label: str
    shown: str
    flag: str
    note: str


def _line(label: str, current: Current, note: str) -> _Line:
    return _Line(label, current.shown, current.flag, note)


def _plain_rows(rows: Sequence[_Line], *, width: int | None) -> list[str]:
    """``label  • value  note`` rows, aligned, with the note cut to ``width`` (None: never cut)."""
    if not rows:
        return []
    label_w = min(max(cells(r.label) for r in rows), 44)
    value_w = min(max(cells(r.shown) for r in rows), 24)
    note_w = None if width is None else width - (4 + label_w + 4 + value_w + 2)
    out = []
    for r in rows:
        mark = {"changed": style.accent("•") + " ", "env": style.mark("warn") + " "}.get(r.flag, "  ")
        value = pad(fit(r.shown, value_w), value_w)
        value = style.bold(value) if r.flag else value
        line = f"    {pad(fit(r.label, label_w), label_w)}  {mark}{value}"
        if r.note and (note_w is None or note_w >= 12):
            line += "  " + style.dim(r.note if note_w is None else fit(r.note, note_w))
        out.append(line.rstrip())
    return out


def _legend(rows: Iterable[_Line]) -> list[str]:
    flags = {r.flag for r in rows}
    parts = []
    if "changed" in flags:
        parts.append(f"{style.accent('•')} changed from default")
    if "env" in flags:
        parts.append(f"{style.mark('warn')} overridden by your environment")
    return ["", "  " + style.dim("   ".join(parts))] if parts else []


def overview_lines(store: SettingsStore, *, width: int | None) -> list[str]:
    """The essentials, anything else you changed, and where to go next."""
    essentials: list[_Line] = []
    seen: set[str] = set()
    for item in ESSENTIALS:
        seen.update(item.keys)
        if len(item.keys) == 1:
            essentials.append(_line(item.label, store.get(item.keys[0]), item.keys[0]))
            continue
        on = [_gate_name(k) for k in item.keys if store.get(k).value is True]
        essentials.append(_Line(item.label, ", ".join(on) or "off", "changed" if on else "", f"{APPROVAL_PREFIX}*"))
    currents = [store.get(s.key) for s in all_settings() if s.key not in seen]
    others = [_line(c.spec.key, c, c.spec.description) for c in currents if c.flag]

    lines = [style.title("LemonCrow settings"), "", "  " + style.heading("Essentials")]
    lines += _plain_rows(essentials, width=width)
    if others:
        lines += ["", "  " + style.heading("Also changed"), *_plain_rows(others, width=width)]
    lines += _legend([*essentials, *others])
    total = len(all_settings())
    lines += [
        "",
        style.next_step("Change them:", "lc settings"),
        style.next_step(f"See all {total} settings:", "lc settings show --all"),
    ]
    return lines


def listing_lines(store: SettingsStore, *, category: str | None, width: int | None) -> list[str]:
    """Every setting (or one area's), grouped by area: key, value, description."""
    areas = [category] if category else list(CATEGORIES)
    specs = [s for s in all_settings() if s.category in areas]
    detail = f"{category} · {style.plural(len(specs), 'setting')}" if category else style.plural(len(specs), "setting")
    lines = [style.title("LemonCrow settings", detail)]
    shown: list[_Line] = []
    for area in areas:
        rows = [_line(s.key, store.get(s.key), s.description) for s in specs if s.category == area]
        if not rows:
            continue
        shown += rows
        lines += ["", "  " + style.heading(area), *_plain_rows(rows, width=width)]
    lines += _legend(shown)
    lines += ["", style.next_step("Change one:", "lc settings set KEY VALUE")]
    return lines


def detail_lines(store: SettingsStore, key: str, *, width: int | None) -> list[str]:
    """One setting in full: value, default, allowed values, env var, what it does."""
    current = store.get(key)
    spec = current.spec
    name = friendly_name(key)
    lines = [style.title(spec.key, name if name != spec.key else "")]
    value = style.bold(current.shown)
    if current.changed or current.env is not None:
        value += style.dim(f"  (default {display_value(spec, spec.default)})")
    lines.append(style.row(None, "Value", value, width=9))
    if current.env is not None:
        lines.append(style.row("warn", f"{current.env_shown} in your environment overrides the stored value"))
    if spec.choices:
        lines.append(style.row(None, "Choices", " · ".join(display_value(spec, v) for v in options(spec)), width=9))
    else:
        kind = {"bool": "on/off", "int": "whole number", "float": "number", "str": "text"}[spec.type]
        lines.append(style.row(None, "Type", kind, width=9))
    if spec.env_var:
        lines.append(style.row(None, "Env var", spec.env_var, width=9))
    lines.append(style.row(None, "Area", spec.category, width=9))
    lines.append("")
    wrap = textwrap.wrap(spec.description, width=max(width - 2, 40)) if width else [spec.description]
    lines += ["  " + style.dim(text) for text in wrap]
    lines.append("")
    if spec.settable:
        lines.append(style.next_step("Change it:", f"lc settings set {spec.key} VALUE"))
        if current.changed:
            lines.append(style.next_step("Back to default:", f"lc settings reset {spec.key}"))
    else:
        lines.append(style.row("off", _read_only(spec)))
    return lines


def detail_payload(store: SettingsStore, key: str) -> dict[str, Any]:
    current = store.get(key)
    spec = current.spec
    return {
        "key": spec.key,
        "value": current.value,
        "default": spec.default,
        "changed": current.changed,
        "env_var": spec.env_var,
        "env_override": current.env,
        "type": spec.type,
        "choices": list(spec.choices),
        "category": spec.category,
        "settable": spec.settable,
        "description": spec.description,
    }


__all__ = [
    "APPROVAL_KEYS",
    "ESSENTIALS",
    "Current",
    "SettingsSource",
    "SettingsStore",
    "detail_lines",
    "detail_payload",
    "display_value",
    "friendly_name",
    "is_secret",
    "listing_lines",
    "overview_lines",
    "require",
    "search",
    "settings_picker",
]
