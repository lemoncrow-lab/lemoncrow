"""PII scrubber for product telemetry string values."""

from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b")
UNIX_PATH_RE = re.compile(r"(?<![\w.-])/(?:Users|home|var|tmp|private|Volumes)/[^\s,;:'\"]+")
WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s,;:'\"]+\\?)+")
REPO_URL_RE = re.compile(
    r"(?:git@(?:github|gitlab|bitbucket)\.com:[^\s]+|"
    r"ssh://git@[^\s]+|"
    r"https?://(?:[^\s/@]+@)?(?:www\.)?(?:github|gitlab|bitbucket)\.[^\s]+)",
    re.IGNORECASE,
)
SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[opsu]_[A-Za-z0-9_]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
)


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_string(value)
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): scrub_value(item) for key, item in value.items()}
    return value


def scrub_props(props: dict[str, Any]) -> dict[str, Any]:
    return {key: scrub_value(value) for key, value in props.items()}


def scrub_string(value: str) -> str:
    text = REPO_URL_RE.sub("<repo>", value)
    text = SECRET_RE.sub("<secret>", text)
    text = EMAIL_RE.sub("<email>", text)
    text = IPV6_RE.sub("<ip>", text)
    text = IPV4_RE.sub("<ip>", text)
    text = WINDOWS_PATH_RE.sub("<path>", text)
    return UNIX_PATH_RE.sub("<path>", text)
