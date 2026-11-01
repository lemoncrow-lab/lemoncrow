"""Cross-repository dependency edges and blast radius (P3 of docs/planning/2026-11-01-multi-repo-workspace.md).

:func:`build_graph` scans the members of a workspace root (:mod:`.workspace_members`) and returns edges
``source -> target``: the source repository depends on the target, so a change flows from target to source.
Every edge carries a kind, a confidence and evidence links pairing the provider side (in the target) with
the consumer side (in the source):

============= ================================================ ========================================== ==========
kind          provider (target)                                consumer (source)                          confidence
============= ================================================ ========================================== ==========
``submodule`` the submodule checkout                           the ``.gitmodules`` entry declaring it     high
``path``      the member folder a local path reaches           ``file:../x``, ``path = "../x"``, ``-e``,  high
                                                               Go ``replace => ../x``
``package``   the name in pyproject/setup.cfg/package.json/    the same name among declared dependencies  high
              go.mod/Cargo.toml
``import``    top-level Python packages, Go module paths,      import statements naming them              medium
              npm names
``proto``     ``service`` in a ``.proto`` file                 generated stubs or a copied ``.proto``     medium
``openapi``   an OpenAPI document (title, paths)               a generated client carrying them           medium
``http``      route declarations                               client calls with a matching path literal  medium/low
``event``     ``publish``/``produce``/``send`` of a topic      ``subscribe``/``consume``/listeners        low
============= ================================================ ========================================== ==========

:func:`impact` answers which members a change reaches, directly or through other members. Everything is
computed on demand from the members' tracked files and nothing is written.

Paths in every result are relative to the workspace root; a directory evidence path ends with ``/``.
"""

from __future__ import annotations

import heapq
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, TextIO

from .workspace_members import Membership, WorkspaceRepo, discover_members, resolve_root

__all__ = [
    "CONFIDENCES",
    "KINDS",
    "Changed",
    "Edge",
    "Evidence",
    "ImpactEdge",
    "ImpactHit",
    "ImpactReport",
    "Link",
    "MemberScan",
    "RepoError",
    "WorkspaceGraph",
    "build_graph",
    "changes_for_paths",
    "impact",
    "render_graph",
    "render_impact",
    "run",
]

KINDS: Final[tuple[str, ...]] = ("submodule", "path", "package", "import", "proto", "openapi", "http", "event")
#: Strongest first.
CONFIDENCES: Final[tuple[str, ...]] = ("high", "medium", "low")
_RANK: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}
_BY_RANK: Final[tuple[str, ...]] = ("low", "medium", "high")

# lc-debt: per-member scan ceiling (tracked files in path order, manifests first); a member past it is marked
# truncated and its later files are never read. Sized for a real folder of 13 repositories whose largest member has
# 8,360 candidate files (a cold scan of all 31k candidates takes about 5 s). Upgrade: reuse the member's Runtime Lite
# index (imports, symbols) instead of reading files.
MAX_FILES_PER_MEMBER: int = 20_000
# lc-debt: files larger than this are skipped (counted as ``oversized``); generated bundles and data dumps sit here.
MAX_FILE_BYTES: int = 512 * 1024
#: Evidence links listed per edge; the rest are counted in ``more``.
MAX_LINKS: int = 5
# lc-debt: generated OpenAPI clients kept per member for matching (the first in path order); later ones are ignored.
_MAX_CLIENTS_PER_MEMBER: Final[int] = 50
_SCAN_WORKERS: Final[int] = 4
_GIT_TIMEOUT_S: Final[float] = 30.0
_DETAIL_CHARS: Final[int] = 160
#: A file modified this close to when it was read may change again within its timestamp granularity unseen
#: ("racy git"): its facts are not kept for reuse.
_RACY_NS: Final[int] = 2_000_000_000
#: Workspace roots whose per-file facts are kept in memory (least recently scanned dropped first).
_CACHE_ROOTS: Final[int] = 4

# lc-debt: vendored and build output folders are skipped by name anywhere in a path; a source folder that happens
# to be called ``build`` or ``vendor`` is skipped too.
_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {"node_modules", "vendor", "dist", "build", ".venv", "venv", "target", "__pycache__", "site-packages"}
)
_MANIFEST_NAMES: Final[frozenset[str]] = frozenset(
    {"pyproject.toml", "setup.cfg", "setup.py", "package.json", "go.mod", "Cargo.toml", ".gitmodules"}
)
_PY_EXT: Final[frozenset[str]] = frozenset({".py"})
_JS_EXT: Final[frozenset[str]] = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"})
_GO_EXT: Final[frozenset[str]] = frozenset({".go"})
_JVM_EXT: Final[frozenset[str]] = frozenset({".java", ".kt"})
_DOC_EXT: Final[frozenset[str]] = frozenset({".yaml", ".yml", ".json"})
_CODE_EXT: Final[frozenset[str]] = _PY_EXT | _JS_EXT | _GO_EXT | _JVM_EXT
_STUB_SUFFIXES: Final[tuple[str, ...]] = (
    "_pb2_grpc.py",
    "_pb2.py",
    "_pb2.pyi",
    ".pb.go",
    "_pb.js",
    "_pb.d.ts",
    "_pb.ts",
    "_connect.ts",
    "Grpc.java",
    "Grpc.kt",
)
# Top-level module names too generic to name another repository's package.
_GENERIC_MODULES: Final[frozenset[str]] = frozenset(
    {
        "app",
        "benchmarks",
        "common",
        "config",
        "conftest",
        "doc",
        "docs",
        "example",
        "examples",
        "lib",
        "main",
        "migrations",
        "scripts",
        "settings",
        "setup",
        "src",
        "test",
        "testing",
        "tests",
        "tools",
        "util",
        "utils",
    }
)
# Single-segment routes every service has; a call to one says nothing about which service it reaches.
_GENERIC_ROUTES: Final[frozenset[str]] = frozenset(
    {
        "api",
        "docs",
        "favicon.ico",
        "health",
        "healthcheck",
        "healthz",
        "live",
        "livez",
        "login",
        "logout",
        "metrics",
        "openapi.json",
        "ping",
        "ready",
        "readyz",
        "status",
        "version",
    }
)
# Test code stands up fake servers and calls them: its routes, calls and topics say nothing about other repositories.
_TEST_DIRS: Final[frozenset[str]] = frozenset(
    {"test", "tests", "__tests__", "testing", "spec", "specs", "e2e", "fixtures", "testdata"}
)
_TEST_FILE: Final[re.Pattern[str]] = re.compile(
    r"^(?:test_.*\.py|.*_test\.(?:py|go)|conftest\.py|.*\.(?:test|spec)\.[cm]?[jt]sx?|.*(?:Test|Tests|IT)\.(?:java|kt))$"
)
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"})
# Names that never resolve on the public internet: a service of this workspace or its cluster.
_PRIVATE_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    ".internal",
    ".local",
    ".localhost",
    ".lan",
    ".home.arpa",
    ".svc",
    ".cluster.local",
    ".consul",
    ".corp",
    ".intranet",
)
_SERVER_RECEIVERS: Final[re.Pattern[str]] = re.compile(
    r"^(?:app|server|api[Rr]outer|routes|bp|blueprint|\w*[Rr]outer)$"
)
_GO_CLIENT_RECEIVERS: Final[re.Pattern[str]] = re.compile(r"^(?:http|\w*[Cc]lient|cli)$")


# --------------------------------------------------------------------------- #
# Public results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Evidence:
    #: Root-relative; a directory ends with ``/`` (``./`` for the root repository itself).
    path: str
    #: 1-based line; 0 when the evidence is a whole file or folder.
    line: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "detail": self.detail}

    def where(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass(frozen=True, slots=True)
class Link:
    """One match behind an edge: what the provider offers and where the consumer uses it."""

    #: The matched name: package, module, ``package.Service``, OpenAPI title or path, route, topic.
    key: str
    confidence: str
    provider: Evidence
    consumer: Evidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "confidence": self.confidence,
            "provider": self.provider.to_dict(),
            "consumer": self.consumer.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Edge:
    """``source`` depends on ``target`` (member relpaths); one edge per (source, target, kind)."""

    source: str
    target: str
    kind: str
    #: The strongest of its links.
    confidence: str
    #: Strongest first, at most :data:`MAX_LINKS`.
    links: tuple[Link, ...]
    #: Links beyond those listed.
    more: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "confidence": self.confidence,
            "links": [link.to_dict() for link in self.links],
            "more": self.more,
        }


@dataclass(frozen=True, slots=True)
class MemberScan:
    repo: str
    #: For a submodule, the member that declares it.
    parent: str | None
    #: Files read.
    files: int
    #: Files skipped for size.
    oversized: int
    #: The per-member file cap stopped the scan; edges from or to this member may be missing.
    truncated: bool
    #: Why the member could not be scanned (``""`` when it was).
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "parent": self.parent,
            "files": self.files,
            "oversized": self.oversized,
            "truncated": self.truncated,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceGraph:
    #: Absolute path of the workspace root.
    root: str
    members: tuple[MemberScan, ...]
    #: Sorted by source, target, kind.
    edges: tuple[Edge, ...]
    #: The member bound (64) left repositories out of ``members``.
    members_truncated: bool = False

    @property
    def truncated(self) -> bool:
        """Whether some edges may be missing (member bound, file cap, or a failed scan)."""

        return self.members_truncated or any(member.truncated or member.error for member in self.members)

    def incomplete(self) -> tuple[str, ...]:
        """Members whose scan did not cover every tracked file."""

        return tuple(member.repo for member in self.members if member.truncated or member.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "members": [member.to_dict() for member in self.members],
            "edges": [edge.to_dict() for edge in self.edges],
            "members_truncated": self.members_truncated,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class Changed:
    repo: str
    #: Root-relative changed files.
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "files": list(self.files)}


@dataclass(frozen=True, slots=True)
class RepoError:
    repo: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "error": self.error}


@dataclass(frozen=True, slots=True)
class ImpactEdge:
    """One edge of the last step into a reached member."""

    kind: str
    #: The edge's confidence for this change: one step lower when the hit is repository-level.
    confidence: str
    #: Its provider-side evidence is in an affected file, or (first step) the change touches a manifest.
    direct: bool
    #: The links an affected file matched (direct), else the edge's strongest links; at most :data:`MAX_LINKS`.
    links: tuple[Link, ...]
    more: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "confidence": self.confidence,
            "direct": self.direct,
            "links": [link.to_dict() for link in self.links],
            "more": self.more,
        }


@dataclass(frozen=True, slots=True)
class ImpactHit:
    """A member a change reaches, by its strongest path."""

    repo: str
    #: Edges between the changed member and this one.
    hops: int
    #: The weakest step along the path.
    confidence: str
    #: Every step along the path is direct.
    direct: bool
    #: Members from the changed one to this one, inclusive.
    path: tuple[str, ...]
    #: The edges of the last step (``repo`` depends on ``path[-2]``), strongest first.
    edges: tuple[ImpactEdge, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "hops": self.hops,
            "confidence": self.confidence,
            "direct": self.direct,
            "path": list(self.path),
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True, slots=True)
class ImpactReport:
    root: str
    #: Members with changes, with their root-relative changed files.
    changed: tuple[Changed, ...]
    #: Sorted by hops, then strongest confidence, then repo.
    reached: tuple[ImpactHit, ...]
    #: Keys of ``changed`` that name no member.
    unknown: tuple[str, ...] = ()
    #: Members whose changes Git could not list.
    errors: tuple[RepoError, ...] = ()
    #: Members whose scan was truncated or failed: reach through them may be missing.
    incomplete: tuple[str, ...] = ()
    #: The member bound left repositories out.
    members_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "changed": [entry.to_dict() for entry in self.changed],
            "reached": [hit.to_dict() for hit in self.reached],
            "unknown": list(self.unknown),
            "errors": [error.to_dict() for error in self.errors],
            "incomplete": list(self.incomplete),
            "members_truncated": self.members_truncated,
        }


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


class _Text:
    """File text with offset -> line lookup (incremental: matches arrive in increasing order per pass)."""

    __slots__ = ("_line", "_offset", "path", "text")

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self._offset = 0
        self._line = 1

    def line_at(self, offset: int) -> int:
        if offset < self._offset:
            self._offset, self._line = 0, 1
        self._line += self.text.count("\n", self._offset, offset)
        self._offset = offset
        return self._line

    def at(self, offset: int) -> Evidence:
        line = self.line_at(offset)
        start = self.text.rfind("\n", 0, offset) + 1
        end = self.text.find("\n", offset)
        detail = self.text[start : end if end >= 0 else len(self.text)].strip()[:_DETAIL_CHARS]
        return Evidence(self.path, line, detail)

    def find(self, *needles: str) -> Evidence:
        """Evidence at the first line holding every needle (the file itself when none does)."""

        for number, raw in enumerate(self.text.splitlines(), start=1):
            if all(needle in raw for needle in needles):
                return Evidence(self.path, number, raw.strip()[:_DETAIL_CHARS])
        return Evidence(self.path, 0, "")


def _dir_evidence(relpath: str, detail: str) -> Evidence:
    return Evidence("./" if relpath in {"", "."} else f"{relpath.rstrip('/')}/", 0, detail)


def _covers(evidence: str, path: str) -> bool:
    if evidence == "./":
        return True
    return path.startswith(evidence) if evidence.endswith("/") else path == evidence


def _is_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in _MANIFEST_NAMES or (name.startswith("requirements") and name.endswith(".txt"))


def _norm_python(name: str) -> str:
    """PEP 503 normalized distribution name."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _norm_cargo(name: str) -> str:
    return name.lower().replace("-", "_")


_PEP508_NAME: Final[re.Pattern[str]] = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_FILE_URL: Final[re.Pattern[str]] = re.compile(r"@\s*file:(?://)?(\S+)")


@dataclass(frozen=True, slots=True)
class _Route:
    segments: tuple[str, ...]
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class _Call:
    segments: tuple[str, ...]
    #: Prefixed by a base URL expression, or a URL whose path may carry a mount prefix: suffix matches allowed.
    prefixed: bool
    evidence: Evidence
    #: The lower-cased host of an absolute URL (classified when edges are assembled); ``None`` for a bare path.
    host: str | None = None


@dataclass(slots=True)
class _Facts:
    """What one member declares; :func:`_scan_file` fills one per file, :func:`_merge` folds them per member."""

    repo: WorkspaceRepo
    files: int = 0
    oversized: int = 0
    truncated: bool = False
    error: str = ""
    #: (ecosystem, normalized name, evidence): names this member publishes.
    packages: list[tuple[str, str, Evidence]] = field(default_factory=list)
    #: (ecosystem, normalized name, evidence): names this member depends on.
    deps: list[tuple[str, str, Evidence]] = field(default_factory=list)
    #: (root-relative folder a local path dependency reaches, consumer evidence).
    path_deps: list[tuple[str, Evidence]] = field(default_factory=list)
    #: (ecosystem, module name, provider evidence).
    modules: list[tuple[str, str, Evidence]] = field(default_factory=list)
    #: (ecosystem, imported name, consumer evidence).
    imports: list[tuple[str, str, Evidence]] = field(default_factory=list)
    #: (``package.Service``, proto package, evidence).
    services: list[tuple[str, str, Evidence]] = field(default_factory=list)
    stub_refs: list[tuple[str, Evidence]] = field(default_factory=list)
    #: (title, [(path, evidence)], document evidence).
    openapi: list[tuple[str, list[tuple[str, Evidence]], Evidence]] = field(default_factory=list)
    clients: list[_Text] = field(default_factory=list)
    routes: list[_Route] = field(default_factory=list)
    calls: list[_Call] = field(default_factory=list)
    publishes: list[tuple[str, Evidence]] = field(default_factory=list)
    subscribes: list[tuple[str, Evidence]] = field(default_factory=list)


#: The :class:`_Facts` lists one file can fill.
_FILE_FACTS: Final[tuple[str, ...]] = (
    "packages",
    "deps",
    "path_deps",
    "modules",
    "imports",
    "services",
    "stub_refs",
    "openapi",
    "clients",
    "routes",
    "calls",
    "publishes",
    "subscribes",
)


# -- manifests --------------------------------------------------------------- #


def _relative_dir(manifest: str) -> str:
    parent = PurePosixPath(manifest).parent.as_posix()
    return "" if parent == "." else parent


def _path_target(root: Path, manifest: str, raw: str) -> str | None:
    """The root-relative folder a manifest's local path reaches (``None`` outside the root)."""

    value = raw.strip().strip("'\"")
    if not value:
        return None
    base = root / _relative_dir(manifest)
    target = Path(os.path.normpath(value if os.path.isabs(value) else base / value))
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError:
        return None
    return "." if relative in {"", "."} else relative


def _pep508_deps(requirements: Iterable[object], text: _Text, facts: _Facts, root: Path) -> None:
    for requirement in requirements:
        if not isinstance(requirement, str):
            continue
        match = _PEP508_NAME.match(requirement)
        if match is None:
            continue
        name = match.group(1)
        evidence = text.find(name)
        facts.deps.append(("py", _norm_python(name), evidence))
        url = _FILE_URL.search(requirement)
        if url is not None:
            target = _path_target(root, text.path, url.group(1))
            if target is not None:
                facts.path_deps.append((target, evidence))


def _pyproject(text: _Text, facts: _Facts, root: Path) -> None:
    try:
        data = tomllib.loads(text.text)
    except tomllib.TOMLDecodeError:
        return
    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    tool = data.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    poetry = tool.get("poetry")
    poetry = poetry if isinstance(poetry, dict) else {}
    for name in (project.get("name"), poetry.get("name")):
        if isinstance(name, str) and name:
            facts.packages.append(("py", _norm_python(name), text.find("name", name)))
    _pep508_deps(project.get("dependencies") or (), text, facts, root)
    optional = project.get("optional-dependencies")
    for group in optional.values() if isinstance(optional, dict) else ():
        _pep508_deps(group if isinstance(group, list) else (), text, facts, root)
    groups = data.get("dependency-groups")
    for group in groups.values() if isinstance(groups, dict) else ():
        _pep508_deps(group if isinstance(group, list) else (), text, facts, root)
    tables: list[object] = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    poetry_groups = poetry.get("group")
    for group in poetry_groups.values() if isinstance(poetry_groups, dict) else ():
        if isinstance(group, dict):
            tables.append(group.get("dependencies"))
    for table in tables:
        if not isinstance(table, dict):
            continue
        for name, spec in table.items():
            if name == "python":
                continue
            evidence = text.find(name)
            facts.deps.append(("py", _norm_python(name), evidence))
            if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                target = _path_target(root, text.path, spec["path"])
                if target is not None:
                    facts.path_deps.append((target, evidence))
    uv = tool.get("uv")
    sources = uv.get("sources") if isinstance(uv, dict) else None
    for name, spec in sources.items() if isinstance(sources, dict) else ():
        if isinstance(spec, dict) and isinstance(spec.get("path"), str):
            target = _path_target(root, text.path, spec["path"])
            if target is not None:
                facts.path_deps.append((target, text.find(name, "path")))


def _setup_cfg(text: _Text, facts: _Facts) -> None:
    section = ""
    key = ""
    for number, raw in enumerate(text.text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section, key = stripped[1:-1].strip().lower(), ""
            continue
        continuation = raw[:1].isspace()
        value = stripped
        if not continuation:
            name, sep, rest = stripped.partition("=")
            if not sep:
                name, sep, rest = stripped.partition(":")
            key, value = name.strip().lower(), rest.strip()
            if section == "metadata" and key == "name" and value:
                facts.packages.append(("py", _norm_python(value), Evidence(text.path, number, stripped)))
                continue
        wanted = (section == "options" and key in {"install_requires", "tests_require", "setup_requires"}) or (
            section == "options.extras_require" and bool(key)
        )
        if not wanted or not value:
            continue
        match = _PEP508_NAME.match(value)
        if match is not None:
            facts.deps.append(("py", _norm_python(match.group(1)), Evidence(text.path, number, stripped)))


def _requirements(text: _Text, facts: _Facts, root: Path) -> None:
    for number, raw in enumerate(text.text.splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        evidence = Evidence(text.path, number, line[:_DETAIL_CHARS])
        editable = re.match(r"^(?:-e|--editable)[\s=]+(.+)$", line)
        value = editable.group(1).strip() if editable else line
        if value.startswith("file:"):
            value = value[len("file:") :].removeprefix("//")
        if value.startswith((".", "/")):
            target = _path_target(root, text.path, value.split("#", 1)[0])
            if target is not None:
                facts.path_deps.append((target, evidence))
            continue
        if line.startswith("-"):
            continue
        match = _PEP508_NAME.match(value)
        if match is not None:
            facts.deps.append(("py", _norm_python(match.group(1)), evidence))
        url = _FILE_URL.search(value)
        if url is not None:
            target = _path_target(root, text.path, url.group(1))
            if target is not None:
                facts.path_deps.append((target, evidence))


_NPM_DEP_TABLES: Final[tuple[str, ...]] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def _package_json(text: _Text, facts: _Facts, root: Path) -> None:
    try:
        data = json.loads(text.text)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    name = data.get("name")
    if isinstance(name, str) and name:
        facts.packages.append(("npm", name, text.find('"name"', name)))
        facts.modules.append(("npm", name, _dir_evidence(_relative_dir(text.path), f"npm package {name}")))
    for table in _NPM_DEP_TABLES:
        deps = data.get(table)
        for dep, spec in deps.items() if isinstance(deps, dict) else ():
            evidence = text.find(f'"{dep}"')
            facts.deps.append(("npm", dep, evidence))
            if isinstance(spec, str) and spec.startswith(("file:", "link:")):
                target = _path_target(root, text.path, spec.split(":", 1)[1])
                if target is not None:
                    facts.path_deps.append((target, evidence))


def _go_mod(text: _Text, facts: _Facts, root: Path) -> None:
    block = ""
    for number, raw in enumerate(text.text.splitlines(), start=1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        evidence = Evidence(text.path, number, line[:_DETAIL_CHARS])
        if line == ")":
            block = ""
            continue
        words = line.split()
        if words[0] in {"require", "replace"} and len(words) > 1 and words[1] == "(":
            block = words[0]
            continue
        directive = block
        if words[0] in {"module", "require", "replace"} and not block:
            directive, words = words[0], words[1:]
        if not words:
            continue
        if directive == "module":
            module = words[0].strip('"')
            facts.packages.append(("go", module, evidence))
            facts.modules.append(("go", module, _dir_evidence(_relative_dir(text.path), f"Go module {module}")))
        elif directive == "require":
            facts.deps.append(("go", words[0], evidence))
        elif directive == "replace" and "=>" in words:
            replacement = words[words.index("=>") + 1 :]
            if replacement and replacement[0].startswith((".", "/")):
                target = _path_target(root, text.path, replacement[0])
                if target is not None:
                    facts.path_deps.append((target, evidence))


def _cargo(text: _Text, facts: _Facts, root: Path) -> None:
    try:
        data = tomllib.loads(text.text)
    except tomllib.TOMLDecodeError:
        return
    package = data.get("package")
    if isinstance(package, dict) and isinstance(package.get("name"), str):
        facts.packages.append(("cargo", _norm_cargo(package["name"]), text.find("name", package["name"])))
    tables: list[object] = [data.get(name) for name in ("dependencies", "dev-dependencies", "build-dependencies")]
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        tables.append(workspace.get("dependencies"))
    targets = data.get("target")
    for target_table in targets.values() if isinstance(targets, dict) else ():
        if isinstance(target_table, dict):
            tables.extend(target_table.get(name) for name in ("dependencies", "dev-dependencies", "build-dependencies"))
    for table in tables:
        if not isinstance(table, dict):
            continue
        for key, spec in table.items():
            name = spec.get("package", key) if isinstance(spec, dict) else key
            evidence = text.find(key)
            facts.deps.append(("cargo", _norm_cargo(str(name)), evidence))
            if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                target = _path_target(root, text.path, spec["path"])
                if target is not None:
                    facts.path_deps.append((target, evidence))


# -- source files ------------------------------------------------------------ #

# Matched at the start of each line holding ``import`` (:func:`_python_imports`), never scanned across the file.
_PY_IMPORT: Final[re.Pattern[str]] = re.compile(
    r"[ \t]*(?:from[ \t]+([A-Za-z_][\w.]*)[ \t]+import\b|import[ \t]+([^\n#;]+))"
)
_JS_IMPORT: Final[re.Pattern[str]] = re.compile(r"""(?:from|import|require)[ \t]*\(?[ \t]*["']([^"'\n]+)["']""")
_GO_IMPORT_LINE: Final[re.Pattern[str]] = re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"')

# Starts with a literal so the engine scans for ``@``; :func:`_scan_python` keeps only decorators that start their
# line (one quoted in prose or a docstring example does not declare a route).
_PY_ROUTE: Final[re.Pattern[str]] = re.compile(
    r"""@[ \t]*[\w.]+\.(?:get|post|put|patch|delete|head|options|route|api_route|websocket)[ \t]*\("""
    r"""[ \t]*[rRbBuU]?["']([^"'\n]*)["']"""
)
_PY_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"""\b(?:APIRouter|Blueprint)[ \t]*\([^)]*?\b(?:url_)?prefix[ \t]*=[ \t]*["']([^"'\n]*)["']""", re.S
)
# Method calls are matched from the ``.method(`` anchor; the receiver is read backwards (:func:`_receiver_before`),
# which keeps the scan linear instead of retrying an identifier pattern at every position.
_PY_CALL: Final[re.Pattern[str]] = re.compile(
    r"""\.(?:get|post|put|patch|delete|head|request|stream)[ \t]*\([ \t]*"""
    r"""(?:["'](?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["'][ \t]*,[ \t]*)?"""
    r"""((?:[A-Za-z_][\w.]*[ \t]*\+[ \t]*)?)[rRfFbBuU]{0,2}["']([^"'\n]+)["']"""
)
_JS_METHOD: Final[re.Pattern[str]] = re.compile(
    r"""\.(get|post|put|patch|delete|head|options|all|request)"""
    r"""[ \t]*\([ \t]*((?:[A-Za-z_$][\w.$]*[ \t]*\+[ \t]*)?)["'`]([^"'`\n]+)["'`]"""
)
_JS_FETCH: Final[re.Pattern[str]] = re.compile(
    r"""(?:fetch|\$fetch|useFetch|ky)[ \t]*\([ \t]*((?:[A-Za-z_$][\w.$]*[ \t]*\+[ \t]*)?)"""
    r"""["'`]([^"'`\n]+)["'`]"""
)
_GO_METHOD: Final[re.Pattern[str]] = re.compile(
    r"""\.(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|Head|Any)"""
    r"""[ \t]*\([ \t]*"([^"\n]+)"[ \t]*(,?)"""
)
_RECEIVER_BEFORE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_$][\w.$]*$")
_GO_REQUEST: Final[re.Pattern[str]] = re.compile(
    r"""\bNewRequest(?:WithContext)?[ \t]*\((?:[^,()\n]*,[ \t]*){1,2}"([^"\n]+)"|\bSprintf[ \t]*\([ \t]*"(%s/[^"\n]+)\""""
)
_SPRING_ROUTE: Final[re.Pattern[str]] = re.compile(
    r"""@(?:Get|Post|Put|Patch|Delete|Request)Mapping[ \t]*\([ \t]*(?:(?:value|path)[ \t]*=[ \t]*)?\{?[ \t]*"([^"\n]*)\""""
)
_JVM_CALL: Final[re.Pattern[str]] = re.compile(
    r"""\.(?:getForObject|getForEntity|postForObject|postForEntity|exchange|uri)[ \t]*\([ \t]*"""
    r"""((?:[A-Za-z_][\w.]*[ \t]*\+[ \t]*)?)"([^"\n]+)\""""
)
# No leading ``\b``: it defeats the regex engine's first-character scan; :func:`_scan_events` checks the boundary.
_PUBLISH: Final[re.Pattern[str]] = re.compile(
    r"""(?:publish|produce|send)\w*[ \t]*\([ \t]*(?:(?:topic|subject|channel|event|routing_key)[ \t]*=[ \t]*)?"""
    r"""[rRbBuU]?["']([^"'\s]+)["']"""
)
_SUBSCRIBE: Final[re.Pattern[str]] = re.compile(
    r"""(?:subscribe|consume|listen|on_message|on_event|handle_event|KafkaListener|RabbitListener|SqsListener"""
    r"""|JmsListener|EventPattern|MessagePattern)\w*[ \t]*\([ \t]*"""
    r"""(?:(?:topics?|queues?|subject|channel|event|destination)[ \t]*=[ \t]*)?[\[{]?[ \t]*[rRbBuU]?["']([^"'\s]+)["']"""
)
_TOPIC: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*[A-Za-z0-9*#]$")
_PROTO_PACKAGE: Final[re.Pattern[str]] = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_PROTO_SERVICE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*service[ \t]+(\w+)", re.M)
_STUB_NAME: Final[re.Pattern[str]] = re.compile(r"""["']/?((?:[A-Za-z_]\w*\.)*[A-Z]\w*)(?:/\w+)?["']""")
_OPENAPI_HEAD: Final[re.Pattern[str]] = re.compile(r"""^\s*["']?(?:openapi|swagger)["']?\s*:""", re.M)
# A client generated from an OpenAPI document says so in its header.
_GENERATED_CLIENT: Final[re.Pattern[str]] = re.compile(
    r"(?:generated|do not edit)[^\n]{0,80}(?:openapi|swagger)|(?:openapi|swagger)[- ]?(?:generator|codegen|typescript)"
)
# Both match a lower-cased file head.
_GENERATED: Final[re.Pattern[str]] = re.compile(
    r"generated by the protocol buffer compiler|code generated .* do not edit|@generated\b"
)
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{[^{}]*\}|<[^<>]*>|%[-+ #0-9.]*[sdvqfx]|^:[A-Za-z_]\w*$|^\*+$")
_PREFIX_EXPR: Final[re.Pattern[str]] = re.compile(r"^(?:\$?\{[^{}]*\}|%[sv]|<[^<>]*>)+$")
_GO_PATTERN_METHOD: Final[re.Pattern[str]] = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+")


def _segments(path: str) -> tuple[str, ...]:
    return tuple("{}" if _PLACEHOLDER.search(segment) else segment for segment in path.split("/") if segment)


def _route_segments(raw: str) -> tuple[str, ...] | None:
    """A route declaration's path with parameters normalized; ``None`` when too generic to match."""

    value = _GO_PATTERN_METHOD.sub("", raw.strip())
    if "://" in value:
        return None
    segments = _segments(value)
    literals = [segment for segment in segments if segment != "{}"]
    if not literals or (len(segments) == 1 and segments[0].lower() in _GENERIC_ROUTES):
        return None
    if segments[0] == ".well-known":
        return None  # protocol discovery (OAuth, OpenID): every such server has it
    return segments


def _call_segments(raw: str, concatenated: bool) -> tuple[tuple[str, ...], bool, str | None] | None:
    """``(segments, prefixed, host)`` of a client call's path or URL literal; ``None`` when it is not path-like.

    ``host`` is the URL's lower-cased host (``None`` for a bare path); whether it can be a member is decided when
    edges are assembled (:func:`_workspace_host`), since that depends on the member names, not on this file.
    """

    value = re.split(r"[?#]", raw.strip(), maxsplit=1)[0]
    prefixed = concatenated
    host: str | None = None
    if "://" in value:
        rest = value.split("://", 1)[1]
        slash = rest.find("/")
        host = _host(rest[:slash] if slash >= 0 else rest)
        value = rest[slash:] if slash >= 0 else ""
    elif not value.startswith("/"):
        head, slash, rest = value.partition("/")
        if not slash or concatenated or not _PREFIX_EXPR.match(head):
            return None
        prefixed, value = True, rest
    segments = _segments(value)
    if not any(segment != "{}" for segment in segments):
        return None
    return segments, prefixed, host


def _host(authority: str) -> str:
    """A URL authority's host, lower-cased, without user info or port (``[::1]`` keeps its brackets)."""

    host = authority.rsplit("@", 1)[-1]
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    return host.lower()


def _host_label(name: str) -> str:
    """A member folder name or a host's first label, compared lower-cased with ``_`` as ``-``."""

    return name.lower().replace("_", "-")


def _workspace_host(host: str, member_labels: frozenset[str]) -> bool:
    """Whether a URL's host (from :func:`_host`) can be a service of this workspace.

    Loopback, a single-label name (a compose or cluster service: ``http://billing:8000``), an IPv4 address, a host
    still to be filled in (``${host}``), a private-by-convention name (``billing.internal``, ``billing.svc``,
    ``billing.svc.cluster.local``, ``billing.consul``, ...) and a dotted name whose first label names a member
    (``billing.mycompany.com`` with a member folder ``billing``) can; any other dotted name (``api.anthropic.com``,
    ``example.test``) is external.
    lc-debt: a public host whose first label happens to equal a member folder name (member ``api``,
    ``api.github.com``) counts as the workspace's. Upgrade: compare the rest of the name with hosts the workspace
    declares (compose files, ingress, env defaults).
    """

    if not host or _PLACEHOLDER.search(host) or "$" in host:
        return True
    if host in _LOOPBACK_HOSTS or "." not in host:
        return True
    if all(part.isdigit() for part in host.split(".")) or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    return _host_label(host.split(".", 1)[0]) in member_labels


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    return any(part in _TEST_DIRS for part in parts[:-1]) or _TEST_FILE.match(parts[-1]) is not None


def _topic(raw: str) -> str | None:
    value = raw.strip()
    if "://" in value or not _TOPIC.match(value) or not re.search(r"[._:/-]", value) or value.startswith("/"):
        return None
    return value


def _receiver(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _receiver_before(text: str, offset: int) -> tuple[str, str]:
    """``(receiver expression, the character before it)`` ending at ``offset`` (a ``.method(`` match)."""

    window_start = max(0, offset - 120)
    match = _RECEIVER_BEFORE.search(text, window_start, offset)
    if match is None:
        return "", text[offset - 1 : offset] if offset else ""
    begin = match.start()
    return match.group(0), text[begin - 1 : begin] if begin else ""


#: Where each event pattern can start (its leading alternatives), for :func:`_matches_at`.
_PUBLISH_WORDS: Final[tuple[str, ...]] = ("publish", "produce", "send")
_SUBSCRIBE_STARTS: Final[tuple[str, ...]] = (
    "subscribe",
    "consume",
    "listen",
    "on_message",
    "on_event",
    "handle_event",
    "KafkaListener",
    "RabbitListener",
    "SqsListener",
    "JmsListener",
    "EventPattern",
    "MessagePattern",
)


def _matches_at(text: str, words: Sequence[str], pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """``pattern`` matched where one of ``words`` (its possible starts) occurs, in text order.

    The event patterns open with an alternation, which the regex engine cannot scan for; finding the words with
    ``str.find`` keeps the cost proportional to their occurrences instead of the file length.
    """

    found: dict[int, re.Match[str]] = {}
    for word in words:
        position = text.find(word)
        while position >= 0:
            match = pattern.match(text, position)
            if match is not None:
                found.setdefault(position, match)
            position = text.find(word, position + len(word))
    return [found[position] for position in sorted(found)]


def _python_imports(text: str) -> list[re.Match[str]]:
    """Import statements: :data:`_PY_IMPORT` matched at the start of each line that holds ``import``."""

    matches: list[re.Match[str]] = []
    position = text.find("import")
    while position >= 0:
        line_start = text.rfind("\n", 0, position) + 1
        match = _PY_IMPORT.match(text, line_start)
        if match is not None:
            matches.append(match)
        line_end = text.find("\n", position)
        if line_end < 0:
            break
        position = text.find("import", line_end)
    return matches


def _scan_events(text: _Text, facts: _Facts) -> None:
    for match in _matches_at(text.text, _PUBLISH_WORDS, _PUBLISH):
        topic = _topic(match.group(1))
        if topic is not None and not _word_before(text.text, match.start()):
            facts.publishes.append((topic, text.at(match.start())))
    for match in _matches_at(text.text, _SUBSCRIBE_STARTS, _SUBSCRIBE):
        topic = _topic(match.group(1))
        if topic is not None and not _word_before(text.text, match.start()):
            facts.subscribes.append((topic, text.at(match.start())))


def _word_before(text: str, offset: int) -> bool:
    previous = text[offset - 1 : offset] if offset else ""
    return previous.isalnum() or previous == "_"


def _add_call(facts: _Facts, text: _Text, offset: int, raw: str, concatenated: bool) -> None:
    parsed = _call_segments(raw, concatenated)
    if parsed is not None:
        facts.calls.append(_Call(parsed[0], parsed[1], text.at(offset), parsed[2]))


def _add_route(facts: _Facts, text: _Text, offset: int, raw: str) -> None:
    segments = _route_segments(raw)
    if segments is not None:
        facts.routes.append(_Route(segments, text.at(offset)))


def _scan_python(text: _Text, facts: _Facts, *, full: bool) -> None:
    seen: set[str] = set()
    for match in _python_imports(text.text):
        names = [match.group(1)] if match.group(1) else match.group(2).split(",")
        for name in names:
            module = name.strip().split()[0].split(".")[0] if name.strip() else ""
            if module and module.isidentifier() and module not in seen:
                seen.add(module)
                facts.imports.append(("py", module, text.at(match.start())))
    if not full:
        return
    if "@" in text.text:
        declares = ("APIRouter" in text.text or "Blueprint" in text.text) and "prefix" in text.text
        prefixes = [match.group(1) for match in _PY_PREFIX.finditer(text.text)] if declares else []
        prefix = prefixes[0].rstrip("/") if len(prefixes) == 1 else ""
        for match in _PY_ROUTE.finditer(text.text):
            line_start = text.text.rfind("\n", 0, match.start()) + 1
            if text.text[line_start : match.start()].strip():
                continue
            path = match.group(1)
            _add_route(facts, text, match.start(), f"{prefix}/{path.lstrip('/')}" if prefix else path)
    for match in _PY_CALL.finditer(text.text):
        if _call_segments(match.group(2), bool(match.group(1))) is None:
            continue  # dict.get("key") and friends: not a path
        receiver, before = _receiver_before(text.text, match.start())
        if not receiver or before == "@" or _SERVER_RECEIVERS.match(_receiver(receiver)):
            continue  # a decorator declares a route; app/router calls register one
        if receiver == "web":
            # aiohttp's route table: ``web.get("/path", handler)`` declares a route.
            if not match.group(1):
                _add_route(facts, text, match.start(), match.group(2))
            continue
        _add_call(facts, text, match.start(), match.group(2), bool(match.group(1)))
    _scan_events(text, facts)


def _npm_name(spec: str) -> str | None:
    if spec.startswith((".", "/", "@/", "~", "#")) or ":" in spec:
        return None
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 and parts[1] else None
    return parts[0] or None


def _scan_js(text: _Text, facts: _Facts, *, full: bool) -> None:
    seen: set[str] = set()
    for match in _JS_IMPORT.finditer(text.text):
        name = _npm_name(match.group(1))
        if name is not None and name not in seen:
            seen.add(name)
            facts.imports.append(("npm", name, text.at(match.start())))
    if not full:
        return
    for match in _JS_METHOD.finditer(text.text):
        receiver, _before = _receiver_before(text.text, match.start())
        method = match.group(1)
        if not receiver:
            continue
        if _SERVER_RECEIVERS.match(_receiver(receiver)):
            if method != "request" and not match.group(2):
                _add_route(facts, text, match.start(), match.group(3))
        elif method != "all":
            _add_call(facts, text, match.start(), match.group(3), bool(match.group(2)))
    for match in _JS_FETCH.finditer(text.text):
        if text.text[match.start() - 1 : match.start()].isalnum():
            continue
        _add_call(facts, text, match.start(), match.group(2), bool(match.group(1)))
    _scan_events(text, facts)


def _scan_go(text: _Text, facts: _Facts, *, full: bool) -> None:
    in_block = False
    offset = 0
    seen: set[str] = set()
    for raw in text.text.splitlines(keepends=True):
        stripped = raw.strip()
        line_offset, offset = offset, offset + len(raw)
        if in_block:
            if stripped.startswith(")"):
                in_block = False
                continue
        elif stripped.startswith("import"):
            if stripped.replace(" ", "").startswith("import("):
                in_block = True
                continue
        else:
            continue
        match = _GO_IMPORT_LINE.match(stripped)
        if match is not None and match.group(1) not in seen:
            seen.add(match.group(1))
            facts.imports.append(("go", match.group(1), text.at(line_offset)))
    if not full:
        return
    for match in _GO_METHOD.finditer(text.text):
        method, literal, comma = match.groups()
        receiver = _receiver(_receiver_before(text.text, match.start())[0])
        if not receiver:
            continue
        if method in {"HandleFunc", "Handle"} or (comma and not _GO_CLIENT_RECEIVERS.match(receiver)):
            _add_route(facts, text, match.start(), literal)
        elif _GO_CLIENT_RECEIVERS.match(receiver):
            _add_call(facts, text, match.start(), literal, False)
    for match in _GO_REQUEST.finditer(text.text):
        _add_call(facts, text, match.start(), match.group(1) or match.group(2), False)
    _scan_events(text, facts)


def _scan_jvm(text: _Text, facts: _Facts, *, full: bool) -> None:
    if not full:
        return
    for match in _SPRING_ROUTE.finditer(text.text):
        _add_route(facts, text, match.start(), match.group(1))
    for match in _JVM_CALL.finditer(text.text):
        _add_call(facts, text, match.start(), match.group(2), bool(match.group(1)))
    _scan_events(text, facts)


def _scan_proto(text: _Text, facts: _Facts) -> None:
    package_match = _PROTO_PACKAGE.search(text.text)
    package = package_match.group(1) if package_match else ""
    for match in _PROTO_SERVICE.finditer(text.text):
        key = f"{package}.{match.group(1)}" if package else match.group(1)
        facts.services.append((key, package, text.at(match.start())))


def _scan_stub(text: _Text, facts: _Facts) -> None:
    seen: set[str] = set()
    for match in _STUB_NAME.finditer(text.text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            facts.stub_refs.append((name, text.at(match.start())))


def _scan_openapi(text: _Text, facts: _Facts) -> None:
    if not _OPENAPI_HEAD.search(text.text[:2048]):
        return
    title = ""
    paths: list[tuple[str, Evidence]] = []
    if text.path.endswith(".json"):
        try:
            data = json.loads(text.text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        info = data.get("info")
        raw_title = info.get("title") if isinstance(info, dict) else None
        title = raw_title if isinstance(raw_title, str) else ""
        raw_paths = data.get("paths")
        for path in raw_paths if isinstance(raw_paths, dict) else ():
            if isinstance(path, str) and path.startswith("/"):
                paths.append((path, text.find(f'"{path}"')))
    else:
        section = ""
        path_indent = -1
        for number, raw in enumerate(text.text.splitlines(), start=1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            if indent == 0:
                section = raw.split(":", 1)[0].strip().strip("'\"")
                path_indent = -1
                continue
            stripped = raw.strip()
            if section == "info" and not title and stripped.startswith("title:"):
                title = stripped[len("title:") :].strip().strip("'\"")
            elif section == "paths":
                key = stripped.split(":", 1)[0].strip().strip("'\"") if ":" in stripped else ""
                if key.startswith("/") and (path_indent < 0 or indent == path_indent):
                    path_indent = indent
                    paths.append((key, Evidence(text.path, number, stripped[:_DETAIL_CHARS])))
    if title or paths:
        facts.openapi.append((title, paths, text.find(title) if title else Evidence(text.path, 0, "")))


def _scan_file(text: _Text, facts: _Facts, root: Path) -> None:
    name = text.path.rsplit("/", 1)[-1]
    suffix = os.path.splitext(name)[1].lower()
    if name == "pyproject.toml":
        _pyproject(text, facts, root)
    elif name == "setup.cfg":
        _setup_cfg(text, facts)
    elif name.startswith("requirements") and name.endswith(".txt"):
        _requirements(text, facts, root)
    elif name == "package.json":
        _package_json(text, facts, root)
    elif name == "go.mod":
        _go_mod(text, facts, root)
    elif name == "Cargo.toml":
        _cargo(text, facts, root)
    elif suffix == ".proto":
        _scan_proto(text, facts)
    elif suffix in _DOC_EXT:
        _scan_openapi(text, facts)
    elif suffix in _CODE_EXT:
        head = text.text[:4096]
        if name.endswith(_STUB_SUFFIXES):
            _scan_stub(text, facts)
            return
        lowered = head.lower()
        # Substring gates first: every alternative of each pattern contains the word tested.
        if ("openapi" in lowered or "swagger" in lowered) and _GENERATED_CLIENT.search(lowered):
            facts.clients.append(text)  # capped per member by :func:`_merge`
            return
        # Generated code and tests import what they need, but their routes, calls and topics are not the
        # repository's own: a test's fake server and the calls to it would link unrelated repositories.
        generated = "generated" in lowered and _GENERATED.search(lowered) is not None
        full = not generated and not _is_test_path(text.path)
        if suffix in _PY_EXT:
            _scan_python(text, facts, full=full)
        elif suffix in _JS_EXT:
            _scan_js(text, facts, full=full)
        elif suffix in _GO_EXT:
            _scan_go(text, facts, full=full)
        else:
            _scan_jvm(text, facts, full=full)


# -- per member ------------------------------------------------------------- #


def _git(cwd: Path, *args: str) -> tuple[int, bytes, str]:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=cwd,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 1, b"", f"git {args[0]} timed out"
    except OSError as exc:
        return 1, b"", str(exc)
    return completed.returncode, completed.stdout, completed.stderr.decode("utf-8", "replace").strip()


def _split_z(output: bytes) -> list[str]:
    return [entry for entry in output.decode("utf-8", "surrogateescape").split("\0") if entry]


def _wanted(path: str) -> bool:
    parts = path.split("/")
    if any(part in _SKIP_DIRS for part in parts[:-1]):
        return False
    name = parts[-1]
    if ".min." in name:
        return False
    suffix = os.path.splitext(name)[1].lower()
    return _is_manifest(path) or suffix in _CODE_EXT or suffix in _DOC_EXT or suffix == ".proto"


def _python_modules(tracked: Sequence[str], facts: _Facts) -> None:
    """Top-level Python packages: ``<project>/X/__init__.py`` or ``<project>/src/X/__init__.py``."""

    projects = {""} | {
        _relative_dir(path)
        for path in tracked
        if path.rsplit("/", 1)[-1] in {"pyproject.toml", "setup.py", "setup.cfg"}
    }
    seen: set[str] = set()
    for path in tracked:
        if not path.endswith("__init__.py") or path.rsplit("/", 1)[-1] != "__init__.py":
            continue
        if any(part in _SKIP_DIRS for part in path.split("/")[:-1]):
            continue  # a vendored copy or stub is not this repository's package
        package = PurePosixPath(path).parent
        container = package.parent
        project = container.parent if container.name == "src" else container
        if (
            _relative_dir(f"{container.as_posix()}/x") not in projects
            and _relative_dir(f"{project.as_posix()}/x") not in projects
        ):
            continue
        name = package.name
        if not name.isidentifier() or name in _GENERIC_MODULES or name in sys.stdlib_module_names or name in seen:
            continue
        seen.add(name)
        facts.modules.append(
            ("py", name, _dir_evidence(facts.repo.prefixed(package.as_posix()), f"Python package {name}"))
        )


@dataclass(frozen=True, slots=True)
class _FileEntry:
    """One file's facts, valid while its ``os.stat`` stamp is unchanged."""

    #: ``(size, mtime_ns, ctime_ns, inode)``.
    stamp: tuple[int, int, int, int]
    #: False for a binary file (read, never counted or scanned).
    text: bool
    #: ``(_Facts list name, items)`` for each non-empty list; shared between scans, never mutated.
    facts: tuple[tuple[str, tuple[Any, ...]], ...]


#: Per-file facts by workspace root, then member (checkout, relpath), then member-relative path. In memory only.
_FileCache = dict[str, _FileEntry]
_CACHE: OrderedDict[str, dict[tuple[str, str], _FileCache]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _member_key(repo: WorkspaceRepo) -> tuple[str, str]:
    return str(repo.root), repo.relpath


def _merge(facts: _Facts, entry: _FileEntry) -> None:
    """Fold one file's facts into its member's (files in path order, so results match a single pass)."""

    facts.files += 1
    for name, items in entry.facts:
        if name == "clients":
            items = items[: max(0, _MAX_CLIENTS_PER_MEMBER - len(facts.clients))]
        getattr(facts, name).extend(items)


def _read_entry(repo: WorkspaceRepo, path: str, root: Path, stamp: tuple[int, int, int, int]) -> _FileEntry | None:
    try:
        content = (repo.root / path).read_bytes()
    except OSError:
        return None
    if b"\0" in content[:1024]:
        return _FileEntry(stamp, False, ())
    found = _Facts(repo)
    _scan_file(_Text(repo.prefixed(path), content.decode("utf-8", "replace")), found, root)
    lists = ((name, tuple(getattr(found, name))) for name in _FILE_FACTS)
    return _FileEntry(stamp, True, tuple((name, items) for name, items in lists if items))


def _scan_member(repo: WorkspaceRepo, root: Path, previous: _FileCache) -> tuple[_Facts, _FileCache]:
    """The member's facts and the file entries to keep: files whose stamp matches ``previous`` are not read."""

    facts = _Facts(repo)
    kept: _FileCache = {}
    code, output, error = _git(repo.root, "ls-files", "-z")
    if code != 0:
        facts.error = error or "git ls-files failed"
        return facts, kept
    tracked = _split_z(output)
    _python_modules(tracked, facts)
    candidates = sorted((path for path in tracked if _wanted(path)), key=lambda path: (not _is_manifest(path), path))
    if len(candidates) > MAX_FILES_PER_MEMBER:
        facts.truncated = True
        candidates = candidates[:MAX_FILES_PER_MEMBER]
    for path in candidates:
        try:
            info = os.lstat(repo.root / path)
        except OSError:
            continue  # deleted in the working tree
        if not stat.S_ISREG(info.st_mode):
            continue  # submodule gitlinks and symlinks
        if info.st_size > MAX_FILE_BYTES:
            facts.oversized += 1
            continue
        stamp = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
        entry = previous.get(path)
        if entry is None or entry.stamp != stamp:
            started = time.time_ns()
            entry = _read_entry(repo, path, root, stamp)
            if entry is None:
                continue
            if info.st_mtime_ns < started - _RACY_NS:
                kept[path] = entry
        else:
            kept[path] = entry
        if entry.text:
            _merge(facts, entry)
    return facts, kept


def _safe_scan(repo: WorkspaceRepo, root: Path, previous: _FileCache) -> tuple[_Facts, _FileCache]:
    try:
        return _scan_member(repo, root, previous)
    except (OSError, ValueError, RecursionError) as exc:
        facts = _Facts(repo)
        facts.error = f"scan failed: {exc}"
        return facts, {}


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #


class _Edges:
    """Links grouped by (source, target, kind); self-edges dropped, repeated links merged."""

    def __init__(self) -> None:
        self.links: dict[tuple[str, str, str], dict[tuple[str, str, int, str, int], Link]] = {}

    def add(self, source: str, target: str, kind: str, link: Link) -> None:
        if source == target:
            return
        bucket = self.links.setdefault((source, target, kind), {})
        identity = (link.key, link.provider.path, link.provider.line, link.consumer.path, link.consumer.line)
        existing = bucket.get(identity)
        if existing is None or _RANK[link.confidence] > _RANK[existing.confidence]:
            bucket[identity] = link

    def sorted_links(self) -> dict[tuple[str, str, str], list[Link]]:
        return {
            key: sorted(
                bucket.values(),
                key=lambda link: (
                    -_RANK[link.confidence],
                    link.consumer.path,
                    link.consumer.line,
                    link.provider.path,
                    link.provider.line,
                    link.key,
                ),
            )
            for key, bucket in sorted(
                self.links.items(), key=lambda item: (item[0][0], item[0][1], KINDS.index(item[0][2]))
            )
        }


def _depth(relpath: str) -> int:
    return 0 if relpath == "." else len(PurePosixPath(relpath).parts)


def _owner(members: Sequence[WorkspaceRepo], relpath: str) -> WorkspaceRepo | None:
    """The deepest member containing a root-relative path (members sorted deepest first)."""

    for member in members:
        if member.relpath == "." or relpath == member.relpath or relpath.startswith(f"{member.relpath}/"):
            return member
    return None


def _name_key(value: str) -> str:
    return re.sub(r"[-_.]", "", value).lower()


def _submodule_edges(root: Path, members: Sequence[WorkspaceRepo], edges: _Edges) -> None:
    by_relpath = {member.relpath: member for member in members}
    texts: dict[str, _Text] = {}
    for member in members:
        parent = by_relpath.get(member.parent) if member.parent is not None else None
        if parent is None:
            continue
        declared = member.relpath if parent.relpath == "." else member.relpath[len(parent.relpath) + 1 :]
        gitmodules = parent.prefixed(".gitmodules")
        text = texts.get(gitmodules)
        if text is None:
            try:
                content = (root / gitmodules).read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            text = texts[gitmodules] = _Text(gitmodules, content)
        edges.add(
            parent.relpath,
            member.relpath,
            "submodule",
            Link(
                declared,
                "high",
                _dir_evidence(member.relpath, "submodule checkout"),
                text.find("path", declared),
            ),
        )


def _assemble(root: Path, membership: Membership, facts: Sequence[_Facts]) -> _Edges:
    edges = _Edges()
    deepest = sorted(membership.members, key=lambda member: -_depth(member.relpath))
    _submodule_edges(root, membership.members, edges)

    # path
    for consumer in facts:
        for target, evidence in consumer.path_deps:
            owner = _owner(deepest, target)
            if owner is not None:
                link = Link(target, "high", _dir_evidence(target, "local path dependency target"), evidence)
                edges.add(consumer.repo.relpath, owner.relpath, "path", link)

    # package
    published: dict[tuple[str, str], list[tuple[str, Evidence]]] = {}
    for provider in facts:
        for ecosystem, name, evidence in provider.packages:
            published.setdefault((ecosystem, name), []).append((provider.repo.relpath, evidence))
    for consumer in facts:
        for ecosystem, name, evidence in consumer.deps:
            for target, provided in published.get((ecosystem, name), ()):
                edges.add(consumer.repo.relpath, target, "package", Link(name, "high", provided, evidence))

    # import
    modules: dict[tuple[str, str], list[tuple[str, Evidence]]] = {}
    go_modules: list[tuple[str, str, Evidence]] = []
    for provider in facts:
        for ecosystem, name, evidence in provider.modules:
            if ecosystem == "go":
                go_modules.append((name, provider.repo.relpath, evidence))
            else:
                modules.setdefault((ecosystem, name), []).append((provider.repo.relpath, evidence))
    go_modules.sort(key=lambda entry: -len(entry[0]))
    for consumer in facts:
        source = consumer.repo.relpath
        own = {(ecosystem, name) for ecosystem, name, _evidence in consumer.modules}
        for ecosystem, name, evidence in consumer.imports:
            if ecosystem == "go":
                for module, target, provided in go_modules:
                    if name != module and not name.startswith(f"{module}/"):
                        continue
                    if target != source:
                        sub = name[len(module) :].strip("/")
                        base = "" if provided.path == "./" else provided.path
                        where = Evidence(f"{base}{sub}/", 0, f"Go package {name}") if sub else provided
                        edges.add(source, target, "import", Link(name, "medium", where, evidence))
                    break
                continue
            if (ecosystem, name) in own:
                continue  # the consumer's own module shadows another repository's
            for target, provided in modules.get((ecosystem, name), ()):
                edges.add(source, target, "import", Link(name, "medium", provided, evidence))

    # proto
    services: dict[str, list[tuple[str, str, Evidence]]] = {}
    for provider in facts:
        for key, package, evidence in provider.services:
            services.setdefault(key, []).append((provider.repo.relpath, package, evidence))
    names: dict[str, set[str]] = {
        entry.repo.relpath: {_name_key(PurePosixPath(entry.repo.relpath).name)}
        | {_name_key(name) for ecosystem, name, _evidence in entry.packages if ecosystem != "go"}
        for entry in facts
    }
    owners: dict[str, list[tuple[str, Evidence]]] = {}
    for key, definitions in services.items():
        definers = {repo for repo, _package, _evidence in definitions}
        if len(definers) == 1:
            owners[key] = [(repo, evidence) for repo, _package, evidence in definitions]
            continue
        # A copied .proto: the provider is the repository the proto package is named after.
        qualified = {
            repo
            for repo, package, _evidence in definitions
            if package and _name_key(package.split(".")[0]) in names[repo]
        }
        if len(qualified) == 1:
            owners[key] = [(repo, evidence) for repo, _package, evidence in definitions if repo in qualified]
            for repo, _package, evidence in definitions:
                for target, provided in owners[key]:
                    edges.add(repo, target, "proto", Link(key, "medium", provided, evidence))
        else:
            # lc-debt: several repositories define the service and none is named after its package; stubs link
            # to all of them and no copy edge is drawn. Upgrade: compare file history or server implementations.
            owners[key] = [(repo, evidence) for repo, _package, evidence in definitions]
    for consumer in facts:
        for name, evidence in consumer.stub_refs:
            for target, provided in owners.get(name, ()):
                edges.add(consumer.repo.relpath, target, "proto", Link(name, "medium", provided, evidence))

    # openapi
    documents = [(entry.repo.relpath, document) for entry in facts for document in entry.openapi]
    for consumer in facts:
        for client in consumer.clients:
            for target, (title, paths, document) in documents:
                if target == consumer.repo.relpath:
                    continue
                if len(title) >= 4 and title in client.text:
                    edges.add(
                        consumer.repo.relpath, target, "openapi", Link(title, "medium", document, client.find(title))
                    )
                for path, provided in paths:
                    quoted = next(
                        (f"{quote}{path}{quote}" for quote in "\"'`" if f"{quote}{path}{quote}" in client.text), None
                    )
                    if quoted is not None:
                        edges.add(
                            consumer.repo.relpath,
                            target,
                            "openapi",
                            Link(path, "medium", provided, client.find(quoted)),
                        )

    # http
    routes: dict[int, list[tuple[str, _Route]]] = {}
    for provider in facts:
        for route in provider.routes:
            routes.setdefault(len(route.segments), []).append((provider.repo.relpath, route))
    member_labels = frozenset(
        _host_label(PurePosixPath(member.relpath).name or member.root.name) for member in membership.members
    )
    hosts: dict[str, bool] = {}
    for consumer in facts:
        source = consumer.repo.relpath
        for call in consumer.calls:
            if call.host is not None:
                internal = hosts.get(call.host)
                if internal is None:
                    internal = hosts[call.host] = _workspace_host(call.host, member_labels)
                if not internal:
                    continue  # a public host: never a member
            if any(_tail_match(route.segments, call.segments) for route in consumer.routes):
                continue  # the repository serves this path itself
            for length in range(1, len(call.segments) + 1):
                for target, route in routes.get(length, ()):
                    if target == source or not _tail_match(route.segments, call.segments):
                        continue
                    full = length == len(call.segments) and not call.prefixed
                    if length == 1 and not full:
                        continue  # a one-segment route as the tail of a longer URL says too little
                    key = "/" + "/".join(route.segments)
                    # One segment (``/token``, ``/probe``) is a name many unrelated services share: low even when exact.
                    confidence = "medium" if full and length > 1 else "low"
                    edges.add(source, target, "http", Link(key, confidence, route.evidence, call.evidence))

    # event
    topics: dict[str, list[tuple[str, Evidence]]] = {}
    for provider in facts:
        for topic, evidence in provider.publishes:
            topics.setdefault(topic, []).append((provider.repo.relpath, evidence))
    for consumer in facts:
        for topic, evidence in consumer.subscribes:
            for target, provided in topics.get(topic, ()):
                edges.add(consumer.repo.relpath, target, "event", Link(topic, "low", provided, evidence))
    return edges


def _tail_match(route: tuple[str, ...], call: tuple[str, ...]) -> bool:
    """Whether the call path ends with the route (a route parameter matches any one segment)."""

    if len(call) < len(route):
        return False
    tail = call[len(call) - len(route) :]
    return all(expected == "{}" or expected == actual for expected, actual in zip(route, tail, strict=True))


@dataclass(frozen=True, slots=True)
class _Scan:
    graph: WorkspaceGraph
    membership: Membership
    #: Every link per (source, target, kind), strongest first (the graph lists at most MAX_LINKS).
    links: dict[tuple[str, str, str], list[Link]]


def _scan(root: Path) -> _Scan:
    base = root.resolve()
    membership = discover_members(base)
    members = membership.members
    facts: list[_Facts] = []
    # Per-file facts are reused within this process while a file's stat stamp is unchanged (Runtime Lite's MCP
    # server, repeated reviews); concurrent scans each publish a complete per-root map, the last one wins. Memory is
    # about 1.3 KB per scanned file (32 MB for 23.6k files), for at most _CACHE_ROOTS roots.
    # lc-debt: nothing is stored on disk, so a fresh process (every `lc workspace` command) scans cold, and every
    # call still lists each member (`git ls-files`) and stats every candidate file. Upgrade: reuse each member's
    # Runtime Lite index, keyed by its index generation.
    key = str(base)
    with _CACHE_LOCK:
        previous = _CACHE.get(key, {})
    if members:
        with ThreadPoolExecutor(max_workers=min(_SCAN_WORKERS, len(members))) as pool:
            scanned = list(
                pool.map(lambda member: _safe_scan(member, base, previous.get(_member_key(member), {})), members)
            )
        facts = [found for found, _kept in scanned]
    else:
        scanned = []
    with _CACHE_LOCK:
        # Only this scan's members and files stay: a removed member or a deleted or renamed file drops out.
        _CACHE[key] = {_member_key(member): kept for member, (_found, kept) in zip(members, scanned, strict=True)}
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_ROOTS:
            _CACHE.popitem(last=False)
    links = _Edges() if not facts else _assemble(base, membership, facts)
    ordered = links.sorted_links()
    edges = tuple(
        Edge(
            source,
            target,
            kind,
            _BY_RANK[max(_RANK[link.confidence] for link in found)],
            tuple(found[:MAX_LINKS]),
            max(0, len(found) - MAX_LINKS),
        )
        for (source, target, kind), found in ordered.items()
    )
    scans = tuple(
        MemberScan(entry.repo.relpath, entry.repo.parent, entry.files, entry.oversized, entry.truncated, entry.error)
        for entry in facts
    )
    graph = WorkspaceGraph(str(base), scans, edges, membership.truncated)
    return _Scan(graph, membership, ordered)


def build_graph(root: Path) -> WorkspaceGraph:
    """The members of ``root`` and the dependency edges between them (``source`` depends on ``target``)."""

    return _scan(root).graph


# --------------------------------------------------------------------------- #
# Impact
# --------------------------------------------------------------------------- #


def _clean(path: str) -> str:
    """A relative POSIX path without ``.`` parts or edge slashes (``""`` for the folder itself)."""

    value = posixpath.normpath(path.strip().replace("\\", "/"))
    return "" if value in {".", "/"} else value.strip("/")


def _git_changes(repo: WorkspaceRepo, base: str | None) -> tuple[list[str], str]:
    """Member-relative files changed in the working tree (and untracked) against ``HEAD`` or ``base``."""

    ref = base or "HEAD"
    code, output, error = _git(
        repo.root, "diff", "--name-only", "-z", "--no-renames", "--ignore-submodules=dirty", ref, "--"
    )
    if code != 0 and base is None and _git(repo.root, "rev-parse", "--verify", "-q", "HEAD")[0] != 0:
        code, output, error = _git(repo.root, "ls-files", "-z")  # no commit yet: every tracked file is new
    if code != 0:
        return [], error or f"git diff {ref} failed"
    files = set(_split_z(output))
    code, untracked, error = _git(repo.root, "ls-files", "--others", "--exclude-standard", "-z")
    if code != 0:
        return sorted(files), error or "git ls-files --others failed"
    files.update(_split_z(untracked))
    return sorted(files), ""


@dataclass(frozen=True, slots=True)
class _Reach:
    member: str
    rank: int
    direct: bool
    hops: int
    path: tuple[str, ...]
    #: Root-relative files of this member the change is known to touch (changed files, or consumer-side evidence).
    affected: frozenset[str]
    manifest: bool
    edges: tuple[ImpactEdge, ...] = ()


def _step_edge(kind: str, links: Sequence[Link], state: _Reach, covered: dict[str, bool]) -> ImpactEdge:
    def hit(evidence: str) -> bool:
        found = covered.get(evidence)
        if found is None:
            if evidence.endswith("/"):
                found = any(_covers(evidence, path) for path in state.affected)
            else:
                found = evidence in state.affected
            covered[evidence] = found
        return found

    matched = [link for link in links if hit(link.provider.path)]
    direct = bool(matched) or (state.hops == 0 and state.manifest)
    shown = matched or list(links)
    rank = max(_RANK[link.confidence] for link in shown)
    if not direct:
        rank = max(0, rank - 1)
    return ImpactEdge(kind, _BY_RANK[rank], direct, tuple(shown[:MAX_LINKS]), max(0, len(shown) - MAX_LINKS))


def _walk(
    origin: str, files: Sequence[str], incoming: Mapping[str, Mapping[str, Sequence[tuple[str, list[Link]]]]]
) -> list[_Reach]:
    """Best-first reach from one changed member: strongest confidence, then direct, then fewest hops.

    Confidence never rises and directness never returns along a path, so the first time a member is popped
    its path is its best one.
    """

    start = _Reach(origin, 2, True, 0, (origin,), frozenset(files), any(_is_manifest(path) for path in files))
    counter = 0
    heap: list[tuple[int, bool, int, int, _Reach]] = [(-start.rank, not start.direct, 0, counter, start)]
    done: dict[str, _Reach] = {}
    while heap:
        state = heapq.heappop(heap)[-1]
        if state.member in done:
            continue
        done[state.member] = state
        covered: dict[str, bool] = {}
        for source, edges in incoming.get(state.member, {}).items():
            if source in done:
                continue
            step = sorted(
                (_step_edge(kind, links, state, covered) for kind, links in edges),
                key=lambda edge: (-_RANK[edge.confidence], not edge.direct, KINDS.index(edge.kind)),
            )
            rank = min(state.rank, _RANK[step[0].confidence])
            direct = state.direct and any(edge.direct for edge in step)
            affected = frozenset(
                link.consumer.path
                for _kind, links in edges
                for link in links
                if not link.consumer.path.endswith("/") and not _is_manifest(link.consumer.path)
            )
            reached = _Reach(source, rank, direct, state.hops + 1, (*state.path, source), affected, False, tuple(step))
            counter += 1
            heapq.heappush(heap, (-rank, not direct, reached.hops, counter, reached))
    return [state for member, state in done.items() if member != origin]


def impact(
    root: Path,
    changed: Mapping[str, Sequence[str]] | None = None,
    *,
    base: str | None = None,
) -> ImpactReport:
    """The members a change reaches.

    ``changed`` maps member relpaths to member-relative changed files; ``None`` asks Git for each member's
    working-tree and untracked changes against ``HEAD`` (or ``base``).
    """

    scan = _scan(root)
    members = {member.relpath: member for member in scan.membership.members}
    unknown: list[str] = []
    errors: list[RepoError] = []
    changed_files: dict[str, tuple[str, ...]] = {}
    if changed is None:
        ordered = list(members.values())
        if ordered:
            with ThreadPoolExecutor(max_workers=min(_SCAN_WORKERS, len(ordered))) as pool:
                results = list(pool.map(lambda member: _git_changes(member, base), ordered))
        else:
            results = []
        for member, (found, error) in zip(ordered, results, strict=True):
            if error:
                errors.append(RepoError(member.relpath, error))
            if found:
                changed_files[member.relpath] = tuple(member.prefixed(path) for path in found)
    else:
        for key, given in changed.items():
            relpath = _clean(key) or "."
            owner = members.get(relpath)
            if owner is None:
                unknown.append(key)
                continue
            listed = [given] if isinstance(given, str) else list(given)
            cleaned = sorted({path for path in (_clean(str(item)) for item in listed) if path})
            if cleaned:
                changed_files[relpath] = tuple(owner.prefixed(path) for path in cleaned)

    incoming: dict[str, dict[str, list[tuple[str, list[Link]]]]] = {}
    for (source, target, kind), links in scan.links.items():
        incoming.setdefault(target, {}).setdefault(source, []).append((kind, links))
    best: dict[str, _Reach] = {}
    for origin, files in changed_files.items():
        for state in _walk(origin, files, incoming):
            current = best.get(state.member)
            if current is None or (-state.rank, not state.direct, state.hops) < (
                -current.rank,
                not current.direct,
                current.hops,
            ):
                best[state.member] = state
    reached = tuple(
        ImpactHit(state.member, state.hops, _BY_RANK[state.rank], state.direct, state.path, state.edges)
        for state in sorted(best.values(), key=lambda state: (state.hops, -state.rank, not state.direct, state.member))
    )
    return ImpactReport(
        root=scan.graph.root,
        changed=tuple(Changed(repo, files) for repo, files in changed_files.items()),
        reached=reached,
        unknown=tuple(unknown),
        errors=tuple(errors),
        incomplete=scan.graph.incomplete(),
        members_truncated=scan.graph.members_truncated,
    )


def changes_for_paths(root: Path, paths: Iterable[str]) -> tuple[dict[str, list[str]], tuple[str, ...]]:
    """Split root-relative paths into ``{member: [member-relative paths]}``; the second item lists paths in no member."""

    base = root.resolve()
    deepest = sorted(discover_members(base).members, key=lambda member: -_depth(member.relpath))
    changed: dict[str, list[str]] = {}
    outside: list[str] = []
    for raw in paths:
        value = raw.strip()
        if os.path.isabs(value):
            try:
                value = Path(os.path.normpath(value)).relative_to(base).as_posix()
            except ValueError:
                outside.append(raw)
                continue
        relative = _clean(value)
        owner = _owner(deepest, relative) if relative and relative.split("/")[0] != ".." else None
        inner = ""
        if owner is not None and owner.relpath != relative:
            inner = relative if owner.relpath == "." else relative[len(owner.relpath) + 1 :]
        if owner is None or not inner:
            outside.append(raw)
            continue
        changed.setdefault(owner.relpath, []).append(inner)
    return changed, tuple(outside)


# --------------------------------------------------------------------------- #
# Rendering and the command line
# --------------------------------------------------------------------------- #


def _notes(members: Sequence[MemberScan], members_truncated: bool) -> list[str]:
    notes = [
        f"[lc:workspace_partial] {member.repo}: not scanned ({member.error})" for member in members if member.error
    ]
    notes.extend(
        f"[lc:workspace_partial] {member.repo}: scan stopped at {MAX_FILES_PER_MEMBER} files; edges may be missing"
        for member in members
        if member.truncated
    )
    if members_truncated:
        notes.append("[lc:workspace_partial] the repository bound was reached; later repositories were not scanned")
    return notes


def _link_line(link: Link) -> str:
    return f"    {link.key} ({link.confidence}): {link.consumer.where()} -> {link.provider.where()}"


def render_graph(graph: WorkspaceGraph) -> str:
    lines = [f"workspace {graph.root}: {len(graph.members)} repositories, {len(graph.edges)} edges"]
    lines.extend(_notes(graph.members, graph.members_truncated))
    if not graph.edges:
        lines.append("no dependency edges between repositories")
    for edge in graph.edges:
        lines.append(f"{edge.source} -> {edge.target}  {edge.kind}  {edge.confidence}")
        lines.extend(_link_line(link) for link in edge.links)
        if edge.more:
            lines.append(f"    (+{edge.more} more)")
    return "\n".join(lines) + "\n"


def render_impact(report: ImpactReport) -> str:
    lines: list[str] = []
    lines.extend(f"[lc:workspace_partial] {error.repo}: changes not listed ({error.error})" for error in report.errors)
    lines.extend(
        f"[lc:workspace_partial] {repo}: scan incomplete; reach through it may be missing" for repo in report.incomplete
    )
    if report.members_truncated:
        lines.append("[lc:workspace_partial] the repository bound was reached; later repositories were not scanned")
    lines.extend(f"not a repository of this root: {key}" for key in report.unknown)
    if not report.changed:
        lines.append("no changes")
        return "\n".join(lines) + "\n"
    summary = ", ".join(
        f"{entry.repo} ({len(entry.files)} file{'s' if len(entry.files) != 1 else ''})" for entry in report.changed
    )
    lines.append(f"changed: {summary}")
    if not report.reached:
        lines.append("nothing reached")
    for hit in report.reached:
        level = "direct" if hit.direct else "repository-level"
        lines.append(
            f"{hit.repo}  {hit.hops} hop{'s' if hit.hops != 1 else ''}  {hit.confidence}  {level}  "
            f"via {' -> '.join(hit.path)}"
        )
        for edge in hit.edges:
            lines.append(f"  {edge.kind} ({edge.confidence}, {'direct' if edge.direct else 'repository-level'})")
            lines.extend(_link_line(link) for link in edge.links)
            if edge.more:
                lines.append(f"    (+{edge.more} more)")
    return "\n".join(lines) + "\n"


def _usage(sink: TextIO, message: str) -> int:
    from .workspace_cli import WORKSPACE_USAGE

    sink.write(f"{message}\n{WORKSPACE_USAGE}")
    return 2


def run(action: str, arguments: Sequence[str], sink: TextIO) -> int:
    """``lc workspace graph [--root DIR] [--json]`` and ``lc workspace impact [--root DIR] [--base REF] [--json] [PATH ...]``."""

    options: dict[str, str] = {}
    as_json = False
    paths: list[str] = []
    items = list(arguments)
    index = 0
    while index < len(items):
        item = items[index]
        name, equals, inline = item.partition("=")
        if item == "--json":
            as_json = True
        elif name in {"--root", "--base"}:
            if name == "--base" and action != "impact":
                return _usage(sink, f"lc workspace {action} does not take --base")
            if equals:
                value = inline
            elif index + 1 < len(items):
                index += 1
                value = items[index]
            else:
                return _usage(sink, f"{name} needs a value")
            if not value:
                return _usage(sink, f"{name} needs a value")
            options[name] = value
        elif item == "--":
            paths.extend(items[index + 1 :])
            break
        elif item.startswith("-"):
            return _usage(sink, f"unknown option: {item}")
        elif action == "impact":
            paths.append(item)
        else:
            return _usage(sink, f"unexpected argument: {item}")
        index += 1
    root = resolve_root(Path(options.get("--root", ".")))
    if not root.is_dir():
        sink.write(f"not a folder: {root}\n")
        return 2

    if action == "graph":
        graph = build_graph(root)
        sink.write(json.dumps(graph.to_dict(), indent=2) + "\n" if as_json else render_graph(graph))
        if not graph.members:
            if not as_json:
                sink.write(f"no repositories under {root}\n")
            return 1
        return 1 if any(member.error for member in graph.members) else 0

    changed: dict[str, list[str]] | None = None
    outside: tuple[str, ...] = ()
    if paths:
        changed, outside = changes_for_paths(root, paths)
    report = impact(root, changed, base=options.get("--base"))
    if as_json:
        payload = report.to_dict()
        payload["outside"] = list(outside)
        sink.write(json.dumps(payload, indent=2) + "\n")
    else:
        sink.write("".join(f"not in a repository of this root: {path}\n" for path in outside))
        sink.write(render_impact(report))
    return 1 if report.errors or outside or report.unknown else 0
