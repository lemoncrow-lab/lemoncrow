"""Self-host S3-shaped backend adapter."""

from __future__ import annotations

from pathlib import Path

from . import DirectorySyncBackend


class S3SyncBackend(DirectorySyncBackend):
    backend_name = "s3"

    def __init__(
        self,
        *,
        remote_root: Path,
        account_id: str = "default",
        bucket: str = "atelier-sync",
        prefix: str = "default",
    ) -> None:
        super().__init__(
            remote_root=remote_root / account_id / "s3" / prefix,
            descriptor_text=f"s3 ({bucket}/{prefix})",
            backend_name="s3",
        )


__all__ = ["S3SyncBackend"]
