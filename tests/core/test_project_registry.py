from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.core.service.project_registry import (
    project_id_for_root,
    register_project,
    registered_projects,
    resolve_registered_project,
)


def test_register_project_is_stable_and_resolves_by_id_or_exact_path(tmp_path: Path) -> None:
    store = tmp_path / "store"
    project = tmp_path / "repo"
    project.mkdir()

    first = register_project(project, store_root=store)
    second = register_project(project, store_root=store)

    assert first == second
    assert first.project_id == project_id_for_root(project)
    assert registered_projects(store_root=store) == (first,)
    assert resolve_registered_project(first.project_id, store_root=store) == first
    assert resolve_registered_project(str(project), store_root=store) == first


def test_registry_rejects_unregistered_and_tampered_paths(tmp_path: Path) -> None:
    store = tmp_path / "store"
    registered = tmp_path / "registered"
    other = tmp_path / "other"
    registered.mkdir()
    other.mkdir()
    record = register_project(registered, store_root=store)

    assert resolve_registered_project(str(other), store_root=store) is None

    record_path = store / "projects" / f"{record.project_id}.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["root"] = str(other)
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    assert registered_projects(store_root=store) == ()
    assert resolve_registered_project(record.project_id, store_root=store) is None


def test_project_id_uses_canonical_path(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    alias = project / ".." / "repo"

    assert project_id_for_root(alias) == project_id_for_root(project)
