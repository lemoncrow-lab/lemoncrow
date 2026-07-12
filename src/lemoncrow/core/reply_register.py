"""Reply-register level plumbing (persona reply style: ultra | lite | off).

The level controls how much reply-style instruction is baked into every
installed agent persona:

- ``ultra`` (default): the full telegraphic template
  (``integrations/agents/shared/reply-register.md``) plus the core-discipline
  "Telegraphic by default" bullet (``telegraphic-default.md``).
- ``lite``: concise-core register only (``reply-register-lite.md``); the
  ultra bullet is stripped.
- ``off``: no reply-style instruction at all.

Resolution order: ``LEMONCROW_TELEGRAPHIC`` env var → persisted
``cli.telegraphic`` key in ``<root>/plugin_settings.json`` (written by
``lc settings set cli.telegraphic <level>``) → ``ultra``. The same
logic is mirrored (self-contained, no import) in
``scripts/lib/managed_context.sh::lemoncrow_apply_reply_register_level`` for
install scripts that stage pre-rendered files — keep the two in sync.
"""

from __future__ import annotations

import os
from pathlib import Path

REPLY_REGISTER_LEVELS: tuple[str, ...] = ("ultra", "lite", "off")
TELEGRAPHIC_SETTING_KEY = "cli.telegraphic"
_ENV_OVERRIDE = "LEMONCROW_TELEGRAPHIC"


def _persisted_level() -> str | None:
    """Direct read of the persisted setting.

    ``apply_settings_env`` seeds ``LEMONCROW_TELEGRAPHIC`` only once at package
    import; a ``settings set`` inside the same process needs the file value.
    """
    try:
        from lemoncrow.core.settings import _resolve_root, load_raw

        value = load_raw(_resolve_root()).get(TELEGRAPHIC_SETTING_KEY)
    except Exception:  # noqa: BLE001 -- level lookup must never break persona rendering
        return None
    return value if isinstance(value, str) else None


def reply_register_level() -> str:
    level = (os.environ.get(_ENV_OVERRIDE) or _persisted_level() or "ultra").strip().lower()
    return level if level in REPLY_REGISTER_LEVELS else "ultra"


def _register_body(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def reply_register_body(shared_dir: Path, level: str | None = None) -> str:
    """The reply-register text for ``level`` (current level when ``None``)."""
    lvl = level if level in REPLY_REGISTER_LEVELS else reply_register_level()
    if lvl == "off":
        return ""
    name = "reply-register.md" if lvl == "ultra" else "reply-register-lite.md"
    return _register_body(shared_dir / name)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def apply_reply_register_level(text: str, shared_dir: Path, level: str | None = None) -> str:
    """Swap the baked-in ultra register in pre-rendered agent text for ``level``.

    ``ultra`` (and unknown levels) return ``text`` unchanged -- it's what ships
    baked into every generated persona. Matches both the raw bodies and their
    TOML-escaped forms (codex ``developer_instructions``). Text without the
    register/bullet passes through untouched.
    """
    lvl = level if level in REPLY_REGISTER_LEVELS else reply_register_level()
    if lvl == "ultra":
        return text
    default_body = _register_body(shared_dir / "reply-register.md")
    if not default_body:
        return text
    pairs: list[tuple[str, str]] = [(default_body, reply_register_body(shared_dir, lvl))]
    bullet_path = shared_dir / "telegraphic-default.md"
    if bullet_path.exists():
        bullet = _register_body(bullet_path)
        if bullet:
            # lite/off soften or drop the register; the ultra bullet
            # ("never on self-judged complexity") goes with it.
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
