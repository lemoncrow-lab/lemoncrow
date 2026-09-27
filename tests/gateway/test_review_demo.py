from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import lemoncrow.pro.capabilities.review.previews as previews_module
import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.previews import (
    PREVIEW_RENDERER_ENTRYPOINT_GROUP,
    built_in_preview_registry,
    render_surface_preview,
)
from lemoncrow.pro.capabilities.review.review_setup import inspect_review_setup
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, record_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.surfaces import SurfaceContext, built_in_registry, load_review_surface_config

DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo" / "review-surfaces"
PLUGIN_SRC = DEMO_ROOT.parent / "review-preview-plugin" / "src"


def _demo_plugin(monkeypatch: pytest.MonkeyPatch, *, installed: bool) -> None:
    """Pin entry-point discovery to exactly the demo's example plugin, or to nothing."""

    groups: dict[str, list[SimpleNamespace]] = {}
    if installed:
        monkeypatch.syspath_prepend(str(PLUGIN_SRC))
        import lemoncrow_review_csv as plugin

        groups = {
            surfaces_module.PROVIDER_ENTRYPOINT_GROUP: [
                SimpleNamespace(name="csv-table", load=lambda: plugin.CsvTableSurfaceProvider)
            ],
            PREVIEW_RENDERER_ENTRYPOINT_GROUP: [
                SimpleNamespace(name="csv-table", load=lambda: plugin.CsvTableRenderer)
            ],
        }
    monkeypatch.setattr(surfaces_module, "_entry_points", lambda group: groups.get(group, []))
    monkeypatch.setattr(previews_module, "_entry_points", lambda group: groups.get(group, []))


def test_review_surface_demo_exercises_builtin_surfaces_and_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    _demo_plugin(monkeypatch, installed=True)
    config = load_review_surface_config(DEMO_ROOT)

    assert {runtime.id: runtime.runner for runtime in config.runtimes} == {
        "app-stack": "docker-compose",
        "backend-tests": "command",
        "demo-api": "http-service",
    }
    assert {(surface.id, surface.provider) for surface in config.surfaces} == {
        ("frontend", "web"),
        ("api-requests", "bruno"),
        ("app-services", "service"),
        ("backend-checks", "service"),
        ("components", "storybook"),
        ("tables", "csv-table"),
    }
    api_surface = next(surface for surface in config.surfaces if surface.id == "api-requests")
    assert api_surface.string("runtime") == "demo-api"

    report = inspect_review_setup(DEMO_ROOT)
    assert {candidate.provider for candidate in report.candidates} >= {"web", "bruno", "storybook"}
    assert {candidate.runner for candidate in report.runtime_candidates} >= {"docker-compose"}
    assert {"storybook", "csv-table"} <= set(report.preview_renderers)
    assert not [issue for issue in report.issues if issue.level == "error"]


def test_review_surface_demo_names_the_uninstalled_third_party_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    _demo_plugin(monkeypatch, installed=False)

    report = inspect_review_setup(DEMO_ROOT)

    assert [issue.message for issue in report.issues if issue.level == "error"] == [
        "surface 'tables' uses unavailable provider 'csv-table'; install its provider plugin or fix the config"
    ]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def test_review_surface_demo_change_reaches_storybook_and_the_csv_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    _demo_plugin(monkeypatch, installed=True)
    root = tmp_path / "demo"
    shutil.copytree(
        DEMO_ROOT, root, ignore=shutil.ignore_patterns("node_modules", "__pycache__", "dist", "storybook-static")
    )
    shutil.copytree(root / ".fixtures" / "media" / "before", root / "media")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Demo Tester")
    _git(root, "config", "user.email", "demo@example.invalid")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline")
    subprocess.run([sys.executable, str(root / "scripts" / "apply-demo-change.py")], check=True, capture_output=True)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "demo change")

    store = ReviewStore(tmp_path / "store")
    rng = resolve_rev_range(root, base="HEAD~1", head="HEAD")
    build = build_review_packet_with_blobs(
        root, rng, store_root=store.root, with_impact=False, with_provenance=False, with_patch_text=True
    )
    session = open_or_create_session(store, root, rng)
    revision = record_revision(store, session, root, rng, store_root=store.root, build=build).revision
    context = SurfaceContext(
        repo_root=root,
        store_root=store.root,
        revision=revision,
        changed_paths=tuple(_git(root, "diff", "--name-only", "HEAD~1", "HEAD").split()),
        config=load_review_surface_config(root),
        store=store,
        dependency_root=root,
    )

    surfaces, issues = built_in_registry().discover_isolated(context)

    assert not issues
    # Five routes and six requests exist; only the three of each that this change touches are listed.
    assert sorted(surface.id for surface in surfaces if surface.provider == "web") == [
        "frontend:/",
        "frontend:/pricing",
        "frontend:/surfaces",
    ]
    assert len(list((root / "bruno").glob("*.bru"))) == 6
    assert sorted(surface.id for surface in surfaces if surface.provider == "bruno") == [
        "bruno/Plans.bru",
        "bruno/ReviewSummary.bru",
        "bruno/Settings.bru",
    ]
    assert all("auto_compare" in surface.capabilities for surface in surfaces if surface.provider in {"web", "bruno"})
    stories = [surface for surface in surfaces if surface.provider == "storybook"]
    assert sorted(surface.id for surface in stories) == [
        "frontend:demo-surfacecard--api",
        "frontend:demo-surfacecard--components",
        "frontend:demo-surfacecard--web",
    ]
    assert {surface.preview.scope for surface in stories if surface.preview} == {"frontend"}
    table = next(surface for surface in surfaces if surface.provider == "csv-table")
    assert table.id == "data/plans.csv"

    renderers = built_in_preview_registry()
    old = render_surface_preview(context, table, side="old", renderers=renderers, source_repo=root)
    new = render_surface_preview(context, table, side="new", renderers=renderers, source_repo=root)
    assert (old.status, new.status) == ("ready", "ready"), (old.reason, new.reason)
    assert old.path is not None and new.path is not None
    old_html, new_html = old.path.read_text(encoding="utf-8"), new.path.read_text(encoding="utf-8")
    assert "<td>49</td>" in old_html and "Enterprise" not in old_html
    assert "<td>59</td>" in new_html and "<td>Enterprise</td>" in new_html


def test_review_surface_demo_carries_distinct_before_after_media_fixtures() -> None:
    before = DEMO_ROOT / ".fixtures" / "media" / "before"
    after = DEMO_ROOT / ".fixtures" / "media" / "after"
    names = {"demo.gif", "demo.mp4", "demo.wav", "demo.pdf"}

    assert {path.name for path in before.iterdir() if path.is_file()} == names
    assert {path.name for path in after.iterdir() if path.is_file()} == names
    for name in names:
        old = (before / name).read_bytes()
        new = (after / name).read_bytes()
        assert old
        assert new
        assert old != new
