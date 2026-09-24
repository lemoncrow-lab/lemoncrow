"""Deployment-neutral decision vocabulary for server event hooks."""

from enum import StrEnum

__all__ = ["AuditDecision"]


class AuditDecision(StrEnum):
    ALLOW = "allow"
    REFUSE = "refuse"
    ERROR = "error"
