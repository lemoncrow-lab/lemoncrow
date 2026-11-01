"""Hermetic Python dependency provisioning (Phase 9R item 3, Python ecosystem).

Same stage contract as npm in :mod:`~lemoncrow.pro.capabilities.review.hermetic`:

``fetch``
    LemonCrow reads the reviewed ``uv.lock`` (or hash-pinned
    ``requirements*.txt``, ``--require-hashes`` semantics) and downloads
    **wheels only**, from allowlisted ``pypi`` registries, with every file's
    sha256 checked against the lock. No repository code runs. A package with
    no wheel compatible with the sandbox interpreter is refused with a visible
    reason: building an sdist would run repository-chosen build code.
``build``
    Offline, inside bubblewrap with every namespace unshared: a virtualenv is
    created for the sandbox interpreter and the verified wheels are unpacked
    into it by a fixed installer (paths confined, symlinks dropped, sizes
    bounded, dist-info name/version checked). Console scripts are generated.
``run``
    The environment is mounted read-only at :data:`PYTHON_ENV_MOUNT` and put
    first on ``PATH``.
"""

from __future__ import annotations

import hashlib
import html.parser
import importlib
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol, cast
from urllib.parse import urljoin, urlsplit

from lemoncrow.pro.capabilities.review.execution import StageOutcome, canonical_digest
from lemoncrow.pro.capabilities.review.hermetic import (
    _PROBE_RUN,
    _SANDBOX_PATH,
    FETCH_CONCURRENCY,
    INSTALL_TIMEOUT_SECONDS,
    MAX_TARBALL_BYTES,
    MAX_TOTAL_FETCH_BYTES,
    DependencyCache,
    HermeticPolicy,
    HermeticRefused,
    HermeticUnavailable,
    RegistryFetcher,
    RegistryRule,
    _stage_ms,
    _tree_lock,
    offline_bwrap_prefix,
    pypi_normalize,
)

PYTHON_ENV_MOUNT: Final[str] = "/lemoncrow-python"
PYTHON_VENV: Final[str] = f"{PYTHON_ENV_MOUNT}/venv"
PYTHON_EXTRACTOR_VERSION: Final[int] = 1
MAX_PYTHON_PACKAGES: Final[int] = 2000
MAX_INDEX_PAGE_BYTES: Final[int] = 16 * 1024 * 1024
_UV_LOCK: Final[str] = "uv.lock"
_HASH_ALGORITHMS: Final[frozenset[str]] = frozenset({"sha256", "sha384", "sha512"})
_SIMPLE_ACCEPT: Final[str] = "application/vnd.pypi.simple.v1+json, text/html;q=0.1"
_INTERPRETERS: Final[tuple[str, ...]] = ("/usr/local/bin/python3", "/usr/bin/python3", "/bin/python3")


class _Fetcher(Protocol):
    def fetch(self, url: str, *, max_bytes: int = ...) -> bytes: ...

    def fetch_page(self, url: str, *, max_bytes: int = ..., accept: str = ...) -> tuple[bytes, str, str]: ...


# --------------------------------------------------------------------------- #
# Sandbox interpreter
# --------------------------------------------------------------------------- #

_PROBE_SCRIPT: Final[str] = r"""
import json, os, platform, sys, sysconfig
def fmt(info):
    value = "%d.%d.%d" % (info.major, info.minor, info.micro)
    if info.releaselevel != "final":
        value += info.releaselevel[0] + str(info.serial)
    return value
print(json.dumps({
    "implementation_name": sys.implementation.name,
    "implementation_version": fmt(sys.implementation.version),
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "platform_python_implementation": platform.python_implementation(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "sys_platform": sys.platform,
    "version": list(sys.version_info[:2]),
    "gil_disabled": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
}))
"""


@dataclass(frozen=True, slots=True)
class PythonTarget:
    """The interpreter ``python3`` resolves to inside the sandbox (read-only host runtime)."""

    executable: str
    version: tuple[int, int]
    implementation: str
    gil_disabled: bool
    environment: tuple[tuple[str, str], ...]

    @property
    def marker_environment(self) -> dict[str, str]:
        # No extras or groups of the dependency are active unless the lock says so.
        return {**dict(self.environment), "extra": ""}

    @property
    def full_version(self) -> str:
        return dict(self.environment).get("python_full_version", "")

    def supported_tags(self) -> tuple[str, ...]:
        """Wheel tags this interpreter accepts, most preferred first."""

        tags = _packaging("tags")
        if self.implementation != "cpython":
            # lc-debt: only CPython sandbox interpreters get wheel tags; PyPy/GraalPy are refused.
            raise HermeticRefused(f"sandbox interpreter {self.implementation} is not CPython; refusing wheel selection")
        interpreter = f"cp{self.version[0]}{self.version[1]}"
        abis = [interpreter + ("t" if self.gil_disabled else "")]
        platforms = list(tags.platform_tags())
        ordered = [
            *tags.cpython_tags(self.version, abis, platforms),
            *tags.compatible_tags(self.version, interpreter, platforms),
        ]
        return tuple(dict.fromkeys(str(tag) for tag in ordered))

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "executable": self.executable,
                "version": list(self.version),
                "implementation": self.implementation,
                "gil_disabled": self.gil_disabled,
                "environment": dict(self.environment),
            }
        )


def _packaging(module: str) -> Any:
    """A ``packaging`` submodule (tags, markers, requirements, specifiers, utils, version)."""

    try:
        return importlib.import_module(f"packaging.{module}")
    except ImportError as exc:  # pragma: no cover - packaging ships with LemonCrow's dependency set
        raise HermeticUnavailable("Python dependency planning needs the 'packaging' library") from exc


@lru_cache(maxsize=4)
def probe_sandbox_python(executable: str = "") -> PythonTarget:
    """Probe the sandbox's ``python3`` (the first on the sandbox ``PATH``)."""

    chosen = executable or next((path for path in _INTERPRETERS if Path(path).is_file()), "")
    if not chosen:
        raise HermeticUnavailable("the sandbox runtime has no python3; Python dependencies cannot be provisioned")
    try:
        proc = _PROBE_RUN(
            [chosen, "-I", "-S", "-c", _PROBE_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
            env={"PATH": _SANDBOX_PATH, "HOME": "/nonexistent"},
        )
        raw = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        raise HermeticUnavailable(f"could not probe the sandbox python3: {exc}") from exc
    if not isinstance(raw, dict):
        raise HermeticUnavailable(f"could not probe the sandbox python3: {proc.stdout[-500:]}")
    version = raw.pop("version")
    gil_disabled = bool(raw.pop("gil_disabled"))
    return PythonTarget(
        executable=chosen,
        version=(int(version[0]), int(version[1])),
        implementation=str(raw.get("implementation_name") or ""),
        gil_disabled=gil_disabled,
        environment=tuple(sorted((str(key), str(value)) for key, value in raw.items())),
    )


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlannedWheel:
    name: str
    version: str
    url: str
    sha256: str
    filename: str


@dataclass(frozen=True, slots=True)
class PythonInstallPlan:
    """Validated, policy-checked wheel set: the only input of the Python fetch/build stages."""

    source: str
    wheels: tuple[PlannedWheel, ...]
    lockfile_digest: str
    interpreter_digest: str
    project_paths: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    hosts: tuple[str, ...] = field(default=())

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "extractor": PYTHON_EXTRACTOR_VERSION,
                "interpreter": self.interpreter_digest,
                "wheels": [[item.name, item.version, item.sha256] for item in self.wheels],
                "project_paths": list(self.project_paths),
            }
        )


def python_dependency_digest(project: Path) -> str:
    """Digest of the Python dependency declarations and lockfiles (fingerprint input)."""

    digest = hashlib.sha256()
    for path in (project / "pyproject.toml", project / _UV_LOCK, *requirements_files(project)):
        if path.is_file() and not path.is_symlink():
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def requirements_files(project: Path) -> tuple[Path, ...]:
    return tuple(path for path in sorted(project.glob("requirements*.txt")) if path.is_file() and not path.is_symlink())


def python_project_state(project: Path) -> str:
    """``uv.lock``, ``requirements``, ``unlocked`` (dependencies declared but not locked) or ``""``."""

    if (project / _UV_LOCK).is_file():
        return "uv.lock"
    if requirements_files(project):
        return "requirements"
    manifest = project / "pyproject.toml"
    if not manifest.is_file():
        return ""
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "unlocked"
    raw_project = data.get("project")
    project_table: Mapping[str, Any] = raw_project if isinstance(raw_project, Mapping) else {}
    declared = (
        project_table.get("dependencies"),
        project_table.get("optional-dependencies"),
        data.get("dependency-groups"),
    )
    return "unlocked" if any(declared) else ""


def _select_wheel(
    candidates: Iterable[tuple[str, str, str]],
    *,
    name: str,
    version: str,
    supported: Sequence[str],
) -> tuple[str, str, str] | None:
    """Best ``(filename, url, sha256)`` among candidates for exactly ``name==version``."""

    utils = _packaging("utils")
    versions = _packaging("version")
    rank = {tag: index for index, tag in enumerate(supported)}
    best: tuple[int, tuple[str, str, str]] | None = None
    try:
        pinned = versions.Version(version)
    except versions.InvalidVersion as exc:
        raise HermeticRefused(f"{name} has an invalid pinned version {version!r}") from exc
    for filename, url, sha256 in candidates:
        if not filename.endswith(".whl"):
            continue
        try:
            wheel_name, wheel_version, _build, wheel_tags = utils.parse_wheel_filename(filename)
        except (utils.InvalidWheelFilename, versions.InvalidVersion):
            continue
        if str(wheel_name) != pypi_normalize(name) or wheel_version != pinned:
            continue
        score = min((rank[str(tag)] for tag in wheel_tags if str(tag) in rank), default=None)
        if score is not None and (best is None or score < best[0]):
            best = (score, (filename, url, sha256))
    return None if best is None else best[1]


def _check_wheel_url(policy: HermeticPolicy, url: str, name: str, index_rule: RegistryRule | None) -> RegistryRule:
    rule = policy.registry_for(url, "pypi")
    if rule is None:
        raise HermeticRefused(f"{name} resolves outside the allowlisted pypi registries: {url}")
    pinned = policy.pinned_registry(name, "pypi")
    if pinned is not None and pinned is not rule and pinned is not index_rule:
        raise HermeticRefused(
            f"{name} is pinned to registry {pinned.host} by trusted policy but resolves from {rule.host} "
            "(dependency confusion)"
        )
    for serving in (rule, index_rule):
        if pinned is None and serving is not None and serving.packages and not serving.pins(name):
            # A private registry serves only the names it is pinned for.
            raise HermeticRefused(f"{name} is not a package this registry ({serving.host}) is allowed to serve")
    return rule


def _marker_applies(marker: str, target: PythonTarget, *, where: str) -> bool:
    if not marker:
        return True
    markers = _packaging("markers")
    try:
        return bool(markers.Marker(marker).evaluate(target.marker_environment))
    except (markers.InvalidMarker, markers.UndefinedComparison, markers.UndefinedEnvironmentName) as exc:
        raise HermeticRefused(f"{where}: marker {marker!r} cannot be evaluated: {exc}") from exc


def _project_path(project: Path, rel: str, sandbox_project: str) -> str:
    pure = PurePosixPath(rel.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise HermeticRefused(f"uv.lock project path {rel!r} leaves the reviewed project")
    host = project / pure
    if not host.is_dir():
        raise HermeticRefused(f"uv.lock project path {rel!r} is not a directory in the reviewed Snapshot")
    # lc-debt: editable projects are put on sys.path by a .pth (src/ layout heuristic), not by their build backend.
    inner = pure / "src" if (host / "src").is_dir() else pure
    base = sandbox_project.rstrip("/") or "/"
    return base if inner.as_posix() == "." else f"{base}/{inner.as_posix()}"


def plan_uv_lock(
    project: Path,
    policy: HermeticPolicy,
    target: PythonTarget,
    *,
    sandbox_project: str,
) -> PythonInstallPlan:
    """Walk the reviewed ``uv.lock`` from the project root (dependencies plus all dev groups)."""

    policy.require_fetch("pypi")
    try:
        lock = tomllib.loads((project / _UV_LOCK).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HermeticRefused(f"uv.lock is unreadable: {exc}") from exc
    if int(lock.get("version") or 0) != 1:
        raise HermeticRefused("uv.lock must be lock version 1")
    requires = str(lock.get("requires-python") or "")
    if requires:
        specifiers = _packaging("specifiers")
        try:
            allowed = specifiers.SpecifierSet(requires).contains(target.full_version, prereleases=True)
        except specifiers.InvalidSpecifier as exc:
            raise HermeticRefused(f"uv.lock requires-python {requires!r} is invalid") from exc
        if not allowed:
            raise HermeticRefused(
                f"uv.lock requires Python {requires}; the sandbox interpreter is {target.full_version}"
            )
    raw_packages = lock.get("package")
    if not isinstance(raw_packages, list) or len(raw_packages) > MAX_PYTHON_PACKAGES:
        raise HermeticRefused(f"uv.lock must list between 1 and {MAX_PYTHON_PACKAGES} packages")
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for item in raw_packages:
        if isinstance(item, Mapping) and str(item.get("name") or ""):
            by_name.setdefault(pypi_normalize(str(item["name"])), []).append(item)

    def source_of(item: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = item.get("source")
        return raw if isinstance(raw, Mapping) else {}

    roots = [item for items in by_name.values() for item in items if source_of(item).get("editable") == "."]
    roots += [item for items in by_name.values() for item in items if source_of(item).get("virtual") == "."]
    if not roots:
        raise HermeticRefused("uv.lock has no root project (editable or virtual source '.')")

    def lookup(dep: Mapping[str, Any], *, where: str) -> Mapping[str, Any]:
        name = pypi_normalize(str(dep.get("name") or ""))
        candidates = by_name.get(name, [])
        if dep.get("version") is not None:
            candidates = [item for item in candidates if str(item.get("version")) == str(dep.get("version"))]
        if dep.get("source") is not None:
            candidates = [item for item in candidates if source_of(item) == dep.get("source")]
        if len(candidates) != 1:
            raise HermeticRefused(f"uv.lock dependency {name} of {where} does not resolve to exactly one package")
        return candidates[0]

    selected: dict[tuple[str, str], Mapping[str, Any]] = {}
    extras_done: set[tuple[str, str, str]] = set()
    queue: list[tuple[Mapping[str, Any], str]] = [(root, "") for root in roots]
    for root in roots:
        raw_groups = root.get("dev-dependencies")
        groups: Mapping[str, Any] = raw_groups if isinstance(raw_groups, Mapping) else {}
        for group in sorted(groups):
            members = groups[group]
            for dep in members if isinstance(members, list) else ():
                if isinstance(dep, Mapping) and _marker_applies(
                    str(dep.get("marker") or ""), target, where=f"group {group}"
                ):
                    queue.append((lookup(dep, where=f"group {group}"), ""))
                    queue.extend((lookup(dep, where=group), str(extra)) for extra in dep.get("extra") or ())
    while queue:
        item, extra = queue.pop()
        key = (pypi_normalize(str(item["name"])), str(item.get("version") or ""))
        if extra:
            if (*key, extra) in extras_done:
                continue
            extras_done.add((*key, extra))
            table = item.get("optional-dependencies")
            deps = table.get(extra) if isinstance(table, Mapping) else None
        else:
            if key in selected:
                continue
            selected[key] = item
            deps = item.get("dependencies")
        for dep in deps if isinstance(deps, list) else ():
            if not isinstance(dep, Mapping):
                continue
            where = f"{key[0]}{f'[{extra}]' if extra else ''}"
            if not _marker_applies(str(dep.get("marker") or ""), target, where=where):
                continue
            found = lookup(dep, where=where)
            queue.append((found, ""))
            queue.extend((found, str(name)) for name in dep.get("extra") or ())

    names = [key[0] for key in selected]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise HermeticRefused(f"uv.lock selects more than one version of {', '.join(duplicated)} for this sandbox")
    supported = target.supported_tags()
    wheels: list[PlannedWheel] = []
    project_paths: list[str] = []
    hosts: set[str] = set()
    for (name, version), item in sorted(selected.items()):
        source = source_of(item)
        local = source.get("editable") or source.get("virtual")
        if isinstance(local, str):
            if source.get("editable") is not None:
                project_paths.append(_project_path(project, local, sandbox_project))
            continue
        index = source.get("registry")
        if not isinstance(index, str):
            kind = next(iter(source), "unknown")
            # lc-debt: git/url/path/directory sources are refused; supporting them needs a source-specific fetcher.
            raise HermeticRefused(f"{name}=={version} comes from a {kind} source; only registry packages are fetched")
        index_rule = policy.registry_for(index, "pypi")
        if index_rule is None:
            raise HermeticRefused(f"{name}=={version} resolves outside the allowlisted pypi registries: {index}")
        candidates: list[tuple[str, str, str]] = []
        for raw_wheel in item.get("wheels") or ():
            if not isinstance(raw_wheel, Mapping):
                continue
            url = str(raw_wheel.get("url") or "")
            algorithm, _, value = str(raw_wheel.get("hash") or "").partition(":")
            if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise HermeticRefused(f"{name}=={version} has a wheel without a sha256 hash in uv.lock")
            candidates.append((PurePosixPath(urlsplit(url).path).name, url, value))
        chosen = _select_wheel(candidates, name=name, version=version, supported=supported)
        if chosen is None:
            if isinstance(item.get("sdist"), Mapping):
                # lc-debt: sdists are refused rather than built; building runs repository-chosen build backends.
                raise HermeticRefused(
                    f"{name}=={version} has no wheel for the sandbox interpreter (cp{target.version[0]}"
                    f"{target.version[1]}); building it from an sdist is not supported hermetically"
                )
            raise HermeticRefused(f"{name}=={version} has no wheel compatible with the sandbox interpreter")
        filename, url, sha256 = chosen
        rule = _check_wheel_url(policy, url, name, index_rule)
        hosts.add(rule.host)
        wheels.append(PlannedWheel(name, version, url, sha256, filename))
    return PythonInstallPlan(
        source="uv.lock",
        wheels=tuple(wheels),
        lockfile_digest=python_dependency_digest(project),
        interpreter_digest=target.digest,
        project_paths=tuple(dict.fromkeys(project_paths)),
        hosts=tuple(sorted(hosts)),
    )


# --------------------------------------------------------------------------- #
# requirements*.txt (--require-hashes)
# --------------------------------------------------------------------------- #


@dataclass
class _Pin:
    name: str
    version: str
    hashes: set[str]
    origin: str


_GLOBAL_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--require-hashes", "--only-binary", "--prefer-binary", "--pre", "--no-binary"}
)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line:
            lines.append(line)
    if pending.strip():
        lines.append(pending.strip())
    return lines


def _parse_requirements(
    project: Path,
    path: Path,
    target: PythonTarget,
    pins: dict[str, _Pin],
    indexes: list[str],
    seen: set[Path],
) -> None:
    resolved = path.resolve()
    root = project.resolve()
    if resolved in seen:
        return
    if root not in resolved.parents or not path.is_file() or path.is_symlink():
        raise HermeticRefused(f"requirements file {path.name} must be a regular file inside the reviewed project")
    seen.add(resolved)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HermeticRefused(f"{path.name} is unreadable: {exc}") from exc
    requirements = _packaging("requirements")
    where = path.relative_to(project).as_posix()
    for line in _logical_lines(text):
        if line.startswith("-"):
            try:
                tokens = shlex.split(line)
            except ValueError as exc:
                raise HermeticRefused(f"{where}: cannot parse option line {line!r}") from exc
            option, _, inline = tokens[0].partition("=")
            value = inline or (tokens[1] if len(tokens) > 1 else "")
            if option in {"-r", "--requirement"}:
                _parse_requirements(project, path.parent / value, target, pins, indexes, seen)
            elif option in {"-c", "--constraint"}:
                # Constraints only narrow versions; every requirement here is already an exact hashed pin.
                continue
            elif option in {"-i", "--index-url"}:
                indexes.insert(0, value)
            elif option == "--extra-index-url":
                indexes.append(value)
            elif option == "--no-binary" and value not in {":none:", ""}:
                raise HermeticRefused(f"{where}: --no-binary {value} needs sdist builds, which are not hermetic")
            elif option not in _GLOBAL_OPTIONS:
                # lc-debt: -e/--editable, --find-links, --trusted-host and URL requirements are refused.
                raise HermeticRefused(f"{where}: option {option} is not supported hermetically")
            continue
        hashes = re.findall(r"--hash[=\s]+(\S+)", line)
        spec = re.sub(r"--hash[=\s]+\S+", "", line).strip()
        if re.search(r"(^|\s)--?[a-z]", spec):
            raise HermeticRefused(f"{where}: per-requirement options are not supported: {line[:200]}")
        try:
            requirement = requirements.Requirement(spec)
        except requirements.InvalidRequirement as exc:
            raise HermeticRefused(f"{where}: invalid requirement {spec[:200]!r}: {exc}") from exc
        if requirement.url:
            raise HermeticRefused(
                f"{where}: {requirement.name} is a direct URL requirement; only index pins are fetched"
            )
        if requirement.marker is not None and not requirement.marker.evaluate(target.marker_environment):
            continue
        exact = [item for item in requirement.specifier if item.operator in {"==", "==="} and "*" not in item.version]
        if len(exact) != 1 or len(requirement.specifier) != 1:
            raise HermeticRefused(f"{where}: {requirement.name} must be pinned with == (--require-hashes semantics)")
        sha256 = set()
        for value in hashes:
            algorithm, _, digest = value.partition(":")
            if algorithm not in _HASH_ALGORITHMS or not re.fullmatch(r"[0-9a-f]+", digest):
                raise HermeticRefused(f"{where}: {requirement.name} has an invalid --hash {value!r}")
            if algorithm == "sha256":
                sha256.add(digest)
        if not sha256:
            raise HermeticRefused(
                f"{where}: {requirement.name} has no sha256 --hash; hash-pinned requirements are required"
            )
        name = pypi_normalize(requirement.name)
        version = exact[0].version
        existing = pins.get(name)
        if existing is not None and existing.version != version:
            raise HermeticRefused(
                f"{name} is pinned to {existing.version} in {existing.origin} and {version} in {where}"
            )
        if existing is not None:
            existing.hashes &= sha256
            if not existing.hashes:
                raise HermeticRefused(f"{name}=={version} has disjoint --hash sets in {existing.origin} and {where}")
        else:
            pins[name] = _Pin(name, version, sha256, where)


class _AnchorParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None


def _index_files(body: bytes, content_type: str, page_url: str) -> list[tuple[str, str, str]]:
    """``(filename, absolute url, sha256)`` of every file an index page lists (PEP 691 JSON or PEP 503 HTML)."""

    files: list[tuple[str, str, str]] = []
    if "json" in content_type or ("html" not in content_type and body.lstrip().startswith(b"{")):
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise HermeticUnavailable(f"index page {page_url} is not valid JSON") from exc
        for item in data.get("files") or () if isinstance(data, Mapping) else ():
            if not isinstance(item, Mapping) or item.get("yanked"):
                continue
            hashes = item.get("hashes") if isinstance(item.get("hashes"), Mapping) else {}
            files.append(
                (
                    str(item.get("filename") or ""),
                    urljoin(page_url, str(item.get("url") or "")),
                    str(cast(Mapping[str, Any], hashes).get("sha256") or ""),
                )
            )
        return files
    parser = _AnchorParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    for href, text in parser.links:
        url, _, fragment = href.partition("#")
        algorithm, _, digest = fragment.partition("=")
        filename = text or PurePosixPath(urlsplit(url).path).name
        files.append((filename, urljoin(page_url, url), digest if algorithm == "sha256" else ""))
    return files


def _default_index(policy: HermeticPolicy) -> str:
    rules = policy.registries_for("pypi")
    rule = next((item for item in rules if "simple" in item.path_prefix), rules[0] if rules else None)
    if rule is None:
        raise HermeticRefused(f"no pypi registry is allowlisted by {policy.source} policy")
    port = "" if rule.port is None else f":{rule.port}"
    return f"{rule.scheme}://{rule.host}{port}{rule.path_prefix.rstrip('/')}/"


def plan_requirements(
    project: Path,
    policy: HermeticPolicy,
    target: PythonTarget,
    fetcher: _Fetcher,
) -> PythonInstallPlan:
    """Resolve hash-pinned ``requirements*.txt`` to exactly one hash-matching wheel each.

    Index pages are read from allowlisted registries only (this is fetch-stage
    work). With every pin exact and hash-bound, which index serves a file
    cannot change its bytes; the pinned-registry rule still applies.
    """

    policy.require_fetch("pypi")
    pins: dict[str, _Pin] = {}
    indexes: list[str] = []
    seen: set[Path] = set()
    for path in requirements_files(project):
        _parse_requirements(project, path, target, pins, indexes, seen)
    if len(pins) > MAX_PYTHON_PACKAGES:
        raise HermeticRefused(f"requirements list more than {MAX_PYTHON_PACKAGES} packages")
    indexes = list(dict.fromkeys(indexes or [_default_index(policy)]))
    index_rules: list[tuple[str, RegistryRule]] = []
    for index in indexes:
        rule = policy.registry_for(index, "pypi")
        if rule is None:
            raise HermeticRefused(f"requirements index {index} is not an allowlisted pypi registry")
        index_rules.append((index.rstrip("/") + "/", rule))
    supported = target.supported_tags()
    wheels: list[PlannedWheel] = []
    hosts: set[str] = set()
    for name, pin in sorted(pins.items()):
        listed: list[tuple[str, str, str]] = []
        chosen_rule: RegistryRule | None = None
        for index, rule in index_rules:
            try:
                body, content_type, final = fetcher.fetch_page(
                    f"{index}{name}/", max_bytes=MAX_INDEX_PAGE_BYTES, accept=_SIMPLE_ACCEPT
                )
            except HermeticUnavailable as exc:
                if "HTTP 404" in str(exc):
                    continue
                raise
            files = [item for item in _index_files(body, content_type, final) if item[2] in pin.hashes]
            if files:
                listed, chosen_rule = files, rule
                break
        chosen = _select_wheel(listed, name=name, version=pin.version, supported=supported)
        if chosen is None:
            if any(not filename.endswith(".whl") for filename, _url, _sha in listed):
                # lc-debt: sdists are refused rather than built; building runs repository-chosen build backends.
                raise HermeticRefused(
                    f"{name}=={pin.version}: only an sdist matches the pinned hashes; "
                    "building from source is not supported hermetically"
                )
            raise HermeticRefused(
                f"{name}=={pin.version}: no wheel matching the pinned hashes is compatible with the sandbox interpreter"
            )
        filename, url, sha256 = chosen
        rule = _check_wheel_url(policy, url, name, chosen_rule)
        hosts.add(rule.host)
        wheels.append(PlannedWheel(name, pin.version, url, sha256, filename))
    return PythonInstallPlan(
        source="requirements",
        wheels=tuple(wheels),
        lockfile_digest=python_dependency_digest(project),
        interpreter_digest=target.digest,
        hosts=tuple(sorted(hosts)),
    )


# --------------------------------------------------------------------------- #
# Offline build (runs inside bubblewrap with every namespace unshared)
# --------------------------------------------------------------------------- #

_PY_INSTALL_SCRIPT: Final[str] = r"""
import hashlib, io, json, os, re, stat, sys, sysconfig, venv, zipfile
plan = json.load(open(sys.argv[1], encoding="utf-8"))
wheel_root, venv_dir = sys.argv[2], sys.argv[3]
limit_total, limit_file, limit_entries = plan["max_total_bytes"], plan["max_file_bytes"], plan["max_entries"]
total = entries = 0
def fail(message):
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(3)
def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()
def inside(base, target):
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    return target == base or target.startswith(base + os.sep)
def confined(base, rel, what):
    rel = rel.replace("\\", "/")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if rel.startswith("/") or "\x00" in rel or ".." in parts or not parts:
        fail("unsafe path in " + what + ": " + rel)
    return os.path.join(base, *parts)
try:
    venv.EnvBuilder(with_pip=False, symlinks=True, clear=True).create(venv_dir)
except Exception as exc:
    fail("could not create the sandbox virtualenv: %s" % exc)
scheme = "venv" if "venv" in sysconfig.get_scheme_names() else "posix_prefix"
base_vars = {"base": venv_dir, "platbase": venv_dir, "installed_base": venv_dir, "installed_platbase": venv_dir}
paths = sysconfig.get_paths(scheme=scheme, vars=base_vars)
purelib, platlib, scripts = paths["purelib"], paths["platlib"], paths["scripts"]
targets = {"purelib": purelib, "platlib": platlib, "scripts": scripts,
           "headers": os.path.join(venv_dir, "include"), "data": venv_dir}
for directory in (purelib, platlib, scripts):
    os.makedirs(directory, exist_ok=True)
python = os.path.join(venv_dir, "bin", "python")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
SCRIPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
def locate(sha256):
    for name in sorted(os.listdir(wheel_root)):
        candidate = os.path.join(wheel_root, name, sha256 + ".whl")
        if os.path.isfile(candidate):
            return candidate
    return None
for wheel in plan["wheels"]:
    source = locate(wheel["sha256"])
    if source is None:
        fail("missing fetched wheel for " + wheel["name"])
    data = open(source, "rb").read()
    if hashlib.sha256(data).hexdigest() != wheel["sha256"]:
        fail("fetched wheel changed after verification: " + wheel["name"])
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = archive.infolist()
    except zipfile.BadZipFile:
        fail("wheel is not a zip archive: " + wheel["name"])
    roots = {item.filename.split("/", 1)[0] for item in members}
    dist_infos = sorted(root for root in roots if root.endswith(".dist-info"))
    if len(dist_infos) != 1:
        fail("wheel must contain exactly one .dist-info: " + wheel["name"])
    dist_info = dist_infos[0]
    dist_name, _, dist_version = dist_info[: -len(".dist-info")].rpartition("-")
    file_version = wheel["filename"].split("-")[1] if wheel["filename"].count("-") >= 4 else ""
    if normalize(dist_name) != normalize(wheel["name"]) or dist_version != file_version:
        fail("wheel for %s==%s contains %s %s (lookalike package)" % (wheel["name"], wheel["version"], dist_name, dist_version))
    try:
        wheel_meta = archive.read(dist_info + "/WHEEL").decode("utf-8")
    except KeyError:
        fail("wheel has no WHEEL metadata: " + wheel["name"])
    purelib_root = re.search(r"(?im)^Root-Is-Purelib:\s*true\s*$", wheel_meta) is not None
    root_dir = purelib if purelib_root else platlib
    data_prefix = dist_info[: -len(".dist-info")] + ".data/"
    for member in members:
        entries += 1
        if entries > limit_entries:
            fail("dependency environment has too many entries")
        name = member.filename
        if name.endswith("/"):
            continue
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            continue
        if member.file_size > limit_file:
            fail("wheel member is too large: " + wheel["name"])
        total += member.file_size
        if total > limit_total:
            fail("dependency environment exceeds the size limit")
        kind = ""
        if name.startswith(data_prefix):
            kind, _, rest = name[len(data_prefix):].partition("/")
            if kind not in targets or not rest:
                fail("unknown wheel data directory in " + wheel["name"] + ": " + name)
            target = confined(targets[kind], rest, wheel["name"])
        else:
            target = confined(root_dir, name, wheel["name"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not inside(venv_dir, os.path.dirname(target)):
            fail("wheel member escapes the environment: " + wheel["name"] + ": " + name)
        body = archive.read(member)
        if kind == "scripts" and body.startswith(b"#!python"):
            body = b"#!" + python.encode() + body[len(b"#!python"):]
        if os.path.lexists(target):
            os.unlink(target)
        with open(target, "wb") as output:
            output.write(body)
        os.chmod(target, 0o755 if (mode & 0o111 or kind == "scripts") else 0o644)
    try:
        points = archive.read(dist_info + "/entry_points.txt").decode("utf-8")
    except KeyError:
        points = ""
    section = ""
    for raw in points.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section not in ("console_scripts", "gui_scripts") or "=" not in line:
            continue
        script, _, reference = (part.strip() for part in line.partition("="))
        reference = reference.split("[", 1)[0].strip()
        module, _, attribute = reference.partition(":")
        module, attribute = module.strip(), attribute.strip()
        if not SCRIPT.match(script) or not IDENT.match(module) or (attribute and not IDENT.match(attribute)):
            continue
        head = attribute.split(".", 1)[0] if attribute else ""
        call = attribute if attribute else module
        lines = ["#!" + python, "import sys"]
        lines.append(("from %s import %s" % (module, head)) if attribute else ("import %s" % module))
        lines += ["if __name__ == '__main__':", "    sys.exit(%s())" % call]
        path = os.path.join(scripts, script)
        if os.path.lexists(path):
            os.unlink(path)
        with open(path, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
        os.chmod(path, 0o755)
    with open(os.path.join(root_dir, dist_info, "INSTALLER"), "w", encoding="utf-8") as output:
        output.write("lemoncrow-hermetic\n")
if plan["project_paths"]:
    with open(os.path.join(purelib, "_lemoncrow_project.pth"), "w", encoding="utf-8") as output:
        output.write("\n".join(plan["project_paths"]) + "\n")
print(json.dumps({"ok": True, "packages": len(plan["wheels"]), "bytes": total}))
"""


@dataclass(frozen=True, slots=True)
class ProvisionedPython:
    """A built environment: mount ``root`` read-only at :data:`PYTHON_ENV_MOUNT`."""

    root: Path
    plan: PythonInstallPlan
    stages: tuple[StageOutcome, ...]


def plan_python_install(
    project: Path,
    policy: HermeticPolicy,
    target: PythonTarget,
    fetcher: _Fetcher,
    *,
    sandbox_project: str,
) -> PythonInstallPlan:
    state = python_project_state(project)
    if state == "uv.lock":
        return plan_uv_lock(project, policy, target, sandbox_project=sandbox_project)
    if state == "requirements":
        return plan_requirements(project, policy, target, fetcher)
    if state == "unlocked":
        raise HermeticRefused(
            "pyproject.toml declares dependencies but the project has no uv.lock or hash-pinned requirements*.txt"
        )
    raise HermeticRefused("the reviewed project declares no Python dependencies")


def provision_python_dependencies(
    project: Path,
    *,
    policy: HermeticPolicy,
    cache: DependencyCache,
    sandbox_project: str,
    fetcher: _Fetcher | None = None,
    target: PythonTarget | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProvisionedPython:
    """Fetch (registry-only, wheels only) then build (offline) the reviewed Python environment."""

    started = time.monotonic()
    interpreter = target or probe_sandbox_python()
    client: _Fetcher = fetcher or RegistryFetcher(policy, should_cancel=should_cancel)
    plan = plan_python_install(project, policy, interpreter, client, sandbox_project=sandbox_project)
    blocked = tuple(f"skipped {item}" for item in plan.skipped)
    existing = cache.tree(plan.digest, content="python")
    if existing is not None:
        hit = (
            StageOutcome(
                "fetch", "skipped", network="registry_only", detail="Python environment cache hit", cache="hit"
            ),
            StageOutcome("build", "passed", detail="Python environment cache hit", cache="hit", blocked=blocked),
        )
        return ProvisionedPython(existing, plan, hit)

    missing = [wheel for wheel in plan.wheels if cache.wheel(wheel.sha256) is None]
    fetched_bytes = 0
    guard = threading.Lock()

    def fetch_one(wheel: PlannedWheel) -> None:
        nonlocal fetched_bytes
        if should_cancel is not None and should_cancel():
            raise HermeticUnavailable("dependency fetch cancelled by revoked authority")
        payload = client.fetch(wheel.url, max_bytes=MAX_TARBALL_BYTES)
        if should_cancel is not None and should_cancel():
            raise HermeticUnavailable("dependency fetch cancelled by revoked authority")
        if hashlib.sha256(payload).hexdigest() != wheel.sha256:
            raise HermeticRefused(f"downloaded wheel for {wheel.name}=={wheel.version} does not match its pinned hash")
        with guard:
            fetched_bytes += len(payload)
            if fetched_bytes > MAX_TOTAL_FETCH_BYTES:
                raise HermeticRefused(f"dependency fetch exceeds {MAX_TOTAL_FETCH_BYTES} bytes")
        cache.store_wheel(wheel.sha256, payload)

    if missing:
        with ThreadPoolExecutor(max_workers=min(FETCH_CONCURRENCY, len(missing))) as pool:
            list(pool.map(fetch_one, missing))
    fetch_stage = StageOutcome(
        "fetch",
        "passed",
        network="registry_only",
        detail=(
            f"python ({plan.source}): {len(missing)} of {len(plan.wheels)} wheel(s) fetched from "
            f"{', '.join(plan.hosts) if missing else 'cache'}; wheels only, no repository code ran"
        ),
        duration_ms=_stage_ms(started),
        blocked=blocked,
        cache="miss" if missing else "hit",
    )
    if should_cancel is not None and should_cancel():
        raise HermeticUnavailable("dependency build cancelled by revoked authority")
    build_started = time.monotonic()
    with _tree_lock(f"{cache.root}:{cache.namespace}:{plan.digest}"):
        existing = cache.tree(plan.digest, content="python")
        if existing is not None:
            return ProvisionedPython(existing, plan, (fetch_stage, StageOutcome("build", "passed", cache="hit")))
        staging, final = cache.new_tree_dir(plan.digest)
        try:
            (staging / "python").mkdir()
            report = _run_offline_python_build(
                plan, interpreter, cache, staging / "python", should_cancel=should_cancel
            )
            (staging / "tree.json").write_text(
                json.dumps({"plan_digest": plan.digest, "namespace": cache.namespace, "created_at": time.time()}),
                encoding="utf-8",
            )
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            staging.replace(final)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    cache.prune()
    build_stage = StageOutcome(
        "build",
        "passed",
        detail=f"python: {report.get('packages', 0)} wheel(s) installed offline into a virtualenv; nothing built",
        duration_ms=_stage_ms(build_started),
        blocked=blocked,
        cache="miss",
    )
    return ProvisionedPython(final / "python", plan, (fetch_stage, build_stage))


def _run_offline_python_build(
    plan: PythonInstallPlan,
    target: PythonTarget,
    cache: DependencyCache,
    out: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> Mapping[str, Any]:
    from lemoncrow.pro.capabilities.review.sandbox_limits import (
        SandboxCapacityBusy,
        SandboxExecutionCancelled,
        run_limited,
        run_limited_bounded,
    )

    plan_file = out.parent / f"{out.parent.name}.python-plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "max_total_bytes": MAX_TOTAL_FETCH_BYTES * 4,
                "max_file_bytes": MAX_TARBALL_BYTES,
                "max_entries": 500_000,
                "project_paths": list(plan.project_paths),
                "wheels": [
                    {"name": wheel.name, "version": wheel.version, "sha256": wheel.sha256, "filename": wheel.filename}
                    for wheel in plan.wheels
                ],
            }
        ),
        encoding="utf-8",
    )
    command = offline_bwrap_prefix()
    command.extend(("--ro-bind", str(plan_file), "/lemoncrow-plan.json", "--dir", "/lemoncrow-wheels"))
    for index, directory in enumerate(cache.readable_wheel_dirs()):
        if directory.is_dir():
            command.extend(("--ro-bind", str(directory), f"/lemoncrow-wheels/{index}"))
    command.extend(
        (
            "--bind",
            str(out),
            PYTHON_ENV_MOUNT,
            "--chdir",
            PYTHON_ENV_MOUNT,
            "--",
            target.executable,
            "-I",
            "-S",
            "-c",
            _PY_INSTALL_SCRIPT,
            "/lemoncrow-plan.json",
            "/lemoncrow-wheels",
            PYTHON_VENV,
        )
    )
    try:
        if should_cancel is None:
            proc = run_limited(
                command,
                timeout=INSTALL_TIMEOUT_SECONDS,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env={"PATH": _SANDBOX_PATH},
            )
        else:
            proc = run_limited_bounded(
                command,
                timeout=INSTALL_TIMEOUT_SECONDS,
                max_output_bytes=65536,
                should_cancel=should_cancel,
                env={"PATH": _SANDBOX_PATH},
            )
    except SandboxExecutionCancelled as exc:
        raise HermeticUnavailable("offline Python environment build cancelled by revoked authority") from exc
    except SandboxCapacityBusy as exc:
        raise HermeticUnavailable(str(exc)) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HermeticUnavailable(f"offline Python environment build could not run: {exc}") from exc
    finally:
        plan_file.unlink(missing_ok=True)
    lines = [line for line in (proc.stdout or "").splitlines() if line.startswith("{")]
    try:
        report = json.loads(lines[-1]) if lines else {}
    except ValueError:
        report = {}
    if proc.returncode != 0 or not report.get("ok"):
        error = str(
            report.get("error")
            or ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
            or f"exit {proc.returncode}"
        )
        if "lookalike" in error or "escapes" in error or "unsafe" in error:
            raise HermeticRefused(f"offline Python environment build refused: {error}")
        raise HermeticUnavailable(f"offline Python environment build failed: {error}")
    return cast(Mapping[str, Any], report)


__all__ = [
    "PYTHON_ENV_MOUNT",
    "PYTHON_VENV",
    "PlannedWheel",
    "ProvisionedPython",
    "PythonInstallPlan",
    "PythonTarget",
    "plan_python_install",
    "plan_requirements",
    "plan_uv_lock",
    "probe_sandbox_python",
    "provision_python_dependencies",
    "python_dependency_digest",
    "python_project_state",
    "requirements_files",
]
