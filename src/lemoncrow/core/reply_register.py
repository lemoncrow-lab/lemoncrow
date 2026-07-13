"""Reply-register level plumbing (persona reply style: ultra | lite | off).

All response policy lives in sectioned ``shared/reply-register.md``:

- ``invariants``: byte-exact technical content and safety expansion; always on.
- ``telegraphic-default``: strict default appended through core discipline and
  removed for lite/off.
- ``ultra`` / ``lite``: mutually exclusive reply-style registers.
- ``off``: no reply-style register; invariants remain.

Resolution order: ``LEMONCROW_TELEGRAPHIC`` env var → persisted
``cli.telegraphic`` key in ``<root>/plugin_settings.json`` → ``ultra``. The
same transformation is mirrored in
``scripts/lib/managed_context.sh::lemoncrow_apply_reply_register_level``.
"""

from __future__ import annotations

import os
from pathlib import Path

from lemoncrow.core.persona_partials import markdown_section

REPLY_REGISTER_LEVELS: tuple[str, ...] = ("ultra", "lite", "off")
TELEGRAPHIC_SETTING_KEY = "cli.telegraphic"
_ENV_OVERRIDE = "LEMONCROW_TELEGRAPHIC"


def _persisted_level() -> str | None:
    """Read the persisted level after package import-time environment seeding."""
    try:
        from lemoncrow.core.settings import _resolve_root, load_raw

        value = load_raw(_resolve_root()).get(TELEGRAPHIC_SETTING_KEY)
    except Exception:  # noqa: BLE001 -- level lookup must never break persona rendering
        return None
    return value if isinstance(value, str) else None


def reply_register_level() -> str:
    level = (os.environ.get(_ENV_OVERRIDE) or _persisted_level() or "ultra").strip().lower()
    return level if level in REPLY_REGISTER_LEVELS else "ultra"


def _register_body(shared_dir: Path, section: str) -> str:
    return markdown_section(shared_dir / "reply-register.md", section)


def reply_register_body(shared_dir: Path, level: str | None = None) -> str:
    """Return the selected style section; ``off`` returns no style text."""
    lvl = level if level in REPLY_REGISTER_LEVELS else reply_register_level()
    if lvl == "off":
        return ""
    return _register_body(shared_dir, lvl)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def apply_reply_register_level(text: str, shared_dir: Path, level: str | None = None) -> str:
    """Replace baked ultra style and remove its telegraphic-default section."""
    lvl = level if level in REPLY_REGISTER_LEVELS else reply_register_level()
    if lvl == "ultra":
        return text
    default_body = _register_body(shared_dir, "ultra")
    if not default_body:
        return text
    replacement = "" if lvl == "off" else _register_body(shared_dir, "lite")
    bullet = _register_body(shared_dir, "telegraphic-default")
    pairs: list[tuple[str, str]] = [(default_body, replacement)]
    if bullet:
        pairs += [(bullet + "\n", ""), (bullet, "")]
    out = text
    for raw_needle, raw_sub in pairs:
        for needle, sub in ((raw_needle, raw_sub), (_toml_escape(raw_needle), _toml_escape(raw_sub))):
            if needle in out:
                out = out.replace(needle, sub)
    if out is not text:
        while "\n\n\n" in out:
            out = out.replace("\n\n\n", "\n\n")
    return out


__all__ = [
    "REPLY_REGISTER_LEVELS",
    "TELEGRAPHIC_SETTING_KEY",
    "apply_reply_register_level",
    "reply_register_body",
    "reply_register_level",
]
