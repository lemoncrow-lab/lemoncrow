"""Reply-register level plumbing (persona reply style: strict | mild | off).

The level controls how much reply-style instruction is baked into every
installed agent persona:

- ``strict`` (default): the full telegraphic template
  (``integrations/agents/shared/reply-register.md``) plus the core-discipline
  "Telegraphic by default" bullet (``telegraphic-default.md``).
- ``mild``: concise-core register only (``reply-register-mild.md``); the
  strict bullet is stripped.
- ``off``: no reply-style instruction at all.

Resolution order: ``ATELIER_TELEGRAPHIC`` env var → persisted
``cli.telegraphic`` key in ``<root>/plugin_settings.json`` (written by
``atelier settings set cli.telegraphic <level>``) → ``strict``. The same
logic is mirrored (self-contained, no import) in
``scripts/lib/managed_context.sh::atelier_apply_reply_register_level`` for
install scripts that stage pre-rendered files — keep the two in sync.
"""

from __future__ import annotations

import os
from pathlib import Path

REPLY_REGISTER_LEVELS: tuple[str, ...] = ("strict", "mild", "off")
TELEGRAPHIC_SETTING_KEY = "cli.telegraphic"
_ENV_OVERRIDE = "ATELIER_TELEGRAPHIC"


def _persisted_level() -> str | None:
    """Direct read of the persisted setting.

    ``apply_settings_env`` seeds ``ATELIER_TELEGRAPHIC`` only once at package
    import; a ``settings set`` inside the same process needs the file value.
    """
    try:
        from atelier.core.settings import _resolve_root, load_raw

        value = load_raw(_resolve_root()).get(TELEGRAPHIC_SETTING_KEY)
    except Exception:  # noqa: BLE001 -- level lookup must never break persona rendering
        return None
    return value if isinstance(value, str) else None


def reply_register_level() -> str:
    level = (os.environ.get(_ENV_OVERRIDE) or _persisted_level() or "strict").strip().lower()
    return level if level in REPLY_REGISTER_LEVELS else "strict"


def _register_body(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def reply_register_body(shared_dir: Path, level: str | None = None) -> str:
    """The reply-register text for ``level`` (current level when ``None``)."""
    lvl = level if level in REPLY_REGISTER_LEVELS else reply_register_level()
    if lvl == "off":
        return ""
    name = "reply-register.md" if lvl == "strict" else "reply-register-mild.md"
    return _register_body(shared_dir / name)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def apply_reply_register_level(text: str, shared_dir: Path, level: str | None = None) -> str:
    """Swap the baked-in strict register in pre-rendered agent text for ``level``.

    ``strict`` (and unknown levels) return ``text`` unchanged. Matches both the
    raw bodies and their TOML-escaped forms (codex ``developer_instructions``).
    Text without the strict register/bullet passes through untouched.
    """
    lvl = level if level in REPLY_REGISTER_LEVELS else reply_register_level()
    if lvl == "strict":
        return text
    strict = _register_body(shared_dir / "reply-register.md")
    if not strict:
        return text
    pairs: list[tuple[str, str]] = [(strict, reply_register_body(shared_dir, lvl))]
    bullet_path = shared_dir / "telegraphic-default.md"
    if bullet_path.exists():
        bullet = _register_body(bullet_path)
        if bullet:
            # mild register already states the softer default; the strict bullet
            # ("never on self-judged complexity") goes for both mild and off.
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
