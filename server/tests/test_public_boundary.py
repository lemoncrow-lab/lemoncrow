from __future__ import annotations

import ast
from pathlib import Path

from lemoncrow_server_core.protocol import server_identity

PUBLIC_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "lemoncrow_server_core"


def test_public_server_never_imports_private_enterprise_package() -> None:
    offenders: list[str] = []
    for path in sorted(PUBLIC_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name == "lemoncrow_server" or name.startswith("lemoncrow_server."):
                    offenders.append(f"{path.relative_to(PUBLIC_PACKAGE)}:{node.lineno} -> {name}")
    assert offenders == [], "public server imports private enterprise code: " + ", ".join(offenders)


def test_public_server_has_no_per_organization_concurrency_policy() -> None:
    limits = (PUBLIC_PACKAGE / "limits.py").read_text(encoding="utf-8")
    assert "max_concurrent_requests_per_org" not in limits
    assert "organization is at its in-flight request limit" not in limits


def test_public_server_has_no_rbac_vocabulary() -> None:
    forbidden = {"AuthMethod", "Permission", "Role", "RoleBinding", "Scope", "ScopeLevel"}
    offenders: list[str] = []
    for path in sorted(PUBLIC_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden:
                offenders.append(f"{path.relative_to(PUBLIC_PACKAGE)}:{node.lineno} -> {node.id}")
    assert offenders == [], "public server contains enterprise RBAC vocabulary: " + ", ".join(offenders)


def test_public_discovery_does_not_advertise_enterprise_identity_routes() -> None:
    endpoints = server_identity(allow_local_fs=False)["endpoints"]
    assert "device_authorization" not in endpoints
    assert set(endpoints) == {"handshake", "session_open", "tools", "views_open", "blobs"}
