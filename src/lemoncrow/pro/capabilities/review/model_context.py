"""Authority tags for model context used by Review Intelligence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ContextAuthority(StrEnum):
    PLATFORM_POLICY = "instruction:platform_policy"
    TRUSTED_BASE_RULE = "instruction:trusted_base_repository_rule"
    REVIEWER_DIRECTIVE = "instruction:authenticated_reviewer_directive"
    REVIEWED_SOURCE = "data:reviewed_repository_source"
    PR_METADATA = "data:pr_metadata"
    WORK_ITEM = "data:work_item"
    HEAD_CONFIG = "data:head_candidate_config"
    BOT_CLAIM = "claim:bot"
    AI_CLAIM = "claim:ai"
    WEB_TOOL_OUTPUT = "data:external_web_tool"
    CROSS_REPOSITORY = "evidence:cross_repository_context"
    PROJECT_KNOWLEDGE = "claim:project_knowledge"
    #: Review comments and replies (DS0): what people and agents said, never an instruction.
    REVIEW_DISCUSSION = "data:review_discussion"


@dataclass(frozen=True, slots=True)
class TaggedModelContext:
    authority: ContextAuthority
    value: Any
    provenance: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "authority": self.authority.value,
            "provenance": self.provenance,
            "value": self.value,
        }


def tagged(authority: ContextAuthority, value: Any, *, provenance: str = "") -> dict[str, Any]:
    return TaggedModelContext(authority, value, provenance).to_wire()


def is_instruction_authority(authority: ContextAuthority) -> bool:
    """Only platform/org, trusted-base rules and authenticated reviewer directives may instruct."""

    return authority in {
        ContextAuthority.PLATFORM_POLICY,
        ContextAuthority.TRUSTED_BASE_RULE,
        ContextAuthority.REVIEWER_DIRECTIVE,
    }


__all__ = ["ContextAuthority", "TaggedModelContext", "is_instruction_authority", "tagged"]
