"""One-command local reproduction of a hermetic execution result.

``lc review --reproduce <evidence-id|record.json>`` re-materializes the exact
Snapshot side a typed :class:`ExecutionResult` ran against, replays its
recipe under the same hermetic stages (registry-only fetch, offline build,
offline run with recording sinks) and compares the outcome and environment
fingerprint with the recorded one.

The execution policy comes from the local trusted base (the committed
``.lemoncrow/review.yaml`` at a trusted ref), never from the record: the
record's policy is only compared, and a mismatch fails unless the caller
explicitly opts into the recorded policy.

A preview record is reproduced over its deterministic parts only: the source
tree and the dependency fetch/build stages are replayed and compared; the
preview renderer's own build and render stages are labelled not compared,
because rendered pixels are not a deterministic output.

A reproduction is always ``author_machine`` and always uses the
``head_untrusted`` cache namespace: it can never write a cache entry a trusted
run reads. Nothing runs unsandboxed; without bubblewrap it refuses.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.hermetic import (
    REPRODUCE_COMMAND,
    HermeticPolicy,
    HermeticRefused,
    HermeticUnavailable,
    ProvisionedDependencies,
    RecordingSinks,
    build_execution_result,
    hermetic_policy_from_wire,
    load_hermetic_policy,
    lockfile_digest,
    offline_bwrap_prefix,
    provision_dependencies,
    provision_npm_dependencies,
    sinks_available,
    workspace_digest,
)
from lemoncrow.pro.capabilities.review.proof import wire
from lemoncrow.pro.capabilities.review.run_evidence import protect_text
from lemoncrow.pro.environment.dependency_cache import DependencyCache
from lemoncrow.pro.environment.execution import (
    EXECUTOR_FINGERPRINT_VERSION,
    REPORT_DIR,
    REPORT_ENV,
    ExecutionResult,
    ExecutionSubject,
    ExecutorClass,
    SinkCapture,
    StageName,
    StageOutcome,
    canonical_digest,
)

_COMMIT: Final = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
# Recipes whose run stage is a Reader render, not a command line.
_RENDER_RECIPES: Final[frozenset[str]] = frozenset({"preview"})
_RENDER_NOT_COMPARED: Final[str] = (
    "preview renderer stage: rendered output is not deterministic, so only the source and dependency stages "
    "are compared"
)
_REVIEW_CONFIG: Final[str] = ".lemoncrow/review.yaml"


class ReproductionError(ValueError):
    """The record or the checkout cannot be reproduced (stated, never guessed)."""


def load_execution_record(raw: Mapping[str, Any]) -> ExecutionResult:
    """The typed execution result inside an evidence record, preview result or bare wire."""

    candidates: list[Any] = [raw]
    data = raw.get("data")
    if isinstance(data, Mapping):
        candidates.insert(0, data.get("execution"))
    candidates.insert(0, raw.get("execution"))
    for candidate in candidates:
        if isinstance(candidate, Mapping) and "schema_version" in candidate and "subject" in candidate:
            try:
                return ExecutionResult.from_wire(candidate)
            except (TypeError, ValueError) as exc:
                raise ReproductionError(f"execution record is invalid: {exc}") from exc
    if isinstance(data, Mapping) and data.get("execution_error"):
        raise ReproductionError(str(data["execution_error"]))
    raise ReproductionError("record carries no typed execution result")


def _git(repo: Path, *args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [wire.system_executable("git"), "-C", str(repo), *args],
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def materialize_snapshot(repo: Path, subject: ExecutionSubject, dest: Path) -> str:
    """Copy the Snapshot side into *dest*; returns what was materialized.

    A commit identity is checked out from the object store (no working-tree
    state); any other identity reproduces the checkout's tracked and
    untracked-but-not-ignored files, and the workspace digest says whether
    that is the recorded tree.
    """

    dest.mkdir(parents=True, exist_ok=True)
    identity = subject.snapshot_identity
    if _COMMIT.match(identity) and _git(repo, "cat-file", "-e", f"{identity}^{{commit}}").returncode == 0:
        index = dest.parent / "reproduce.index"
        env = {"GIT_INDEX_FILE": str(index)}
        steps = (
            _git(repo, "read-tree", identity, env=env),
            _git(repo, "checkout-index", "--all", f"--prefix={dest}{os.sep}", env=env),
        )
        index.unlink(missing_ok=True)
        failed = next((step for step in steps if step.returncode != 0), None)
        if failed is not None:
            raise ReproductionError(
                f"could not materialize commit {identity}: {failed.stderr.decode(errors='replace')}"
            )
        _flatten_symlinks(dest)
        return f"commit {identity}"
    listed = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise ReproductionError(f"{repo} is not a git checkout: {listed.stderr.decode(errors='replace').strip()}")
    for raw in sorted(set(listed.stdout.split(b"\0"))):
        if not raw:
            continue
        rel = Path(os.fsdecode(raw))
        source = repo / rel
        if source.is_symlink() or not source.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return "working tree"
    return "working tree"


def _flatten_symlinks(dest: Path) -> None:
    """Replace every committed symlink with a plain file holding its target text.

    The Review snapshot materializes links the same way. Host-side steps
    (lockfile digests, dependency planning) then never read through a link
    such as ``package-lock.json -> ~/.npmrc`` to a file outside the workspace.
    """

    for current, dirs, files in os.walk(dest):
        for name in (*dirs, *files):
            path = Path(current) / name
            if path.is_symlink():
                target = os.fsencode(os.readlink(path))
                path.unlink()
                path.write_bytes(target)


#: The only environment a LemonCrow runner records for a command recipe.
_RECIPE_ENV_KEYS: Final[frozenset[str]] = frozenset({"CI", REPORT_ENV, "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"})


def _checked_recipe_env(env: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    for key, _value in env:
        if key not in _RECIPE_ENV_KEYS and not key.startswith("LEMONCROW_OBSERVE_"):
            raise ReproductionError(
                f"the record's environment sets {key}, which no LemonCrow runner records; refusing to replay it"
            )
    return env


def trusted_base_commands(repo: Path, ref: str) -> frozenset[tuple[str, ...]]:
    """Every runtime ``command`` committed at *ref*: what a reproduction may execute."""

    from lemoncrow.pro.capabilities.review.surfaces import load_review_surface_config

    shown = _git(repo, "show", f"{ref}:{_REVIEW_CONFIG}")
    if shown.returncode != 0:
        return frozenset()
    with tempfile.TemporaryDirectory(prefix="lc-reproduce-base-") as tmp:
        config_path = Path(tmp) / _REVIEW_CONFIG
        config_path.parent.mkdir(parents=True)
        config_path.write_bytes(shown.stdout)
        try:
            config = load_review_surface_config(Path(tmp))
        except ValueError as exc:
            raise ReproductionError(f"trusted-base review configuration at {ref} is invalid: {exc}") from exc
    return frozenset(command for runtime in config.runtimes if (command := runtime.strings("command")))


def apply_recorded_overlay(repo: Path, subject: ExecutionSubject, dest: Path) -> str:
    """Write the recorded overlay (another side's files) over *dest*, verifying each digest.

    The overlay source must be a commit in *repo*; anything else cannot be
    reproduced exactly and is refused rather than approximated.
    """

    source = subject.overlay_source
    if not _COMMIT.match(source) or _git(repo, "cat-file", "-e", f"{source}^{{commit}}").returncode != 0:
        raise ReproductionError(f"overlay source {source or '(none)'} is not a commit in {repo}")
    root = dest.resolve()
    for path, digest in subject.overlay:
        rel = Path(path)
        target = (root / rel).resolve()
        if rel.is_absolute() or root not in target.parents:
            raise ReproductionError(f"recorded overlay path escapes the workspace: {path}")
        if digest == "deleted":
            target.unlink(missing_ok=True)
            continue
        shown = _git(repo, "show", f"{source}:{path}")
        if shown.returncode != 0:
            raise ReproductionError(f"overlay file {path} is missing from {source}")
        if "sha256:" + hashlib.sha256(shown.stdout).hexdigest() != digest:
            raise ReproductionError(f"overlay file {path} at {source} does not match the recorded digest")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(shown.stdout)
    return f"{len(subject.overlay)} overlaid file(s) from {source}"


def _scope_dir(workspace: Path, scope: str) -> Path:
    if scope in {"", "."}:
        return workspace
    candidate = (workspace / scope.strip("/")).resolve()
    if not candidate.is_relative_to(workspace.resolve()):
        raise ReproductionError(f"recorded scope {scope!r} escapes the workspace")
    return candidate


def default_trusted_base(repo: Path) -> str:
    """The local trusted base: the remote default branch when known, else ``HEAD``."""

    if _git(repo, "rev-parse", "--verify", "--quiet", "origin/HEAD^{commit}").returncode == 0:
        return "origin/HEAD"
    return "HEAD"


def trusted_base_policy(repo: Path, ref: str) -> HermeticPolicy:
    """The execution policy committed at *ref*; never the working tree, never the record."""

    if ref.startswith("-") or _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise ReproductionError(f"trusted base {ref!r} does not resolve to a commit in {repo}")
    shown = _git(repo, "show", f"{ref}:{_REVIEW_CONFIG}")
    text = shown.stdout.decode("utf-8", errors="replace") if shown.returncode == 0 else None
    try:
        return load_hermetic_policy(text)
    except ValueError as exc:
        raise ReproductionError(f"trusted-base execution policy at {ref} is invalid: {exc}") from exc


def recorded_policy_matches(original: ExecutionResult, policy: HermeticPolicy) -> bool:
    """Whether *policy* governs execution exactly as the recorded one did (source label aside)."""

    recorded = dict(original.recipe.policy)
    recorded.pop("source", None)
    return bool(recorded) and canonical_digest(recorded) == policy.comparable_digest


@dataclass(frozen=True, slots=True)
class Reproduction:
    original: ExecutionResult
    replay: ExecutionResult
    materialized: str
    workspace_match: bool | None
    compared: tuple[tuple[StageName, str, str], ...]
    not_replayed: tuple[StageName, ...]
    not_compared: tuple[tuple[StageName, str, str], ...] = ()
    policy_source: str = "recorded"
    policy_matches_record: bool | None = None

    @property
    def scope(self) -> str:
        """``full`` replays every stage; ``deterministic`` (previews) skips the renderer stages."""

        command = self.original.recipe.command
        return "deterministic" if command and command[0] in _RENDER_RECIPES else "full"

    @property
    def environment_differences(self) -> tuple[str, ...]:
        return self.original.fingerprint.differences(self.replay.fingerprint)

    @property
    def outcome_match(self) -> bool:
        if not self.compared:
            # A preview whose dependencies were borrowed has no replayable stage: source only.
            return self.scope == "deterministic"
        return all(before == after for _stage, before, after in self.compared)

    @property
    def compared_outputs(self) -> tuple[str, ...]:
        """What the verdict rests on, in words."""

        return ("source tree digest", *(f"{stage} stage outcome" for stage, _before, _after in self.compared))

    @property
    def reproduced(self) -> bool:
        """Same source, same outcome of every compared stage, every comparable stage replayed."""

        return self.workspace_match is True and self.outcome_match and not self.not_replayed

    def to_wire(self) -> dict[str, Any]:
        return {
            "reproduced": self.reproduced,
            "identity": self.original.identity,
            "scope": self.scope,
            "materialized": self.materialized,
            "workspace_match": self.workspace_match,
            "outcome_match": self.outcome_match,
            "compared_outputs": list(self.compared_outputs),
            "stages": [
                {"stage": stage, "recorded": before, "replayed": after} for stage, before, after in self.compared
            ],
            "not_compared": [
                {"stage": stage, "recorded": recorded, "reason": reason}
                for stage, recorded, reason in self.not_compared
            ],
            "not_replayed": list(self.not_replayed),
            "policy": {"source": self.policy_source, "matches_record": self.policy_matches_record},
            "environment_differences": list(self.environment_differences),
            "recorded_environment": self.original.environment_fingerprint,
            "replay_environment": self.replay.environment_fingerprint,
            "verification": self.replay.verification.statement,
            "replay": self.replay.to_wire(),
        }


def _run_stage(
    command: tuple[str, ...],
    *,
    workspace: Path,
    scope: str,
    env: tuple[tuple[str, str], ...],
    dependencies: ProvisionedDependencies,
    policy: HermeticPolicy,
    sinks: RecordingSinks | None,
    timeout: float,
) -> StageOutcome:
    sandbox_cwd = "/workspace" if scope in {"", "."} else "/workspace/" + scope.strip("/")
    argv = offline_bwrap_prefix(unshare_all=True)
    if Path("/usr/local").is_dir():
        argv.extend(("--ro-bind", "/usr/local", "/usr/local"))
    argv.extend(("--bind", str(workspace), "/workspace"))
    for key, value in env:
        if key == REPORT_ENV and value == REPORT_DIR:
            # The recorded run had a fresh report directory in its private /tmp.
            argv.extend(("--dir", REPORT_DIR))
        argv.extend(("--setenv", key, value))
    if sinks is not None:
        argv.extend(sinks.bwrap_args())
    # Provisioned dependency trees, read-only and first on PATH (after --clearenv).
    argv.extend(dependencies.bwrap_args())
    argv.extend(("--chdir", sandbox_cwd, "--"))
    argv.extend(sinks.wrap(command) if sinks is not None else command)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return StageOutcome("run", "unavailable", detail=f"command could not run: {exc}"[:500])
    output, _count = protect_text((proc.stdout or "").strip()[-8000:], policy.redaction)
    return StageOutcome(
        "run",
        "passed" if proc.returncode == 0 else "failed",
        detail=f"command exited {proc.returncode}",
        exit_code=proc.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        output=output[-2000:],
    )


def _provision(
    project: Path, sandbox_cwd: str, *, render: bool, policy: HermeticPolicy, cache: DependencyCache
) -> ProvisionedDependencies:
    """The recorded fetch/build stages again: npm only for previews (as recorded), every ecosystem otherwise."""

    if not render:
        return provision_dependencies(project, policy=policy, cache=cache, sandbox_project=sandbox_cwd)
    tree = provision_npm_dependencies(project, policy=policy, cache=cache)
    return ProvisionedDependencies(
        stages=tree.stages,
        mounts=((tree.node_modules, f"{sandbox_cwd}/node_modules"),),
        path_prefix=(f"{sandbox_cwd}/node_modules/.bin",),
        lockfile_digest=tree.plan.lockfile_digest,
        ecosystems=("npm",),
    )


def reproduce_execution(
    original: ExecutionResult,
    *,
    repo: Path,
    store_root: Path,
    policy: HermeticPolicy | None = None,
    policy_source: str = "recorded",
    timeout: float = 600.0,
    trusted_commands: frozenset[tuple[str, ...]] | None = None,
) -> Reproduction:
    """Replay *original* from *repo* in the hermetic sandbox and compare.

    *policy* is the caller's trusted policy (``lc review --reproduce`` passes
    the local trusted base's); ``None`` replays under the recorded policy,
    which only an explicit caller opt-in should choose. *trusted_commands*
    (the trusted base's runtime commands, :func:`trusted_base_commands`) bounds
    what may execute: a record whose command is not one of them is refused,
    because the record file is data, not authority. Raises
    :class:`HermeticUnavailable` when bubblewrap is missing (nothing runs
    unsandboxed) and :class:`ReproductionError` for an unusable record.
    """

    if policy is None:
        try:
            policy = hermetic_policy_from_wire(original.recipe.policy)
        except ValueError as exc:
            raise ReproductionError(f"recorded execution policy is invalid: {exc}") from exc
    matches = recorded_policy_matches(original, policy)
    recipe = original.recipe
    render = bool(recipe.command) and recipe.command[0] in _RENDER_RECIPES
    if trusted_commands is not None and recipe.command and not render and recipe.command not in trusted_commands:
        raise ReproductionError(
            f"the record's command {list(recipe.command)!r} is not a runtime command at the trusted base; "
            "only a command committed in the trusted base's .lemoncrow/review.yaml is replayed"
        )
    _checked_recipe_env(recipe.env)
    sandbox_cwd = (
        "/workspace" if original.subject.scope in {"", "."} else "/workspace/" + original.subject.scope.strip("/")
    )
    stages: list[StageOutcome] = []
    captures: tuple[SinkCapture, ...] = ()
    with tempfile.TemporaryDirectory(prefix="lc-reproduce-") as tmp:
        workspace = Path(tmp) / "workspace"
        materialized = materialize_snapshot(repo, original.subject, workspace)
        if original.subject.overlay:
            materialized += " + " + apply_recorded_overlay(repo, original.subject, workspace)
        recorded_tree = original.subject.workspace_digest
        tree = workspace_digest(workspace)
        workspace_match = (tree == recorded_tree) if recorded_tree and tree else None
        project = _scope_dir(workspace, original.subject.scope)
        locks = lockfile_digest(project)
        dependencies = ProvisionedDependencies()
        if "fetch" in recipe.stages:
            # Reproductions never write a cache a trusted run reads.
            cache = DependencyCache.for_store(store_root, "head_untrusted")
            try:
                dependencies = _provision(project, sandbox_cwd, render=render, policy=policy, cache=cache)
            except HermeticRefused as exc:
                stages.append(StageOutcome("fetch", "refused", network="registry_only", detail=str(exc)[:500]))
            except HermeticUnavailable as exc:
                stages.append(StageOutcome("fetch", "unavailable", network="registry_only", detail=str(exc)[:500]))
            else:
                stages.extend(dependencies.stages)
        command = recipe.command
        environment = None
        if original.fingerprint.version >= EXECUTOR_FINGERPRINT_VERSION and command:
            # The record names what its command ran; probe the same for the replay.
            from lemoncrow.pro.capabilities.review.runner_host import command_identity, command_view

            environment = command_identity(
                command,
                view=command_view(workspace=workspace, cwd=sandbox_cwd, deps=dependencies),
                sinks=recipe.sinks,
            )
        blocked = any(stage.status not in {"passed", "skipped"} for stage in stages)
        if "run" in recipe.stages and command and not render and not blocked:
            if recipe.sinks and not sinks_available():
                raise HermeticUnavailable("recorded run used recording sinks; python3 is missing from the runtime")
            sinks = RecordingSinks.create(Path(tmp) / "sinks", sidecars=policy.sidecars) if recipe.sinks else None
            try:
                stages.append(
                    _run_stage(
                        command,
                        workspace=workspace,
                        scope=original.subject.scope,
                        env=recipe.env,
                        dependencies=dependencies,
                        policy=policy,
                        sinks=sinks,
                        timeout=timeout,
                    )
                )
                if sinks is not None:
                    captures, _refs = sinks.collect(
                        review_id=original.subject.review_id,
                        policy=policy.redaction,
                        content_access=original.content_access,
                        artifacts=None,
                    )
            finally:
                if sinks is not None:
                    sinks.cleanup()
    # Recorded and replayed stages pair up in order per stage name, so a
    # preview's own build never compares against the dependency build.
    replayed: dict[str, list[StageOutcome]] = {}
    for stage in stages:
        replayed.setdefault(stage.stage, []).append(stage)
    compared: list[tuple[StageName, str, str]] = []
    not_replayed: list[StageName] = []
    not_compared: list[tuple[StageName, str, str]] = []
    for recorded in original.stages:
        queue = replayed.get(recorded.stage) or []
        replay = queue.pop(0) if queue else None
        if replay is None:
            if render and recorded.stage in {"build", "run"}:
                not_compared.append((recorded.stage, recorded.status, _RENDER_NOT_COMPARED))
            elif recorded.status != "skipped":
                not_replayed.append(recorded.stage)
            continue
        # A cache hit skips a fetch on either side; only stages that ran compare.
        if "skipped" not in {recorded.status, replay.status}:
            compared.append((recorded.stage, recorded.status, replay.status))
    replay_result = build_execution_result(
        subject=replace(original.subject, workspace_digest=tree),
        executor_class=ExecutorClass.AUTHOR_MACHINE,
        policy=policy,
        recipe=replace(recipe, policy=policy.to_wire()),
        stages=stages,
        namespace="head_untrusted",
        content_access=original.content_access,
        lockfile_digest=locks,
        tools=tuple(name for name, _version in original.fingerprint.toolchain),
        captures=captures,
        sinks_active=bool(recipe.sinks and any(stage.stage == "run" for stage in stages)),
        reproduce=REPRODUCE_COMMAND,
        environment=environment,
    )
    return Reproduction(
        original=original,
        replay=replay_result,
        materialized=materialized,
        workspace_match=workspace_match,
        compared=tuple(compared),
        not_replayed=tuple(not_replayed),
        not_compared=tuple(not_compared),
        policy_source=policy_source,
        policy_matches_record=matches,
    )


__all__ = [
    "Reproduction",
    "ReproductionError",
    "apply_recorded_overlay",
    "default_trusted_base",
    "load_execution_record",
    "materialize_snapshot",
    "recorded_policy_matches",
    "reproduce_execution",
    "trusted_base_commands",
    "trusted_base_policy",
]
