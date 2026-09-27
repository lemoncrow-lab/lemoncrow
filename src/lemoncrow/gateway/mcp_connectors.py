"""Durable hostname -> workspace bindings for central remote MCP transport.

A persistent MCP connector is no longer a LemonCrow server process.  Its
Cloudflare hostname selects a workspace on the one configured LemonCrow server;
this small registry is the durable routing table shared by ``lc mcp serve
--persistent`` and the server's HTTP/OAuth MCP surface.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from lemoncrow.core.foundation.paths import default_store_root

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ConnectorBinding:
    hostname: str
    workspace: str
    #: Hosted Authward connectors bind the hostname/workspace to one LC
    #: principal. Local persistent connectors leave both empty and use their
    #: local pairing/auth topology instead.
    org_id: str = ""
    subject: str = ""

    def to_json(self) -> dict[str, str]:
        return asdict(self)


def connector_slug(hostname: str) -> str:
    return _SLUG_RE.sub("-", hostname.strip().lower()).strip("-") or "default"


def default_connector_dir() -> Path:
    return default_store_root() / "mcp" / "connectors"


def connector_path(hostname: str) -> Path:
    return default_connector_dir() / f"{connector_slug(hostname)}.json"


def save_connector(binding: ConnectorBinding, *, path: Path | None = None) -> Path:
    hostname = binding.hostname.strip().lower()
    workspace = Path(binding.workspace).expanduser().resolve()
    if not hostname or "." not in hostname:
        raise ValueError("connector hostname must be a fully-qualified hostname")
    if not workspace.is_dir():
        raise ValueError(f"connector workspace is not a directory: {workspace}")
    target = path if path is not None else connector_path(hostname)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".connector.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            document = {"hostname": hostname, "workspace": str(workspace)}
            if binding.org_id:
                document["org_id"] = binding.org_id
            if binding.subject:
                document["subject"] = binding.subject
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


def load_connector(path: Path) -> ConnectorBinding | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    hostname = str(raw.get("hostname") or "").strip().lower()
    workspace = str(raw.get("workspace") or "").strip()
    if not hostname or not workspace:
        return None
    root = Path(workspace).expanduser()
    if not root.is_absolute() or not root.is_dir():
        return None
    org_id = str(raw.get("org_id") or "").strip()
    subject = str(raw.get("subject") or "").strip()
    if bool(org_id) != bool(subject):
        return None
    return ConnectorBinding(
        hostname=hostname,
        workspace=str(root.resolve()),
        org_id=org_id,
        subject=subject,
    )


def load_connector_for_hostname(hostname: str) -> ConnectorBinding | None:
    wanted = hostname.strip().lower().split(":", 1)[0]
    if not wanted:
        return None
    binding = load_connector(connector_path(wanted))
    if binding is None or binding.hostname != wanted:
        return None
    return binding


def load_all_connectors() -> list[ConnectorBinding]:
    try:
        paths = sorted(default_connector_dir().glob("*.json"))
    except OSError:
        return []
    rows = [binding for path in paths if (binding := load_connector(path)) is not None]
    return sorted(rows, key=lambda binding: binding.hostname)


def remove_connector(hostname: str) -> bool:
    try:
        connector_path(hostname).unlink()
        return True
    except FileNotFoundError:
        return False
