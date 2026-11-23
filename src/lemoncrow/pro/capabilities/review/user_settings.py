"""Per-user Review settings (SPEC 4.11): the keys, their defaults and the validation of a patch.

Pure: no store, no network. The store keeps only the keys a reviewer set; ``effective`` fills the rest with the defaults.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Keys no rebinding may take. Must equal ``RESERVED_KEYS`` in ``frontend/src/review/flowKeymap.ts`` (a test compares them).
RESERVED_KEYS: tuple[str, ...] = (
    "j",
    "k",
    "s",
    "w",
    "u",
    "g",
    "e",
    "q",
    "r",
    "x",
    "z",
    "?",
    "/",
    "[",
    "]",
    "enter",
    "escape",
    "tab",
    " ",
)

#: What each reserved key does, so a refusal can name it (SPEC 4.11). Must cover ``RESERVED_KEYS`` and match the keymap.
RESERVED_ACTIONS: dict[str, str] = {
    "j": "next part",
    "k": "previous part",
    "s": "structure",
    "w": "wrap up",
    "u": "next unseen risky part",
    "g": "the other view",
    "e": "show the whole part",
    "q": "ask",
    "r": "run test",
    "x": "select",
    "z": "undo",
    "?": "all shortcuts",
    "/": "search",
    "[": "previous file",
    "]": "next file",
    " ": "another shortcut",
}

KEY_LABELS: dict[str, str] = {
    "key_looks_good": "Looks good",
    "key_needs_change": "Needs a change",
    "key_decide_later": "Decide later",
}

DEFAULTS: dict[str, Any] = {
    "default_view": "one",
    "skip_overview_single_decision": True,
    "start_all_files_over_30_parts": True,
    "theme": "auto",
    "key_looks_good": "a",
    "key_needs_change": "c",
    "key_decide_later": "n",
}

_CHOICES: dict[str, tuple[str, ...]] = {"default_view": ("one", "all"), "theme": ("light", "dark", "auto")}
_BOOLEANS = ("skip_overview_single_decision", "start_all_files_over_30_parts")


class UserSettingsError(ValueError):
    """A patch the server refuses; ``str(error)`` is the sentence shown to the reviewer."""


def effective(stored: Mapping[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **{key: value for key, value in stored.items() if key in DEFAULTS}}


def _check_key(name: str, value: Any, others: Mapping[str, Any]) -> str:
    if not isinstance(value, str) or len(value) != 1:
        raise UserSettingsError("Pick a single letter, number or symbol.")
    key = value.lower()
    if key != value:
        raise UserSettingsError("Use the lowercase key.")
    for other, label in KEY_LABELS.items():
        if other != name and others[other] == key:
            raise UserSettingsError(f"{key.upper()} is already used for {label}. Pick another key.")
    if key in RESERVED_KEYS:
        raise UserSettingsError(
            f"{key.upper()} is already used for {RESERVED_ACTIONS.get(key, 'another shortcut')}. Pick another key."
        )
    return key


def apply_patch(stored: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """The stored settings after *patch*. A ``None`` value forgets the key (back to its default).

    Every key in the patch is applied first and the end state is validated once, so two keys swapped in one patch
    are accepted and a forgotten key that would clash with another is refused. Raises :class:`UserSettingsError`
    for an unknown key, a wrong type or value, or a key binding that is taken.
    """

    unknown = sorted(set(patch) - set(DEFAULTS))
    if unknown:
        raise UserSettingsError(f"Unknown setting: {unknown[0]}.")
    result = {key: value for key, value in stored.items() if key in DEFAULTS}
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        elif name in _CHOICES:
            if value not in _CHOICES[name]:
                raise UserSettingsError(f"{name} must be one of {', '.join(_CHOICES[name])}.")
            result[name] = value
        elif name in _BOOLEANS:
            if not isinstance(value, bool):
                raise UserSettingsError(f"{name} must be true or false.")
            result[name] = value
        else:
            result[name] = value
    end = effective(result)
    # The keys the patch names are checked first, so a clash is reported against the key being changed; the rest are
    # checked too, because forgetting one binding can bring its default back onto another's key.
    for name in sorted(KEY_LABELS, key=lambda key: key not in patch):
        _check_key(name, end[name], end)
    return result
