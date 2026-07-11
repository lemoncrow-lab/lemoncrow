"""Parse Playbooks from human-readable Markdown.

Complements renderer.py to enable bidirectional sync between the Git-tracked
lessons directory and the local SQLite index.
"""

from __future__ import annotations

import re
from typing import cast

from lemoncrow.core.foundation.models import Playbook, PlaybookStatus


def parse_block_markdown(content: str) -> Playbook:
    """Parse a Playbook from its canonical Markdown representation."""
    lines = content.splitlines()
    title = ""
    block_id = ""
    domain = ""
    status: PlaybookStatus = "active"
    task_types: list[str] = []
    triggers: list[str] = []
    dead_ends: list[str] = []
    procedure: list[str] = []
    verification: list[str] = []
    failure_signals: list[str] = []
    file_patterns: list[str] = []
    tool_patterns: list[str] = []
    situation_lines: list[str] = []
    when_not_to_apply_lines: list[str] = []

    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()

    for line in lines:
        if line.startswith("- **id:**"):
            block_id = _extract_code(line)
        elif line.startswith("- **domain:**"):
            domain = _extract_code(line)
        elif line.startswith("- **status:**"):
            raw_status = _extract_code(line)
            if raw_status not in {"active", "deprecated", "quarantined"}:
                raise ValueError(f"unsupported block status: {raw_status}")
            status = cast(PlaybookStatus, raw_status)
        elif line.startswith("- **task_types:**"):
            task_types = _split_csv(line.split(":", 1)[1])

    current_section = ""
    for line in lines:
        if line.startswith("## "):
            current_section = line[3:].strip().lower()
            continue

        if not current_section:
            continue

        clean = line.strip()
        if not clean:
            continue

        if current_section == "situation":
            situation_lines.append(clean)
        elif current_section == "triggers" and clean.startswith("- "):
            triggers.append(clean[2:])
        elif current_section == "dead ends" and clean.startswith("- "):
            dead_ends.append(clean[2:])
        elif current_section == "procedure" and re.match(r"^\d+\.", clean):
            procedure.append(clean.split(".", 1)[1].strip())
        elif current_section == "verification" and clean.startswith("- "):
            verification.append(clean[2:])
        elif current_section == "failure signals" and clean.startswith("- "):
            failure_signals.append(clean[2:])
        elif current_section == "when not to apply":
            when_not_to_apply_lines.append(clean)
        elif current_section == "scope" and clean.startswith("- "):
            if "file_patterns:" in clean:
                file_patterns = _split_csv(clean.split(":", 1)[1])
            elif "tool_patterns:" in clean:
                tool_patterns = _split_csv(clean.split(":", 1)[1])

    situation = " ".join(situation_lines).strip()
    when_not_to_apply = " ".join(when_not_to_apply_lines).strip()

    missing = [
        field_name
        for field_name, value in (
            ("title", title),
            ("id", block_id),
            ("domain", domain),
            ("situation", situation),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"missing required playbook fields: {', '.join(missing)}")

    return Playbook(
        id=block_id,
        title=title,
        domain=domain,
        status=status,
        task_types=task_types,
        triggers=triggers,
        file_patterns=file_patterns,
        tool_patterns=tool_patterns,
        situation=situation,
        dead_ends=dead_ends,
        procedure=procedure,
        verification=verification,
        failure_signals=failure_signals,
        when_not_to_apply=when_not_to_apply,
    )


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _extract_code(line: str) -> str:
    match = re.search(r"`([^`]+)`", line)
    if match:
        return match.group(1)
    return line.split(":", 1)[1].strip()
