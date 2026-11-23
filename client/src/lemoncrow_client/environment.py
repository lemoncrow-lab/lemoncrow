"""The environment definition: ``.lemoncrow/environment.toml``, one per repository.

Workspaces and Review both read this file (``docs/architecture/workspaces-and-review.md``). The schema is versioned
and strict: a section is accepted only once something reads it, and unknown keys are refused with the file and the
key in the message. Version 1 holds:

* ``checks``: what ``lc verify`` and Review run, with an optional ``kind``, ``timeout`` and the ``needs`` (services)
  each check runs against;
* ``toolchain``: the base image, pinned by digest;
* ``dependencies``: lockfiles and the command that installs them;
* ``services``: images with a readiness probe, started for the checks that need them;
* ``previews``: commands that serve the application, with the path that answers once it is up;
* ``needs``: the network hosts and secret names the repository asks for (Workspaces grants them within an
  organization ceiling; Review grants none).

The module also computes the environment fingerprint both products record for a check (:class:`Fingerprint`), and
reads and writes the record ``lc verify`` leaves for a later Review (:func:`write_verify_record`). Standard library
only: the public client ships it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

__all__ = [
    "CHECK_KINDS",
    "ENVIRONMENT_FILE",
    "FINGERPRINT_VERSION",
    "MAX_CHECK_TIMEOUT",
    "VERIFY_RECORD_DIR",
    "VERIFY_RECORD_KEEP",
    "Check",
    "DefinitionError",
    "Dependency",
    "Environment",
    "Fingerprint",
    "Needs",
    "Preview",
    "Service",
    "Toolchain",
    "check_digest",
    "image_digest",
    "is_attr_pathspec",
    "load_environment",
    "lockfile_digest",
    "parse_environment",
    "read_verify_record",
    "write_verify_record",
]

ENVIRONMENT_FILE: Final[str] = ".lemoncrow/environment.toml"
#: A check's ``kind``: how Review files its result (``checks.test`` and so on). Without one a check is a build.
CHECK_KINDS: Final[tuple[str, ...]] = ("test", "lint", "format", "typecheck", "build", "precommit")
#: The longest ``timeout`` a check may ask for, in seconds.
MAX_CHECK_TIMEOUT: Final[int] = 3600
FINGERPRINT_VERSION: Final[int] = 1
#: Under the Git common directory: one ``<commit>.json`` per commit ``lc verify`` checked.
VERIFY_RECORD_DIR: Final[str] = "lemoncrow/verify"
#: The newest records kept; writing one removes the older ones past this count.
VERIFY_RECORD_KEEP: Final[int] = 64

_VERSION: Final[int] = 1
_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {"version", "checks", "toolchain", "dependencies", "services", "previews", "needs"}
)
_CHECK_KEYS: Final[frozenset[str]] = frozenset({"id", "paths", "run", "kind", "timeout", "needs"})
_TOOLCHAIN_KEYS: Final[frozenset[str]] = frozenset({"image"})
_DEPENDENCY_KEYS: Final[frozenset[str]] = frozenset({"lockfile", "install"})
_SERVICE_KEYS: Final[frozenset[str]] = frozenset({"image", "ready", "env", "sql"})
#: A service's ``sql``: the SQL engine it serves, so Review can read that server's own statement log.
SQL_ENGINES: Final[tuple[str, ...]] = ("postgres", "mysql")
_ENV_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_PREVIEW_KEYS: Final[frozenset[str]] = frozenset({"run", "ready"})
_NEEDS_KEYS: Final[frozenset[str]] = frozenset({"network", "secrets"})
_CHECK_ID: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]*")
# A container image reference: optional registry (host[:port]), repository path, optional tag, optional digest.
_IMAGE: Final[re.Pattern[str]] = re.compile(
    r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"(?:@sha256:[0-9a-f]{64})?"
)
_DIGEST: Final[re.Pattern[str]] = re.compile(r"@(sha256:[0-9a-f]{64})$")
_HOST: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_SECRET: Final[re.Pattern[str]] = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_COMMIT: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# The pathspec magic `attr:` (long form only: Git has no short form for it).
_ATTR_MAGIC: Final[re.Pattern[str]] = re.compile(r"^:\((?:[^)]*,)?\s*attr:")


class DefinitionError(ValueError):
    """The definition file cannot be read or breaks the schema; the message names the file and the key."""


@dataclass(frozen=True)
class Check:
    """Runs ``run`` from the repository root when a change touches ``paths`` (Git pathspecs).

    ``kind`` is ``""`` when the file gives none (a build to Review). ``timeout`` is ``None`` when the file gives
    none: ``lc verify`` then sets no limit and Review uses its own ceiling. ``needs`` names the services it runs
    against.
    """

    id: str
    paths: tuple[str, ...]
    run: str
    kind: str = ""
    timeout: int | None = None
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Toolchain:
    """The base image checks and sandboxes are built from."""

    image: str

    @property
    def digest(self) -> str:
        """``sha256:<hex>`` when the image is pinned by digest, else ``""``."""

        return image_digest(self.image)


@dataclass(frozen=True)
class Dependency:
    """A lockfile (relative to the repository root) and the command that installs what it locks."""

    lockfile: str
    install: str


@dataclass(frozen=True)
class Service:
    """An image started for the checks that ``need`` it; ``ready`` exits 0 once it serves.

    ``env`` holds non-secret settings the image reads (they travel on the engine's argv and in the repository).
    ``sql`` names the SQL engine the image serves (``postgres``, ``mysql``), or ``""``.
    """

    name: str
    image: str
    ready: str
    env: tuple[tuple[str, str], ...] = ()
    sql: str = ""

    @property
    def digest(self) -> str:
        return image_digest(self.image)


@dataclass(frozen=True)
class Preview:
    """``run`` serves the application on ``$PORT``; ``ready`` is the path that answers once it is up."""

    name: str
    run: str
    ready: str


@dataclass(frozen=True)
class Needs:
    """What the repository asks for. Asking grants nothing: the product decides."""

    network: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Environment:
    checks: tuple[Check, ...]
    toolchain: Toolchain | None = None
    dependencies: tuple[Dependency, ...] = ()
    services: tuple[Service, ...] = ()
    previews: tuple[Preview, ...] = ()
    needs: Needs = field(default_factory=Needs)

    def service(self, name: str) -> Service | None:
        return next((service for service in self.services if service.name == name), None)

    def check(self, check_id: str) -> Check | None:
        return next((check for check in self.checks if check.id == check_id), None)

    def unpinned_images(self) -> tuple[str, ...]:
        """Images not pinned by digest, as written: each one makes the fingerprint unreproducible."""

        images = ([self.toolchain.image] if self.toolchain is not None else []) + [s.image for s in self.services]
        return tuple(image for image in dict.fromkeys(images) if not image_digest(image))

    def fingerprint(self, check: Check, read_lockfile: Callable[[str], bytes | None]) -> Fingerprint:
        """The environment *check* runs in, as this definition says, with lockfile bytes from *read_lockfile*."""

        return Fingerprint(
            image_digest=self.toolchain.digest if self.toolchain is not None else "",
            toolchain=self.toolchain.image if self.toolchain is not None else "",
            lockfile_digest=lockfile_digest(self, read_lockfile),
            check_digest=check_digest(self, check),
        )


def image_digest(image: str) -> str:
    """``sha256:<hex>`` of an image reference pinned by digest (``name@sha256:...``), else ``""``."""

    found = _DIGEST.search(image)
    return found.group(1) if found else ""


def is_attr_pathspec(spec: str) -> bool:
    """Whether *spec* uses Git's ``attr:`` magic, which reads ``.gitattributes`` to match."""

    return _ATTR_MAGIC.match(spec) is not None


def load_environment(repo_root: Path) -> Environment | None:
    """The repository's definition, or None when it has no ``environment.toml``."""

    path = repo_root / ENVIRONMENT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise DefinitionError(f"{ENVIRONMENT_FILE}: cannot read: {exc}") from exc
    return parse_environment(text, source=ENVIRONMENT_FILE)


def parse_environment(text: str, *, source: str = ENVIRONMENT_FILE) -> Environment:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DefinitionError(f"{source}: not valid TOML: {exc}") from exc
    _refuse_unknown(data, _TOP_KEYS, source)
    version = data.get("version")
    if type(version) is not int or version != _VERSION:
        raise DefinitionError(f"{source}: version must be {_VERSION}, got {version!r}")
    toolchain = _toolchain(data["toolchain"], f"{source}: toolchain") if "toolchain" in data else None
    dependencies = _dependencies(data.get("dependencies", []), source)
    services = tuple(
        _service(name, raw, f"{source}: services.{name}")
        for name, raw in _named_tables(data.get("services", {}), "services", source).items()
    )
    previews = tuple(
        _preview(name, raw, f"{source}: previews.{name}")
        for name, raw in _named_tables(data.get("previews", {}), "previews", source).items()
    )
    needs = _needs(data["needs"], f"{source}: needs") if "needs" in data else Needs()
    raw_checks = data.get("checks", [])
    if not isinstance(raw_checks, list):
        raise DefinitionError(f"{source}: checks must be an array of tables ([[checks]])")
    declared = {service.name for service in services}
    checks: list[Check] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_checks):
        check = _check(raw, f"{source}: checks[{index}]", declared)
        if check.id in seen:
            raise DefinitionError(f"{source}: checks[{index}]: duplicate id {check.id!r}")
        seen.add(check.id)
        checks.append(check)
    return Environment(
        checks=tuple(checks),
        toolchain=toolchain,
        dependencies=dependencies,
        services=services,
        previews=previews,
        needs=needs,
    )


def _check(raw: Any, where: str, services: set[str]) -> Check:
    if not isinstance(raw, dict):
        raise DefinitionError(f"{where}: must be a table")
    _refuse_unknown(raw, _CHECK_KEYS, where)
    check_id = raw.get("id")
    if not isinstance(check_id, str) or _CHECK_ID.fullmatch(check_id) is None:
        raise DefinitionError(f"{where}: id must match {_CHECK_ID.pattern}, got {check_id!r}")
    where = f"{where} ({check_id})"
    paths = raw.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        raise DefinitionError(f"{where}: paths must be a non-empty array of Git pathspecs")
    run = raw.get("run")
    if not isinstance(run, str) or not run.strip():
        raise DefinitionError(f"{where}: run must be a non-empty shell command")
    kind = raw.get("kind", "")
    if "kind" in raw and (not isinstance(kind, str) or kind not in CHECK_KINDS):
        raise DefinitionError(f"{where}: kind must be one of {', '.join(CHECK_KINDS)}, got {kind!r}")
    timeout = raw.get("timeout")
    if "timeout" in raw and (type(timeout) is not int or not 1 <= timeout <= MAX_CHECK_TIMEOUT):
        raise DefinitionError(f"{where}: timeout must be a whole number of seconds from 1 to {MAX_CHECK_TIMEOUT}")
    needs = _strings(raw.get("needs", []), f"{where}: needs", "service names")
    for name in needs:
        if name not in services:
            raise DefinitionError(f"{where}: needs names {name!r}, which is not declared under [services]")
    return Check(id=check_id, paths=tuple(paths), run=run, kind=kind, timeout=timeout, needs=needs)


def _toolchain(raw: Any, where: str) -> Toolchain:
    if not isinstance(raw, dict):
        raise DefinitionError(f"{where}: must be a table ([toolchain])")
    _refuse_unknown(raw, _TOOLCHAIN_KEYS, where)
    return Toolchain(image=_image(raw.get("image"), f"{where}: image"))


def _dependencies(raw: Any, source: str) -> tuple[Dependency, ...]:
    if not isinstance(raw, list):
        raise DefinitionError(f"{source}: dependencies must be an array of tables ([[dependencies]])")
    out: list[Dependency] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{source}: dependencies[{index}]"
        if not isinstance(item, dict):
            raise DefinitionError(f"{where}: must be a table")
        _refuse_unknown(item, _DEPENDENCY_KEYS, where)
        lockfile = item.get("lockfile")
        if not isinstance(lockfile, str) or not _relative_file(lockfile):
            raise DefinitionError(f"{where}: lockfile must be a file path relative to the repository root")
        if lockfile in seen:
            raise DefinitionError(f"{where}: duplicate lockfile {lockfile!r}")
        seen.add(lockfile)
        install = item.get("install")
        if not isinstance(install, str) or not install.strip():
            raise DefinitionError(f"{where} ({lockfile}): install must be a non-empty shell command")
        out.append(Dependency(lockfile=lockfile, install=install))
    return tuple(out)


def _named_tables(raw: Any, section: str, source: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not all(isinstance(value, dict) for value in raw.values()):
        raise DefinitionError(f"{source}: {section} must be tables ([{section}.<name>])")
    for name in raw:
        if _CHECK_ID.fullmatch(name) is None:
            raise DefinitionError(f"{source}: {section}.{name}: the name must match {_CHECK_ID.pattern}")
    return raw


def _service(name: str, raw: dict[str, Any], where: str) -> Service:
    _refuse_unknown(raw, _SERVICE_KEYS, where)
    image = _image(raw.get("image"), f"{where}: image")
    ready = raw.get("ready")
    if not isinstance(ready, str) or not ready.strip():
        raise DefinitionError(f"{where}: ready must be a non-empty shell command that exits 0 once the service serves")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and _ENV_NAME.fullmatch(key) and isinstance(value, str) for key, value in env.items()
    ):
        raise DefinitionError(f'{where}: env must be a table of NAME = "value" strings')
    sql = raw.get("sql", "")
    if sql != "" and sql not in SQL_ENGINES:
        raise DefinitionError(f"{where}: sql must be one of {', '.join(SQL_ENGINES)}")
    return Service(name=name, image=image, ready=ready, env=tuple(sorted(env.items())), sql=str(sql))


def _preview(name: str, raw: dict[str, Any], where: str) -> Preview:
    _refuse_unknown(raw, _PREVIEW_KEYS, where)
    run = raw.get("run")
    if not isinstance(run, str) or not run.strip():
        raise DefinitionError(f"{where}: run must be a non-empty shell command")
    ready = raw.get("ready")
    if (
        not isinstance(ready, str)
        or not ready.startswith("/")
        or ready.startswith("//")
        or len(ready) > 2048
        or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in ready)
    ):
        raise DefinitionError(f'{where}: ready must be a path on the preview, such as "/" or "/health"')
    return Preview(name=name, run=run, ready=ready)


def _needs(raw: Any, where: str) -> Needs:
    if not isinstance(raw, dict):
        raise DefinitionError(f"{where}: must be a table ([needs])")
    _refuse_unknown(raw, _NEEDS_KEYS, where)
    network = _strings(raw.get("network", []), f"{where}: network", "host names")
    for host in network:
        if len(host) > 253 or _HOST.fullmatch(host) is None:
            raise DefinitionError(f"{where}: network: {host!r} is not a lowercase host name")
    secrets = _strings(raw.get("secrets", []), f"{where}: secrets", "environment variable names")
    for name in secrets:
        if _SECRET.fullmatch(name) is None:
            raise DefinitionError(f"{where}: secrets: {name!r} is not an environment variable name ([A-Z_][A-Z0-9_]*)")
    return Needs(network=network, secrets=secrets)


def _image(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or len(raw) > 512 or _IMAGE.fullmatch(raw) is None:
        raise DefinitionError(f"{where}: must be an image reference, pinned as name@sha256:<digest>, got {raw!r}")
    return raw


def _strings(raw: Any, where: str, what: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise DefinitionError(f"{where}: must be an array of {what}")
    if len(set(raw)) != len(raw):
        duplicate = next(item for item in raw if raw.count(item) > 1)
        raise DefinitionError(f"{where}: {duplicate!r} is listed twice")
    return tuple(raw)


def _relative_file(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value.strip())
        and value == value.strip()
        and not path.is_absolute()
        and "\\" not in value
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _refuse_unknown(table: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise DefinitionError(f"{where}: unknown key {unknown[0]!r} (allowed: {', '.join(sorted(allowed))})")


# --------------------------------------------------------------------------- #
# The environment fingerprint, computed the same way by lc verify and Review
# --------------------------------------------------------------------------- #


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def check_digest(environment: Environment, check: Check) -> str:
    """``sha256:<hex>`` of what *check* runs: its command, paths, kind, timeout and the services it needs.

    The id is presentation and is left out; a needed service counts by its image and readiness probe.
    """

    needed = {name: environment.service(name) for name in check.needs}
    return _sha256(
        {
            "v": FINGERPRINT_VERSION,
            "run": check.run,
            "paths": list(check.paths),
            "kind": check.kind,
            "timeout": check.timeout,
            "needs": {
                name: (_service_identity(svc) if svc is not None else None) for name, svc in sorted(needed.items())
            },
        }
    )


def _service_identity(service: Service) -> dict[str, Any]:
    """What makes two definitions of one service the same; settings count only when a definition has them."""

    identity: dict[str, Any] = {"image": service.image, "ready": service.ready}
    if service.env:
        identity["env"] = dict(service.env)
    if service.sql:
        identity["sql"] = service.sql
    return identity


def lockfile_digest(environment: Environment, read_lockfile: Callable[[str], bytes | None]) -> str:
    """``sha256:<hex>`` over every declared lockfile's bytes (a missing one counts as missing); ``""`` without any."""

    if not environment.dependencies:
        return ""
    digests: dict[str, str | None] = {}
    for dependency in environment.dependencies:
        payload = read_lockfile(dependency.lockfile)
        digests[dependency.lockfile] = None if payload is None else "sha256:" + hashlib.sha256(payload).hexdigest()
    return _sha256({"lockfiles": digests})


_FINGERPRINT_FIELDS: Final[tuple[str, ...]] = ("image_digest", "toolchain", "lockfile_digest", "check_digest")


@dataclass(frozen=True)
class Fingerprint:
    """The environment one check runs in, as the definition says it: image digest, toolchain, lockfiles, check.

    ``lc verify`` records it for each check it runs and Review computes it for each check it runs, with the same
    code, so "passed for the agent, failed for Review" can point at the environment first.
    """

    image_digest: str = ""
    toolchain: str = ""
    lockfile_digest: str = ""
    check_digest: str = ""
    version: int = FINGERPRINT_VERSION

    @property
    def reproducible(self) -> bool:
        """False when the toolchain image is not pinned by digest: the same text may name another image later."""

        return not self.toolchain or bool(self.image_digest)

    @property
    def digest(self) -> str:
        return "envdef:" + _sha256({"v": self.version, **{name: getattr(self, name) for name in _FINGERPRINT_FIELDS}})

    def differences(self, other: Fingerprint) -> tuple[str, ...]:
        """The components that differ (``check_digest``, ``lockfile_digest`` ...), in a fixed order."""

        if self.version != other.version:
            return ("version",)
        return tuple(name for name in _FINGERPRINT_FIELDS if getattr(self, name) != getattr(other, name))

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"version": self.version, **{name: getattr(self, name) for name in _FINGERPRINT_FIELDS}}
        wire["digest"] = self.digest
        wire["reproducible"] = self.reproducible
        return wire

    @classmethod
    def from_wire(cls, raw: object) -> Fingerprint | None:
        """A recorded fingerprint, or ``None`` when it is malformed or of another version."""

        if not isinstance(raw, Mapping) or raw.get("version") != FINGERPRINT_VERSION:
            return None
        values = {name: raw.get(name) for name in _FINGERPRINT_FIELDS}
        if not all(isinstance(value, str) and len(value) <= 1024 for value in values.values()):
            return None
        fingerprint = cls(
            image_digest=str(values["image_digest"]),
            toolchain=str(values["toolchain"]),
            lockfile_digest=str(values["lockfile_digest"]),
            check_digest=str(values["check_digest"]),
        )
        claimed = raw.get("digest")
        if claimed is not None and claimed != fingerprint.digest:
            return None
        return fingerprint


# --------------------------------------------------------------------------- #
# The record lc verify leaves for a later Review
# --------------------------------------------------------------------------- #


def _record_path(git_common_dir: Path, commit: str) -> Path | None:
    return git_common_dir / VERIFY_RECORD_DIR / f"{commit}.json" if _COMMIT.fullmatch(commit) else None


def write_verify_record(
    git_common_dir: Path,
    commit: str,
    checks: Mapping[str, tuple[Fingerprint, str]],
    *,
    dirty: bool,
    keep: int = VERIFY_RECORD_KEEP,
    executor: str = "",
) -> Path | None:
    """Record which fingerprint each check ran under and how it ended (``passed``, ``failed``, ``timed_out``).

    The record is ``<git common dir>/lemoncrow/verify/<commit>.json``, replaced atomically; *dirty* says the checks
    also saw uncommitted work. Only the newest *keep* records stay. Returns the path, or ``None`` for a bad commit.
    *executor* names where the checks ran when that is not the author's machine (a Workspaces server writes
    ``workspace_task``); every record is self-reported either way.
    """

    path = _record_path(git_common_dir, commit)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": FINGERPRINT_VERSION,
        "commit": commit,
        "dirty": dirty,
        "recorded_at": int(time.time()),
        "checks": {
            check_id: {"status": status, "fingerprint": fingerprint.to_wire()}
            for check_id, (fingerprint, status) in sorted(checks.items())
        },
    }
    if executor:
        document["executor"] = executor
    handle, temporary = tempfile.mkstemp(prefix=".verify-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    records = sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
    for stale in records[max(1, keep) :]:
        if stale != path:
            stale.unlink(missing_ok=True)
    return path


def read_verify_record(git_common_dir: Path, commit: str) -> dict[str, Any] | None:
    """The record :func:`write_verify_record` left for *commit*: ``{"dirty", "checks": {id: {status, fingerprint}}}``.

    ``None`` when there is none or it cannot be read; a check whose fingerprint is malformed is left out.
    """

    path = _record_path(git_common_dir, commit)
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, Mapping) or raw.get("version") != FINGERPRINT_VERSION or raw.get("commit") != commit:
        return None
    rows = raw.get("checks")
    checks: dict[str, dict[str, Any]] = {}
    for check_id, row in (rows.items() if isinstance(rows, Mapping) else ()):
        fingerprint = Fingerprint.from_wire(row.get("fingerprint")) if isinstance(row, Mapping) else None
        status = row.get("status") if isinstance(row, Mapping) else None
        if fingerprint is not None and isinstance(status, str) and isinstance(check_id, str):
            checks[check_id] = {"status": status, "fingerprint": fingerprint}
    record: dict[str, Any] = {"commit": commit, "dirty": raw.get("dirty") is True, "checks": checks}
    executor = raw.get("executor")
    if isinstance(executor, str) and executor:
        record["executor"] = executor[:64]
    return record
