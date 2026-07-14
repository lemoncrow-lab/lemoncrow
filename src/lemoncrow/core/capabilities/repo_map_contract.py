"""Public contract type for the repo-map builder.

``RepoMapResult`` is the caller-facing result shape (data contract, not engine
IP). It lives here (open) because pydantic models cannot be mypyc-compiled, so
the pro ``repo_map`` logic can compile to native ``.so`` while callers import the
same type.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RepoMapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outline: str
    ranked_files: list[str]
    token_count: int
    budget_tokens: int
