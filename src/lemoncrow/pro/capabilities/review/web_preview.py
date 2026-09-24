"""Revision-pinned visual previews for frontend review files.

Review should answer *what did this UI actually look like?* without leaving the
same immutable revision boundary as the source diff. The exact base/head tree is
materialized and built in a network-isolated bubblewrap sandbox; dependencies
are reused from the proven checkout or provisioned from its lockfile when the
reviewed dependency graph matches exactly.

Normal Preview/Compare uses a live, read-only static render on an isolated
loopback origin, embedded by Review in a sandboxed iframe. Frozen Chrome
screenshots remain available as a secondary primitive for visual-diff/evidence
work, not as the primary preview surface.

Supported project shapes are intentionally explicit:

* Next.js app router with a static export (``out/``)
* Astro static builds (``dist/``)
* Vite static builds (``dist/``)

Route inference follows local/``@/`` imports, styles and UI assets from static
pages/layouts to the changed file. Dynamic route templates are skipped until
Review has concrete parameters/fixtures rather than inventing them.
"""

from __future__ import annotations

import hashlib
import http.cookies
import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from lemoncrow.core.foundation.run_file_io import RunFileLock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision
    from lemoncrow.pro.capabilities.review.store import ReviewStore

_CODE_SUFFIXES = (".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs", ".astro", ".mdx")
_STYLE_SUFFIXES = (".css", ".scss", ".sass", ".less", ".styl", ".pcss")
_ASSET_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".ico", ".woff", ".woff2", ".ttf")
_DATA_SUFFIXES = (".json",)
_WEB_SURFACE_SUFFIXES = _CODE_SUFFIXES + _STYLE_SUFFIXES + _ASSET_SUFFIXES + _DATA_SUFFIXES
_GRAPH_SUFFIXES = _WEB_SURFACE_SUFFIXES
_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
_MAX_BUILD_SECONDS = 180
_MAX_INSTALL_SECONDS = 300
_MAX_CAPTURE_SECONDS = 45
_CAPTURE_WIDTH = 1440
_CAPTURE_HEIGHT = 5000
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _operation_lock(key: str) -> threading.Lock:
    """Return one lock for an immutable preview build/capture artifact.

    The review API renders previews in a thread pool. Multiple changed UI files
    can therefore request the same revision build concurrently; serializing by
    artifact keeps one request from deleting ``.next`` while another Next build
    is still writing it.
    """

    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


_IMPORT_RE = re.compile(
    r"(?:import|export)\s+(?:[^\"']*?\sfrom\s*)?[\"']([^\"']+)[\"']" r"|require\(\s*[\"']([^\"']+)[\"']\s*\)",
    flags=re.MULTILINE,
)
_DYNAMIC_IMPORT_RE = re.compile(r"\bimport\(\s*[\"']([^\"']+)[\"']\s*\)")
_STATIC_IMPORT_BINDING_RE = re.compile(
    r"\bimport\s+(?!type\s+)(?P<clause>[^;\n]+?)\s+from\s+[\"'](?P<specifier>[^\"']+)[\"']",
    flags=re.MULTILINE,
)
_DYNAMIC_IMPORT_BINDING_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*[^;]{0,800}?"
    r"\bimport\(\s*[\"'](?P<specifier>[^\"']+)[\"']\s*\)",
    flags=re.DOTALL,
)
_REACT_ROUTE_START_RE = re.compile(r"<Route\b")
_REACT_ROUTE_PATH_RE = re.compile(r"\bpath\s*=\s*[\"']([^\"']+)[\"']")
_JSX_COMPONENT_RE = re.compile(r"\b([A-Z][A-Za-z0-9_$]*)\b")
_STYLE_REFERENCE_RE = re.compile(
    r"@(?:import|use|forward)\s+(?:url\()?\s*[\"']([^\"']+)[\"']" r"|url\(\s*[\"']?([^\"')]+)[\"']?\s*\)",
    flags=re.IGNORECASE,
)
_PUBLIC_REFERENCE_RE = re.compile(
    r"[\"'](/[^\"'?#]+\.(?:svg|png|jpe?g|webp|gif|avif|ico|woff2?|ttf))(?:[?#][^\"']*)?[\"']",
    flags=re.IGNORECASE,
)


class WebPreviewUnavailable(RuntimeError):
    """The reviewed revision cannot be rendered exactly and safely."""


@dataclass(frozen=True)
class WebPreviewDescriptor:
    framework: str
    routes: tuple[str, ...]
    default_route: str
    root: str = "."

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "web",
            "framework": self.framework,
            "routes": list(self.routes),
            "default_route": self.default_route,
            "root": self.root,
        }


@dataclass
class _LivePreviewServer:
    output: Path
    secret: str
    cookie_name: str
    server: http.server.ThreadingHTTPServer
    thread: threading.Thread

    @property
    def origin(self) -> str:
        host_raw, port = self.server.server_address[:2]
        host = host_raw.decode() if isinstance(host_raw, bytes) else str(host_raw)
        return f"http://{host}:{port}"


_LIVE_SERVER_GUARD = threading.Lock()
_LIVE_SERVERS: dict[str, _LivePreviewServer] = {}


def _preview_csp() -> str:
    return (
        "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; media-src 'self' data: blob:; connect-src 'none'; "
        "frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'none'; "
        "frame-ancestors http://127.0.0.1:* http://localhost:*"
    )


def _inject_scroll_bridge(payload: bytes) -> bytes:
    """Inject preview theming plus the parent/iframe Compare protocol.

    Theme initialization is placed immediately after ``<head>`` so common
    class/data-attribute theme systems see it before application scripts run.
    Compare scrolling stays cross-origin-safe via normalized postMessage data.
    """

    theme_script = b"""<script data-lemoncrow-preview-theme>(()=>{\nconst p=new URLSearchParams(location.search).get('lc-theme');\nif(p!=='light'&&p!=='dark')return;\nconst apply=()=>{const r=document.documentElement;r.classList.remove('light','dark');r.classList.add(p);r.dataset.theme=p;r.dataset.bsTheme=p;r.style.colorScheme=p;};\napply();addEventListener('DOMContentLoaded',apply,{once:true});\n})();</script>"""
    scroll_script = b"""<script data-lemoncrow-preview-bridge>(()=>{\nlet ignoreUntil=0,queued=false;\nconst progress=()=>{const d=document.documentElement;const max=Math.max(1,d.scrollHeight-innerHeight);return Math.max(0,Math.min(1,scrollY/max));};\nconst publish=()=>{queued=false;if(performance.now()<ignoreUntil)return;parent.postMessage({type:'lc-review-preview-scroll',ratio:progress()},'*');};\naddEventListener('scroll',()=>{if(!queued){queued=true;requestAnimationFrame(publish);}},{passive:true});\naddEventListener('message',(event)=>{if(event.source!==parent)return;const m=event.data;if(!m||m.type!=='lc-review-preview-scroll-to'||typeof m.ratio!=='number')return;ignoreUntil=performance.now()+120;const d=document.documentElement;const max=Math.max(0,d.scrollHeight-innerHeight);scrollTo({top:Math.max(0,Math.min(1,m.ratio))*max,left:0,behavior:'auto'});});\n})();</script>"""

    lower = payload.lower()
    head = lower.find(b"<head")
    if head >= 0:
        head_end = lower.find(b">", head)
        if head_end >= 0:
            payload = payload[: head_end + 1] + theme_script + payload[head_end + 1 :]
    else:
        payload = theme_script + payload

    lower = payload.lower()
    for marker in (b"</body>", b"</html>"):
        index = lower.rfind(marker)
        if index >= 0:
            return payload[:index] + scroll_script + payload[index:]
    return payload + scroll_script


def _resolve_live_file(output: Path, request_path: str, *, spa_fallback: bool = False) -> Path | None:
    raw = unquote(request_path).replace("\\", "/")
    relative = raw.lstrip("/")
    parts = PurePosixPath(relative).parts
    if any(part in {"", ".", ".."} for part in parts):
        if relative not in {"", "."}:
            return None
    root = output.resolve()
    candidate = root.joinpath(*parts).resolve() if parts else root
    if candidate != root and root not in candidate.parents:
        return None
    possibilities: list[Path] = []
    if candidate.is_dir():
        possibilities.append(candidate / "index.html")
    else:
        possibilities.append(candidate)
        if not candidate.suffix:
            possibilities.extend((candidate.with_suffix(".html"), candidate / "index.html"))
    resolved = next((item for item in possibilities if item.is_file()), None)
    if resolved is not None:
        return resolved
    if spa_fallback and not PurePosixPath(relative).suffix:
        index = root / "index.html"
        if index.is_file():
            return index
    return None


def _live_handler(
    output: Path, secret: str, cookie_name: str, *, spa_fallback: bool = False
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "LemonCrowPreview/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _authorized_path(self) -> tuple[str, bool] | None:
            parsed = urlsplit(self.path)
            request_path = parsed.path or "/"
            prefix = f"/{secret}"
            fresh = request_path == prefix or request_path.startswith(prefix + "/")
            if fresh:
                stripped = request_path[len(prefix) :] or "/"
                return stripped, True
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except http.cookies.CookieError:
                return None
            morsel = jar.get(cookie_name)
            if morsel is None or not secrets.compare_digest(morsel.value, secret):
                return None
            return request_path, False

        def _serve(self, *, body: bool) -> None:
            authorized = self._authorized_path()
            if authorized is None:
                self.send_error(404)
                return
            request_path, fresh = authorized
            target = _resolve_live_file(output, request_path, spa_fallback=spa_fallback)
            if target is None:
                self.send_error(404)
                return
            payload = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if mime.startswith("text/html"):
                payload = _inject_scroll_bridge(payload)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if mime.startswith("text/html"):
                self.send_header("Content-Security-Policy", _preview_csp())
            if fresh:
                self.send_header(
                    "Set-Cookie",
                    f"{cookie_name}={secret}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def do_GET(self) -> None:
            self._serve(body=True)

        def do_HEAD(self) -> None:
            self._serve(body=False)

    return Handler


def _ensure_live_server(output: Path, *, spa_fallback: bool = False) -> _LivePreviewServer:
    key = f"{output.resolve()}::spa={int(spa_fallback)}"
    with _LIVE_SERVER_GUARD:
        current = _LIVE_SERVERS.get(key)
        if current is not None and current.thread.is_alive():
            return current
        secret = secrets.token_urlsafe(24)
        cookie_name = "lc_preview_" + hashlib.sha256(key.encode()).hexdigest()[:12]
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _live_handler(output, secret, cookie_name, spa_fallback=spa_fallback),
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="lc-web-preview", daemon=True)
        thread.start()
        live = _LivePreviewServer(output=output, secret=secret, cookie_name=cookie_name, server=server, thread=thread)
        _LIVE_SERVERS[key] = live
        return live


def is_web_preview_path(path: str) -> bool:
    """True when *path* can contribute to a rendered frontend surface."""

    return PurePosixPath(path).suffix.lower() in _WEB_SURFACE_SUFFIXES


def _walk_tree(repo: Any, tree: Any, prefix: str = "") -> Iterable[tuple[str, Any, int]]:
    for entry in tree:
        path = f"{prefix}/{entry.name}" if prefix else entry.name
        mode = int(getattr(entry, "filemode", 0) or 0)
        # Gitlinks point at commits owned by a separate submodule repository.
        # Dereferencing that OID through the parent repo raises KeyError and
        # used to abort web preview inference for every otherwise unrelated
        # frontend file in a repository that happened to contain a submodule.
        if mode == 0o160000:
            continue
        try:
            obj = repo[entry.id]
        except KeyError:
            # Be defensive around incomplete/dangling trees too: a missing
            # object cannot contribute source to a revision-pinned preview.
            continue
        if getattr(obj, "type_str", "") == "tree":
            yield from _walk_tree(repo, obj, path)
        elif getattr(obj, "type_str", "") == "blob":
            yield path, obj, mode


def _tree_files(repo_root: Path, sha: str, *, source_only: bool) -> dict[str, bytes]:
    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha

    repo = _open_repo(repo_root)
    tree = _tree_for_sha(repo, sha)
    out: dict[str, bytes] = {}
    for path, blob, _mode in _walk_tree(repo, tree):
        if source_only:
            suffix = PurePosixPath(path).suffix.lower()
            if suffix not in _GRAPH_SUFFIXES and PurePosixPath(path).name != "package.json":
                continue
        try:
            out[path] = bytes(blob.data)
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _decode_sources(files: Mapping[str, bytes]) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, payload in files.items():
        if PurePosixPath(path).suffix.lower() not in _GRAPH_SUFFIXES and PurePosixPath(path).name != "package.json":
            continue
        try:
            out[path] = payload.decode("utf-8")
        except UnicodeDecodeError:
            # Binary UI assets still participate as terminal graph nodes so an
            # imported logo/font/image can inherit the consuming route.
            out[path] = ""
    return out


def _framework(files: Mapping[str, str]) -> str:
    raw = files.get("package.json", "")
    try:
        package = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        package = {}
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            deps.update(value)
    if "next" in deps:
        return "next"
    if "astro" in deps:
        return "astro"
    if "vite" in deps:
        return "vite"
    return ""


def _framework_roots(files: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Return ``(project_root, framework)`` for every supported web project."""

    roots: list[tuple[str, str]] = []
    for path in sorted(files):
        if PurePosixPath(path).name != "package.json":
            continue
        root_path = PurePosixPath(path).parent
        root = "." if root_path.as_posix() in {"", "."} else root_path.as_posix()
        scoped = _scope_project_sources(files, root)
        framework = _framework(scoped)
        if framework:
            roots.append((root, framework))
    return tuple(roots)


def _scope_project_sources(files: Mapping[str, str], root: str) -> dict[str, str]:
    if root in {"", "."}:
        return dict(files)
    prefix = root.rstrip("/") + "/"
    return {path[len(prefix) :]: text for path, text in files.items() if path.startswith(prefix)}


def _project_target(path: str, root: str) -> str | None:
    if root in {"", "."}:
        return path
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def _configured_web_roots(repo_root: Path) -> tuple[str, ...]:
    try:
        from lemoncrow.pro.capabilities.review.surfaces import load_review_surface_config

        config = load_review_surface_config(repo_root)
    except (OSError, ValueError):
        return ()
    roots: list[str] = []
    for entry in config.for_provider("web"):
        root = entry.string("root", ".") or "."
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _candidate_import_paths(importer: str, specifier: str) -> tuple[str, ...]:
    specifier = specifier.strip().split("?", 1)[0].split("#", 1)[0]
    if not specifier or specifier.startswith(("http:", "https:", "data:")):
        return ()
    if specifier.startswith("@/"):
        base = PurePosixPath("src") / specifier[2:]
    elif specifier.startswith("~/"):
        base = PurePosixPath("src") / specifier[2:]
    elif specifier.startswith("/"):
        base = PurePosixPath("public") / specifier.lstrip("/")
    elif specifier.startswith("."):
        base = PurePosixPath(importer).parent / specifier
    elif specifier.startswith("src/"):
        base = PurePosixPath(specifier)
    else:
        return ()
    normalized = PurePosixPath(os.path.normpath(str(base)).replace("\\", "/"))
    raw = normalized.as_posix()
    candidates = [raw]
    if normalized.suffix:
        return tuple(candidates)
    candidates.extend(f"{raw}{suffix}" for suffix in _GRAPH_SUFFIXES)
    candidates.extend(f"{raw}/index{suffix}" for suffix in _GRAPH_SUFFIXES)
    return tuple(candidates)


def _import_graph(files: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    present = set(files)
    graph: dict[str, tuple[str, ...]] = {}
    for path, text in files.items():
        if PurePosixPath(path).suffix.lower() not in _GRAPH_SUFFIXES:
            continue
        specifiers = [(match.group(1) or match.group(2) or "") for match in _IMPORT_RE.finditer(text)]
        specifiers.extend(match.group(1) for match in _DYNAMIC_IMPORT_RE.finditer(text))
        if PurePosixPath(path).suffix.lower() in _STYLE_SUFFIXES:
            specifiers.extend((match.group(1) or match.group(2) or "") for match in _STYLE_REFERENCE_RE.finditer(text))
        specifiers.extend(match.group(1) for match in _PUBLIC_REFERENCE_RE.finditer(text))
        resolved: list[str] = []
        for specifier in specifiers:
            target = next(
                (candidate for candidate in _candidate_import_paths(path, specifier) if candidate in present), ""
            )
            if target and target not in resolved:
                resolved.append(target)
        graph[path] = tuple(resolved)
    return graph


def _reachable(graph: Mapping[str, tuple[str, ...]], roots: Iterable[str], target: str) -> bool:
    pending = list(roots)
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return False


def _reachable_without(
    graph: Mapping[str, tuple[str, ...]],
    roots: Iterable[str],
    target: str,
    blocked: set[str],
) -> bool:
    """Reach *target* without entering any route-specific component root."""

    pending = list(roots)
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in blocked:
            continue
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return False


def _resolve_local_import(importer: str, specifier: str, files: Mapping[str, str]) -> str:
    present = set(files)
    return next((candidate for candidate in _candidate_import_paths(importer, specifier) if candidate in present), "")


def _import_bindings(importer: str, text: str, files: Mapping[str, str]) -> dict[str, str]:
    """Local JSX binding -> source path, including ``lazy(() => import(...))``."""

    bindings: dict[str, str] = {}
    for match in _STATIC_IMPORT_BINDING_RE.finditer(text):
        resolved = _resolve_local_import(importer, match.group("specifier"), files)
        if not resolved:
            continue
        clause = match.group("clause").strip()
        if clause.startswith("{"):
            names = clause.strip("{} ")
            for raw in names.split(","):
                part = raw.strip()
                if not part:
                    continue
                pieces = re.split(r"\s+as\s+", part)
                bindings[pieces[-1].strip()] = resolved
            continue
        if clause.startswith("*"):
            namespace = re.search(r"\bas\s+([A-Za-z_$][\w$]*)", clause)
            if namespace:
                bindings[namespace.group(1)] = resolved
            continue
        default = clause.split(",", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", default):
            bindings[default] = resolved

    for match in _DYNAMIC_IMPORT_BINDING_RE.finditer(text):
        resolved = _resolve_local_import(importer, match.group("specifier"), files)
        if resolved:
            bindings[match.group("name")] = resolved
    return bindings


def _react_router_routes(files: Mapping[str, str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Static React Router routes and the local component modules they render."""

    routes: list[tuple[str, tuple[str, ...]]] = []
    for importer, text in files.items():
        if "<Route" not in text:
            continue
        starts = list(_REACT_ROUTE_START_RE.finditer(text))
        if not starts:
            continue
        bindings = _import_bindings(importer, text, files)
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else text.find("</Routes>", start.start())
            if end < 0:
                end = len(text)
            segment = text[start.start() : end]
            path_match = _REACT_ROUTE_PATH_RE.search(segment)
            if path_match is None:
                continue
            route = path_match.group(1).strip()
            if not route.startswith("/") or any(token in route for token in (":", "*")):
                continue
            component_roots = tuple(
                dict.fromkeys(bindings[name] for name in _JSX_COMPONENT_RE.findall(segment) if name in bindings)
            )
            routes.append((route, component_roots))
    return tuple(dict.fromkeys(routes))


def _vite_routes_for(
    files: Mapping[str, str],
    target: str,
    graph: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    roots = tuple(
        path
        for path in (
            "src/main.tsx",
            "src/main.jsx",
            "src/main.ts",
            "src/main.js",
            "src/App.tsx",
            "src/App.jsx",
        )
        if path in files
    )
    if not roots:
        return ()

    route_entries = _react_router_routes(files)
    if not route_entries:
        return ("/",) if _reachable(graph, roots, target) else ()

    blocked = {component for _route, components in route_entries for component in components}
    if _reachable_without(graph, roots, target, blocked):
        return tuple(dict.fromkeys(route for route, _components in route_entries))

    return tuple(
        dict.fromkeys(
            route
            for route, component_roots in route_entries
            if component_roots and _reachable(graph, component_roots, target)
        )
    )


def _next_route(page: str) -> str | None:
    parts = list(PurePosixPath(page).parts)
    try:
        app_index = parts.index("app")
    except ValueError:
        return None
    segments = parts[app_index + 1 : -1]
    clean: list[str] = []
    for segment in segments:
        if segment.startswith("(") and segment.endswith(")"):
            continue
        if segment.startswith("@"):
            continue
        if "[" in segment or "]" in segment:
            return None
        clean.append(segment)
    return "/" + "/".join(clean) if clean else "/"


def _next_layouts(files: Mapping[str, str], page: str) -> tuple[str, ...]:
    path = PurePosixPath(page)
    parents: list[PurePosixPath] = []
    current = path.parent
    while True:
        parents.append(current)
        if current.name == "app" or current == current.parent:
            break
        current = current.parent
    roots: list[str] = [page]
    for parent in reversed(parents):
        for suffix in (".tsx", ".jsx", ".ts", ".js", ".mdx"):
            candidate = (parent / f"layout{suffix}").as_posix()
            if candidate in files:
                roots.append(candidate)
                break
    return tuple(roots)


def _next_pages_route(page: str) -> str | None:
    parts = list(PurePosixPath(page).parts)
    try:
        pages_index = parts.index("pages")
    except ValueError:
        return None
    segments = parts[pages_index + 1 :]
    if not segments or segments[0] == "api":
        return None
    leaf = PurePosixPath(segments[-1]).stem
    if leaf.startswith("_"):
        return None
    segments = segments[:-1] + ([] if leaf == "index" else [leaf])
    if any("[" in segment or "]" in segment for segment in segments):
        return None
    return "/" + "/".join(segments) if segments else "/"


def _next_pages_roots(files: Mapping[str, str], page: str) -> tuple[str, ...]:
    path = PurePosixPath(page)
    parts = list(path.parts)
    try:
        pages_index = parts.index("pages")
    except ValueError:
        return (page,)
    pages_root = PurePosixPath(*parts[: pages_index + 1])
    roots = [page]
    for suffix in (".tsx", ".jsx", ".ts", ".js", ".mdx"):
        candidate = (pages_root / f"_app{suffix}").as_posix()
        if candidate in files:
            roots.append(candidate)
            break
    return tuple(roots)


def _astro_route(page: str) -> str | None:
    path = PurePosixPath(page)
    parts = list(path.parts)
    try:
        pages_index = parts.index("pages")
    except ValueError:
        return None
    segments = parts[pages_index + 1 :]
    if not segments:
        return None
    leaf = PurePosixPath(segments[-1]).stem
    segments = segments[:-1] + ([] if leaf == "index" else [leaf])
    if any("[" in segment or "]" in segment for segment in segments):
        return None
    return "/" + "/".join(segments) if segments else "/"


def _routes_for(
    files: Mapping[str, str],
    target: str,
    framework: str,
    graph: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    graph = _import_graph(files) if graph is None else graph
    routes: list[str] = []
    if framework == "next":
        app_pages = sorted(
            path for path in files if re.search(r"(?:^|/)app(?:/.*)?/page\.(?:tsx|jsx|ts|js|mdx)$", path)
        )
        for page in app_pages:
            route = _next_route(page)
            if route is not None and _reachable(graph, _next_layouts(files, page), target):
                routes.append(route)

        pages_router = sorted(path for path in files if re.search(r"(?:^|/)pages/.+\.(?:tsx|jsx|ts|js|mdx)$", path))
        for page in pages_router:
            route = _next_pages_route(page)
            if route is not None and _reachable(graph, _next_pages_roots(files, page), target):
                routes.append(route)
    elif framework == "astro":
        pages = sorted(path for path in files if path.startswith("src/pages/") and path.endswith(".astro"))
        for page in pages:
            route = _astro_route(page)
            if route is not None and _reachable(graph, (page,), target):
                routes.append(route)
    elif framework == "vite":
        routes.extend(_vite_routes_for(files, target, graph))
    return tuple(dict.fromkeys(routes))


@lru_cache(maxsize=24)
def _cached_revision_sources(repo_root: str, sha: str) -> dict[str, str]:
    root_path = Path(repo_root)
    files = _tree_files(root_path, sha, source_only=True)
    from lemoncrow.pro.capabilities.review.snapshot import submodule_files_at_parent_revision

    for root in _configured_web_roots(root_path):
        nested = submodule_files_at_parent_revision(root_path, sha, root)
        if nested is None:
            continue
        prefix = root.rstrip("/") + "/"
        for rel, (payload, _mode) in nested.items():
            suffix = PurePosixPath(rel).suffix.lower()
            if suffix not in _GRAPH_SUFFIXES and PurePosixPath(rel).name != "package.json":
                continue
            files[prefix + rel] = payload
    return _decode_sources(files)


@lru_cache(maxsize=24)
def _cached_revision_graph(
    repo_root: str,
    sha: str,
) -> tuple[str, dict[str, str], dict[str, tuple[str, ...]]]:
    files = _cached_revision_sources(repo_root, sha)
    return _framework(files), files, _import_graph(files)


@dataclass(frozen=True)
class _ArtifactStore:
    """Carry the artifact-backed store through ``lru_cache`` without keying on it.

    Hosted Review keeps packet/blob/source-tree artifacts in the server's artifact
    backend, not under ``store_root``: a plain ``ReviewStore(store_root)`` reads
    none of them. Revisions are immutable, so the cache key stays
    ``(repo_root, store_root, revision_id)``.
    """

    store: ReviewStore | None = field(default=None, compare=False)

    def resolve(self, store_root: str) -> ReviewStore:
        if self.store is not None:
            return self.store
        from lemoncrow.pro.capabilities.review.store import ReviewStore

        return ReviewStore(Path(store_root))


_NO_ARTIFACT_STORE = _ArtifactStore()


def _frozen_revision_overlay(
    store_root: Path,
    revision: ReviewRevision,
    *,
    scope: str = ".",
    store: ReviewStore | None = None,
) -> dict[str, bytes | None] | None:
    from lemoncrow.pro.capabilities.review.snapshot import frozen_revision_overlay

    return frozen_revision_overlay(store_root, revision, scope=scope, store=store)


def _merge_frozen_submodule_sources(
    files: dict[str, bytes],
    repo_root: Path,
    store_root: Path,
    revision: ReviewRevision,
    *,
    side: str,
    store: ReviewStore | None = None,
) -> dict[str, bytes]:
    from lemoncrow.pro.capabilities.review.snapshot import review_submodule_files

    merged = dict(files)
    roots = tuple(dict.fromkeys((*_configured_web_roots(repo_root),)))
    for root in roots:
        nested = review_submodule_files(store_root, repo_root, revision, root, side=side, store=store)
        if nested is None:
            continue
        prefix = root.rstrip("/") + "/"
        for rel, (payload, _mode) in nested.items():
            suffix = PurePosixPath(rel).suffix.lower()
            if suffix not in _GRAPH_SUFFIXES and PurePosixPath(rel).name != "package.json":
                continue
            merged[prefix + rel] = payload
    return merged


def _frozen_source_tree_graph_files(
    store: ReviewStore,
    revision: ReviewRevision,
    *,
    side: str,
) -> dict[str, bytes] | None:
    from lemoncrow.pro.capabilities.review.snapshot import _read_source_tree_artifact

    entries = _read_source_tree_artifact(store, revision.review_id, revision.id, side=side)
    if entries is None:
        return None
    files: dict[str, bytes] = {}
    for rel, raw in entries.items():
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix not in _GRAPH_SUFFIXES and PurePosixPath(rel).name != "package.json":
            continue
        digest = str(raw["content_digest"])
        payload = store.read_source_content(digest)
        if payload is None or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"review source blob is unavailable: {rel}")
        if len(payload) != int(raw["size"]):
            raise ValueError(f"review source blob size changed: {rel}")
        files[rel] = payload
    return files


@lru_cache(maxsize=24)
def _cached_frozen_base_sources(
    repo_root: str,
    store_root: str,
    revision_id: str,
    artifacts: _ArtifactStore = _NO_ARTIFACT_STORE,
) -> dict[str, str]:
    store = artifacts.resolve(store_root)
    revision = store.get_revision(revision_id)
    if revision is None or not revision.base_sha:
        raise ValueError("review revision is unavailable")
    files = _frozen_source_tree_graph_files(store, revision, side="old")
    if files is None:
        files = _tree_files(Path(repo_root), revision.base_sha, source_only=True)
    files = _merge_frozen_submodule_sources(files, Path(repo_root), Path(store_root), revision, side="old", store=store)
    return _decode_sources(files)


@lru_cache(maxsize=24)
def _cached_frozen_revision_sources(
    repo_root: str,
    store_root: str,
    revision_id: str,
    artifacts: _ArtifactStore = _NO_ARTIFACT_STORE,
) -> dict[str, str]:
    store = artifacts.resolve(store_root)
    revision = store.get_revision(revision_id)
    if revision is None or not revision.base_sha:
        raise ValueError("review revision is unavailable")
    frozen_files = _frozen_source_tree_graph_files(store, revision, side="new")
    if frozen_files is not None:
        files = _merge_frozen_submodule_sources(
            frozen_files,
            Path(repo_root),
            Path(store_root),
            revision,
            side="new",
            store=store,
        )
        return _decode_sources(files)

    files = _tree_files(Path(repo_root), revision.base_sha, source_only=True)
    files = _merge_frozen_submodule_sources(files, Path(repo_root), Path(store_root), revision, side="new", store=store)
    decoded_base = _decode_sources(files)
    configured_roots = _configured_web_roots(Path(repo_root))
    roots = configured_roots or tuple(root for root, _framework in _framework_roots(decoded_base))
    if not roots:
        roots = (".",)

    overlay: dict[str, bytes | None] = {}
    from lemoncrow.pro.capabilities.review.snapshot import frozen_submodule_snapshots

    nested_roots = set(frozen_submodule_snapshots(Path(store_root), revision, store=store))
    for root in roots:
        if root in nested_roots:
            continue
        scoped = _frozen_revision_overlay(Path(store_root), revision, scope=root, store=store)
        if scoped is None:
            raise ValueError(f"review revision does not contain a complete executable snapshot for {root}")
        overlay.update(scoped)

    for path, payload in overlay.items():
        suffix = PurePosixPath(path).suffix.lower()
        graph_relevant = suffix in _GRAPH_SUFFIXES or PurePosixPath(path).name == "package.json"
        if payload is None:
            files.pop(path, None)
        elif graph_relevant:
            files[path] = payload
    return _decode_sources(files)


@lru_cache(maxsize=24)
def _cached_frozen_revision_graph(
    repo_root: str,
    store_root: str,
    revision_id: str,
    artifacts: _ArtifactStore = _NO_ARTIFACT_STORE,
) -> tuple[str, dict[str, str], dict[str, tuple[str, ...]]]:
    decoded = _cached_frozen_revision_sources(repo_root, store_root, revision_id, artifacts)
    return _framework(decoded), decoded, _import_graph(decoded)


def infer_web_preview_descriptor(
    repo_root: Path,
    revision: ReviewRevision,
    path: str,
    *,
    store_root: Path | None = None,
    store: ReviewStore | None = None,
) -> WebPreviewDescriptor | None:
    """Infer exact static routes affected by one reviewed frontend file.

    Commit ranges read immutable Git trees. Staged/working-tree revisions use
    the frozen review artifact. Monorepos are handled by finding supported
    package roots (or explicit ``web`` roots in ``.lemoncrow/review.yaml``) and
    running route reachability relative to that project root.
    """

    if not is_web_preview_path(path) or not revision.base_sha:
        return None

    resolved_path = repo_root.expanduser().resolve()
    resolved_repo = str(resolved_path)
    artifacts = _ArtifactStore(store)
    sources_by_side: list[dict[str, str]] = []
    try:
        if revision.range_mode == "commit_range":
            if not revision.head_sha:
                return None
            sources_by_side.append(_cached_revision_sources(resolved_repo, revision.head_sha))
        elif revision.range_mode in {"working_tree", "staged"}:
            if store_root is None:
                return None
            sources_by_side.append(
                _cached_frozen_revision_sources(
                    resolved_repo,
                    str(store_root.expanduser().resolve()),
                    revision.id,
                    artifacts,
                )
            )
        else:
            return None
        if revision.range_mode in {"working_tree", "staged"} and store_root is not None:
            sources_by_side.append(
                _cached_frozen_base_sources(
                    resolved_repo,
                    str(store_root.expanduser().resolve()),
                    revision.id,
                    artifacts,
                )
            )
        else:
            sources_by_side.append(_cached_revision_sources(resolved_repo, revision.base_sha))
    except (OSError, ValueError):
        return None

    configured_roots = _configured_web_roots(resolved_path)
    discovered: list[tuple[str, str]] = []
    for sources in sources_by_side:
        for root, framework in _framework_roots(sources):
            if configured_roots and root not in configured_roots:
                continue
            if (root, framework) not in discovered:
                discovered.append((root, framework))
    if configured_roots:
        # Preserve config order while still taking the framework from the exact
        # revision's package.json rather than trusting a stale declaration.
        discovered.sort(key=lambda item: configured_roots.index(item[0]) if item[0] in configured_roots else 10_000)
    else:
        # A nested app is more specific than a parent workspace when both could
        # technically contain the path.
        discovered.sort(key=lambda item: (-len(PurePosixPath(item[0]).parts), item[0]))

    for root, framework in discovered:
        target = _project_target(path, root)
        if target is None:
            continue
        route_union: list[str] = []
        for sources in sources_by_side:
            scoped = _scope_project_sources(sources, root)
            if _framework(scoped) != framework:
                continue
            graph = _import_graph(scoped)
            for route in _routes_for(scoped, target, framework, graph):
                if route not in route_union:
                    route_union.append(route)
        if not route_union:
            continue
        default_route = (
            "/" if "/" in route_union else min(route_union, key=lambda item: (item.count("/"), len(item), item))
        )
        return WebPreviewDescriptor(
            framework=framework,
            routes=tuple(route_union),
            default_route=default_route,
            root=root,
        )
    return None


def _materialize_revision(repo_root: Path, sha: str, target: Path) -> None:
    from lemoncrow.pro.capabilities.review.snapshot import materialize_git_revision

    materialize_git_revision(repo_root, sha, target)


def _materialize_review_side(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    side: str,
    target: Path,
    scope: str = ".",
    store: ReviewStore | None = None,
) -> str:
    from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side

    try:
        return materialize_review_side(
            store_root, repo_root, revision, side=side, target=target, scope=scope, store=store
        )
    except ReviewSnapshotUnavailable as exc:
        raise WebPreviewUnavailable(str(exc)) from exc


def _dependency_signature(root: Path) -> str:
    package_path = root / "package.json"
    if not package_path.is_file():
        return ""
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return ""
    relevant = {
        key: package.get(key, {})
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "packageManager")
    }
    digest = hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for name in _LOCKFILES:
        path = root / name
        if path.is_file():
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _sandbox_visible_executable(name: str) -> bool:
    resolved = shutil.which(name)
    if not resolved:
        return False
    path = Path(resolved).expanduser().resolve()
    # The build sandbox binds these host trees read-only. A user-local tool
    # such as ~/.bun/bin/bun can be executable on the host yet absent inside
    # bubblewrap, which previously made Review select it and then fail at exec.
    return any(path == root or root in path.parents for root in (Path("/usr"), Path("/bin")))


def _package_manager(root: Path) -> str:
    # Prefer a lockfile whose executable is actually visible inside the sandbox.
    # npm comes first when package-lock.json exists; repositories sometimes keep
    # a Bun lockfile too even though the Review sandbox only exposes system npm.
    if (root / "package-lock.json").is_file() and _sandbox_visible_executable("npm"):
        return "npm"
    if (root / "pnpm-lock.yaml").is_file() and _sandbox_visible_executable("pnpm"):
        return "pnpm"
    if (root / "yarn.lock").is_file() and _sandbox_visible_executable("yarn"):
        return "yarn"
    if ((root / "bun.lock").is_file() or (root / "bun.lockb").is_file()) and _sandbox_visible_executable("bun"):
        return "bun"
    if _sandbox_visible_executable("npm"):
        return "npm"
    raise WebPreviewUnavailable("no supported JavaScript package manager is available inside the preview sandbox")


def _locked_dependency_install(root: Path) -> list[str]:
    """Return an exact, script-free install command for a checkout lockfile."""

    candidates = (
        ("package-lock.json", "npm", ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]),
        ("pnpm-lock.yaml", "pnpm", ["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"]),
        ("yarn.lock", "yarn", ["yarn", "install", "--frozen-lockfile", "--ignore-scripts"]),
        ("bun.lock", "bun", ["bun", "install", "--frozen-lockfile", "--ignore-scripts"]),
        ("bun.lockb", "bun", ["bun", "install", "--frozen-lockfile", "--ignore-scripts"]),
    )
    for lockfile, executable, command in candidates:
        if (root / lockfile).is_file():
            if shutil.which(executable):
                return command
            raise WebPreviewUnavailable(
                f"preview dependencies are missing and {lockfile} requires {executable}, but {executable} is not installed"
            )
    raise WebPreviewUnavailable(
        "preview dependencies are missing and no lockfile is available for an exact automatic install"
    )


def _ensure_checkout_dependencies(dependency_repo: Path, live_project: Path) -> Path:
    """Create missing checkout dependencies deterministically, then reuse them.

    Preview builds never need a manual package install first. When node_modules
    is absent, install from the checkout's lockfile with lifecycle scripts
    disabled. Reviewed project code still executes only inside bubblewrap.
    """

    project_modules = live_project / "node_modules"
    root_modules = dependency_repo / "node_modules"
    if project_modules.is_dir():
        return project_modules
    if root_modules.is_dir():
        return root_modules

    install_root = next(
        (
            root
            for root in (live_project, dependency_repo)
            if (root / "package.json").is_file() and any((root / name).is_file() for name in _LOCKFILES)
        ),
        live_project,
    )
    command = _locked_dependency_install(install_root)
    lock = _operation_lock(f"preview-dependencies:{install_root.resolve()}")
    with lock:
        if project_modules.is_dir():
            return project_modules
        if root_modules.is_dir():
            return root_modules
        try:
            proc = subprocess.run(
                command,
                cwd=install_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=_MAX_INSTALL_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WebPreviewUnavailable(f"preview dependency install could not run: {exc}") from exc
        if proc.returncode != 0:
            raise WebPreviewUnavailable(f"preview dependency install failed:\n{_tail_log(proc.stdout)}")

    if project_modules.is_dir():
        return project_modules
    if root_modules.is_dir():
        return root_modules
    raise WebPreviewUnavailable("preview dependency install completed but produced no node_modules directory")


def _prepare_node_modules_facade(
    workspace: Path,
    live_node_modules: Path,
    *,
    project_root: str = ".",
) -> None:
    """Expose checkout dependencies read-only while keeping project caches writable.

    Binding ``live_node_modules`` directly over ``/workspace/.../node_modules``
    makes the entire directory read-only. Tools such as Vite then fail before
    their build starts because they need to create ``node_modules/.vite`` (and
    other tools use ``.astro``/``.cache``). Mounting a writable child on top of a
    read-only bind only works when that child mountpoint already exists, which a
    fresh install does not guarantee.

    Instead the immutable build workspace owns a tiny writable ``node_modules``
    facade. Each dependency entry is a symlink to the read-only ``/lemoncrow-deps``
    mount inside bubblewrap. Build tools can therefore create their own top-level
    cache directories without being able to mutate the checkout's dependencies.
    """

    project_workspace = workspace if project_root in {"", "."} else workspace / project_root
    facade = project_workspace / "node_modules"
    if facade.is_symlink() or facade.is_file():
        facade.unlink()
    elif facade.exists():
        shutil.rmtree(facade)
    facade.mkdir(parents=True, exist_ok=True)

    for entry in live_node_modules.iterdir():
        # Dependency-manager caches belong to the reviewed build workspace, not
        # to the checkout we are borrowing packages from.
        if entry.name in {".vite", ".astro", ".cache"}:
            continue
        (facade / entry.name).symlink_to(
            f"/lemoncrow-deps/node_modules/{entry.name}", target_is_directory=entry.is_dir()
        )


def _bubblewrap_prefix(
    workspace: Path,
    live_node_modules: Path,
    *,
    repo_root: Path | None = None,
    project_root: str = ".",
    include_chrome: bool = False,
    capture_dir: Path | None = None,
) -> list[str]:
    bwrap = shutil.which("bwrap") or shutil.which("bubblewrap")
    if not bwrap:
        raise WebPreviewUnavailable("visual preview requires bubblewrap; refusing to run project code unsandboxed")
    _prepare_node_modules_facade(workspace, live_node_modules, project_root=project_root)
    command = [
        bwrap,
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/bin",
        "/bin",
        "--ro-bind",
        "/lib",
        "/lib",
    ]
    if Path("/lib64").exists():
        command.extend(("--ro-bind", "/lib64", "/lib64"))
    # Keep localhost/NSS resolution available inside the isolated network
    # namespace. External networking remains unavailable because --unshare-net
    # gives the build its own network namespace.
    for etc_path in ("/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf", "/etc/resolv.conf"):
        if Path(etc_path).is_file():
            command.extend(("--ro-bind", etc_path, etc_path))
    if include_chrome and Path("/opt/google/chrome").is_dir():
        command.extend(("--ro-bind", "/opt/google/chrome", "/opt/google/chrome"))
    if capture_dir is not None:
        capture_dir.mkdir(parents=True, exist_ok=True)
        command.extend(("--bind", str(capture_dir), "/capture"))
    # Bun workspaces commonly keep package payloads in the repository-level
    # ``node_modules/.bun`` store while a nested project's ``node_modules``
    # contains relative symlinks back to that store. We mount nested dependencies
    # at /lemoncrow-deps/node_modules, so those symlinks would otherwise resolve
    # to /node_modules/.bun and become dangling inside the sandbox. Expose the
    # repository-level store read-only at that exact location when it is distinct
    # from the project's dependency tree. This preserves Bun's link topology
    # without making checkout files writable or enabling network installs.
    if repo_root is not None:
        root_modules = repo_root / "node_modules"
        if root_modules.is_dir() and root_modules.resolve() != live_node_modules.resolve():
            command.extend(("--dir", "/node_modules", "--ro-bind", str(root_modules), "/node_modules"))
    root_segment = "" if project_root in {"", "."} else "/" + project_root.strip("/")
    sandbox_project = "/workspace" + root_segment
    command.extend(
        (
            "--bind",
            str(workspace),
            "/workspace",
            "--dir",
            "/lemoncrow-deps",
            "--dir",
            "/lemoncrow-deps/node_modules",
            "--ro-bind",
            str(live_node_modules),
            "/lemoncrow-deps/node_modules",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/home",
            "--clearenv",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--setenv",
            "CI",
            "1",
            "--setenv",
            "NEXT_TELEMETRY_DISABLED",
            "1",
            "--chdir",
            sandbox_project,
        )
    )
    return command


def _next_font_mock(workspace: Path) -> Path:
    target = workspace / ".lemoncrow-next-font-mock.cjs"
    target.write_text(
        "module.exports = new Proxy({}, { get() { return \"@font-face { font-family: 'LemonCrow Preview'; "
        "font-style: normal; font-weight: 100 900; src: url(https://fonts.gstatic.com/lc-preview.woff2) format('woff2'); }\"; } });\n",
        encoding="utf-8",
    )
    return target


def _tail_log(text: str, *, limit: int = 3500) -> str:
    compact = text.strip()
    return compact[-limit:] if len(compact) > limit else compact


def _ensure_built_unlocked(
    cache_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    store_root: Path,
    side: str,
    framework: str,
    project_root: str = ".",
    store: ReviewStore | None = None,
    dependency_root: Path | None = None,
) -> Path:
    workspace = cache_root / side / "workspace"
    marker = cache_root / side / "build.json"
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            output = workspace / str(data.get("output") or "")
            expected_identity = (
                revision.base_sha
                if side == "old"
                else (
                    revision.head_sha
                    if revision.range_mode == "commit_range"
                    else revision.source_fingerprint or revision.tree_fingerprint or revision.id
                )
            )
            if data.get("identity") == expected_identity and output.is_dir():
                return output
        except (OSError, TypeError, ValueError):
            pass

    if workspace.exists():
        shutil.rmtree(workspace)
    identity = _materialize_review_side(
        store_root,
        repo_root,
        revision,
        side=side,
        target=workspace,
        scope=project_root,
        store=store,
    )
    project_workspace = workspace if project_root in {"", "."} else workspace / project_root
    dependency_repo = dependency_root or repo_root
    live_project = dependency_repo if project_root in {"", "."} else dependency_repo / project_root
    if not project_workspace.is_dir() or not (project_workspace / "package.json").is_file():
        raise WebPreviewUnavailable(f"web project root is missing from the reviewed snapshot: {project_root}")
    if not live_project.is_dir() or not (live_project / "package.json").is_file():
        raise WebPreviewUnavailable(f"web project root is missing from the proven checkout: {project_root}")
    if _dependency_signature(project_workspace) != _dependency_signature(live_project):
        raise WebPreviewUnavailable(
            "the reviewed dependency graph differs from the checkout; refusing automatic dependency provisioning"
        )

    # The workspace facade is writable for build caches; the borrowed or freshly
    # provisioned dependency tree is mounted read-only by bubblewrap.
    (project_workspace / "node_modules").mkdir(exist_ok=True)
    project_modules = live_project / "node_modules"
    live_modules = project_modules if project_modules.is_dir() else dependency_repo / "node_modules"
    if not live_modules.is_dir():
        live_modules = _ensure_checkout_dependencies(dependency_repo, live_project)

    package_manager = _package_manager(project_workspace)
    prefix = _bubblewrap_prefix(workspace, live_modules, repo_root=dependency_repo, project_root=project_root)
    root_segment = "" if project_root in {"", "."} else "/" + project_root.strip("/")
    env_args: list[str] = []
    if framework == "next":
        env_args = [
            "--setenv",
            "NEXT_FONT_GOOGLE_MOCKED_RESPONSES",
            f"/workspace{root_segment}/.lemoncrow-next-font-mock.cjs",
        ]
    command = [*prefix[:-2], *env_args, *prefix[-2:], package_manager, "run", "build"]
    try:
        proc = subprocess.run(
            command,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=_MAX_BUILD_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WebPreviewUnavailable(f"preview build could not run: {exc}") from exc
    if proc.returncode != 0:
        raise WebPreviewUnavailable(f"preview build failed:\n{_tail_log(proc.stdout)}")

    candidates = ("out",) if framework == "next" else ("dist",)
    built_output = next((project_workspace / name for name in candidates if (project_workspace / name).is_dir()), None)
    if built_output is None:
        raise WebPreviewUnavailable("preview build completed but produced no supported static output directory")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"identity": identity, "output": built_output.relative_to(workspace).as_posix()}, sort_keys=True),
        encoding="utf-8",
    )
    return built_output


def _ensure_built(
    cache_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    store_root: Path,
    side: str,
    framework: str,
    project_root: str = ".",
    store: ReviewStore | None = None,
    dependency_root: Path | None = None,
) -> Path:
    lock = _operation_lock(f"build:{cache_root}:{side}")
    with lock, RunFileLock(cache_root / side / "build"):
        return _ensure_built_unlocked(
            cache_root,
            repo_root,
            revision,
            store_root=store_root,
            side=side,
            framework=framework,
            project_root=project_root,
            store=store,
            dependency_root=dependency_root,
        )


def _static_route(output: Path, route: str, *, spa_fallback: bool = False) -> str:
    route = "/" + route.strip("/") if route != "/" else "/"
    if route == "/":
        if (output / "index.html").is_file():
            return "/"
        raise WebPreviewUnavailable("the preview build has no root page")
    relative = route.lstrip("/")
    if (output / f"{relative}.html").is_file():
        return f"/{relative}.html"
    if (output / relative / "index.html").is_file():
        return f"/{relative}/"
    if (output / relative).is_file():
        return f"/{relative}"
    if spa_fallback and (output / "index.html").is_file():
        return route
    raise WebPreviewUnavailable(f"the preview build did not emit route {route}")


def ensure_live_web_preview(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    path: str,
    route: str,
    side: str,
    store: ReviewStore | None = None,
    dependency_root: Path | None = None,
) -> str:
    """Return an isolated loopback URL for the exact reviewed static route.

    The project build stays revision-pinned and sandboxed. Only the immutable
    static output is served to the browser, on a separate origin whose first
    navigation carries a random capability and establishes a host-only session
    cookie for its own assets.
    """

    if side not in {"old", "new"}:
        raise WebPreviewUnavailable("preview side must be old or new")
    descriptor = infer_web_preview_descriptor(repo_root, revision, path, store_root=store_root, store=store)
    if descriptor is None or route not in descriptor.routes:
        raise WebPreviewUnavailable("this file has no revision-pinned static route preview")
    project_key = hashlib.sha256(descriptor.root.encode("utf-8")).hexdigest()[:12]
    cache_root = store_root / "review" / "web-previews" / revision.review_id / revision.id / project_key
    output = _ensure_built(
        cache_root,
        repo_root,
        revision,
        store_root=store_root,
        side=side,
        framework=descriptor.framework,
        project_root=descriptor.root,
        store=store,
        dependency_root=dependency_root,
    )
    spa_fallback = descriptor.framework == "vite"
    _static_route(
        output, route, spa_fallback=spa_fallback
    )  # Prove the selected route can be served before exposing a live URL.
    live = _ensure_live_server(output, spa_fallback=spa_fallback)
    normalized = "/" + route.strip("/") if route != "/" else "/"
    return f"{live.origin}/{live.secret}{normalized}"


def _chrome_executable() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    raise WebPreviewUnavailable("visual preview requires Chrome/Chromium")


def _render_web_preview_unlocked(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    path: str,
    route: str,
    side: str,
    width: int = _CAPTURE_WIDTH,
    height: int = _CAPTURE_HEIGHT,
    store: ReviewStore | None = None,
    dependency_root: Path | None = None,
) -> Path:
    """Build and capture one exact reviewed route, returning a cached PNG path."""

    if side not in {"old", "new"}:
        raise WebPreviewUnavailable("preview side must be old or new")
    width = max(320, min(int(width), 2560))
    height = max(480, min(int(height), 8000))
    descriptor = infer_web_preview_descriptor(repo_root, revision, path, store_root=store_root, store=store)
    if descriptor is None or route not in descriptor.routes:
        raise WebPreviewUnavailable("this file has no revision-pinned static route preview")

    digest = hashlib.sha256(f"{route}\0{width}\0{height}".encode()).hexdigest()[:20]
    project_key = hashlib.sha256(descriptor.root.encode("utf-8")).hexdigest()[:12]
    cache_root = store_root / "review" / "web-previews" / revision.review_id / revision.id / project_key
    screenshot = cache_root / side / f"{digest}.png"
    if screenshot.is_file() and screenshot.stat().st_size > 0:
        return screenshot

    output = _ensure_built(
        cache_root,
        repo_root,
        revision,
        store_root=store_root,
        side=side,
        framework=descriptor.framework,
        project_root=descriptor.root,
        store=store,
        dependency_root=dependency_root,
    )
    static_route = _static_route(output, route)
    workspace = cache_root / side / "workspace"
    capture_dir = cache_root / side / "capture-tmp"
    temp_name = f".{digest}.{os.getpid()}.{threading.get_ident()}.png"
    temp_shot = capture_dir / temp_name
    temp_shot.unlink(missing_ok=True)
    dependency_repo = dependency_root or repo_root
    live_project = dependency_repo if descriptor.root in {"", "."} else dependency_repo / descriptor.root
    project_modules = live_project / "node_modules"
    live_modules = project_modules if project_modules.is_dir() else dependency_repo / "node_modules"
    prefix = _bubblewrap_prefix(
        workspace,
        live_modules,
        repo_root=dependency_repo,
        project_root=descriptor.root,
        include_chrome=True,
        capture_dir=capture_dir,
    )
    chrome = _chrome_executable()
    chrome_in_sandbox = "/opt/google/chrome/google-chrome" if chrome.startswith("/opt/google/chrome/") else chrome
    output_relative = output.relative_to(workspace).as_posix()
    script = (
        "python3 -m http.server 8787 --bind 127.0.0.1 --directory "
        f"/workspace/{output_relative} >/tmp/lc-preview-http.log 2>&1 & srv=$!; "
        "trap 'kill $srv 2>/dev/null || true' EXIT; "
        "sleep .15; "
        f"{chrome_in_sandbox} --headless=new --no-sandbox --disable-gpu --disable-background-networking "
        "--no-first-run --no-default-browser-check --run-all-compositor-stages-before-draw "
        "--virtual-time-budget=2500 "
        f"--window-size={width},{height} --screenshot=/capture/{temp_name} "
        f"http://127.0.0.1:8787{static_route}; rc=$?; kill $srv 2>/dev/null || true; exit $rc"
    )
    try:
        proc = subprocess.run(
            [*prefix, "sh", "-lc", script],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=_MAX_CAPTURE_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WebPreviewUnavailable(f"preview capture could not run: {exc}") from exc
    if proc.returncode != 0 or not temp_shot.is_file():
        raise WebPreviewUnavailable(f"preview capture failed:\n{_tail_log(proc.stdout)}")
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    temp_shot.replace(screenshot)
    return screenshot


def render_web_preview(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    path: str,
    route: str,
    side: str,
    width: int = _CAPTURE_WIDTH,
    height: int = _CAPTURE_HEIGHT,
    store: ReviewStore | None = None,
    dependency_root: Path | None = None,
) -> Path:
    """Deduplicate one immutable viewport capture across concurrent UI files."""

    width = max(320, min(int(width), 2560))
    height = max(480, min(int(height), 8000))
    descriptor = infer_web_preview_descriptor(repo_root, revision, path, store_root=store_root, store=store)
    if descriptor is None or route not in descriptor.routes:
        raise WebPreviewUnavailable("this file has no revision-pinned static route preview")
    digest = hashlib.sha256(f"{route}\0{width}\0{height}".encode()).hexdigest()[:20]
    project_key = hashlib.sha256(descriptor.root.encode("utf-8")).hexdigest()[:12]
    cache_root = store_root / "review" / "web-previews" / revision.review_id / revision.id / project_key
    screenshot = cache_root / side / f"{digest}.png"
    key = f"capture:{revision.review_id}:{revision.id}:{descriptor.root}:{side}:{route}:{width}:{height}"
    with _operation_lock(key), RunFileLock(screenshot):
        return _render_web_preview_unlocked(
            store_root,
            repo_root,
            revision,
            path=path,
            route=route,
            side=side,
            width=width,
            height=height,
            store=store,
            dependency_root=dependency_root,
        )


__all__ = [
    "ensure_live_web_preview",
    "infer_web_preview_descriptor",
    "is_web_preview_path",
    "render_web_preview",
]
