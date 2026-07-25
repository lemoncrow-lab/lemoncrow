"""Shared LemonCrow identity metadata for MCP clients and HTTP surfaces."""

from __future__ import annotations

import base64
from typing import Any

ICON_PATH = "/favicon.png"
ICON_MIME_TYPE = "image/png"
ICON_SIZES = ["64x64"]
# 64x64 PNG derived from the compact LemonCrow docs logo (rounded dark tile +
# white terminal chevron). Keeping it in-package makes the icon available from
# wheels and lets stdio MCP clients receive a credential-free data URI too.
_ICON_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAABUUlEQVR42u2b0QrDMAhFjfSp/f9PbV63p0IpTeeg0Ztc87oxOCdqNCNFDGtd148MuGqt5dd3yozg/4goM4NbRCgLfItNWeBbjMoEf8eqQr6UbfevUUAfAYVx9x+PwRSQAlJACkgBKeC9te87r4ADHlmCeu08qgT1DHtECa+3wlbIbdvmjAArGEo0dEmBkSR0K4KjSHAZhy2QUTXBpRO0wEVFglsrjCrB/UYI7Zh0H4bQimPINIgkIWwcRpEAcSv8BNm7FkBciLQgPQohhIDI6TE0BRCORGWGDxOA1AwpM7yIyIIG7z0VKjO8mwDk+4CFKd9dI2CU22Flhu8iYLT/Bbq0wpHTHUQKRE53MEXwCosI370POKBR4cPH4ekjIAWkgBSQAuAFWJ6WzbpqrSUj4DDBuPtZA84CmKLgzKqtDxjgb1NgZgl3bPl42vIDMz+f/wJQFKYFfAO0yQAAAABJRU5ErkJggg=="
ICON_BYTES = base64.b64decode(_ICON_BASE64)
ICON_DATA_URI = f"data:{ICON_MIME_TYPE};base64,{_ICON_BASE64}"


def icon_metadata() -> dict[str, Any]:
    """Return a fresh MCP ``Icon`` object so callers cannot mutate globals."""
    return {"src": ICON_DATA_URI, "mimeType": ICON_MIME_TYPE, "sizes": list(ICON_SIZES)}
