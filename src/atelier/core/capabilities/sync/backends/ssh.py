"""Self-host SSH/SCP-style backend adapter."""

from __future__ import annotations

from pathlib import Path

from . import DirectorySyncBackend


class SSHSyncBackend(DirectorySyncBackend):
    backend_name = "ssh"

    def __init__(
        self,
        *,
        remote_root: Path,
        account_id: str = "default",
        host: str = "localhost",
        remote_path: str = "atelier-sync",
    ) -> None:
        descriptor = f"ssh ({host}:{remote_path})"
        super().__init__(
            remote_root=remote_root / account_id / "ssh" / remote_path.strip("/").replace("/", "_"),
            descriptor_text=descriptor,
            backend_name="ssh",
        )


__all__ = ["SSHSyncBackend"]
