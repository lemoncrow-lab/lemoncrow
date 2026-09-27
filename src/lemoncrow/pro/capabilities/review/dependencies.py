"""Semantic dependency review for immutable Review Snapshots.

Supported manifests/locks are normalized into component changes so generated
lockfile churn does not become the human judgment unit.  Parsing is local and
deterministic; no package manager, network advisory service, or proprietary
supply-chain score is introduced here.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections import Counter, deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewUnit
from lemoncrow.pro.capabilities.review.supply_chain import (
    inspect_sbom,
    parse_evidence_metadata,
    vulnerability_findings,
)
from lemoncrow.pro.capabilities.review.units import unit_key

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
_CARGO_DEP_NAME = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
# Dependency metadata can legitimately exceed the generic source-analysis blob
# cap (large lockfiles are common). Keep this bounded and purpose-specific.
MAX_SEMANTIC_DEPENDENCY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class DependencyComponent:
    ecosystem: str
    identity: str
    name: str
    version: str = ""
    source: str = ""
    integrity: str = ""
    license: str = ""
    dependencies: tuple[str, ...] = ()
    # Canonical JSON of the exact lock entry. Human judgment covers these bytes,
    # not just the summarized fields above.
    raw: str = ""


@dataclass(frozen=True)
class _LockCoverage:
    """Complete partition of one parsed lockfile for human-judgment purposes.

    Every lock entry attributable to a third-party package lands in ``copies``
    under that package's normalized name; everything else (root/workspace
    entries, entries the component parser skips, top-level metadata) stays in
    ``residual``. A lockfile's raw hunks may only leave the review denominator
    because this partition is complete.
    """

    components: dict[str, DependencyComponent] = field(default_factory=dict)
    copies: dict[str, tuple[DependencyComponent, ...]] = field(default_factory=dict)
    residual: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeclaredDependency:
    ecosystem: str
    name: str
    requirement: str
    scope: str


@dataclass(frozen=True)
class EcosystemFiles:
    ecosystem: str
    directory: str
    manifest: str
    lockfile: str


_ECOSYSTEM_FILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pypi", "pyproject.toml", ("uv.lock", "poetry.lock")),
    ("npm", "package.json", ("package-lock.json",)),
    ("cargo", "Cargo.toml", ("Cargo.lock",)),
)


def _normalize_name(ecosystem: str, name: str) -> str:
    value = name.strip().lower()
    return value.replace("_", "-").replace(".", "-") if ecosystem in {"pypi", "cargo"} else value


def _source_text(value: object) -> str:
    if isinstance(value, Mapping):
        for key in ("registry", "git", "url", "path", "directory", "editable", "virtual"):
            item = value.get(key)
            if item:
                return f"{key}:{item}"
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    return str(value or "")


def _hashes(raw: Mapping[str, Any]) -> str:
    values: list[str] = []
    sdist = raw.get("sdist")
    if isinstance(sdist, Mapping) and sdist.get("hash"):
        values.append(str(sdist["hash"]))
    for wheel in raw.get("wheels") or ():
        if isinstance(wheel, Mapping) and wheel.get("hash"):
            values.append(str(wheel["hash"]))
    for file_row in raw.get("files") or ():
        if isinstance(file_row, Mapping) and file_row.get("hash"):
            values.append(str(file_row["hash"]))
    return ",".join(sorted(set(values)))


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _unique_identity(out: Mapping[str, object], identity: str) -> str:
    """Keep malformed exact-duplicate lock entries instead of overwriting one."""

    if identity not in out:
        return identity
    suffix = 2
    while f"{identity}~{suffix}" in out:
        suffix += 1
    return f"{identity}~{suffix}"


def _copy_identity(base: str, version: str, source: str, *, copies: int, same_version: int) -> str:
    """Order-independent identity for one resolved lock entry.

    A name with a single entry keeps its historical identity. Several entries
    for one name (Cargo/uv multi-version resolution) are distinguished by
    version, and by source only when the version alone still collides, so the
    identity depends on the entry set rather than on file order.
    """

    if copies == 1:
        return base
    identity = f"{base}@{version}"
    return f"{identity}#{source}" if same_version > 1 and source else identity


def _toml_rows(packages: object) -> tuple[list[tuple[Mapping[str, Any], str, str]], list[str]]:
    rows: list[tuple[Mapping[str, Any], str, str]] = []
    unparsed: list[str] = []
    for raw in packages if isinstance(packages, list) else ():
        if not isinstance(raw, Mapping):
            unparsed.append(_canonical(raw))
            continue
        name = str(raw.get("name") or "").strip()
        version = str(raw.get("version") or "").strip()
        if not name or not version:
            unparsed.append(_canonical(raw))
            continue
        rows.append((raw, name, version))
    return rows, unparsed


def _toml_coverage(text: str, *, ecosystem: str) -> _LockCoverage:
    if not text.strip():
        return _LockCoverage()
    doc = tomllib.loads(text)
    rows, unparsed = _toml_rows(doc.get("package"))
    name_counts = Counter(_normalize_name(ecosystem, name) for _raw, name, _version in rows)
    version_counts = Counter((_normalize_name(ecosystem, name), version) for _raw, name, version in rows)
    out: dict[str, DependencyComponent] = {}
    for raw, name, version in rows:
        normalized = _normalize_name(ecosystem, name)
        if ecosystem == "cargo":
            source = str(raw.get("source") or "")
            single = f"{name}@{source}" if source else name
            base = name
            integrity = str(raw.get("checksum") or "")
            dep_names = [
                match.group(1)
                for dep in raw.get("dependencies") or ()
                if (match := _CARGO_DEP_NAME.match(str(dep))) is not None
            ]
            license_value = ""
        else:
            source = _source_text(raw.get("source"))
            single = normalized
            base = normalized
            integrity = _hashes(raw)
            dep_names = []
            deps = raw.get("dependencies")
            if isinstance(deps, Mapping):
                dep_names.extend(str(key) for key in deps)
            elif isinstance(deps, list):
                for dep in deps:
                    if isinstance(dep, str):
                        match = _REQUIREMENT_NAME.match(dep)
                        if match:
                            dep_names.append(match.group(1))
                    elif isinstance(dep, Mapping) and dep.get("name"):
                        dep_names.append(str(dep["name"]))
            license_value = str(raw.get("license") or "")
        copies = name_counts[normalized]
        identity = (
            single
            if copies == 1
            else _copy_identity(
                base,
                version,
                source,
                copies=copies,
                same_version=version_counts[(normalized, version)],
            )
        )
        identity = _unique_identity(out, identity)
        out[identity] = DependencyComponent(
            ecosystem=ecosystem,
            identity=identity,
            name=name,
            version=version,
            source=source,
            integrity=integrity,
            license=license_value,
            dependencies=tuple(sorted(set(dep_names))),
            raw=_canonical(raw),
        )
    grouped: dict[str, list[DependencyComponent]] = {}
    for component in out.values():
        if _third_party(component):
            grouped.setdefault(_normalize_name(ecosystem, component.name), []).append(component)
        else:
            unparsed.append(component.raw)
    residual = {key: value for key, value in doc.items() if key != "package"}
    residual["package"] = sorted(unparsed)
    return _LockCoverage(
        components=out,
        copies={name: tuple(sorted(items, key=lambda item: item.identity)) for name, items in grouped.items()},
        residual=residual,
    )


def _toml_package_components(text: str, *, ecosystem: str) -> dict[str, DependencyComponent]:
    return _toml_coverage(text, ecosystem=ecosystem).components


def _npm_name(path_key: str, raw: Mapping[str, Any]) -> str:
    explicit = str(raw.get("name") or "").strip()
    if explicit:
        return explicit
    normalized = path_key.replace("\\", "/")
    marker = "/node_modules/"
    if marker in normalized:
        return normalized.rsplit(marker, 1)[-1]
    if normalized.startswith("node_modules/"):
        return normalized[len("node_modules/") :]
    return ""


def _npm_legacy_entries(
    dependencies: Mapping[str, Any], prefix: str = ""
) -> tuple[list[tuple[str, str, Mapping[str, Any]]], dict[str, str]]:
    """Flatten a lockfileVersion 1 ``dependencies`` tree, nested copies included."""

    entries: list[tuple[str, str, Mapping[str, Any]]] = []
    unparsed: dict[str, str] = {}
    for name, value in dependencies.items():
        path = f"{prefix}/{name}" if prefix else str(name)
        if not isinstance(value, Mapping):
            unparsed[path] = _canonical(value)
            continue
        own = {key: item for key, item in value.items() if key != "dependencies"}
        if str(value.get("version") or "").strip():
            entries.append((path, str(name), own))
        else:
            unparsed[path] = _canonical(own)
        nested = value.get("dependencies")
        if isinstance(nested, Mapping):
            nested_entries, nested_unparsed = _npm_legacy_entries(nested, path)
            entries.extend(nested_entries)
            unparsed.update(nested_unparsed)
    return entries, unparsed


def _npm_legacy_component(identity: str, name: str, value: Mapping[str, Any]) -> DependencyComponent:
    requires = value.get("requires")
    return DependencyComponent(
        ecosystem="npm",
        identity=identity,
        name=name,
        version=str(value.get("version") or "").strip(),
        source=str(value.get("resolved") or ""),
        integrity=str(value.get("integrity") or ""),
        dependencies=tuple(sorted(str(key) for key in requires)) if isinstance(requires, Mapping) else (),
        raw=_canonical(value),
    )


def _npm_coverage(text: str) -> _LockCoverage:
    if not text.strip():
        return _LockCoverage()
    doc = json.loads(text)
    if not isinstance(doc, Mapping):
        return _LockCoverage(residual={"document": _canonical(doc)})
    packages = doc.get("packages")
    legacy = doc.get("dependencies")
    residual: dict[str, Any] = {key: value for key, value in doc.items() if key not in {"packages", "dependencies"}}
    out: dict[str, DependencyComponent] = {}
    grouped: dict[str, list[DependencyComponent]] = {}
    legacy_entries, legacy_unparsed = _npm_legacy_entries(legacy) if isinstance(legacy, Mapping) else ([], {})
    if isinstance(packages, Mapping):
        uncovered_packages: dict[str, Any] = {}
        for path_key, value in packages.items():
            if not path_key or not isinstance(value, Mapping):
                uncovered_packages[str(path_key)] = value
                continue
            name = _npm_name(str(path_key), value)
            version = str(value.get("version") or "").strip()
            if not name or not version:
                uncovered_packages[str(path_key)] = value
                continue
            deps = value.get("dependencies")
            dep_names = tuple(sorted(str(key) for key in deps)) if isinstance(deps, Mapping) else ()
            identity = str(path_key).replace("\\", "/")
            component = DependencyComponent(
                ecosystem="npm",
                identity=identity,
                name=name,
                version=version,
                source=str(value.get("resolved") or ""),
                integrity=str(value.get("integrity") or ""),
                license=str(value.get("license") or ""),
                dependencies=dep_names,
                raw=_canonical(value),
            )
            out[identity] = component
            grouped.setdefault(_normalize_name("npm", name), []).append(component)
        residual["packages"] = uncovered_packages
        # lockfileVersion 2 keeps a legacy ``dependencies`` mirror for npm 6.
        # It is judged with the package it describes (identity ``legacy:``)
        # rather than dropped, so it can never diverge unseen.
        for path, name, value in legacy_entries:
            component = _npm_legacy_component(f"legacy:{path}", name, value)
            grouped.setdefault(_normalize_name("npm", name), []).append(component)
    else:
        for path, name, value in legacy_entries:
            identity = _unique_identity(out, path)
            component = _npm_legacy_component(identity, name, value)
            out[identity] = component
            grouped.setdefault(_normalize_name("npm", name), []).append(component)
    if legacy_unparsed:
        residual["dependencies"] = legacy_unparsed
    return _LockCoverage(
        components=out,
        copies={name: tuple(sorted(items, key=lambda item: item.identity)) for name, items in grouped.items()},
        residual=residual,
    )


def _npm_components(text: str) -> dict[str, DependencyComponent]:
    return _npm_coverage(text).components


def _cargo_components(text: str) -> dict[str, DependencyComponent]:
    return _toml_coverage(text, ecosystem="cargo").components


def _lock_coverage(ecosystem: str, filename: str, text: str) -> _LockCoverage:
    if ecosystem == "npm":
        return _npm_coverage(text)
    if ecosystem == "cargo":
        return _toml_coverage(text, ecosystem="cargo")
    if ecosystem == "pypi" and filename in {"uv.lock", "poetry.lock"}:
        return _toml_coverage(text, ecosystem="pypi")
    return _LockCoverage()


def parse_lock(ecosystem: str, filename: str, text: str) -> dict[str, DependencyComponent]:
    return _lock_coverage(ecosystem, filename, text).components


def _requirement_from_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    return str(value or "")


def parse_manifest(ecosystem: str, text: str) -> dict[str, DeclaredDependency]:
    if not text.strip():
        return {}
    out: dict[str, DeclaredDependency] = {}
    if ecosystem == "npm":
        doc = json.loads(text)
        if not isinstance(doc, Mapping):
            return {}
        for field, scope in (
            ("dependencies", "runtime"),
            ("optionalDependencies", "optional"),
            ("peerDependencies", "peer"),
            ("devDependencies", "dev"),
        ):
            raw = doc.get(field)
            if not isinstance(raw, Mapping):
                continue
            for name, requirement in raw.items():
                key = _normalize_name(ecosystem, str(name))
                out.setdefault(
                    key,
                    DeclaredDependency(ecosystem, str(name), _requirement_from_value(requirement), scope),
                )
        return out

    doc = tomllib.loads(text)
    if ecosystem == "pypi":
        project = doc.get("project")
        if isinstance(project, Mapping):
            for requirement in project.get("dependencies") or ():
                match = _REQUIREMENT_NAME.match(str(requirement))
                if match:
                    name = match.group(1)
                    out[_normalize_name(ecosystem, name)] = DeclaredDependency(
                        ecosystem, name, str(requirement), "runtime"
                    )
            optional = project.get("optional-dependencies")
            if isinstance(optional, Mapping):
                for group, requirements in optional.items():
                    if not isinstance(requirements, list):
                        continue
                    for requirement in requirements:
                        match = _REQUIREMENT_NAME.match(str(requirement))
                        if match:
                            name = match.group(1)
                            out.setdefault(
                                _normalize_name(ecosystem, name),
                                DeclaredDependency(ecosystem, name, str(requirement), f"optional:{group}"),
                            )
        tool = doc.get("tool")
        poetry = tool.get("poetry") if isinstance(tool, Mapping) else None
        if isinstance(poetry, Mapping):
            for field, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev")):
                raw = poetry.get(field)
                if not isinstance(raw, Mapping):
                    continue
                for name, requirement in raw.items():
                    if str(name).lower() == "python":
                        continue
                    key = _normalize_name(ecosystem, str(name))
                    out.setdefault(
                        key,
                        DeclaredDependency(ecosystem, str(name), _requirement_from_value(requirement), scope),
                    )
        groups = doc.get("dependency-groups")
        if isinstance(groups, Mapping):
            for group, requirements in groups.items():
                if not isinstance(requirements, list):
                    continue
                for requirement in requirements:
                    match = _REQUIREMENT_NAME.match(str(requirement))
                    if match:
                        name = match.group(1)
                        out.setdefault(
                            _normalize_name(ecosystem, name),
                            DeclaredDependency(ecosystem, name, str(requirement), f"group:{group}"),
                        )
        return out

    if ecosystem == "cargo":
        for field, scope in (
            ("dependencies", "runtime"),
            ("build-dependencies", "build"),
            ("dev-dependencies", "dev"),
        ):
            raw = doc.get(field)
            if not isinstance(raw, Mapping):
                continue
            for name, requirement in raw.items():
                key = _normalize_name(ecosystem, str(name))
                out.setdefault(
                    key,
                    DeclaredDependency(ecosystem, str(name), _requirement_from_value(requirement), scope),
                )
        return out
    return {}


def supported_path(path: str) -> EcosystemFiles | None:
    normalized = path.replace("\\", "/")
    filename = normalized.rsplit("/", 1)[-1]
    directory = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
    for ecosystem, manifest_name, lock_names in _ECOSYSTEM_FILES:
        if filename == manifest_name:
            return EcosystemFiles(ecosystem, directory, normalized, "")
        if filename in lock_names:
            manifest = f"{directory}/{manifest_name}" if directory else manifest_name
            return EcosystemFiles(ecosystem, directory, manifest, normalized)
    return None


def supported_lockfile(path: str) -> tuple[str, str] | None:
    info = supported_path(path)
    if info is None or not info.lockfile:
        return None
    return info.ecosystem, info.manifest.rsplit("/", 1)[-1]


def _read_text(root: Path, relative: str) -> str:
    if not relative:
        return ""
    target = root / relative
    try:
        return target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""


def _find_lock(root: Path, info: EcosystemFiles) -> str:
    if info.lockfile:
        return info.lockfile
    manifest_name = info.manifest.rsplit("/", 1)[-1]
    for ecosystem, candidate_manifest, locks in _ECOSYSTEM_FILES:
        if ecosystem != info.ecosystem or candidate_manifest != manifest_name:
            continue
        for lock_name in locks:
            candidate = f"{info.directory}/{lock_name}" if info.directory else lock_name
            if (root / candidate).is_file():
                return candidate
    return ""


def _component_changes(before: DependencyComponent | None, after: DependencyComponent | None) -> tuple[str, ...]:
    if before is None and after is not None:
        return ("added",)
    if after is None and before is not None:
        return ("removed",)
    if before is None or after is None:
        return ()
    out: list[str] = []
    if before.version != after.version:
        out.append("version")
    if before.source != after.source:
        out.append("source")
    if before.integrity != after.integrity:
        out.append("integrity")
    if before.license != after.license:
        out.append("license")
    if before.dependencies != after.dependencies:
        out.append("dependency_set")
    return tuple(out)


def _manifest_changes(before: DeclaredDependency | None, after: DeclaredDependency | None) -> tuple[str, ...]:
    if before is None and after is not None:
        return ("added",)
    if after is None and before is not None:
        return ("removed",)
    if before is None or after is None:
        return ()
    out: list[str] = []
    if before.requirement != after.requirement:
        out.append("requirement")
    if before.scope != after.scope:
        out.append("scope")
    return tuple(out)


def _third_party(component: DependencyComponent) -> bool:
    if component.ecosystem == "pypi" and component.source.startswith(("editable:", "directory:", "virtual:", "path:")):
        return False
    return True


def _component_by_name(components: Mapping[str, DependencyComponent]) -> dict[str, list[DependencyComponent]]:
    out: dict[str, list[DependencyComponent]] = {}
    for component in components.values():
        if not _third_party(component):
            continue
        key = _normalize_name(component.ecosystem, component.name)
        out.setdefault(key, []).append(component)
    return out


def _preferred_component(
    components: Mapping[str, DependencyComponent], ecosystem: str, name: str
) -> DependencyComponent | None:
    matches = _component_by_name(components).get(_normalize_name(ecosystem, name), ())
    if not matches:
        return None
    if ecosystem == "npm":
        top = next((item for item in matches if item.identity == f"node_modules/{item.name}"), None)
        if top is not None:
            return top
    return sorted(matches, key=lambda item: item.identity)[0]


def _shortest_paths(
    components: Mapping[str, DependencyComponent],
    direct: Mapping[str, DeclaredDependency],
    ecosystem: str,
    target_name: str,
    *,
    limit: int = 3,
) -> tuple[tuple[str, ...], ...]:
    by_name = _component_by_name(components)
    target = _normalize_name(ecosystem, target_name)
    queue: deque[tuple[str, tuple[str, ...]]] = deque((name, (direct[name].name,)) for name in sorted(direct))
    seen: set[str] = set()
    found: list[tuple[str, ...]] = []
    while queue and len(found) < limit:
        name, path = queue.popleft()
        if name == target:
            found.append(path)
            continue
        if name in seen:
            continue
        seen.add(name)
        for component in by_name.get(name, ()):
            for dep in component.dependencies:
                dep_key = _normalize_name(ecosystem, dep)
                if dep_key not in {_normalize_name(ecosystem, item) for item in path}:
                    queue.append((dep_key, (*path, dep)))
    return tuple(found)


def _descendant_change_count(
    components: Mapping[str, DependencyComponent],
    ecosystem: str,
    root_name: str,
    changed_transitive_names: set[str],
) -> int:
    """Count changed transitive components reachable below one direct dependency."""

    by_name = _component_by_name(components)
    root = _normalize_name(ecosystem, root_name)
    queue: deque[str] = deque([root])
    seen: set[str] = set()
    count = 0
    while queue:
        name = queue.popleft()
        if name in seen:
            continue
        seen.add(name)
        if name != root and name in changed_transitive_names:
            count += 1
        for component in by_name.get(name, ()):
            for dependency in component.dependencies:
                queue.append(_normalize_name(ecosystem, dependency))
    return count


def _semantic_payload(
    *,
    ecosystem: str,
    name: str,
    old_declared: DeclaredDependency | None,
    new_declared: DeclaredDependency | None,
    old_copies: Sequence[DependencyComponent],
    new_copies: Sequence[DependencyComponent],
) -> str:
    def declared(value: DeclaredDependency | None) -> object:
        if value is None:
            return None
        return {"requirement": value.requirement, "scope": value.scope}

    # v2: the exact bytes of every resolved lock entry for this package (nested
    # npm copies, Cargo/uv multi-version entries, legacy mirrors) feed the
    # fingerprint, so any change to any copy reopens the human judgment.
    data = {
        "v": 2,
        "ecosystem": ecosystem,
        "name": _normalize_name(ecosystem, name),
        "old_declared": declared(old_declared),
        "new_declared": declared(new_declared),
        "old_copies": [[item.identity, item.raw] for item in old_copies],
        "new_copies": [[item.identity, item.raw] for item in new_copies],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_CHANGE_LABELS: dict[str, str] = {
    "source": "package source changed",
    "integrity": "integrity/checksum changed",
    "license": "license changed",
    "dependency_set": "dependency set changed",
}


def _copy_change_labels(before: DependencyComponent | None, after: DependencyComponent | None) -> list[str]:
    changes = _component_changes(before, after)
    labels: list[str] = []
    for change in changes:
        if change in {"added", "removed"}:
            labels.append(change)
        elif change == "version" and before is not None and after is not None:
            labels.append(f"version {before.version} → {after.version}")
        elif change in _CHANGE_LABELS:
            labels.append(_CHANGE_LABELS[change])
    return labels or ["lock entry changed"]


def _copy_reasons(old: Sequence[DependencyComponent], new: Sequence[DependencyComponent]) -> list[str]:
    old_by_identity = {item.identity: item for item in old}
    new_by_identity = {item.identity: item for item in new}
    reasons: list[str] = []
    for identity in sorted(set(old_by_identity) | set(new_by_identity)):
        before = old_by_identity.get(identity)
        after = new_by_identity.get(identity)
        if before is not None and after is not None and before.raw == after.raw:
            continue
        reasons.append(f"resolved copy {identity}: {', '.join(_copy_change_labels(before, after))}")
    return reasons


def _residual_changes(old: Mapping[str, Any], new: Mapping[str, Any], *, limit: int = 8) -> list[str]:
    changed: list[str] = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if _canonical(before) == _canonical(after):
            continue
        if isinstance(before, Mapping) and isinstance(after, Mapping):
            changed.extend(
                f"{key}[{sub!r}]"
                for sub in sorted(set(before) | set(after))
                if _canonical(before.get(sub)) != _canonical(after.get(sub))
            )
        else:
            changed.append(str(key))
    return changed[:limit] + ([f"{len(changed) - limit} more"] if len(changed) > limit else [])


def _dependency_scope(ecosystem: str, directory: str) -> str:
    """Identity scope for one package root.

    The repository-root package keeps the historical ``<ecosystem>`` scope so
    existing judgments stay valid. A nested package root (``frontend/``,
    ``landing/``) is a distinct dependency graph: bumping ``react`` in two
    package roots is two semantic changes, never one target silently
    overwriting the other.
    """

    return ecosystem if not directory else f"{ecosystem}:{directory}"


def _dependency_label(ecosystem: str, directory: str, name: str) -> str:
    return f"{ecosystem}:{name}" if not directory else f"{ecosystem}:{name} ({directory})"


def _blob_pair(blobs: Mapping[str, str], path: str) -> str:
    return blobs.get(path, "")


# npm restates package.json's declarations in the lock's root entry
# (``packages[""]``). Where that copy equals the same side's package.json it is
# judged with the manifest -- whose hunks stay reviewable and whose declarations
# become package targets -- so it is not independent lockfile metadata. A root
# entry that disagrees with package.json stays in the residual, visible.
_NPM_DECLARATION_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _npm_residual_without_manifest_mirror(residual: Mapping[str, Any], manifest_text: str) -> dict[str, Any]:
    packages = residual.get("packages")
    root = packages.get("") if isinstance(packages, Mapping) else None
    if not isinstance(packages, Mapping) or not isinstance(root, Mapping) or not manifest_text.strip():
        return dict(residual)
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError:
        return dict(residual)
    if not isinstance(manifest, Mapping):
        return dict(residual)
    mirrored = {
        name
        for name in _NPM_DECLARATION_FIELDS
        if name in root and name in manifest and _canonical(root[name]) == _canonical(manifest[name])
    }
    if not mirrored:
        return dict(residual)
    root_rest = {key: value for key, value in root.items() if key not in mirrored}
    return {**residual, "packages": {**packages, "": root_rest}}


def dependency_capture_gaps(
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
    changed_paths: Sequence[str],
    *,
    base_expected: Collection[str] = (),
    head_expected: Collection[str] = (),
) -> tuple[str, ...]:
    """Changed manifest/lockfile paths whose existing side was never read.

    ``base_expected``/``head_expected`` name the changed paths that exist on
    that side (not added / not deleted). A missing text there is a capture gap
    -- over the size cap, unreadable -- never an empty file. Deriving package
    targets from it would assert that every declared and locked package was
    added (or removed), so such a group stays ordinary source instead.
    """

    base = set(base_expected)
    head = set(head_expected)
    gaps: set[str] = set()
    for info in _changed_groups(changed_paths):
        for path in (info.manifest, info.lockfile):
            if not path:
                continue
            if (path in base and path not in old_blobs) or (path in head and path not in new_blobs):
                gaps.add(path)
    return tuple(sorted(gaps))


def _changed_groups(changed_paths: Sequence[str]) -> tuple[EcosystemFiles, ...]:
    grouped: dict[tuple[str, str], EcosystemFiles] = {}
    for path in changed_paths:
        info = supported_path(path)
        if info is None:
            continue
        key = (info.ecosystem, info.directory)
        existing = grouped.get(key)
        manifest = info.manifest or (existing.manifest if existing else "")
        lockfile = info.lockfile or (existing.lockfile if existing else "")
        grouped[key] = EcosystemFiles(info.ecosystem, info.directory, manifest, lockfile)
    return tuple(grouped[key] for key in sorted(grouped))


def derive_dependency_units(
    old_blobs: Mapping[str, str],
    new_blobs: Mapping[str, str],
    changed_paths: Sequence[str],
    *,
    base_expected: Collection[str] = (),
    head_expected: Collection[str] = (),
) -> tuple[ReviewUnit, ...]:
    """Derive semantic component units from exact changed manifest/lock bytes.

    A package root with a capture gap (see :func:`dependency_capture_gaps`)
    yields no semantic units: its files stay reviewable as ordinary source.
    """

    gaps = set(
        dependency_capture_gaps(
            old_blobs, new_blobs, changed_paths, base_expected=base_expected, head_expected=head_expected
        )
    )
    units: dict[str, ReviewUnit] = {}
    for info in _changed_groups(changed_paths):
        if info.manifest in gaps or info.lockfile in gaps:
            continue
        old_declared: dict[str, DeclaredDependency] = {}
        new_declared: dict[str, DeclaredDependency] = {}
        if info.manifest in old_blobs or info.manifest in new_blobs:
            try:
                old_declared = parse_manifest(info.ecosystem, _blob_pair(old_blobs, info.manifest))
                new_declared = parse_manifest(info.ecosystem, _blob_pair(new_blobs, info.manifest))
            except (json.JSONDecodeError, tomllib.TOMLDecodeError):
                old_declared = {}
                new_declared = {}

        old_lock = _LockCoverage()
        new_lock = _LockCoverage()
        lock_semantics_available = False
        if info.lockfile and (info.lockfile in old_blobs or info.lockfile in new_blobs):
            filename = info.lockfile.rsplit("/", 1)[-1]
            old_text = _blob_pair(old_blobs, info.lockfile)
            new_text = _blob_pair(new_blobs, info.lockfile)
            try:
                old_lock = _lock_coverage(info.ecosystem, filename, old_text)
                new_lock = _lock_coverage(info.ecosystem, filename, new_text)
                # Both exact sides parsed into a complete entry partition, so
                # the lockfile may anchor semantic targets instead of hunks.
                lock_semantics_available = bool(old_text.strip() or new_text.strip())
            except (json.JSONDecodeError, tomllib.TOMLDecodeError):
                old_lock = _LockCoverage()
                new_lock = _LockCoverage()
        # An unreadable lockfile remains visible as ordinary source.
        anchor = info.lockfile if info.lockfile and lock_semantics_available else info.manifest or info.lockfile
        scope = _dependency_scope(info.ecosystem, info.directory)

        names = set(old_declared) | set(new_declared) | set(old_lock.copies) | set(new_lock.copies)
        for name_key in sorted(names):
            old_decl = old_declared.get(name_key)
            new_decl = new_declared.get(name_key)
            old_copies = old_lock.copies.get(name_key, ())
            new_copies = new_lock.copies.get(name_key, ())
            declared_changes = _manifest_changes(old_decl, new_decl)
            copies_changed = [(item.identity, item.raw) for item in old_copies] != [
                (item.identity, item.raw) for item in new_copies
            ]
            if not declared_changes and not copies_changed:
                continue
            old_primary = [item for item in old_copies if not item.identity.startswith("legacy:")]
            new_primary = [item for item in new_copies if not item.identity.startswith("legacy:")]
            old_component = _preferred_component(old_lock.components, info.ecosystem, name_key)
            new_component = _preferred_component(new_lock.components, info.ecosystem, name_key)
            representative = new_decl or old_decl
            component = new_component or old_component
            display_name = (
                representative.name if representative is not None else (component.name if component else name_key)
            )
            reasons: list[str] = []
            if "added" in declared_changes:
                reasons.append("direct dependency added")
            elif "removed" in declared_changes:
                reasons.append("direct dependency removed")
            elif declared_changes:
                reasons.append("direct dependency declaration changed")
            if len(old_primary) <= 1 and len(new_primary) <= 1:
                resolved_changes = _component_changes(old_component, new_component)
                if "version" in resolved_changes:
                    before = old_component.version if old_component else "?"
                    after = new_component.version if new_component else "?"
                    reasons.append(f"resolved version {before} → {after}")
                for change, label in (
                    ("source", "package source changed"),
                    ("integrity", "integrity/checksum changed"),
                    ("license", "license changed"),
                ):
                    if change in resolved_changes:
                        reasons.append(label)
                primary_changed = [(item.identity, item.raw) for item in old_primary] != [
                    (item.identity, item.raw) for item in new_primary
                ]
                if primary_changed and not resolved_changes:
                    reasons.append("resolved lock entry changed")
            else:
                # Several resolved copies: name each copy that changed so a
                # nested/secondary copy can never hide behind the primary one.
                reasons.extend(_copy_reasons(old_primary, new_primary))
            if not reasons:
                reasons.extend(
                    _copy_reasons(
                        [item for item in old_copies if item.identity.startswith("legacy:")],
                        [item for item in new_copies if item.identity.startswith("legacy:")],
                    )
                    or ["transitive dependency changed"]
                )
            stable_key = unit_key("dependency", scope, name_key, 0)
            units[stable_key] = ReviewUnit(
                revision_id="",
                unit_key=stable_key,
                kind="dependency",
                path=anchor,
                content_fingerprint=_semantic_payload(
                    ecosystem=info.ecosystem,
                    name=display_name,
                    old_declared=old_decl,
                    new_declared=new_decl,
                    old_copies=old_copies,
                    new_copies=new_copies,
                ),
                fingerprint_method="semantic",
                symbol=_dependency_label(info.ecosystem, info.directory, display_name),
                attention_group="production",
                reasons=tuple(reasons),
            )

        if lock_semantics_available:
            old_residual = old_lock.residual
            new_residual = new_lock.residual
            if info.ecosystem == "npm":
                old_residual = _npm_residual_without_manifest_mirror(old_residual, _blob_pair(old_blobs, info.manifest))
                new_residual = _npm_residual_without_manifest_mirror(new_residual, _blob_pair(new_blobs, info.manifest))
            metadata_changes = _residual_changes(old_residual, new_residual)
            if metadata_changes:
                filename = info.lockfile.rsplit("/", 1)[-1]
                # Lock content not owned by any third-party package (top-level
                # settings, root/workspace entries, unparsed entries) is its
                # own judgment target; otherwise it would vanish with the hunks.
                stable_key = unit_key("dependency", scope, f"lockfile:{filename}", 0)
                units[stable_key] = ReviewUnit(
                    revision_id="",
                    unit_key=stable_key,
                    kind="dependency",
                    path=info.lockfile,
                    content_fingerprint=hashlib.sha256(
                        _canonical(
                            {
                                "v": 1,
                                "kind": "lockfile_metadata",
                                "old": old_residual,
                                "new": new_residual,
                            }
                        ).encode()
                    ).hexdigest(),
                    fingerprint_method="semantic",
                    symbol=_dependency_label(info.ecosystem, info.directory, "lockfile metadata"),
                    attention_group="production",
                    reasons=(f"lockfile metadata changed: {', '.join(metadata_changes)}",),
                )
    return tuple(units[key] for key in sorted(units))


def _supply_chain_by_package(
    evidence: Sequence[ReviewEvidence],
    *,
    read_artifact: Any,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    artifacts: dict[str, list[dict[str, Any]]] = {}
    vulnerabilities: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        metadata = parse_evidence_metadata(item.detail)
        artifact_digest = str(metadata.get("artifact_digest") or "")
        if item.kind == "sbom":
            payload = read_artifact(item)
            if payload is None:
                continue
            try:
                inspection = inspect_sbom(payload)
            except ValueError:
                continue
            for component in inspection.components:
                key = component.name.lower()
                artifacts.setdefault(key, []).append(
                    {
                        "artifact_digest": artifact_digest,
                        "version": component.version,
                        "license": component.license,
                        "purl": component.purl,
                        "sbom_evidence_id": item.id,
                        "standard": inspection.standard,
                    }
                )
        elif item.kind == "vulnerability_scan":
            payload = read_artifact(item)
            if payload is None:
                continue
            for finding in vulnerability_findings(payload):
                vulnerabilities.setdefault(finding.package.lower(), []).append(
                    {
                        "advisory": finding.advisory,
                        "severity": finding.severity,
                        "artifact_digest": artifact_digest,
                        "source": str(metadata.get("observation_source") or item.source_ref or item.source),
                        "observed_at": item.created_at,
                        "evidence_id": item.id,
                    }
                )
    return artifacts, vulnerabilities


def build_dependency_review(
    old_root: Path,
    new_root: Path,
    changed_paths: Sequence[str],
    *,
    evidence: Sequence[ReviewEvidence] = (),
    read_artifact: Any = lambda _item: None,
) -> dict[str, Any]:
    """Project exact old/new Snapshot source into bounded semantic dependency rows."""

    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    lockfiles: list[dict[str, Any]] = []
    artifacts_by_package, vulnerabilities_by_package = _supply_chain_by_package(evidence, read_artifact=read_artifact)

    for initial in _changed_groups(changed_paths):
        old_lock = _find_lock(old_root, initial)
        new_lock = _find_lock(new_root, initial)
        lockfile = initial.lockfile or new_lock or old_lock
        manifest = initial.manifest
        old_manifest_text = _read_text(old_root, manifest)
        new_manifest_text = _read_text(new_root, manifest)
        try:
            old_declared = parse_manifest(initial.ecosystem, old_manifest_text)
            new_declared = parse_manifest(initial.ecosystem, new_manifest_text)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError):
            old_declared = {}
            new_declared = {}

        old_components: dict[str, DependencyComponent] = {}
        new_components: dict[str, DependencyComponent] = {}
        if old_lock:
            try:
                old_components = parse_lock(
                    initial.ecosystem,
                    old_lock.rsplit("/", 1)[-1],
                    _read_text(old_root, old_lock),
                )
            except (json.JSONDecodeError, tomllib.TOMLDecodeError):
                old_components = {}
        if new_lock:
            try:
                new_components = parse_lock(
                    initial.ecosystem,
                    new_lock.rsplit("/", 1)[-1],
                    _read_text(new_root, new_lock),
                )
            except (json.JSONDecodeError, tomllib.TOMLDecodeError):
                new_components = {}

        direct_names = set(old_declared) | set(new_declared)
        changed_direct = 0
        changed_transitive = 0
        changed_transitive_names: set[str] = set()
        for identity in set(old_components) | set(new_components):
            old_candidate = old_components.get(identity)
            new_candidate = new_components.get(identity)
            if not _component_changes(old_candidate, new_candidate):
                continue
            candidate = new_candidate or old_candidate
            if candidate is None:
                continue
            normalized = _normalize_name(initial.ecosystem, candidate.name)
            if normalized not in direct_names:
                changed_transitive_names.add(normalized)

        # First project direct declarations, resolving to lock components when possible.
        for name_key in sorted(direct_names):
            old_decl = old_declared.get(name_key)
            new_decl = new_declared.get(name_key)
            old_component = _preferred_component(old_components, initial.ecosystem, name_key)
            new_component = _preferred_component(new_components, initial.ecosystem, name_key)
            declared_changes = _manifest_changes(old_decl, new_decl)
            resolved_changes = _component_changes(old_component, new_component)
            if not declared_changes and not resolved_changes:
                continue
            current = new_decl or old_decl
            component = new_component or old_component
            name = current.name if current else (component.name if component else name_key)
            key = (initial.ecosystem, initial.directory, name_key)
            rows[key] = _dependency_row(
                ecosystem=initial.ecosystem,
                directory=initial.directory,
                identity=name_key,
                name=name,
                direct=True,
                manifest=manifest,
                lockfile=lockfile,
                old_declared=old_decl,
                new_declared=new_decl,
                old_component=old_component,
                new_component=new_component,
                changes=tuple(dict.fromkeys((*declared_changes, *resolved_changes))),
                paths=_shortest_paths(
                    new_components if new_component else old_components,
                    new_declared if new_component else old_declared,
                    initial.ecosystem,
                    name,
                ),
                artifacts=artifacts_by_package.get(name.lower(), ()),
                vulnerabilities=vulnerabilities_by_package.get(name.lower(), ()),
                transitive_changes=_descendant_change_count(
                    new_components if new_component else old_components,
                    initial.ecosystem,
                    name,
                    changed_transitive_names,
                ),
            )
            changed_direct += 1

        # Then project every changed resolved component not already represented as direct.
        component_identities = set(old_components) | set(new_components)
        for identity in sorted(component_identities):
            old_component = old_components.get(identity)
            new_component = new_components.get(identity)
            component_changes = _component_changes(old_component, new_component)
            if not component_changes:
                continue
            component = new_component or old_component
            if component is None or not _third_party(component):
                continue
            name_key = _normalize_name(initial.ecosystem, component.name)
            if (initial.ecosystem, initial.directory, name_key) in rows:
                continue
            key = (initial.ecosystem, initial.directory, identity)
            rows[key] = _dependency_row(
                ecosystem=initial.ecosystem,
                directory=initial.directory,
                identity=identity,
                name=component.name,
                direct=False,
                manifest=manifest,
                lockfile=lockfile,
                old_declared=None,
                new_declared=None,
                old_component=old_component,
                new_component=new_component,
                changes=component_changes,
                paths=_shortest_paths(
                    new_components if new_component else old_components,
                    new_declared if new_component else old_declared,
                    initial.ecosystem,
                    component.name,
                ),
                artifacts=artifacts_by_package.get(component.name.lower(), ()),
                vulnerabilities=vulnerabilities_by_package.get(component.name.lower(), ()),
            )
            changed_transitive += 1

        if lockfile:
            lockfiles.append(
                {
                    "path": lockfile,
                    "manifest": manifest,
                    "ecosystem": initial.ecosystem,
                    "semantic_changes": changed_direct + changed_transitive,
                    "direct_changes": changed_direct,
                    "transitive_changes": changed_transitive,
                }
            )

    semantic_rows = list(rows.values())[:200]
    return {
        "supported": bool(_changed_groups(changed_paths)),
        "lockfiles": lockfiles,
        "changes": semantic_rows,
        "summary": {
            "semantic_changes": len(semantic_rows),
            "direct_changes": sum(1 for row in semantic_rows if row["direct"]),
            "transitive_changes": sum(1 for row in semantic_rows if not row["direct"]),
            "added": sum("added" in row["changes"] for row in semantic_rows),
            "removed": sum("removed" in row["changes"] for row in semantic_rows),
            "version_changed": sum("version" in row["changes"] for row in semantic_rows),
            "source_changed": sum("source" in row["changes"] for row in semantic_rows),
            "integrity_changed": sum("integrity" in row["changes"] for row in semantic_rows),
        },
    }


def _dependency_row(
    *,
    ecosystem: str,
    directory: str = "",
    identity: str,
    name: str,
    direct: bool,
    manifest: str,
    lockfile: str,
    old_declared: DeclaredDependency | None,
    new_declared: DeclaredDependency | None,
    old_component: DependencyComponent | None,
    new_component: DependencyComponent | None,
    changes: Sequence[str],
    paths: Sequence[Sequence[str]],
    artifacts: Sequence[Mapping[str, Any]],
    vulnerabilities: Sequence[Mapping[str, Any]],
    transitive_changes: int = 0,
) -> dict[str, Any]:
    current_component = new_component or old_component
    current_declared = new_declared or old_declared
    license_value = current_component.license if current_component else ""
    license_source = "lockfile" if license_value else ""
    if not license_value:
        for artifact in artifacts:
            candidate = str(artifact.get("license") or "")
            if candidate:
                license_value = candidate
                license_source = "sbom"
                break
    # ReviewUnit identity intentionally follows the semantic package name so a
    # version/source change reopens the same target instead of becoming a fake
    # remove+add. Duplicate lock occurrences may project as multiple rows, but
    # they point at one human judgment for that component.
    target_identity = _normalize_name(ecosystem, name)
    unit = unit_key("dependency", _dependency_scope(ecosystem, directory), target_identity, 0)
    return {
        "ecosystem": ecosystem,
        "package_root": directory,
        "identity": identity,
        "name": name,
        "direct": direct,
        "scope": current_declared.scope if current_declared else "transitive",
        "changes": list(changes),
        "before_requirement": old_declared.requirement if old_declared else "",
        "after_requirement": new_declared.requirement if new_declared else "",
        "before_version": old_component.version if old_component else "",
        "after_version": new_component.version if new_component else "",
        "before_source": old_component.source if old_component else "",
        "after_source": new_component.source if new_component else "",
        "before_integrity": old_component.integrity if old_component else "",
        "after_integrity": new_component.integrity if new_component else "",
        "license": license_value,
        "license_source": license_source,
        "manifest": manifest,
        "lockfile": lockfile,
        "dependency_paths": [list(path) for path in paths],
        "transitive_changes": transitive_changes,
        "artifacts": [dict(item) for item in artifacts],
        "vulnerabilities": [dict(item) for item in vulnerabilities],
        "target_unit_key": unit,
        "target_id": f"target:{unit}",
    }


__all__ = [
    "DeclaredDependency",
    "DependencyComponent",
    "build_dependency_review",
    "dependency_capture_gaps",
    "derive_dependency_units",
    "parse_lock",
    "parse_manifest",
    "supported_lockfile",
    "supported_path",
]
