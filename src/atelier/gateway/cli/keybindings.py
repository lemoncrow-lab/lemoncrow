"""Key bindings for the interactive Atelier CLI."""

from __future__ import annotations

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent


def make_keybindings() -> KeyBindings:
    """Build the key bindings used by the interactive prompt."""
    kb = KeyBindings()

    @kb.add("c-l")
    def _clear(event: KeyPressEvent) -> None:
        event.app.renderer.clear()

    @kb.add("escape", "enter")
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    return kb
