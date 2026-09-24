from __future__ import annotations

import hashlib
import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pygit2
import pytest

from lemoncrow.pro.capabilities.review import web_preview
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, record_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore


def _revision(*, mode: str = "commit_range") -> SimpleNamespace:
    return SimpleNamespace(
        id="revision",
        review_id="review",
        range_mode=mode,
        base_sha="base",
        head_sha="head",
    )


def _next_files() -> dict[str, bytes]:
    return {
        "package.json": json.dumps({"dependencies": {"next": "15", "react": "19"}}).encode(),
        "src/app/layout.tsx": b'import "./globals.css"; export default function Layout({children}) { return children }',
        "src/app/globals.css": b'@import "../styles/tokens.css"; body { background: url("/hero.png") }',
        "src/styles/tokens.css": b":root { --ink: #111; }",
        "public/hero.png": b"\x89PNG\r\n\x1a\n\x00\xff",
        "src/app/page.tsx": b'import Hero from "@/components/Hero"; export default function Page(){ return <Hero/> }',
        "src/app/scan/page.tsx": b'import Hero from "@/components/Hero"; export default function Page(){ return <Hero/> }',
        "src/components/Hero.tsx": b'import "./hero.css"; export default function Hero(){ return <div/> }',
        "src/components/hero.css": b'.hero { background-image: url("/hero.png") }',
    }


def _vite_files() -> dict[str, bytes]:
    return {
        "package.json": json.dumps({"dependencies": {"vite": "6", "react": "19", "react-router-dom": "7"}}).encode(),
        "src/main.tsx": b'import App from "./App"; import "./index.css"; render(<App/>);',
        "src/index.css": b"body { margin: 0 }",
        "src/App.tsx": (
            b'import { Route, Routes } from "react-router-dom";\n'
            b'import Overview from "./pages/Overview";\n'
            b'import Sessions from "./pages/Sessions";\n'
            b'const ReviewReader = lazy(() => import("./review/ReviewReader"));\n'
            b"export default function App(){ return <Routes>\n"
            b'<Route path="/" element={<Overview/>} />\n'
            b'<Route path="/overview" element={<Overview/>} />\n'
            b'<Route path="/sessions" element={<Sessions/>} />\n'
            b'<Route path="/sessions/:id" element={<Sessions/>} />\n'
            b'<Route path="/review" element={<ReviewReader/>} />\n'
            b"</Routes> }\n"
        ),
        "src/pages/Overview.tsx": b"export default function Overview(){ return <main>overview</main> }",
        "src/pages/Sessions.tsx": b"export default function Sessions(){ return <main>sessions</main> }",
        "src/review/ReviewReader.tsx": b"export default function ReviewReader(){ return <main>review</main> }",
    }


def test_vite_react_router_global_change_expands_to_all_static_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _vite_files()
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: files)

    preview = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/index.css")

    assert preview is not None
    assert preview.routes == ("/", "/overview", "/sessions", "/review")
    assert "/sessions/:id" not in preview.routes


def test_vite_react_router_route_component_only_affects_its_static_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _vite_files()
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: files)

    overview = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/pages/Overview.tsx")
    review = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/review/ReviewReader.tsx")

    assert overview is not None and overview.routes == ("/", "/overview")
    assert review is not None and review.routes == ("/review",)


def test_tree_walk_skips_submodule_gitlinks_without_aborting_parent_preview() -> None:
    blob = SimpleNamespace(type_str="blob", data=b"export default 1")
    tree = [
        SimpleNamespace(name="src", id="source", filemode=0o100644),
        SimpleNamespace(name="landing", id="submodule-commit", filemode=0o160000),
    ]
    repo = {"source": blob}

    assert list(web_preview._walk_tree(repo, tree)) == [("src", blob, 0o100644)]


def test_next_ui_component_style_and_asset_inherit_their_rendered_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _next_files()
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: files)

    component = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/components/Hero.tsx")
    style = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/components/hero.css")
    asset = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "public/hero.png")

    assert component is not None
    assert component.framework == "next"
    assert component.routes == ("/", "/scan")
    assert component.default_route == "/"
    assert style is not None and style.routes == ("/", "/scan")
    # The same asset is referenced from both the component style and the global
    # layout style, so it legitimately affects every route rooted in the layout.
    assert asset is not None
    assert "/" in asset.routes
    assert "/scan" in asset.routes


def test_next_layout_style_is_a_web_surface_change_for_every_static_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _next_files()
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: files)

    preview = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/app/globals.css")

    assert preview is not None
    assert preview.routes == ("/", "/scan")
    assert preview.default_route == "/"


def test_dynamic_routes_are_not_invented_without_concrete_preview_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _next_files() | {
        "src/app/products/[slug]/page.tsx": b'import Hero from "@/components/Hero"; export default function Page(){ return <Hero/> }',
    }
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: files)

    preview = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/components/Hero.tsx")

    assert preview is not None
    assert all("[" not in route for route in preview.routes)
    assert "/products/[slug]" not in preview.routes


def test_mutable_review_falls_back_to_source_instead_of_rendering_today_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: _next_files())

    assert (
        web_preview.infer_web_preview_descriptor(
            tmp_path,
            _revision(mode="working_tree"),
            "src/components/Hero.tsx",
        )
        is None
    )


def test_package_manager_ignores_user_local_bun_when_system_npm_is_sandbox_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bun.lock").write_text("lock")
    (tmp_path / "package-lock.json").write_text("{}")
    monkeypatch.setattr(
        web_preview.shutil,
        "which",
        lambda name: {"bun": "/home/user/.bun/bin/bun", "npm": "/usr/bin/npm"}.get(name),
    )

    assert web_preview._package_manager(tmp_path) == "npm"


def test_bun_workspace_preview_mounts_repository_package_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    project = repo / "frontend"
    project_modules = project / "node_modules"
    root_modules = repo / "node_modules"
    (project_modules / ".bin").mkdir(parents=True)
    (root_modules / ".bun").mkdir(parents=True)
    (workspace / "frontend").mkdir(parents=True)
    (project_modules / "next").symlink_to("../../node_modules/.bun/next/node_modules/next")
    monkeypatch.setattr(web_preview.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

    command = web_preview._bubblewrap_prefix(
        workspace,
        project_modules,
        repo_root=repo,
        project_root="frontend",
    )

    bind = ["--ro-bind", str(root_modules), "/node_modules"]
    assert any(command[index : index + 3] == bind for index in range(len(command) - 2))
    facade_next = workspace / "frontend" / "node_modules" / "next"
    assert facade_next.is_symlink()
    assert facade_next.readlink().as_posix() == "/lemoncrow-deps/node_modules/next"


def test_same_revision_side_build_is_serialized_across_concurrent_preview_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    peak = 0
    guard = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    output = tmp_path / "out"
    output.mkdir()

    def fake_unlocked(*_args: object, **_kwargs: object) -> Path:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            entered.set()
        release.wait(timeout=2)
        with guard:
            active -= 1
        return output

    monkeypatch.setattr(web_preview, "_ensure_built_unlocked", fake_unlocked)
    cache_root = tmp_path / "cache"
    revision = _revision()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            web_preview._ensure_built,
            cache_root,
            tmp_path,
            revision,
            store_root=tmp_path,
            side="new",
            framework="next",
        )
        assert entered.wait(timeout=1)
        second = pool.submit(
            web_preview._ensure_built,
            cache_root,
            tmp_path,
            revision,
            store_root=tmp_path,
            side="new",
            framework="next",
        )
        release.set()
        assert first.result(timeout=2) == output
        assert second.result(timeout=2) == output

    assert peak == 1


def test_live_preview_server_requires_capability_then_serves_root_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    (output / "_next").mkdir(parents=True)
    (output / "index.html").write_text('<script src="/_next/app.js"></script><h1>Preview</h1>')
    (output / "_next/app.js").write_text("window.previewLoaded = true;")
    descriptor = web_preview.WebPreviewDescriptor(framework="next", routes=("/",), default_route="/")
    monkeypatch.setattr(web_preview, "infer_web_preview_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(web_preview, "_ensure_built", lambda *_args, **_kwargs: output)

    url = web_preview.ensure_live_web_preview(
        tmp_path,
        tmp_path,
        _revision(),
        path="src/components/Hero.tsx",
        route="/",
        side="new",
    )
    origin = url.split("/", 3)[:3]
    asset_url = "/".join(origin) + "/_next/app.js"

    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(asset_url, timeout=2)
    assert denied.value.code == 404

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(url, timeout=2) as response:
        html = response.read()
        assert b"Preview" in html
        assert b"data-lemoncrow-preview-theme" in html
        assert b"lc-theme" in html
        assert html.index(b"data-lemoncrow-preview-theme") < html.index(b"/_next/app.js")
        assert b"lc-review-preview-scroll" in html
        assert b"lc-review-preview-scroll-to" in html
        assert response.headers["Content-Security-Policy"]
    with opener.open(asset_url, timeout=2) as response:
        assert b"previewLoaded" in response.read()


def test_vite_live_preview_serves_browser_router_route_through_spa_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "dist"
    output.mkdir()
    (output / "index.html").write_text("<html><head></head><body>Vite route</body></html>")
    descriptor = web_preview.WebPreviewDescriptor(framework="vite", routes=("/review",), default_route="/review")
    monkeypatch.setattr(web_preview, "infer_web_preview_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(web_preview, "_ensure_built", lambda *_args, **_kwargs: output)

    url = web_preview.ensure_live_web_preview(
        tmp_path, tmp_path, _revision(), path="src/review/ReviewReader.tsx", route="/review", side="new"
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(url, timeout=2) as response:
        assert b"Vite route" in response.read()


def test_node_modules_facade_keeps_dependencies_read_only_and_build_cache_writable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "frontend"
    project.mkdir(parents=True)
    live_modules = tmp_path / "checkout" / "frontend" / "node_modules"
    (live_modules / "vite").mkdir(parents=True)
    (live_modules / ".bin").mkdir()
    (live_modules / ".vite").mkdir()

    web_preview._prepare_node_modules_facade(workspace, live_modules, project_root="frontend")

    facade = project / "node_modules"
    assert (facade / "vite").is_symlink()
    assert (facade / "vite").readlink().as_posix() == "/lemoncrow-deps/node_modules/vite"
    assert (facade / ".bin").is_symlink()
    assert not (facade / ".vite").exists()

    # The facade itself belongs to the isolated build workspace, so Vite/Astro
    # can create caches there even though all borrowed dependencies resolve
    # through the read-only /lemoncrow-deps mount.
    (facade / ".vite").mkdir()
    (facade / ".astro").mkdir()
    assert (facade / ".vite").is_dir()
    assert (facade / ".astro").is_dir()


def test_preview_build_can_borrow_dependencies_from_a_separate_proven_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "cache"
    frozen_root = tmp_path / "frozen"
    dependency_root = tmp_path / "checkout"
    dependency_project = dependency_root / "frontend"
    dependency_modules = dependency_project / "node_modules"
    (dependency_modules / "vite").mkdir(parents=True)
    package = '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"6"}}\n'
    dependency_project.mkdir(parents=True, exist_ok=True)
    (dependency_project / "package.json").write_text(package, encoding="utf-8")

    def fake_materialize(
        _store_root: Path,
        _repo_root: Path,
        _revision: object,
        *,
        side: str,
        target: Path,
        scope: str,
        store: object = None,
    ) -> str:
        del side, scope, store
        project = target / "frontend"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(package, encoding="utf-8")
        return "frozen-identity"

    observed: dict[str, Path] = {}

    def fake_prefix(
        workspace: Path,
        live_node_modules: Path,
        *,
        repo_root: Path | None = None,
        project_root: str = ".",
        **_kwargs: object,
    ) -> list[str]:
        observed["modules"] = live_node_modules
        observed["repo"] = Path(repo_root or "")
        return ["bwrap", "--chdir", f"/workspace/{project_root}"]

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        output = cache_root / "new" / "workspace" / "frontend" / "dist"
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text("<html></html>", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(web_preview, "_materialize_review_side", fake_materialize)
    monkeypatch.setattr(web_preview, "_package_manager", lambda _root: "npm")
    monkeypatch.setattr(web_preview, "_bubblewrap_prefix", fake_prefix)
    monkeypatch.setattr(web_preview.subprocess, "run", fake_run)

    output = web_preview._ensure_built_unlocked(
        cache_root,
        frozen_root,
        _revision(),
        store_root=tmp_path / "store",
        side="new",
        framework="vite",
        project_root="frontend",
        dependency_root=dependency_root,
    )

    assert output.name == "dist"
    assert observed["modules"] == dependency_modules
    assert output.name == "dist"
    assert observed["modules"] == dependency_modules
    assert observed["repo"] == dependency_root


def test_preview_build_repairs_missing_checkout_dependencies_from_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "cache"
    frozen_root = tmp_path / "frozen"
    dependency_root = tmp_path / "checkout"
    dependency_project = dependency_root / "frontend"
    dependency_project.mkdir(parents=True)
    package = '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"6"}}\n'
    lock = '{"name":"preview","lockfileVersion":3,"packages":{}}\n'
    (dependency_project / "package.json").write_text(package, encoding="utf-8")
    (dependency_project / "package-lock.json").write_text(lock, encoding="utf-8")

    def fake_materialize(
        _store_root: Path,
        _repo_root: Path,
        _revision: object,
        *,
        side: str,
        target: Path,
        scope: str,
        store: object = None,
    ) -> str:
        del side, scope, store
        project = target / "frontend"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(package, encoding="utf-8")
        (project / "package-lock.json").write_text(lock, encoding="utf-8")
        return "frozen-identity"

    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[:2] == ["npm", "ci"]:
            (dependency_project / "node_modules" / "vite").mkdir(parents=True)
            return SimpleNamespace(returncode=0, stdout="installed")
        output = cache_root / "new" / "workspace" / "frontend" / "dist"
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text("<html></html>", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    def fake_prefix(
        _workspace: Path,
        live_node_modules: Path,
        **_kwargs: object,
    ) -> list[str]:
        assert live_node_modules == dependency_project / "node_modules"
        return ["bwrap", "--chdir", "/workspace/frontend"]

    monkeypatch.setattr(web_preview, "_materialize_review_side", fake_materialize)
    monkeypatch.setattr(web_preview, "_package_manager", lambda _root: "npm")
    monkeypatch.setattr(web_preview, "_bubblewrap_prefix", fake_prefix)
    monkeypatch.setattr(web_preview.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(web_preview.subprocess, "run", fake_run)

    output = web_preview._ensure_built_unlocked(
        cache_root,
        frozen_root,
        _revision(),
        store_root=tmp_path / "store",
        side="new",
        framework="vite",
        project_root="frontend",
        dependency_root=dependency_root,
    )

    assert output.name == "dist"
    assert commands[0][:2] == ["npm", "ci"]
    assert "--ignore-scripts" in commands[0]


def test_nested_web_project_maps_repo_relative_change_to_project_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {f"frontend/{path}": payload for path, payload in _next_files().items()}
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: files)

    preview = web_preview.infer_web_preview_descriptor(
        tmp_path,
        _revision(),
        "frontend/src/components/Hero.tsx",
    )

    assert preview is not None
    assert preview.framework == "next"
    assert preview.root == "frontend"
    assert preview.routes == ("/", "/scan")


def test_astro_component_maps_to_each_static_page_that_imports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {
        "package.json": json.dumps({"dependencies": {"astro": "5"}}).encode(),
        "src/pages/index.astro": b'---\nimport Card from "../components/Card.astro";\n---\n<Card />',
        "src/pages/about.astro": b'---\nimport Card from "../components/Card.astro";\n---\n<Card />',
        "src/components/Card.astro": b"<article>Card</article>",
    }
    monkeypatch.setattr(web_preview, "_tree_files", lambda *_args, **_kwargs: files)

    preview = web_preview.infer_web_preview_descriptor(tmp_path, _revision(), "src/components/Card.astro")

    assert preview is not None
    assert preview.framework == "astro"
    assert preview.routes == ("/about", "/")
    assert preview.default_route == "/"


class _MemoryArtifactBackend:
    """Artifact bytes held outside ``store.root``, as hosted Review keeps them."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def write(self, key: str, payload: bytes) -> str:
        self.blobs[key] = payload
        return hashlib.sha256(payload).hexdigest()

    def read(self, key: str) -> bytes | None:
        return self.blobs.get(key)

    def delete_prefix(self, prefix: str) -> None:
        for key in [key for key in self.blobs if key.startswith(prefix)]:
            del self.blobs[key]


def test_artifact_backed_preview_cache_is_scoped_by_store_root(tmp_path: Path) -> None:
    first = web_preview._ArtifactStore(SimpleNamespace())
    second = web_preview._ArtifactStore(SimpleNamespace())
    # Store identity is intentionally excluded so per-request hosted stores do
    # not fragment immutable revision caches. The surrounding store_root remains
    # part of every cache key and is the org/repo namespace in hosted Review.
    assert first == second
    assert ("repo", "store-a", "revision", first) != ("repo", "store-b", "revision", second)


def test_working_tree_preview_reads_artifacts_from_the_stores_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted revisions keep packet/blob artifacts in the backend, not on disk.

    A plain ``ReviewStore(store_root)`` reads none of them, which used to make
    every frontend file report "no revision-pinned static route preview".
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    root = tmp_path / "repo"
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    files = {
        "frontend/package.json": json.dumps({"devDependencies": {"vite": "7"}}),
        "frontend/src/main.js": 'import "./style.css";\nconsole.log("v1");\n',
        "frontend/src/style.css": "body { margin: 0 }\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.test", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "baseline", tree, [])
    (root / "frontend/src/main.js").write_text('import "./style.css";\nconsole.log("v2");\n', encoding="utf-8")

    store = ReviewStore(tmp_path / "store", artifact_backend=_MemoryArtifactBackend())
    rng = resolve_rev_range(root)
    session = open_or_create_session(store, root, rng)
    build = build_review_packet_with_blobs(
        root,
        rng,
        store_root=store.root,
        with_impact=False,
        with_provenance=False,
        with_patch_text=True,
        unbounded_patch_text=True,
    )
    revision = record_revision(store, session, root, rng, store_root=store.root, build=build).revision
    assert not (store.root / "review" / "artifacts").exists()

    plain = web_preview.infer_web_preview_descriptor(root, revision, "frontend/src/main.js", store_root=store.root)
    assert plain is None

    preview = web_preview.infer_web_preview_descriptor(
        root, revision, "frontend/src/main.js", store_root=store.root, store=store
    )
    assert preview is not None
    assert (preview.framework, preview.root, preview.routes) == ("vite", "frontend", ("/",))
