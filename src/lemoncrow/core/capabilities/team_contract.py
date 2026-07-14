"""Public contract types for local team workspaces.

Both the workspace exception hierarchy and the pydantic state models are
caller-facing contract, not engine IP. They live in this open module because
neither builtin-exception subclasses nor pydantic models can be mypyc-compiled;
the pro team logic compiles to native ``.so`` and imports these names.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lemoncrow.core.foundation.models import _utcnow
from lemoncrow.infra.storage.ids import make_uuid7


class TeamWorkspaceError(RuntimeError):
    """Base class for workspace operations."""


class TeamWorkspaceNotInitializedError(TeamWorkspaceError):
    """Raised when a command expects an initialized workspace."""


class TeamPermissionError(TeamWorkspaceError):
    """Raised when the current user lacks sufficient role."""


TeamRole = Literal["admin", "member", "viewer"]


def _team_id() -> str:
    return f"team-{make_uuid7()}"


def _account_id() -> str:
    return f"acct-{make_uuid7()}"


def _invite_id() -> str:
    return f"invite-{make_uuid7()}"


class TeamMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    email: str
    role: TeamRole
    auth_provider: str = "google"
    joined_at: datetime = Field(default_factory=_utcnow)


class TeamInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=_invite_id)
    code: str
    email: str
    role: TeamRole
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    used_by_user_id: str | None = None
    used_at: datetime | None = None


class TeamAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: datetime = Field(default_factory=_utcnow)
    action: str
    actor_user_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class TeamWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=_team_id)
    account_id: str = Field(default_factory=_account_id)
    name: str
    created_at: datetime = Field(default_factory=_utcnow)
    current_user_id: str | None = None
    members: list[TeamMember] = Field(default_factory=list)
    invites: list[TeamInvite] = Field(default_factory=list)
    machine_bindings: dict[str, str] = Field(default_factory=dict)
