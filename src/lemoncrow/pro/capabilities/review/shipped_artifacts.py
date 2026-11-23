"""Diff shipped artifacts between base and head (Phase 9R item 32, ``artifacts.shipped``).

Two stages per side. Stage A (sandbox) builds the package with every process
of reviewed code sealed (Python sdist/wheel with the sandbox's build backend
and no network, ``npm pack --ignore-scripts``, or a trusted-base ``command``
builder with artifact globs): the build runs as children of the driver, which
kills everything they left behind and records any interference. Stage B
(``artifact_inspection``) then derives member paths, sizes and content keys,
the exported API surface (Python public names, npm
``exports``/``main``/``types``/``bin`` and JS exports) and credential-shaped
content. It runs on the host over the copied-out artifacts (regular files
only, bounded, validated names) when they fit the report; otherwise the
sealed driver runs the same code in the sandbox after the kill sweep, and that
result counts only when the seal recorded no interference. A credential
finding carries only the member path, the rule and the line; the raw bytes are
never stored.

Container images need a Docker/BuildKit daemon, which the offline sandbox does
not provide. A detected Dockerfile is ``not_available`` with a setup hint, never
a pass; a ``container`` builder configured with base images pinned by digest is
built by the ``container_image`` runner (``environment/image_build.py``), outside
the sandbox under that runner's Dockerfile policy, and its root filesystem is
diffed like any other artifact.
"""

from __future__ import annotations

import difflib
import inspect
import json
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, Final

from lemoncrow_client.environment import image_digest

from lemoncrow.pro.capabilities.review import artifact_inspection
from lemoncrow.pro.capabilities.review.analysis_common import (
    MAX_TIMEOUT,
    SHIPPED_ENTRY,
    AnalysisSpec,
    Evaluation,
    Finding,
    Observation,
    SideRun,
    bounded,
    build_driver,
    decode_outputs,
    first_line,
    inspection_sides,
    omitted_outputs,
    relative_path,
    seal_tampered,
    sealed_inspection_problem,
    sealed_problem,
    sha256_hex,
    valid_spec_id,
)
from lemoncrow.pro.capabilities.review.artifact_inspection import inspect_files

BUILDERS: Final[tuple[str, ...]] = ("python", "npm", "command", "container")
_MAX_DISCOVERED: Final[int] = 8
_SIZE_RATIO: Final[float] = 0.10
_SIZE_BYTES: Final[int] = 50_000
_SKIP_DIRS: Final[tuple[str, ...]] = ("node_modules/", "tests/", "test/", "fixtures/", "examples/", "vendor/", ".")
_CONTAINER_HINT: Final[str] = (
    "Container image layers need a Docker/BuildKit daemon, which the offline sandbox does not provide. "
    "Configure a container builder under analyses.shipped with base_images pinned by digest and a rootless "
    "container engine on the executor, or declare a command builder that produces an archive (for example a "
    "rootfs tarball) to diff image contents."
)
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|fixtures?|testdata|spec)(?:/|$)|(?:^|/)test_[^/]+$|_test\.[a-z]+$|\.spec\.[a-z]+$",
    re.I,
)

# Stage A (sandbox): build with reviewed code sealed in ``sealed_run``, then copy the artifacts
# out, or, when they do not fit the report, run stage B here once the seal holds.
DRIVER: Final[str] = build_driver(
    r"""
def build_python(src, out):
    pyproject = text(os.path.join(src, "pyproject.toml")) or ""
    match = re.search(r"(?m)^\s*build-backend\s*=\s*[\"']([^\"']+)[\"']", pyproject)
    backend = match.group(1) if match else "setuptools.build_meta"
    module = backend.split(":")[0].strip()
    if not re.match(r"^[A-Za-z_][\w.]*$", module):
        finish(4, "the build backend name %r is not a module" % module)
    # Importing the backend can already run reviewed code (an in-tree backend): sealed too.
    code, output = sealed_run([sys.executable, "-c", "import " + module], src)
    if code is None:
        finish(2, output)
    if code != 0:
        finish(2, "the build backend %s is not installed in the sandbox Python" % module)
    # The PEP 517 hooks are called directly: no frontend, no isolation, no network.
    hooks = (
        "import importlib, sys\n"
        # setuptools rewrites sys.argv while building: read the arguments first.
        "spec, out = sys.argv[1], sys.argv[2]\n"
        "name, _, attr = spec.partition(':')\n"
        "backend = importlib.import_module(name.strip())\n"
        "for part in [p for p in attr.strip().split('.') if p]:\n"
        "    backend = getattr(backend, part)\n"
        "backend.build_sdist(out)\n"
        "backend.build_wheel(out)\n"
    )
    return sealed_run([sys.executable, "-c", hooks, backend, out], src, extra={"SOURCE_DATE_EPOCH": "315532800"})


def top_files(folder):
    return [(name, os.path.join(folder, name)) for name in sorted(os.listdir(folder))]


def main():
    seal_start()
    builder = str(P.get("builder") or "")
    src = copy_root()
    out = WORK + "/dist"
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    trees = (dir_identity(src), dir_identity(out))
    root = out
    if builder == "python":
        code, output = build_python(src, out)
    elif builder == "npm":
        if which("npm") is None:
            finish(2, "npm is not installed in the sandbox")
        code, output = sealed_run(["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", out], src, extra={
            "npm_config_cache": WORK + "/npm-cache", "npm_config_offline": "true", "npm_config_audit": "false",
            "npm_config_fund": "false", "npm_config_update_notifier": "false", "npm_config_loglevel": "error"})
    elif builder == "command":
        code, output = sealed_run([str(a) for a in P.get("command") or []], src)
        root = src
    else:
        finish(2, "builder %s cannot run in the sandbox" % builder)
    R["builder"] = builder
    R["build"] = {"exit": code, "output": (output or "")[-4000:]}
    if code is None:
        finish(2, output)
    if code != 0:
        finish(4, "the package build exited %s" % code)
    require_same_dir(src, trees[0])
    require_same_dir(out, trees[1])
    if builder == "command":
        patterns = [globre(p) for p in P.get("artifacts") or []]
        files = []
        for base, dirs, names in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for name in sorted(names):
                path = rel(os.path.join(base, name), src)
                if any(p.match(path) for p in patterns):
                    files.append((path, os.path.join(base, name)))
    else:
        files = top_files(out)
    if not files:
        finish(4, "the build produced no artifact")
    if len(files) <= 8 and fits_report(files):
        R["inspection"] = "host"
        R["outputs"], R["omitted"] = copy_out(files, root, copy_budget(), limit=8)
        finish(0)
    # Too large to copy out: the same stage B runs here, after the kill sweep (no reviewed
    # code is alive). The host accepts it only if the seal recorded no interference at all.
    R["inspection"] = "sealed_in_sandbox"
    omitted = [{"path": name[:1024], "why": "over the inspection file count", "size": 0} for name, _full in files[8:]]

    def artifacts():
        left = INSPECT_BYTES
        for item in files[:8]:
            got, why = read_files([item], root, min(left, 64 << 20), bound="inspection")
            omitted.extend(why)
            for name, data in got:
                left -= len(data)
                yield name, data

    R.update(inspect_files(artifacts(), builder, min(len(files), 8)))
    R["omitted"] = omitted
    fit_inspection(R, LIMIT - 4096)
    finish(0)
""",
    sealed=True,
    shared=inspect.getsource(artifact_inspection),
)

# --------------------------------------------------------------------------- #
# stage B: ``artifact_inspection`` on the host (copied-out bytes) or in the sealed sandbox
# --------------------------------------------------------------------------- #


def _derived(report: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The stage-B result a sealed driver derived in the sandbox, shape-checked."""

    rows, secrets, api = report.get("artifacts"), report.get("secrets"), report.get("api")
    if not isinstance(rows, list) or not rows or len(rows) > 8:
        return None, "the in-sandbox inspection reported no artifacts"
    if not all(isinstance(row, Mapping) and row.get("kind") and isinstance(row.get("members"), list) for row in rows):
        return None, "the in-sandbox inspection reported a malformed artifact"
    if not isinstance(secrets, list) or len(secrets) > 200 or not isinstance(api, Mapping):
        return None, "the in-sandbox inspection reported a malformed result"
    found = [dict(row) for row in secrets if isinstance(row, Mapping)]
    return {"artifacts": [dict(row) for row in rows], "secrets": found, "api": dict(api)}, ""


def inspect_outputs(report: Mapping[str, Any], exit_code: int | None) -> tuple[dict[str, Any] | None, str]:
    """Stage B: the inspection report of one sealed build, or ``(None, why)``.

    ``inspection: "host"``: the artifacts were copied out and are inspected here;
    the raw bytes never leave this function. ``"sealed_in_sandbox"``: they were
    too large to copy out, and the sealed driver ran the same inspection after
    its kill sweep; that result is accepted only when the seal recorded no
    interference. Either way only member paths, sizes, short content keys, API
    names and (path, rule, line) credential findings remain.
    """

    problem = sealed_problem(report, exit_code)
    if problem:
        return None, problem
    out: dict[str, Any] = {
        key: report[key] for key in ("v", "entry", "spec", "exit", "reason", "build", "notes", "seal") if key in report
    }
    if report.get("exit") != 0:
        return out, ""
    omitted = omitted_outputs(report)
    if report.get("inspection") == "sealed_in_sandbox":
        problem = sealed_inspection_problem(report)
        if problem:
            return None, problem
        derived, problem = _derived(report)
        if derived is None:
            return None, problem
        out["inspection"] = "sealed_in_sandbox"
        if omitted:
            path, why = next(iter(omitted.items()))
            out.update(exit=3, reason=f"the artifact {path} was not inspected in the sandbox: {why}")
            return out, ""
    else:
        files, problem = decode_outputs(report, limit=8)
        if problem:
            return None, problem
        out["inspection"] = "host"
        if omitted:
            path, why = next(iter(omitted.items()))
            out.update(exit=3, reason=f"the artifact {path} was not copied out of the sandbox: {why}")
            return out, ""
        derived = inspect_files(list(files.items()), str(report.get("builder") or ""), len(files))
        tampered = seal_tampered(report)
        if tampered:
            out["tampered"] = tampered
    out.update(derived, exit=1 if derived["secrets"] else 0)
    return out, ""


def _argv(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        try:
            parts = tuple(shlex.split(value))
        except ValueError:
            return None
    elif isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        parts = tuple(str(item) for item in value)
    else:
        return None
    return parts if parts and len(parts) <= 64 and all(len(part) <= 2000 for part in parts) else None


def _spec(spec_id: str, builder: str, root: str, *, origin: str, source: str, **options: Any) -> AnalysisSpec:
    # A container build runs only from a trusted-base definition naming the base images it may start from.
    container = builder == "container" and not options.get("base_images")
    label = {
        "python": "Python package",
        "npm": "npm package",
        "command": "Build artifacts",
        "container": "Container image",
    }[builder]
    return AnalysisSpec(
        entry_id=SHIPPED_ENTRY,
        id=spec_id,
        tool=builder,
        root=root,
        title=str(options.pop("title", "") or "") or f"{label} · {root}",
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"builder": builder, **options},
        executable=not container,
        reason="container image builds need a Docker/BuildKit daemon" if container else "",
        setup_hint=_CONTAINER_HINT if builder == "container" else "",
        timeout=int(options.get("timeout") or 600) if not container else 60,
    )


def parse_specs(rows: Sequence[Any], errors: list[str]) -> list[AnalysisSpec]:
    specs: list[AnalysisSpec] = []
    for index, row in enumerate(rows):
        where = f"analyses.shipped[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(
            set(row)
            - {"id", "title", "builder", "root", "command", "artifacts", "timeout", "dockerfile", "base_images"}
        )
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            continue
        spec_id, builder, root = row.get("id"), str(row.get("builder") or ""), relative_path(row.get("root"))
        try:
            timeout = max(30, min(int(row.get("timeout") or 600), MAX_TIMEOUT))
        except (TypeError, ValueError):
            errors.append(f"{where}.timeout must be a number of seconds")
            continue
        command = _argv(row.get("command")) if row.get("command") is not None else None
        artifacts = row.get("artifacts") or []
        dockerfile = relative_path(row.get("dockerfile") or "Dockerfile", allow_root=False)
        images = row.get("base_images") or []
        if not valid_spec_id(spec_id):
            errors.append(f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        elif builder not in BUILDERS:
            errors.append(f"{where}.builder must be one of {', '.join(BUILDERS)}")
        elif root is None:
            errors.append(f"{where}.root must be a repository-relative directory")
        elif builder == "command" and (
            command is None
            or not isinstance(artifacts, list)
            or not artifacts
            or any(not isinstance(item, str) or item.startswith(("/", "-")) or ".." in item for item in artifacts)
        ):
            errors.append(f"{where}: a command builder needs command and relative artifacts globs")
        elif builder != "command" and (row.get("command") is not None or row.get("artifacts") is not None):
            errors.append(f"{where}: only the command builder takes command and artifacts")
        elif builder != "container" and (row.get("dockerfile") is not None or row.get("base_images") is not None):
            errors.append(f"{where}: only the container builder takes dockerfile and base_images")
        elif builder == "container" and (
            dockerfile is None
            or not isinstance(images, list)
            or any(not isinstance(item, str) or not image_digest(item) for item in images)
        ):
            errors.append(f"{where}: dockerfile must be relative and base_images must be name@sha256:<digest> strings")
        else:
            extra: dict[str, Any] = {"title": str(row.get("title") or "")[:200], "timeout": timeout}
            if builder == "command":
                extra.update(command=list(command or ()), artifacts=[str(item) for item in artifacts])
            if builder == "container":
                extra.update(dockerfile=dockerfile, base_images=[str(item) for item in images])
            specs.append(_spec(str(spec_id), builder, root, origin="configured", source=where, **extra))
    return specs


def discover_specs(paths: Sequence[str], read_base: Callable[[str], str | None]) -> list[AnalysisSpec]:
    """Packages and images the trusted base ships (manifests only; nothing under tests or node_modules).

    A discovered Dockerfile stays not executable: only a configured builder names its base images.
    """

    found: dict[tuple[str, str], str] = {}
    for path in sorted(paths):
        if any(f"/{marker}" in f"/{path}" for marker in _SKIP_DIRS if marker != ".") or any(
            part.startswith(".") for part in PurePosixPath(path).parts[:-1]
        ):
            continue
        name = PurePosixPath(path).name
        root = str(PurePosixPath(path).parent)
        if name == "setup.py":
            found.setdefault(("python", root), path)
        elif name == "pyproject.toml":
            text = read_base(path) or ""
            if "[project]" in text or "[tool.poetry]" in text:
                found.setdefault(("python", root), path)
        elif name == "package.json":
            try:
                manifest = json.loads(read_base(path) or "{}")
            except ValueError:
                continue
            if isinstance(manifest, Mapping) and manifest.get("name") and manifest.get("private") is not True:
                found.setdefault(("npm", root), path)
        elif name == "Dockerfile" or name.endswith(".Dockerfile"):
            found.setdefault(("container", root), path)
    specs: list[AnalysisSpec] = []
    seen: set[str] = set()
    for (builder, root), source in sorted(
        found.items(), key=lambda item: (item[0][1].count("/"), item[0][1], item[0][0])
    ):
        if len(specs) >= _MAX_DISCOVERED:
            break
        base_id = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{builder}-{root}" if root != "." else builder).strip("-.")[:60]
        spec_id = base_id
        while spec_id in seen:
            spec_id = f"{base_id}-{len(seen)}"
        seen.add(spec_id)
        specs.append(_spec(spec_id, builder, root, origin="discovered", source=source))
    return specs


def payload(spec: AnalysisSpec) -> dict[str, Any]:
    out: dict[str, Any] = {"builder": spec.tool}
    if spec.tool == "command":
        out.update(command=list(spec.options.get("command") or ()), artifacts=list(spec.options.get("artifacts") or ()))
    if spec.tool == "container":
        out.update(dockerfile=spec.options.get("dockerfile"), base_images=list(spec.options.get("base_images") or ()))
    return out


def runtime_values(spec: AnalysisSpec) -> dict[str, Any]:
    """What the ``container_image`` runner needs for one configured image build."""

    return {
        "root": spec.root,
        "dockerfile": spec.options.get("dockerfile") or "Dockerfile",
        "base_images": list(spec.options.get("base_images") or ()),
        "timeout": spec.timeout,
    }


def inspect_image_report(report: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The ``container_image`` runner's report: built on the host, so it needs no seal; only its shape is checked."""

    if report.get("exit") not in {0, 1, 4} or report.get("builder") != "container":
        return None, "the image build returned no usable report"
    if report.get("exit") == 4:
        return dict(report), ""
    derived, problem = _derived(report)
    if derived is None:
        return None, problem
    return {**dict(report), **derived}, ""


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #


def _artifacts(run: SideRun | None) -> dict[str, Mapping[str, Any]]:
    if run is None or not run.ran:
        return {}
    return {
        str(row.get("kind")): row
        for row in run.report.get("artifacts") or ()
        if isinstance(row, Mapping) and row.get("kind")
    }


def _members(row: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for item in row.get("members") or ():
        if isinstance(item, Sequence) and not isinstance(item, str) and len(item) == 3:
            out[str(item[0])] = (int(item[1] or 0), str(item[2] or ""))
    return out


def _bound(row: Mapping[str, Any]) -> str | None:
    return str(row.get("last") or "") or None if row.get("truncated") else None


def _size(value: int) -> str:
    for unit in ("B", "KB", "MB"):
        if abs(value) < 1024 or unit == "MB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value = value / 1024  # type: ignore[assignment]
    return f"{value} B"


def _secret_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("artifact") or ""), str(row.get("path") or ""), str(row.get("rule") or ""))


def evaluate(spec: AnalysisSpec, head: SideRun | None, base: SideRun | None) -> Evaluation:
    """New/removed files, credentials, size and exported API between the base and head artifacts."""

    if head is None or head.state in {"not_run", "unavailable"}:
        return Evaluation(
            "NOT_RUN",
            [head.reason if head is not None and head.reason else "the package was not built"],
            setup_hint=spec.setup_hint
            or "The sandbox needs the build toolchain offline (Python build backend, npm); see analyses.shipped.",
        )
    if head.state == "error":
        return Evaluation("UNKNOWN", [head.reason or "the artifact driver returned no usable result"])
    if head.state == "failed":
        output = first_line(
            (head.report.get("build") or {}).get("output") if isinstance(head.report.get("build"), Mapping) else ""
        )
        if base is not None and base.state == "failed":
            return Evaluation(
                "NOT_RUN", [f"the package does not build on the trusted base either ({output or base.reason})"]
            )
        return Evaluation(
            "FAIL",
            [f"the package no longer builds: {head.reason}"],
            [
                Finding(
                    rule="build_failed",
                    category="failed",
                    subject=f"{spec.id}::build",
                    title=f"Package build fails · {spec.title or spec.id}"[:200],
                    summary=bounded(f"{head.reason}: {output}"),
                    severity="high",
                    critical=True,
                    base=(
                        Observation(base.state, "built" if base.ran else base.reason, "") if base is not None else None
                    ),
                    head=Observation("failed", bounded(head.reason, 200), sha256_hex(output)),
                )
            ],
        )
    # A base build that interfered with its own analysis is not a baseline.
    base_tampered = str(base.report.get("tampered") or "") if base is not None and base.ran else ""
    compared = base is not None and base.ran and not base_tampered
    head_rows, base_rows = _artifacts(head), (_artifacts(base) if compared else {})
    findings: list[Finding] = []
    reasons: list[str] = []
    diffs: list[str] = []
    summary_rows: list[dict[str, Any]] = []
    base_secrets = {
        _secret_key(row)
        for row in (base.report.get("secrets") or () if compared and base else ())
        if isinstance(row, Mapping)
    }
    introduced = [
        row
        for row in head.report.get("secrets") or ()
        if isinstance(row, Mapping) and _secret_key(row) not in base_secrets
    ]
    # One finding per (path, rule); the artifacts that ship it are facts.
    grouped: dict[tuple[str, str], tuple[list[str], int]] = {}
    for row in introduced:
        artifact, path, rule = _secret_key(row)
        kinds, _line = grouped.setdefault((path, rule), ([], int(row.get("line") or 0)))
        if artifact not in kinds:
            kinds.append(artifact)
    for (path, rule), (kinds, line) in list(grouped.items())[:40]:
        where = f"{path}:{line}" if line else path
        shipped_in = ", ".join(sorted(kinds))
        findings.append(
            Finding(
                rule="secret",
                category="secret",
                # No "secret:"-shaped text: stored findings pass through redaction, which masks it.
                subject=f"{spec.id}::credential::{path}::{rule}",
                title=f"Credential-shaped content shipped · {path} ({rule})"[:200],
                summary=bounded(
                    f"The {shipped_in} artifact(s) ship {where}, which matches the {rule} rule. "
                    "Only the path and rule are recorded; the value is never stored."
                ),
                severity="high",
                critical=True,
                change="added",
                paths=(path,),
                facts=(
                    ("Artifacts", shipped_in),
                    ("Path", path),
                    ("Rule", rule),
                    *((("Line", str(line)),) if line else ()),
                ),
                head=Observation("shipped", f"{path} · {rule}", sha256_hex([sorted(kinds), path, rule, line])),
            )
        )
    for kind in sorted(set(head_rows) | set(base_rows)):
        new_row, old_row = head_rows.get(kind), base_rows.get(kind)
        if new_row is None and old_row is not None and compared:
            findings.append(
                Finding(
                    rule="artifact_removed",
                    category="package",
                    subject=f"{spec.id}::artifact::{kind}",
                    title=f"Artifact no longer produced · {kind}",
                    summary=f"The base build produced a {kind} artifact ({old_row.get('file')}); the head build does not.",
                    severity="high",
                    change="removed",
                    base=Observation("built", str(old_row.get("file") or ""), sha256_hex(old_row.get("members"))),
                )
            )
            continue
        if new_row is None:
            continue
        head_members = _members(new_row)
        base_members = _members(old_row) if old_row is not None else {}
        bounds = [b for b in (_bound(new_row), _bound(old_row) if old_row is not None else None) if b]
        limit = min(bounds) if bounds else None

        def within(path: str, limit: str | None = limit) -> bool:
            return limit is None or path <= limit

        added = (
            sorted(path for path in set(head_members) - set(base_members) if within(path))
            if old_row is not None
            else []
        )
        removed = (
            sorted(path for path in set(base_members) - set(head_members) if within(path))
            if old_row is not None
            else []
        )
        changed = sorted(
            path for path in set(head_members) & set(base_members) if head_members[path][1] != base_members[path][1]
        )
        unpacked_new, unpacked_old = (
            int(new_row.get("unpacked") or 0),
            (int(old_row.get("unpacked") or 0) if old_row else 0),
        )
        bytes_new, bytes_old = int(new_row.get("bytes") or 0), int(old_row.get("bytes") or 0) if old_row else 0
        summary_rows.append(
            {
                "kind": kind,
                "file": new_row.get("file"),
                "members": int(new_row.get("count") or 0),
                "added": added[:100],
                "removed": removed[:100],
                "changed": len(changed),
                "bytes": {"base": bytes_old, "head": bytes_new},
                "unpacked": {"base": unpacked_old, "head": unpacked_new},
                "truncated": bool(new_row.get("truncated") or (old_row or {}).get("truncated")),
            }
        )
        flagged_paths = {path for _artifact, path, _rule in (_secret_key(row) for row in introduced)}
        notable = [
            path
            for path in added
            if path not in flagged_paths and (_TEST_PATH.search(path) or PurePosixPath(path).name.startswith("."))
        ]
        for path in notable[:20]:
            hidden = PurePosixPath(path).name.startswith(".")
            findings.append(
                Finding(
                    rule="new_file",
                    category="new_file",
                    subject=f"{spec.id}::new::{kind}::{path}",
                    title=f"{'Hidden' if hidden else 'Test or fixture'} file newly shipped · {path}"[:200],
                    summary=f"The {kind} artifact now includes {path}, which the base package did not ship.",
                    severity="medium",
                    change="added",
                    paths=(path,),
                    facts=(("Artifact", kind), ("Size", _size(head_members[path][0]))),
                    head=Observation("shipped", path, head_members[path][1]),
                )
            )
        rest = [path for path in added if path not in notable and path not in flagged_paths]
        if rest or removed:
            findings.append(
                Finding(
                    rule="contents",
                    category="package",
                    subject=f"{spec.id}::contents::{kind}",
                    title=f"Shipped files changed · {kind}: +{len(added)} / -{len(removed)}",
                    summary=bounded(
                        "; ".join(
                            filter(
                                None,
                                (
                                    ("new: " + ", ".join(rest[:8]) + (" …" if len(rest) > 8 else "")) if rest else "",
                                    (
                                        ("removed: " + ", ".join(removed[:8]) + (" …" if len(removed) > 8 else ""))
                                        if removed
                                        else ""
                                    ),
                                ),
                            )
                        )
                    ),
                    severity="medium" if removed else "low",
                    paths=tuple((rest + removed)[:10]),
                    facts=(
                        ("Artifact", kind),
                        ("New files", str(len(added))),
                        ("Removed files", str(len(removed))),
                        ("Changed files", str(len(changed))),
                    ),
                    base=Observation("base", f"{len(base_members)} files", sha256_hex(sorted(base_members))),
                    head=Observation("head", f"{len(head_members)} files", sha256_hex(sorted(head_members))),
                )
            )
        if old_row is not None:
            delta = unpacked_new - unpacked_old
            if abs(delta) >= _SIZE_BYTES or (
                unpacked_old and abs(delta) / unpacked_old >= _SIZE_RATIO and abs(delta) >= 1024
            ):
                findings.append(
                    Finding(
                        rule="size",
                        category="size",
                        subject=f"{spec.id}::size::{kind}",
                        title=f"Bundle size {'grows' if delta > 0 else 'shrinks'} · {kind} {'+' if delta > 0 else ''}{_size(delta)}",
                        summary=f"Unpacked {_size(unpacked_old)} -> {_size(unpacked_new)}; archive {_size(bytes_old)} -> {_size(bytes_new)}.",
                        severity="medium" if delta > 0 else "low",
                        facts=(
                            ("Artifact", kind),
                            ("Unpacked", f"{_size(unpacked_old)} -> {_size(unpacked_new)}"),
                            ("Archive", f"{_size(bytes_old)} -> {_size(bytes_new)}"),
                        ),
                        base=Observation("base", _size(unpacked_old), str(unpacked_old)),
                        head=Observation("head", _size(unpacked_new), str(unpacked_new)),
                    )
                )
            listing_old = [f"{path} {base_members[path][0]}" for path in sorted(base_members)]
            listing_new = [f"{path} {head_members[path][0]}" for path in sorted(head_members)]
            if listing_old != listing_new:
                diffs.append(
                    "\n".join(
                        difflib.unified_diff(listing_old, listing_new, f"base/{kind}", f"head/{kind}", lineterm="", n=1)
                    )
                )
    findings.extend(_api_findings(spec, head, base if compared else None))
    pre_existing = [
        row for row in head.report.get("secrets") or () if isinstance(row, Mapping) and _secret_key(row) in base_secrets
    ]
    tampered = str(head.report.get("tampered") or "")
    incomplete = [f"{kind}: {row.get('incomplete')}" for kind, row in head_rows.items() if row.get("incomplete")]
    unscanned = sum(int(row.get("unscanned") or 0) for row in head_rows.values())
    if introduced:
        status = "FAIL"
        reasons.append(f"{len(grouped)} credential-shaped file(s) or value(s) newly shipped (paths and rules only)")
    elif tampered or incomplete:
        status = "UNKNOWN"
    else:
        status = "PASS"
        reasons.append(
            "artifacts built and compared with the base" if compared else "artifacts built (no base build to compare)"
        )
    if tampered:
        reasons.insert(
            0,
            f"the head build interfered with the analysis ({tampered}); its artifacts are not trusted "
            "to be complete, so this result cannot pass",
        )
    if incomplete:
        reasons.append("not fully inspected: " + "; ".join(incomplete[:3]))
    if unscanned:
        reasons.append(f"{unscanned} member(s) over 2 MB were listed but not content-scanned")
    if pre_existing:
        reasons.append(f"{len(pre_existing)} credential-shaped match(es) are already shipped by the base")
    if base_tampered:
        reasons.append(f"base side not compared: the base build interfered with the analysis ({base_tampered})")
    if base is not None and not base.ran:
        reasons.append(f"base side: {base.reason or base.state}")
    return Evaluation(
        status,  # type: ignore[arg-type]
        reasons,
        findings,
        summary={
            "builder": spec.tool,
            "inspection": inspection_sides(head, base),
            "artifacts": summary_rows,
            "secrets": [
                {"artifact": a, "path": p, "rule": r, "introduced": (a, p, r) not in base_secrets}
                for a, p, r in (
                    _secret_key(row) for row in head.report.get("secrets") or () if isinstance(row, Mapping)
                )
            ][:50],
        },
        heavy={"contents.diff": "\n".join(diffs)} if diffs else {},
    )


def _api_findings(spec: AnalysisSpec, head: SideRun, base: SideRun | None) -> list[Finding]:
    if base is None:
        return []
    out: list[Finding] = []
    new_api = head.report.get("api") if isinstance(head.report.get("api"), Mapping) else {}
    old_api = base.report.get("api") if isinstance(base.report.get("api"), Mapping) else {}
    assert isinstance(new_api, Mapping) and isinstance(old_api, Mapping)
    wheel_new = new_api.get("wheel") if isinstance(new_api.get("wheel"), Mapping) else None
    wheel_old = old_api.get("wheel") if isinstance(old_api.get("wheel"), Mapping) else None
    if wheel_new is not None and wheel_old is not None:
        removed: list[str] = []
        added: list[str] = []
        for module in sorted(set(wheel_new) | set(wheel_old)):
            names_new = set(map(str, wheel_new.get(module) or ())) if module in wheel_new else set()
            names_old = set(map(str, wheel_old.get(module) or ())) if module in wheel_old else set()
            removed += [f"{module}.{name}" for name in sorted(names_old - names_new)]
            added += [f"{module}.{name}" for name in sorted(names_new - names_old)]
        if removed:
            out.append(
                Finding(
                    rule="api_removed",
                    category="api",
                    subject=f"{spec.id}::api::removed",
                    title=f"Exported API removed · {len(removed)} name(s)",
                    summary=bounded(
                        "No longer exported: " + ", ".join(removed[:12]) + (" …" if len(removed) > 12 else "")
                    ),
                    severity="high",
                    change="removed",
                    facts=(("Removed", str(len(removed))), ("Examples", bounded(", ".join(removed[:5]), 200))),
                    base=Observation("exported", f"{len(removed)} names", sha256_hex(removed)),
                    head=Observation("missing", "not exported", ""),
                )
            )
        if added:
            out.append(
                Finding(
                    rule="api_added",
                    category="api",
                    subject=f"{spec.id}::api::added",
                    title=f"Exported API added · {len(added)} name(s)",
                    summary=bounded("Newly exported: " + ", ".join(added[:12]) + (" …" if len(added) > 12 else "")),
                    severity="low",
                    change="added",
                    facts=(("Added", str(len(added))), ("Examples", bounded(", ".join(added[:5]), 200))),
                    head=Observation("exported", f"{len(added)} names", sha256_hex(added)),
                )
            )
    npm_new = new_api.get("npm") if isinstance(new_api.get("npm"), Mapping) else None
    npm_old = old_api.get("npm") if isinstance(old_api.get("npm"), Mapping) else None
    if npm_new is not None and npm_old is not None:
        if npm_new.get("manifest") != npm_old.get("manifest"):
            out.append(
                Finding(
                    rule="api_manifest",
                    category="api",
                    subject=f"{spec.id}::api::manifest",
                    title="Package entry points changed (main/exports/types/bin)",
                    summary=bounded(f"{npm_old.get('manifest')} -> {npm_new.get('manifest')}"),
                    severity="medium",
                    base=Observation(
                        "base", bounded(npm_old.get("manifest"), 200), sha256_hex(npm_old.get("manifest"))
                    ),
                    head=Observation(
                        "head", bounded(npm_new.get("manifest"), 200), sha256_hex(npm_new.get("manifest"))
                    ),
                )
            )
        old_names, new_names = set(map(str, npm_old.get("exports") or ())), set(map(str, npm_new.get("exports") or ()))
        if old_names - new_names:
            gone = sorted(old_names - new_names)
            out.append(
                Finding(
                    rule="api_removed",
                    category="api",
                    subject=f"{spec.id}::api::removed",
                    title=f"Exported API removed · {len(gone)} name(s)",
                    summary=bounded("No longer exported: " + ", ".join(gone[:12])),
                    severity="high",
                    change="removed",
                    facts=(("Removed", str(len(gone))),),
                    base=Observation("exported", f"{len(gone)} names", sha256_hex(gone)),
                    head=Observation("missing", "not exported", ""),
                )
            )
        if new_names - old_names:
            fresh = sorted(new_names - old_names)
            out.append(
                Finding(
                    rule="api_added",
                    category="api",
                    subject=f"{spec.id}::api::added",
                    title=f"Exported API added · {len(fresh)} name(s)",
                    summary=bounded("Newly exported: " + ", ".join(fresh[:12])),
                    severity="low",
                    change="added",
                    facts=(("Added", str(len(fresh))),),
                    head=Observation("exported", f"{len(fresh)} names", sha256_hex(fresh)),
                )
            )
    return out


__all__ = [
    "DRIVER",
    "discover_specs",
    "evaluate",
    "inspect_outputs",
    "parse_specs",
    "payload",
]
