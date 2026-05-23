"""Atelier Cloud backend contract and local-dev adapter."""

from __future__ import annotations

from pathlib import Path

from . import DirectorySyncBackend, SyncBackendError


class CloudAccessError(SyncBackendError):
    """Raised when Atelier Cloud access is unavailable or denied."""


class CloudSyncBackend(DirectorySyncBackend):
    backend_name = "cloud"

    def __init__(
        self, *, remote_root: Path | None = None, api_url: str | None = None, account_id: str = "default"
    ) -> None:
        if remote_root is not None:
            super().__init__(
                remote_root=remote_root / account_id / "cloud",
                descriptor_text=f"cloud ({account_id})",
                backend_name="cloud",
            )
            return
        if not api_url:
            raise CloudAccessError(
                "Atelier Cloud access is not available for this machine. "
                "Re-run `atelier sync init --backend s3` or `--backend ssh` to choose self-host explicitly."
            )
        raise CloudAccessError(
            f"Atelier Cloud endpoint {api_url!r} is configured but this repo does not ship the cloud service implementation."
        )


__all__ = ["CloudAccessError", "CloudSyncBackend"]
