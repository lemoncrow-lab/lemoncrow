"""Versioned Review preview contract: discovery, revision pinning, rendering, failures."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import lemoncrow.pro.capabilities.review.comparisons as comparisons_module
import lemoncrow.pro.capabilities.review.previews as previews_module
import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
import lemoncrow.pro.environment.sandbox_limits as sandbox_limits
from lemoncrow.pro.capabilities.review.comparisons import (
    COMPARATOR_ENTRYPOINT_GROUP,
    ComparisonRequest,
    ComparisonValue,
    ImpactScore,
    JsonComparator,
    built_in_comparator_registry,
    comparison_descriptor,
    comparison_payload,
)
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.previews import (
    PREVIEW_RENDERER_ENTRYPOINT_GROUP,
    PageCapture,
    PreviewArtifact,
    PreviewRendererRegistry,
    PreviewRenderRequest,
    PreviewSandbox,
    PreviewUnavailable,
    SandboxResult,
    built_in_preview_registry,
    preview_availability,
    render_surface_preview,
)
from lemoncrow.pro.capabilities.review.review_setup import inspect_review_setup
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, record_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.storybook_preview import (
    StorybookPreviewRenderer,
    StorybookSurfaceProvider,
    parse_story_file,
    story_id,
)
from lemoncrow.pro.capabilities.review.surfaces import (
    ComparisonDescriptor,
    PreviewDescriptor,
    ReviewSurface,
    SurfaceContext,
    SurfaceRegistry,
    built_in_registry,
    load_review_surface_config,
)

PLUGIN_SRC = Path(__file__).resolve().parents[2] / "demo" / "review-preview-plugin" / "src"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


@pytest.fixture
def csv_plugin(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(PLUGIN_SRC))
    import lemoncrow_review_csv

    return lemoncrow_review_csv


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _write_tree(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        target = root / rel
        if text is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


class Repo:
    """A real Git repository whose commits become real immutable Review revisions."""

    def __init__(self, tmp_path: Path, base: dict[str, str | None]) -> None:
        self.root = tmp_path / "source"
        self.root.mkdir()
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.name", "Fixture Tester")
        _git(self.root, "config", "user.email", "fixture@example.invalid")
        _write_tree(self.root, base)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "base")
        self.store = ReviewStore(tmp_path / "store")

    def commit(self, files: dict[str, str | None], message: str = "change") -> Any:
        _write_tree(self.root, files)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", message)
        rng = resolve_rev_range(self.root, base="HEAD~1", head="HEAD")
        session = open_or_create_session(self.store, self.root, rng)
        build = build_review_packet_with_blobs(
            self.root,
            rng,
            store_root=self.store.root,
            with_impact=False,
            with_provenance=False,
            with_patch_text=True,
            unbounded_patch_text=True,
        )
        return record_revision(self.store, session, self.root, rng, store_root=self.store.root, build=build).revision

    def context(self, revision: Any, changed: tuple[str, ...]) -> SurfaceContext:
        return SurfaceContext(
            repo_root=self.root,
            store_root=self.store.root,
            revision=revision,
            changed_paths=changed,
            config=load_review_surface_config(self.root),
            store=self.store,
            dependency_root=self.root,
        )


CSV_CONFIG = "version: 1\nsurfaces:\n  - id: tables\n    provider: csv-table\n    paths: ['data/*.csv']\n"


def _registry(*renderers: Any) -> PreviewRendererRegistry:
    return PreviewRendererRegistry(renderers)


def _surface(renderer: str = "fixture", **descriptor: Any) -> ReviewSurface:
    return ReviewSurface(
        id="data/prices.csv",
        provider="fixture",
        kind="fixture.kind",
        title="prices",
        affected_paths=("data/prices.csv",),
        capabilities=("preview", "compare"),
        metadata={"surface_id": "tables"},
        preview=PreviewDescriptor(renderer=renderer, **{"display": "text", "media_type": "text/plain", **descriptor}),
    )


class _Renderer:
    renderer_id = "fixture"
    contract_version = 1

    def __init__(self, render: Callable[[PreviewRenderRequest], PreviewArtifact]) -> None:
        self._render = render
        self.calls: list[str] = []

    def render(self, request: PreviewRenderRequest) -> PreviewArtifact:
        self.calls.append(request.side)
        return self._render(request)


def test_preview_surfaces_inherit_generic_comparators_without_plugin_changes(tmp_path: Path) -> None:
    for display, media_type, expected in (
        ("image", "image/png", "image"),
        ("json", "application/json", "json"),
        ("text", "text/plain", "text"),
        ("html", "text/html", "text"),
        ("download", "application/octet-stream", "binary"),
    ):
        surface = _surface(display=display, media_type=media_type)
        descriptor = comparison_descriptor(surface)
        assert descriptor is not None
        assert descriptor.source == "preview"
        assert descriptor.comparator == expected
        assert descriptor.auto is True
        payload = comparison_payload(surface, built_in_comparator_registry(include_entrypoints=False))
        assert payload is not None
        assert payload["status"] == "available"
        assert payload["inferred"] is True


def test_json_comparator_ignores_key_order_and_reports_structural_change(tmp_path: Path) -> None:
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    old_path.write_text('{"b":2,"a":1}')
    new_path.write_text('{"a":1,"b":2}')
    surface = _surface(display="json", media_type="application/json")
    descriptor = ComparisonDescriptor(source="preview", comparator="json")
    comparator = JsonComparator()

    unchanged = comparator.compare(
        ComparisonRequest(
            surface,
            descriptor,
            ComparisonValue("old", path=old_path, media_type="application/json"),
            ComparisonValue("new", path=new_path, media_type="application/json"),
        )
    )
    assert unchanged.changed is False
    assert unchanged.magnitude == 0

    new_path.write_text('{"a":1,"b":3}')
    changed = comparator.compare(
        ComparisonRequest(
            surface,
            descriptor,
            ComparisonValue("old", path=old_path, media_type="application/json"),
            ComparisonValue("new", path=new_path, media_type="application/json"),
        )
    )
    assert changed.changed is True
    assert changed.magnitude > 0
    assert "JSON" in changed.label


def test_custom_comparator_entry_points_load_in_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixtureComparator:
        comparator_id = "fixture-domain"
        contract_version = 1

        def compare(self, _request: ComparisonRequest) -> ImpactScore:
            return ImpactScore(self.comparator_id, True, 0.75, "domain Δ 75%")

    def point(name: str, load: Callable[[], Any]) -> SimpleNamespace:
        return SimpleNamespace(name=name, load=load)

    def broken() -> Any:
        raise ImportError("missing comparator dependency")

    monkeypatch.setattr(
        comparisons_module,
        "_entry_points",
        lambda group: (
            [point("fixture-domain", lambda: FixtureComparator), point("broken", broken)]
            if group == COMPARATOR_ENTRYPOINT_GROUP
            else []
        ),
    )

    registry = built_in_comparator_registry()

    assert registry.get("fixture-domain") is not None
    assert {item.comparator_id for item in registry.comparators()} >= {
        "image",
        "json",
        "text",
        "binary",
        "api-response",
        "run-result",
        "fixture-domain",
    }
    assert len(registry.issues) == 1
    assert registry.issues[0].plugin == "broken"
    assert "missing comparator dependency" in registry.issues[0].reason


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


STORY_V1 = """import type { Meta, StoryObj } from "@storybook/react";
import { Button } from "./Button";
const meta = { title: "Design/Button", component: Button, args: { title: "not the meta title" } } satisfies Meta<typeof Button>;
export default meta;
export const Primary: StoryObj<typeof meta> = { args: { label: "Go" } };
"""
STORY_V2 = STORY_V1 + "export const WithIcon2: StoryObj<typeof meta> = { args: { icon: true } };\n"


def test_storybook_story_ids_follow_storybook_csf_rules() -> None:
    assert story_id("Design/Button", "Primary") == "design-button--primary"
    assert story_id("Design System/Button", "WithIcon2") == "design-system-button--with-icon-2"
    assert parse_story_file(STORY_V2) == ("Design/Button", ("Primary", "WithIcon2"))
    assert parse_story_file("export default { component: X };\nexport { A, B as C };\n") == ("", ("A", "C"))


def test_storybook_provider_discovers_changed_stories_and_component_siblings(tmp_path: Path) -> None:
    repo = Repo(
        tmp_path,
        {
            "web/.storybook/main.ts": "export default {};\n",
            "web/src/Button.tsx": "export const Button = () => null;\n",
            "web/src/Button.stories.tsx": STORY_V1,
            "api/handler.ts": "export {};\n",
        },
    )
    revision = repo.commit(
        {
            "web/src/Button.tsx": "export const Button = () => 'changed';\n",
            "web/src/Button.stories.tsx": STORY_V2,
            "api/handler.ts": "export const x = 1;\n",
        }
    )
    context = repo.context(revision, ("web/src/Button.tsx", "web/src/Button.stories.tsx", "api/handler.ts"))

    surfaces = SurfaceRegistry([StorybookSurfaceProvider()]).discover(context)

    by_id = {surface.id: surface for surface in surfaces}
    assert set(by_id) == {"web:design-button--primary", "web:design-button--with-icon-2"}
    primary = by_id["web:design-button--primary"]
    assert primary.kind == "storybook.story"
    # The component edit and the story edit are one surface, not two.
    assert set(primary.affected_paths) == {"web/src/Button.tsx", "web/src/Button.stories.tsx"}
    assert primary.preview is not None
    assert primary.preview.renderer == "storybook"
    assert primary.preview.scope == "web"
    assert primary.preview.options["story_id"] == "design-button--primary"
    assert primary.to_payload()["preview"]["version"] == 1


def test_storybook_setup_detects_projects(tmp_path: Path) -> None:
    _write_tree(tmp_path, {"apps/ui/.storybook/main.js": "module.exports = {};\n", "src/main.ts": ""})

    candidates = StorybookSurfaceProvider().setup_candidates(tmp_path)

    assert [(item.id, item.root, item.auto_write) for item in candidates] == [("storybook-apps-ui", "apps/ui", True)]
    report = inspect_review_setup(tmp_path)
    assert "storybook" in report.preview_renderers
    assert {"image", "json", "text", "binary", "api-response", "run-result"} <= set(report.surface_comparators)
    assert any(item["provider"] == "storybook" for item in report.to_payload()["detected"])
    assert "image" in report.to_payload()["surface_comparators"]


def test_failing_provider_is_isolated_with_a_specific_reason(tmp_path: Path) -> None:
    class Broken:
        provider_id = "broken"

        def discover(self, _context: SurfaceContext) -> tuple[ReviewSurface, ...]:
            raise RuntimeError("collection index is corrupt")

        def setup_candidates(self, _repo_root: Path) -> tuple[Any, ...]:
            return ()

    class Healthy:
        provider_id = "healthy"

        def discover(self, _context: SurfaceContext) -> tuple[ReviewSurface, ...]:
            return (ReviewSurface(id="a", provider="healthy", kind="unknown.kind", title="A"),)

        def setup_candidates(self, _repo_root: Path) -> tuple[Any, ...]:
            return ()

    registry = SurfaceRegistry([Broken(), Healthy()])
    context = SurfaceContext(
        tmp_path, tmp_path, SimpleNamespace(id="r", review_id="v"), (), load_review_surface_config(tmp_path)
    )  # type: ignore[arg-type]

    surfaces, issues = registry.discover_isolated(context)

    assert [surface.id for surface in surfaces] == ["a"]
    assert [(issue.plugin, issue.kind) for issue in issues] == [("broken", "provider")]
    assert "collection index is corrupt" in issues[0].reason
    with pytest.raises(RuntimeError):  # the historical strict API still propagates
        registry.discover(context)


def test_configured_provider_that_is_not_installed_is_reported(tmp_path: Path) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\nsurfaces:\n  - id: tables\n    provider: csv-table\n    paths: [data/*.csv]\n", encoding="utf-8"
    )
    registry = SurfaceRegistry([])
    context = SurfaceContext(
        tmp_path, tmp_path, SimpleNamespace(id="r", review_id="v"), (), load_review_surface_config(tmp_path)
    )  # type: ignore[arg-type]

    surfaces, issues = registry.discover_isolated(context)

    assert surfaces == ()
    assert [(issue.plugin, issue.kind) for issue in issues] == [("csv-table", "provider")]
    assert "no installed surface provider is named 'csv-table'" in issues[0].reason


def test_entry_point_plugins_load_in_isolation(monkeypatch: pytest.MonkeyPatch, csv_plugin: Any) -> None:
    def point(name: str, load: Callable[[], Any]) -> SimpleNamespace:
        return SimpleNamespace(name=name, load=load)

    def missing_module() -> Any:
        raise ImportError("No module named 'lemoncrow_review_gone'")

    class WebClash:
        provider_id = "web"

        def discover(self, _context: SurfaceContext) -> tuple[ReviewSurface, ...]:
            return ()

        def setup_candidates(self, _repo_root: Path) -> tuple[Any, ...]:
            return ()

    groups = {
        surfaces_module.PROVIDER_ENTRYPOINT_GROUP: [
            point("csv-table", lambda: csv_plugin.CsvTableSurfaceProvider),
            point("gone", missing_module),
            point("not-a-provider", lambda: object()),
            point("web-clash", lambda: WebClash),
        ],
        PREVIEW_RENDERER_ENTRYPOINT_GROUP: [
            point("csv-table", lambda: csv_plugin.CsvTableRenderer),
            point("gone", missing_module),
        ],
    }
    monkeypatch.setattr(surfaces_module, "_entry_points", lambda group: groups.get(group, []))
    monkeypatch.setattr(previews_module, "_entry_points", lambda group: groups.get(group, []))

    surfaces = built_in_registry()
    renderers = built_in_preview_registry()

    assert surfaces.get("csv-table") is not None
    assert surfaces.get("web").__class__.__name__ == "WebSurfaceProvider"
    reasons = {issue.plugin: issue.reason for issue in surfaces.issues}
    assert "No module named 'lemoncrow_review_gone'" in reasons["gone"]
    assert "does not implement ReviewSurfaceProvider" in reasons["not-a-provider"]
    assert "already registered" in reasons["web"]
    assert {renderer.renderer_id for renderer in renderers.renderers()} == {"storybook", "csv-table"}
    assert [issue.plugin for issue in renderers.issues] == ["gone"]


# --------------------------------------------------------------------------- #
# rendering and old/new revision isolation (example third-party plugin)
# --------------------------------------------------------------------------- #


def test_example_plugin_renders_each_side_from_its_own_immutable_revision(tmp_path: Path, csv_plugin: Any) -> None:
    repo = Repo(
        tmp_path,
        {".lemoncrow/review.yaml": CSV_CONFIG, "data/prices.csv": "sku,price\nA,1\n"},
    )
    first = repo.commit({"data/prices.csv": "sku,price\nA,2\nB,3\n"})
    renderers = _registry(csv_plugin.CsvTableRenderer())
    context = repo.context(first, ("data/prices.csv",))
    (surface,) = csv_plugin.CsvTableSurfaceProvider().discover(context)
    assert preview_availability(surface, renderers, context) == ""

    old = render_surface_preview(context, surface, side="old", renderers=renderers, source_repo=repo.root)
    new = render_surface_preview(context, surface, side="new", renderers=renderers, source_repo=repo.root)

    assert (old.status, new.status) == ("ready", "ready")
    assert old.path is not None and new.path is not None
    old_html, new_html = old.path.read_text(), new.path.read_text()
    assert "<td>1</td>" in old_html and "<td>2</td>" not in old_html
    assert "<td>2</td>" in new_html and "<td>B</td>" in new_html
    assert (old.revision_id, new.revision_id) == (first.id, first.id)
    assert old.path.parent != new.path.parent  # each side renders into its own artifact
    assert old.media_type == "text/html" and old.filename == "prices-old.html"

    # The live checkout moves on; the reviewed revision's preview does not.
    second = repo.commit({"data/prices.csv": "sku,price\nA,5\n"})
    again = render_surface_preview(context, surface, side="new", renderers=renderers, source_repo=repo.root)
    assert again.cached and again.path is not None and "<td>2</td>" in again.path.read_text()
    later = render_surface_preview(
        repo.context(second, ("data/prices.csv",)), surface, side="new", renderers=renderers, source_repo=repo.root
    )
    assert not later.cached and later.path is not None
    assert "<td>5</td>" in later.path.read_text() and later.revision_id == second.id


def test_snapshots_sharing_a_side_render_it_once(tmp_path: Path, csv_plugin: Any) -> None:
    """Every Snapshot of a Review usually shares the old side; it renders once."""
    repo = Repo(tmp_path, {".lemoncrow/review.yaml": CSV_CONFIG, "data/prices.csv": "sku,price\nA,1\n"})
    calls: list[str] = []

    class CountingRenderer(csv_plugin.CsvTableRenderer):  # type: ignore[misc, name-defined]
        def render(self, request: PreviewRenderRequest) -> PreviewArtifact:
            calls.append(request.side)
            return super().render(request)

    renderers = _registry(CountingRenderer())

    def snapshot_and_render(prices: str) -> tuple[Any, str]:
        # The agent edits the working tree again; the same Review gets a new Snapshot.
        _write_tree(repo.root, {"data/prices.csv": prices})
        rng = resolve_rev_range(repo.root, None, working_tree=True)
        session = open_or_create_session(repo.store, repo.root, rng)
        build = build_review_packet_with_blobs(
            repo.root, rng, store_root=repo.store.root, with_impact=False, with_provenance=False, with_patch_text=True
        )
        revision = record_revision(repo.store, session, repo.root, rng, store_root=repo.store.root, build=build)
        context = repo.context(revision.revision, ("data/prices.csv",))
        (surface,) = csv_plugin.CsvTableSurfaceProvider().discover(context)
        old = render_surface_preview(context, surface, side="old", renderers=renderers, source_repo=repo.root)
        new = render_surface_preview(context, surface, side="new", renderers=renderers, source_repo=repo.root)
        assert (old.status, new.status) == ("ready", "ready")
        assert old.revision_id == new.revision_id == revision.revision.id  # served for the requested Snapshot
        assert old.path is not None and "<td>1</td>" in old.path.read_text()
        assert new.path is not None
        return revision.revision, new.path.read_text()

    first, first_new = snapshot_and_render("sku,price\nA,2\n")
    second, second_new = snapshot_and_render("sku,price\nA,3\n")

    assert first.review_id == second.review_id and first.base_sha == second.base_sha and first.id != second.id
    assert "<td>2</td>" in first_new and "<td>3</td>" in second_new
    # The shared old side rendered once; each Snapshot's own new side rendered once.
    assert calls == ["old", "new", "new"]


def test_added_file_has_a_specific_old_side_reason(tmp_path: Path, csv_plugin: Any) -> None:
    repo = Repo(tmp_path, {".lemoncrow/review.yaml": CSV_CONFIG, "README.md": "x\n"})
    revision = repo.commit({"data/prices.csv": "sku\nA\n"})
    renderers = _registry(csv_plugin.CsvTableRenderer())
    context = repo.context(revision, ("data/prices.csv",))
    (surface,) = csv_plugin.CsvTableSurfaceProvider().discover(context)

    old = render_surface_preview(context, surface, side="old", renderers=renderers, source_repo=repo.root)
    new = render_surface_preview(context, surface, side="new", renderers=renderers, source_repo=repo.root)

    assert old.status == "unavailable"
    assert old.reason == "data/prices.csv does not exist on the old side (added by this change)"
    assert new.status == "ready"


def test_storybook_renderer_builds_once_per_side_and_isolates_new_stories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo(
        tmp_path,
        {
            "web/.storybook/main.ts": "export default {};\n",
            "web/package.json": "{}\n",
            "web/src/Button.stories.tsx": STORY_V1,
        },
    )
    revision = repo.commit({"web/src/Button.stories.tsx": STORY_V2})
    context = repo.context(revision, ("web/src/Button.stories.tsx",))
    surfaces = {item.locator: item for item in StorybookSurfaceProvider().discover(context)}
    builds: list[tuple[str, list[str]]] = []
    pages: list[str] = []
    modules = tmp_path / "node_modules"
    (modules / ".bin").mkdir(parents=True)
    (modules / ".bin" / "storybook").write_text("#!/usr/bin/env node\n")

    def fake_build(self: PreviewSandbox, argv: list[str], **kwargs: Any) -> SandboxResult:
        story_source = (self.workspace / "web/src/Button.stories.tsx").read_text()
        side = "old" if story_source == STORY_V1 else "new"
        builds.append((side, list(argv)))
        _title, exports = parse_story_file(story_source)
        static = self.workspace / "web" / ".lemoncrow-storybook"
        static.mkdir(parents=True)
        (static / "iframe.html").write_text("<html></html>")
        entries = {story_id("Design/Button", name): {} for name in exports}
        (static / "index.json").write_text(json.dumps({"v": 5, "entries": entries}))
        assert kwargs["cwd"] == "web" and kwargs["node_modules"] == modules
        return SandboxResult(0, "built", 5)

    def fake_capture(self: PreviewSandbox, site: Path, page: str = "/", **kwargs: Any) -> PageCapture:
        pages.append(page)
        target = self.output_dir / kwargs["filename"]
        target.write_bytes(PNG + page.encode())
        return PageCapture(path=target, dom='<html><body class="sb-main-padded sb-show-main"></body></html>')

    monkeypatch.setattr(PreviewSandbox, "node_modules", lambda self, root=".": modules)
    monkeypatch.setattr(PreviewSandbox, "run", fake_build)
    monkeypatch.setattr(PreviewSandbox, "capture", fake_capture)
    renderers = _registry(StorybookPreviewRenderer())

    def render(locator: str, side: str) -> Any:
        return render_surface_preview(context, surfaces[locator], side=side, renderers=renderers, source_repo=repo.root)

    new_primary = render("design-button--primary", "new")
    new_added = render("design-button--with-icon-2", "new")
    old_primary = render("design-button--primary", "old")
    old_added = render("design-button--with-icon-2", "old")

    assert [new_primary.status, new_added.status, old_primary.status] == ["ready"] * 3
    assert old_added.status == "unavailable"
    assert old_added.reason == "story design-button--with-icon-2 does not exist in the old side's Storybook"
    assert [side for side, _argv in builds] == ["new", "old"]  # one build per side, shared by stories
    assert builds[0][1][:2] == ["node_modules/.bin/storybook", "build"]
    assert "/iframe.html?id=design-button--primary&viewMode=story" in pages
    assert new_primary.media_type == "image/png" and new_primary.filename == "design-button--primary-new.png"


STORYBOOK_ERROR_DOM = (
    '<html><body class="sb-main-padded sb-show-errordisplay"><div class="sb-errordisplay sb-wrapper">'
    '<div class="sb-errordisplay_main"><h1 id="error-message"></h1>'
    '<pre class="sb-errordisplay_code"><code id="error-stack">ReferenceError: React is not defined\n'
    "    at Primary (http://127.0.0.1:8787/assets/Button.js:3:5)</code></pre></div></div></body></html>"
)


@pytest.mark.parametrize(
    ("dom", "status", "reason"),
    [
        (
            STORYBOOK_ERROR_DOM,
            "failed",
            "story design-button--primary threw while rendering on the new side: ReferenceError: React is not defined",
        ),
        (
            '<body class="sb-show-nopreview"></body>',
            "unavailable",
            "Storybook showed no preview for story design-button--primary on the new side",
        ),
        (
            '<body class="sb-show-preparing-story"></body>',
            "failed",
            "story design-button--primary was still loading on the new side when the capture finished",
        ),
    ],
)
def test_a_story_that_does_not_render_is_reported_not_screenshotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dom: str, status: str, reason: str
) -> None:
    repo = Repo(
        tmp_path,
        {
            "web/.storybook/main.ts": "export default {};\n",
            "web/package.json": "{}\n",
            "web/src/Button.stories.tsx": STORY_V1,
        },
    )
    revision = repo.commit({"web/src/Button.stories.tsx": STORY_V2})
    context = repo.context(revision, ("web/src/Button.stories.tsx",))
    surface = next(item for item in StorybookSurfaceProvider().discover(context) if item.locator.endswith("primary"))

    def fake_build(self: PreviewSandbox, _argv: list[str], **_kwargs: Any) -> SandboxResult:
        static = self.workspace / "web" / ".lemoncrow-storybook"
        static.mkdir(parents=True)
        (static / "iframe.html").write_text("<html></html>")
        return SandboxResult(0, "built", 5)

    def fake_capture(self: PreviewSandbox, _site: Path, _page: str = "/", **kwargs: Any) -> PageCapture:
        target = self.output_dir / kwargs["filename"]
        target.write_bytes(PNG)  # the error screen still captures fine
        return PageCapture(path=target, dom=dom)

    modules = tmp_path / "node_modules"
    (modules / ".bin").mkdir(parents=True)
    (modules / ".bin" / "storybook").write_text("#!/usr/bin/env node\n")
    monkeypatch.setattr(PreviewSandbox, "node_modules", lambda self, root=".": modules)
    monkeypatch.setattr(PreviewSandbox, "run", fake_build)
    monkeypatch.setattr(PreviewSandbox, "capture", fake_capture)

    result = render_surface_preview(
        context, surface, side="new", renderers=_registry(StorybookPreviewRenderer()), source_repo=repo.root
    )

    assert (result.status, result.reason) == (status, reason)
    assert result.path is None


def test_a_build_killed_by_the_memory_limit_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sandbox_limits.MEMORY_ENV, "512")

    def killed(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 137, stdout="building...\n")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.web_preview.shutil.which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(previews_module.subprocess, "run", killed)

    result = _sandbox(tmp_path).run(["node", "build.js"], cwd="web", timeout=30)

    assert result.exit_code == 137
    assert result.limit_note.startswith("the sandbox was killed (SIGKILL), most likely for exceeding its 512 MB")
    assert result.limit_note in result.output


# --------------------------------------------------------------------------- #
# unknown / incompatible / failing plugins
# --------------------------------------------------------------------------- #


@pytest.fixture
def csv_repo(tmp_path: Path) -> tuple[Repo, Any]:
    repo = Repo(tmp_path, {".lemoncrow/review.yaml": CSV_CONFIG, "data/prices.csv": "sku\nA\n"})
    return repo, repo.commit({"data/prices.csv": "sku\nB\n"})


def test_unknown_renderer_and_contract_versions_are_unavailable_with_reasons(csv_repo: tuple[Repo, Any]) -> None:
    repo, revision = csv_repo
    context = repo.context(revision, ("data/prices.csv",))
    renderers = _registry(_Renderer(lambda _request: PreviewArtifact.unavailable("unused")))

    unknown = render_surface_preview(context, _surface("figma"), side="new", renderers=renderers)
    assert unknown.status == "unavailable"
    assert "preview renderer 'figma' is not installed" in unknown.reason
    assert "lemoncrow.review_preview_renderers" in unknown.reason

    future = render_surface_preview(context, _surface(version=2), side="new", renderers=renderers)
    assert future.status == "unavailable"
    assert future.reason.startswith("preview contract version 2 is not supported")

    bad_display = render_surface_preview(context, _surface(display="hologram"), side="new", renderers=renderers)
    assert bad_display.reason == "preview display 'hologram' is not supported"

    stale = _Renderer(lambda _request: PreviewArtifact.unavailable("unused"))
    stale.contract_version = 99  # type: ignore[misc]
    incompatible = render_surface_preview(context, _surface(), side="new", renderers=_registry(stale))
    assert "implements contract version 99" in incompatible.reason
    assert stale.calls == []


def test_renderer_failures_become_specific_reasons_and_are_not_cached(csv_repo: tuple[Repo, Any]) -> None:
    repo, revision = csv_repo
    context = repo.context(revision, ("data/prices.csv",))

    def crash(_request: PreviewRenderRequest) -> PreviewArtifact:
        raise RuntimeError("storybook exploded")

    def escape(request: PreviewRenderRequest) -> PreviewArtifact:
        outside = request.workspace / "leak.txt"
        outside.write_text("secret")
        return PreviewArtifact.ready(outside, "text/plain")

    def fake_png(request: PreviewRenderRequest) -> PreviewArtifact:
        target = request.output_dir / "shot.png"
        target.write_text("<script>alert(1)</script>")
        return PreviewArtifact.ready(target, "image/png")

    def unavailable(_request: PreviewRenderRequest) -> PreviewArtifact:
        raise PreviewUnavailable("Chrome is not installed")

    cases: list[tuple[Callable[[PreviewRenderRequest], PreviewArtifact], ReviewSurface, str, str]] = [
        (crash, _surface(), "failed", "preview renderer 'fixture' raised RuntimeError: storybook exploded"),
        (
            escape,
            _surface(),
            "failed",
            "preview renderer 'fixture' wrote its artifact outside the preview output directory",
        ),
        (
            fake_png,
            _surface(display="image", media_type="image/png"),
            "failed",
            "preview renderer 'fixture': artifact is not a valid image/png image",
        ),
        (unavailable, _surface(), "unavailable", "Chrome is not installed"),
    ]
    for render, surface, status, reason in cases:
        result = render_surface_preview(context, surface, side="new", renderers=_registry(_Renderer(render)))
        assert (result.status, result.reason) == (status, reason)

    def good(request: PreviewRenderRequest) -> PreviewArtifact:
        target = request.output_dir / "ok.txt"
        target.write_text("fine")
        return PreviewArtifact.ready(target, "text/plain")

    recovered = _Renderer(good)
    result = render_surface_preview(context, _surface(), side="new", renderers=_registry(recovered))
    assert result.status == "ready" and not result.cached
    assert recovered.calls == ["new"]


def test_configured_limits_and_disable_switch_apply_to_plugins(csv_repo: tuple[Repo, Any]) -> None:
    repo, revision = csv_repo

    def configure(preview: str) -> SurfaceContext:
        (repo.root / ".lemoncrow/review.yaml").write_text(
            "version: 1\nsurfaces:\n  - id: tables\n    provider: fixture\n" + preview, encoding="utf-8"
        )
        return repo.context(revision, ("data/prices.csv",))

    def big(request: PreviewRenderRequest) -> PreviewArtifact:
        target = request.output_dir / "big.txt"
        target.write_text("x" * 4096)
        return PreviewArtifact.ready(target, "text/plain")

    limited = render_surface_preview(
        configure("    preview:\n      max_bytes: 1024\n"), _surface(), side="new", renderers=_registry(_Renderer(big))
    )
    assert limited.status == "failed"
    assert limited.reason == "preview renderer 'fixture' artifact is 4096 bytes; the limit is 1024"

    ceiling = previews_module.preview_limits(configure("    preview:\n      timeout: 99999\n").config.surfaces[0])
    assert ceiling.timeout_seconds == previews_module.MAX_PREVIEW_SECONDS

    disabled_renderer = _Renderer(big)
    disabled = render_surface_preview(
        configure("    preview:\n      enabled: false\n"),
        _surface(),
        side="new",
        renderers=_registry(disabled_renderer),
    )
    assert disabled.reason == "preview is disabled for this surface in .lemoncrow/review.yaml"
    assert disabled_renderer.calls == []


def test_missing_snapshot_side_is_reported_not_substituted(
    csv_repo: tuple[Repo, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable

    repo, revision = csv_repo

    def unavailable(*_args: Any, **_kwargs: Any) -> str:
        raise ReviewSnapshotUnavailable("old revision tree is unavailable")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.snapshot.materialize_review_side", unavailable)
    renderer = _Renderer(lambda _request: PreviewArtifact.unavailable("unused"))

    result = render_surface_preview(
        repo.context(revision, ("data/prices.csv",)), _surface(), side="old", renderers=_registry(renderer)
    )

    assert result.status == "unavailable"
    assert result.reason == "exact revision old side is unavailable: old revision tree is unavailable"
    assert renderer.calls == []


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #


def _sandbox(tmp_path: Path, *, deadline: float = 1e12) -> PreviewSandbox:
    workspace = tmp_path / "ws"
    (workspace / "web").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    return PreviewSandbox(
        workspace=workspace, output_dir=output, trusted_checkout=tmp_path / "checkout", deadline=deadline
    )


def test_sandbox_refuses_to_run_reviewed_code_without_bubblewrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.web_preview.shutil.which", lambda _name: None)

    with pytest.raises(PreviewUnavailable, match="requires bubblewrap; refusing to run project code unsandboxed"):
        _sandbox(tmp_path).run(["node", "build.js"])


def test_sandbox_applies_network_isolation_mounts_env_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(command, 0, stdout="ok\n" * 3000)

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.web_preview.shutil.which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(previews_module.subprocess, "run", fake_run)
    sandbox = _sandbox(tmp_path)

    result = sandbox.run(["node", "build.js"], cwd="web", timeout=30, env={"STORYBOOK_DISABLE_TELEMETRY": "1"})

    command = seen["command"]
    # The host memory/task limiter wraps the whole sandbox, then bubblewrap isolates it.
    limiter = sandbox_limits.limit_prefix()
    assert command[: len(limiter)] == limiter
    command = command[len(limiter) :]
    assert command[0] == "/usr/bin/bwrap" and "--unshare-net" in command and "--clearenv" in command
    assert command[command.index(str(sandbox.workspace)) + 1] == "/workspace"
    assert command[command.index(str(sandbox.output_dir)) + 1] == "/capture"
    assert command[command.index("--chdir") + 1] == "/workspace/web"
    assert command[command.index("STORYBOOK_DISABLE_TELEMETRY") + 1] == "1"
    assert command[-2:] == ["node", "build.js"]
    assert seen["timeout"] == 30
    assert len(result.output) <= previews_module.MAX_LOG_CHARS

    with pytest.raises(PreviewUnavailable, match="environment variable is not allowed: PATH"):
        sandbox.run(["node"], env={"PATH": "/tmp/evil"})
    with pytest.raises(PreviewUnavailable, match="must stay inside the repository"):
        sandbox.run(["node"], cwd="../outside")
    with pytest.raises(PreviewUnavailable, match="time budget exhausted"):
        _sandbox(tmp_path / "late", deadline=0).run(["node"])


def test_sandbox_refuses_dependencies_that_differ_from_the_reviewed_lockfile(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    (sandbox.workspace / "web" / "package.json").write_text(json.dumps({"dependencies": {"react": "19"}}))
    checkout = sandbox.trusted_checkout / "web"
    checkout.mkdir(parents=True)
    (checkout / "package.json").write_text(json.dumps({"dependencies": {"react": "18"}}))

    with pytest.raises(PreviewUnavailable, match="reviewed dependency graph differs from the checkout"):
        sandbox.node_modules("web")
