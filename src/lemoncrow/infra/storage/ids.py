"""Storage ID helpers."""

from __future__ import annotations

import secrets
import time


def make_uuid7() -> str:
    """Return a UUIDv7-like sortable identifier using only stdlib.

    The value is formatted as UUID text. Its first 48 bits are the Unix
    timestamp in milliseconds and the remaining 80 bits include the UUIDv7
    version/variant bits plus random data.
    """

    unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_80 = secrets.randbits(80)
    value = (unix_ms << 80) | random_80

    value &= ~(0xF << 76)
    value |= 0x7 << 76
    value &= ~(0x3 << 62)
    value |= 0x2 << 62

    hex_value = f"{value:032x}"
    return f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-{hex_value[16:20]}-{hex_value[20:]}"


__all__ = ["make_uuid7"]
