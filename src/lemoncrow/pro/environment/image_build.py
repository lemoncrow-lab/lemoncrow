"""Build a reviewed Dockerfile into a root filesystem archive, on a container engine, for inspection.

A Dockerfile is reviewed code and a build talks to a daemon outside any sandbox, so a build runs only under these
rules, each failing closed (the side is reported as not run, never as passed):

* the engine is one :func:`~lemoncrow.pro.environment.oci.detect_runtime` accepts (rootless, or the development
  override);
* every ``FROM`` is ``scratch``, an earlier stage, or an image the trusted base lists, pinned by digest. Those are
  pulled by digest first and the build itself never pulls;
* anything that would make the daemon or the frontend reach out on the Dockerfile's behalf is refused before the
  engine is asked: a ``# syntax`` frontend, ``ADD`` of a URL or Git source, ``COPY --from`` or ``RUN --mount from`` of
  an image, ``RUN --network`` / ``--security``, ``ONBUILD``, and any ``$`` in those positions (a variable could spell
  any of them);
* ``RUN`` steps have no network, no cache is read, what the build adds to the builder is removed afterwards, no image
  is kept, and the archive is size-bounded.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final

from lemoncrow.pro.environment import oci
from lemoncrow.pro.environment.runners import RunnerSetupCandidate, SurfaceRunResult
from lemoncrow.pro.environment.sandbox_limits import SandboxCapacityBusy, run_limited_bounded

RUNNER_ID: Final[str] = "container_image"
#: The largest root filesystem archive that is written and inspected.
MAX_ARCHIVE_BYTES: Final[int] = 256 * 1024 * 1024
MAX_DOCKERFILE_BYTES: Final[int] = 1024 * 1024
DEFAULT_TIMEOUT: Final[int] = 600
RECORD_TIMEOUT: Final[int] = 30
MAX_RECORDS: Final[int] = 512
MAX_RECORD_PASSES: Final[int] = 16
EXIT_OK: Final[int] = 0
EXIT_FINDINGS: Final[int] = 1
EXIT_TOOL_FAILED: Final[int] = 4
_DIRECTIVE: Final[re.Pattern[str]] = re.compile(r"^#\s*([A-Za-z]+)\s*=")
_FSIZE_SHIM: Final[str] = (
    "import os,resource,signal,sys;n=int(sys.argv[1]);resource.setrlimit(resource.RLIMIT_FSIZE,(n,n));"
    "signal.signal(signal.SIGXFSZ,signal.SIG_DFL);os.execv(sys.argv[2],sys.argv[2:])"
)


def _instructions(text: str) -> tuple[list[tuple[str, str]], str]:
    """``(WORD, arguments)`` per line-level instruction, or ``([], why)``.

    Heredoc bodies are read as instructions too. Skipping them would need this parser to agree with the engine's on
    where each one ends, and any disagreement would hide an instruction; the cost is refusing a body that happens to
    hold a line such as ``FROM x``.
    """

    lines = text.splitlines()
    index = 0
    while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("#")):
        match = _DIRECTIVE.match(lines[index].strip())
        if match is not None and match.group(1).lower() in {"syntax", "escape"}:
            return [], f"the {match.group(1).lower()} parser directive is not allowed"
        if not lines[index].strip():
            break  # directives end at the first blank line
        index += 1
    rows: list[tuple[str, str]] = []
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        while line.endswith("\\"):
            line = line[:-1].rstrip() + " "
            while index < len(lines) and lines[index].lstrip().startswith("#"):
                index += 1
            if index >= len(lines):
                break
            line += lines[index].strip()
            index += 1
        parts = line.split(None, 1)  # any whitespace: the engine accepts a tab after the instruction
        if parts:
            rows.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
    return rows, ""


def _flags(rest: str) -> tuple[dict[str, str], list[str], str]:
    """Leading ``--name[=value]`` flags, the remaining words, or a parse problem."""

    try:
        words = shlex.split(rest, comments=False, posix=True)
    except ValueError:
        return {}, [], "its arguments cannot be parsed"
    flags: dict[str, str] = {}
    while words and words[0].startswith("--"):
        name, _, value = words.pop(0)[2:].partition("=")
        flags[name.lower()] = value
    return flags, words, ""


def dockerfile_refusal(text: str, allowed_images: Iterable[str]) -> str:
    """Why this Dockerfile may not be built here, or ``""``. Anything not understood is refused."""

    allowed = set(allowed_images)
    rows, problem = _instructions(text)
    if problem:
        return problem
    stages: list[str] = []
    count = 0

    def known_stage(name: str) -> bool:
        return name.lower() in stages or (name.isdigit() and int(name) < count)

    for word, rest in rows:
        flags, args, problem = _flags(rest) if word in {"FROM", "COPY", "ADD", "RUN"} else ({}, [], "")
        if problem:
            return f"{word}: {problem}"
        if word == "FROM":
            if flags or not args or "$" in rest:
                return f"FROM {rest[:120]}: flags and variables are not allowed"
            image = args[0]
            if image != "scratch" and image not in allowed and not known_stage(image):
                return f"FROM {image[:120]}: not one of the base images the trusted base pins by digest"
            count += 1
            if len(args) >= 3 and args[1].lower() == "as":
                stages.append(args[2].lower())
        elif count == 0 and word != "ARG":
            return f"{word} comes before the first FROM"
        elif word == "ONBUILD":
            return "ONBUILD is not allowed"
        elif word == "ARG" and rest.upper().startswith("BUILDKIT_"):
            return "ARG BUILDKIT_* is not allowed"
        elif word == "ADD":
            if flags:
                return f"ADD --{next(iter(flags))} is not allowed"
            for raw in args[:-1] if not rest.startswith("[") else args:
                item = raw.strip('[],"')  # the JSON form
                if any(mark in item for mark in ("$", "://", "@")) or item.endswith(".git") or "github.com/" in item:
                    return f"ADD {item[:120]}: remote and variable sources are not allowed"
        elif word == "COPY":
            source = flags.get("from")
            if source is not None and not known_stage(source):
                return f"COPY --from={source[:120]}: only an earlier stage may be copied from"
        elif word == "RUN":
            for name in ("network", "security", "device"):
                if name in flags:
                    return f"RUN --{name} is not allowed"
            if "mount" in flags:
                if "$" in flags["mount"]:
                    return "RUN --mount may not use variables"
                options = dict(
                    (part.partition("=")[0].lower(), part.partition("=")[2]) for part in flags["mount"].split(",")
                )
                if options.get("type", "bind") in {"secret", "ssh"}:
                    return "RUN --mount of a secret or SSH agent is not allowed"
                source = options.get("from")
                if source is not None and not known_stage(source):
                    return f"RUN --mount from={source[:120]}: only an earlier stage may be mounted"
    if count == 0:
        return "the Dockerfile has no FROM"
    return ""


def build_argv(client: oci.OciClient, *, dockerfile: Path, context: Path, archive: Path) -> list[str]:
    """The engine's build command: no pull, no cache, no network, one archive and no image."""

    pull = "--pull=never" if client.runtime.engine == "podman" else "--pull=false"
    extra = [] if client.runtime.engine == "podman" else ["--provenance=false", "--sbom=false"]
    return [
        client.runtime.binary,
        "build",
        pull,
        "--no-cache",
        "--network=none",
        "--progress=plain",
        *extra,
        "--file",
        str(dockerfile),
        "--output",
        f"type=tar,dest={archive}",
        str(context),
    ]


def bounded_command(argv: Sequence[str], limit: int) -> list[str]:
    """*argv* unable to write a file larger than *limit* (the archive)."""

    return [sys.executable, "-I", "-S", "-c", _FSIZE_SHIM, str(limit), *argv]


def _buildx(client: oci.OciClient, *args: str) -> tuple[int, str]:
    """``(exit code, stdout)`` of one ``docker buildx`` call; a client that cannot run it is exit 127."""

    try:
        done = subprocess.run(
            [client.runtime.binary, "buildx", *args],
            env=client.engine_env,
            capture_output=True,
            text=True,
            timeout=RECORD_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return done.returncode, done.stdout


def build_records(client: oci.OciClient) -> frozenset[str] | None:
    """The ids of the builder's records, or ``None`` when they cannot be listed (never Podman's)."""

    if client.runtime.engine != "docker":
        return None
    code, output = _buildx(client, "du", "--verbose")
    if code != 0:
        return None
    return frozenset(
        line.split()[1] for line in output.splitlines() if line.startswith("ID:") and len(line.split()) > 1
    )


def drop_new_records(client: oci.OciClient, before: frozenset[str] | None) -> None:
    """Remove what this build added to the builder: ``--no-cache`` skips reading the cache, not writing it.

    Only ids absent before the build go, one at a time; the builder keeps any still in use. A concurrent build's
    finished record may go too, which costs that build nothing: every build here runs without the cache.
    """

    # lc-debt: Podman's and an unlistable builder's records stay; give each run its own builder to bound them too.
    if before is None:
        return
    # A record's parents become removable only once it is gone, so this repeats until nothing more goes.
    remaining: frozenset[str] = frozenset()
    for _pass in range(MAX_RECORD_PASSES):
        current = build_records(client)
        fresh = (current or frozenset()) - before
        if not fresh or fresh == remaining:
            return
        remaining = fresh
        for record in sorted(fresh)[:MAX_RECORDS]:
            _buildx(client, "prune", "--force", "--filter", f"id={record}")


class ContainerImageRunner:
    """Builds one side's Dockerfile and reports what the image would contain (see the module docstring)."""

    runner_id = RUNNER_ID

    def setup_candidates(self, _repo_root: Path) -> tuple[RunnerSetupCandidate, ...]:
        return ()

    def _unavailable(self, surface: Any, runtime: Any, side: str, why: str, started: float = 0.0) -> SurfaceRunResult:
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "unavailable",
            why[:1000],
            self.runner_id,
            runtime.id,
            duration_ms=int((time.monotonic() - started) * 1000) if started else 0,
        )

    def _report(
        self, surface: Any, runtime: Any, side: str, report: dict[str, Any], summary: str, output: str, started: float
    ) -> SurfaceRunResult:
        code = int(report["exit"])
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "passed" if code == EXIT_OK else "failed",
            summary[:1000],
            self.runner_id,
            runtime.id,
            exit_code=code,
            duration_ms=int((time.monotonic() - started) * 1000),
            output=output,
            data={"report": json.dumps(report, sort_keys=True, separators=(",", ":")).encode().hex()},
        )

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> SurfaceRunResult:
        started = time.monotonic()
        host = context.runner_host
        images = tuple(runtime.strings("base_images"))
        name = str(runtime.values.get("dockerfile") or "Dockerfile")
        root = runtime.string("root", ".") or "."
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            return self._unavailable(surface, runtime, side, "the Dockerfile path escapes the build root")
        try:
            engine = oci.detect_runtime()
            oci.refuse_socket_mounts(())
        except oci.OciUnavailable as exc:
            return self._unavailable(surface, runtime, side, f"container images cannot be built here: {exc}")
        client = oci.OciClient(engine)
        digest = hashlib.sha256(f"{runtime.id}\0{side}".encode()).hexdigest()[:16]
        base = (
            context.store_root
            / "review"
            / "runtime-runs"
            / context.revision.review_id
            / context.revision.id
            / self.runner_id
            / digest
        )
        workspace = base / "workspace"
        try:
            host.materialize_review_side(
                context.store_root,
                context.source_root or context.repo_root,
                context.revision,
                side=side,
                target=workspace,
                scope=root,
                store=context.store,
            )
        except host.ReviewSnapshotUnavailable as exc:
            return self._unavailable(surface, runtime, side, str(exc))
        build_root = (workspace / root).resolve()
        top = workspace.resolve()
        dockerfile = build_root / relative
        if not (build_root == top or top in build_root.parents) or not build_root.is_dir():
            return self._unavailable(surface, runtime, side, f"the build root {root} is missing on this side")
        failed = {"v": 1, "exit": EXIT_TOOL_FAILED, "builder": "container"}
        if (
            top not in dockerfile.resolve().parents
            or dockerfile.is_symlink()
            or not dockerfile.is_file()
            or dockerfile.stat().st_size > MAX_DOCKERFILE_BYTES
        ):
            reason = f"{name} is not a regular file of at most {MAX_DOCKERFILE_BYTES} bytes on this side"
            report = {**failed, "reason": reason, "build": {"exit": None, "output": ""}}
            return self._report(surface, runtime, side, report, reason, "", started)
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        refusal = dockerfile_refusal(text, images)
        if refusal:
            return self._unavailable(surface, runtime, side, f"the Dockerfile was not built: {refusal}", started)
        archive = base / "rootfs.tar"
        timeout = max(30, min(int(runtime.values.get("timeout") or DEFAULT_TIMEOUT), 3600))
        records: frozenset[str] | None = None
        try:
            for image in images:
                client.ensure_image(image)
            records = build_records(client)
            done = run_limited_bounded(
                bounded_command(
                    build_argv(client, dockerfile=dockerfile, context=build_root, archive=archive),
                    MAX_ARCHIVE_BYTES + 1,
                ),
                timeout=timeout,
                cwd=build_root,
                env=client.engine_env,
                max_output_bytes=8192,
            )
        except oci.OciUnavailable as exc:
            return self._unavailable(surface, runtime, side, str(exc), started)
        except subprocess.TimeoutExpired:
            drop_new_records(client, records)
            return self._unavailable(surface, runtime, side, "the image build timed out", started)
        except (OSError, SandboxCapacityBusy) as exc:
            return self._unavailable(surface, runtime, side, f"the image build could not start: {exc}", started)
        output = host.protect_text("\n".join(filter(None, (done.stdout, done.stderr)))[-8000:])[0]
        try:
            # Checked first: a client that survives the write limit may exit 0 over a cut-off archive.
            if archive.is_file() and archive.stat().st_size > MAX_ARCHIVE_BYTES:
                why = f"the image is larger than the {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB inspection bound"
                return self._unavailable(surface, runtime, side, why, started)
            if done.returncode != 0:
                reason = f"the image build exited {done.returncode}"
                report = {**failed, "reason": reason, "build": {"exit": done.returncode, "output": output[-4000:]}}
                return self._report(surface, runtime, side, report, reason, output, started)
            if not archive.is_file():
                return self._unavailable(surface, runtime, side, "the build produced no archive", started)
            derived = host.inspect_container_archive(archive.read_bytes())
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(workspace, ignore_errors=True)
            drop_new_records(client, records)
        report = {
            "v": 1,
            "builder": "container",
            "inspection": "host",
            "build": {"exit": 0, "output": output[-4000:]},
            "base_images": list(images),
            **derived,
            "exit": EXIT_FINDINGS if derived.get("secrets") else EXIT_OK,
        }
        return self._report(surface, runtime, side, report, "image built", output, started)


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "RUNNER_ID",
    "ContainerImageRunner",
    "bounded_command",
    "build_argv",
    "build_records",
    "dockerfile_refusal",
    "drop_new_records",
]
