"""Recorded execution outputs for hermetic Gate I replay.

Model fixtures answer the AI seam. Execution fixtures answer the product's
``RunSide`` seams. Replay is exact and fail closed: changed case inputs or an
unrecorded request become fixture events and reviewed code is never executed as
a fallback.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from lemoncrow.pro.capabilities.review.evaluation.corpus import EvalCase, digest
from lemoncrow.pro.capabilities.review.evaluation.fixtures import FixtureEvent, FixtureFormatError
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry
from lemoncrow.pro.environment.execution import (
    EnvironmentFingerprint,
    ExecutionRecipe,
    ExecutionResult,
    ExecutionSubject,
    ExecutorClass,
    StageOutcome,
    VerificationEnvironment,
)
from lemoncrow.pro.environment.runners import SurfaceRunResult

EXECUTION_FIXTURE_VERSION: Final[int] = 1
RunSide = Callable[[RuntimeConfigEntry, ReviewSurface, str], SurfaceRunResult]


class ExecutionFixtureFormatError(FixtureFormatError):
    pass


def execution_fixture_path(fixtures_dir: Path, case_id: str) -> Path:
    return fixtures_dir / f"{case_id}.execution.json"


def _unit_digest(surface: ReviewSurface) -> str:
    return str(surface.metadata.get("analysis_digest") or surface.metadata.get("recipe_digest") or "")


def _variant(surface: ReviewSurface) -> str:
    return str(surface.metadata.get("variant") or "")


@dataclass(frozen=True, slots=True)
class ExecutionKey:
    entry_id: str
    side: str
    unit_digest: str
    variant: str
    case_input_digest: str

    @property
    def key(self) -> str:
        return digest(self.to_wire())

    def to_wire(self) -> dict[str, str]:
        return {
            "entry_id": self.entry_id,
            "side": self.side,
            "unit_digest": self.unit_digest,
            "variant": self.variant,
            "case_input_digest": self.case_input_digest,
        }

    @classmethod
    def for_request(cls, case: EvalCase, entry_id: str, surface: ReviewSurface, side: str) -> ExecutionKey:
        return cls(entry_id, side, _unit_digest(surface), _variant(surface), case.input_digest())

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ExecutionKey:
        return cls(
            entry_id=str(raw["entry_id"]),
            side=str(raw["side"]),
            unit_digest=str(raw["unit_digest"]),
            variant=str(raw.get("variant") or ""),
            case_input_digest=str(raw["case_input_digest"]),
        )


@dataclass(frozen=True, slots=True)
class ExecutionFixtureCall:
    key: ExecutionKey
    result: Mapping[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {"key": self.key.key, **self.key.to_wire(), "result": dict(self.result)}


@dataclass(frozen=True, slots=True)
class CaseExecutionFixtures:
    case_id: str
    case_input_digest: str
    recorded_with: Mapping[str, Any]
    runs: tuple[ExecutionFixtureCall, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": EXECUTION_FIXTURE_VERSION,
            "case_id": self.case_id,
            "case_input_digest": self.case_input_digest,
            "recorded_with": dict(self.recorded_with),
            "runs": [row.to_wire() for row in sorted(self.runs, key=lambda item: item.key.key)],
        }


def load_execution_fixtures(fixtures_dir: Path, case_id: str) -> CaseExecutionFixtures | None:
    path = execution_fixture_path(fixtures_dir, case_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != EXECUTION_FIXTURE_VERSION or raw.get("case_id") != case_id:
            raise ExecutionFixtureFormatError(f"{path}: unsupported execution fixture version or case id")
        runs: list[ExecutionFixtureCall] = []
        for row in raw.get("runs") or ():
            if not isinstance(row, Mapping) or not isinstance(row.get("result"), Mapping):
                raise ExecutionFixtureFormatError(f"{path}: malformed execution fixture row")
            key = ExecutionKey.from_wire(row)
            if str(row.get("key") or "") != key.key:
                raise ExecutionFixtureFormatError(f"{path}: an execution key does not match its identity")
            runs.append(ExecutionFixtureCall(key, dict(row["result"])))
        return CaseExecutionFixtures(
            case_id=case_id,
            case_input_digest=str(raw["case_input_digest"]),
            recorded_with=dict(raw.get("recorded_with") or {}),
            runs=tuple(runs),
        )
    except ExecutionFixtureFormatError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ExecutionFixtureFormatError(f"{path}: {exc}") from exc


def write_execution_fixtures(fixtures_dir: Path, fixtures: CaseExecutionFixtures) -> Path:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    path = execution_fixture_path(fixtures_dir, fixtures.case_id)
    path.write_text(
        json.dumps(fixtures.to_wire(), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _surface_result(raw: Mapping[str, Any]) -> SurfaceRunResult:
    code = raw.get("exit_code")
    return SurfaceRunResult(
        surface_id=str(raw.get("surface_id") or ""),
        provider=str(raw.get("provider") or ""),
        side=str(raw.get("side") or ""),
        status=str(raw.get("status") or "unavailable"),
        summary=str(raw.get("summary") or ""),
        runner=str(raw.get("runner") or ""),
        runtime=str(raw.get("runtime") or ""),
        exit_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        duration_ms=max(0, int(raw.get("duration_ms") or 0)),
        output=str(raw.get("output") or ""),
        data=dict(raw.get("data") or {}) if isinstance(raw.get("data"), Mapping) else {},
    )


class ExecutionFixtureReplayer:
    """Exact execution-fixture replay; exposes one ``RunSide`` bound to an entry."""

    live = False

    def __init__(self, case: EvalCase, fixtures: CaseExecutionFixtures | None) -> None:
        self.case = case
        self.fixtures = fixtures
        self._by_key = {row.key.key: row for row in fixtures.runs} if fixtures is not None else {}
        self.events: list[FixtureEvent] = []
        self._lock = threading.Lock()

    def covers(self, entry_id: str) -> bool:
        return entry_id in self.case.execution or any(row.key.entry_id == entry_id for row in self._by_key.values())

    def for_entry(self, entry_id: str) -> RunSide:
        def run(runtime: RuntimeConfigEntry, surface: ReviewSurface, side: str) -> SurfaceRunResult:
            key = ExecutionKey.for_request(self.case, entry_id, surface, side)
            fixtures = self.fixtures
            if fixtures is None:
                event = FixtureEvent("missing", entry_id, "no execution fixtures were recorded for this case", key.key)
            elif fixtures.case_input_digest != self.case.input_digest():
                event = FixtureEvent("stale", entry_id, "case inputs changed since execution recording", key.key)
            else:
                found = self._by_key.get(key.key)
                if found is not None:
                    return _surface_result(found.result)
                same_entry = [row.key for row in fixtures.runs if row.key.entry_id == entry_id]
                if not same_entry:
                    reason = f"no {entry_id} execution was recorded for this case"
                elif not any(row.side == side for row in same_entry):
                    reason = f"no {entry_id} {side} execution was recorded for this case"
                elif not any(row.unit_digest == key.unit_digest for row in same_entry if row.side == side):
                    reason = "recipe or analysis digest changed since execution recording"
                elif not any(row.variant == key.variant for row in same_entry if row.side == side):
                    reason = "execution request variant changed since recording"
                else:
                    reason = "execution request changed since recording"
                event = FixtureEvent("missing", entry_id, reason, key.key)
            with self._lock:
                if event not in self.events:
                    self.events.append(event)
            raise ValueError(event.reason)

        return run


def _script_for(case: EvalCase, entry_id: str, side: str) -> Mapping[str, Any]:
    entry = case.execution.get(entry_id)
    if not isinstance(entry, Mapping):
        raise ValueError(f"case {case.id} has no synthetic execution for {entry_id}")
    raw = entry.get(side, entry.get("default"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"case {case.id} execution.{entry_id} has no {side} result")
    return raw


def _execution_wire(
    runtime: RuntimeConfigEntry, surface: ReviewSurface, side: str, status: str, exit_code: int
) -> dict[str, Any]:
    unit = _unit_digest(surface)
    image = "synthetic:review-eval"
    executor = ExecutorClass.AUTHOR_MACHINE
    fingerprint = EnvironmentFingerprint(image_digest=image, recipe_digest=unit, sandbox="fixture")
    stage_status: Literal["passed", "failed"] = "passed" if status == "passed" and exit_code == 0 else "failed"
    result = ExecutionResult(
        subject=ExecutionSubject(side=side),
        executor_class=executor,
        fingerprint=fingerprint,
        verification=VerificationEnvironment(executor_class=executor, image_digest=image),
        recipe=ExecutionRecipe(command=runtime.strings("command"), cwd=runtime.string("root", ".")),
        stages=(StageOutcome("run", stage_status, exit_code=exit_code),),
        cache_namespace="base" if side == "old" else "head_untrusted",
        reproduce="synthetic Gate I execution fixture",
    )
    return result.to_wire()


class ExecutionSource(Protocol):
    live: bool
    events: list[FixtureEvent]

    def covers(self, entry_id: str) -> bool: ...

    def for_entry(self, entry_id: str) -> RunSide: ...


class LocalExecutionRunner:
    """Real local authoring source. Product runners remain sandboxed and network-disabled."""

    live = True

    def __init__(self, case: EvalCase) -> None:
        self.case = case
        self.events: list[FixtureEvent] = []
        self._registry: Any = None
        self._context: Any = None

    def covers(self, entry_id: str) -> bool:
        return entry_id in self.case.execution

    def bind_context(
        self,
        *,
        repo_root: Path,
        store_root: Path,
        revision: Any,
        changed_paths: tuple[str, ...],
        store: Any,
    ) -> None:
        from lemoncrow.pro.capabilities.review.runner_host import built_in_runner_registry
        from lemoncrow.pro.capabilities.review.surfaces import ReviewSurfaceConfig, SurfaceContext

        self._registry = built_in_runner_registry(include_entrypoints=False)
        self._context = SurfaceContext(
            repo_root=repo_root,
            source_root=repo_root,
            store_root=store_root,
            revision=revision,
            changed_paths=changed_paths,
            config=ReviewSurfaceConfig(),
            store=store,
        )

    def for_entry(self, _entry_id: str) -> RunSide:
        def run(runtime: RuntimeConfigEntry, surface: ReviewSurface, side: str) -> SurfaceRunResult:
            if self._registry is None or self._context is None:
                raise RuntimeError("local execution recorder was not bound to the evaluation revision")
            from dataclasses import replace

            from lemoncrow.pro.capabilities.review.surfaces import ReviewSurfaceConfig

            context = replace(self._context, config=ReviewSurfaceConfig(runtimes=(runtime,)))
            return self._registry.run(context, surface, side=side, cleanup_workspace=True)

        return run


class SyntheticExecutionRunner:
    """Deterministic authoring runner used by ``record --synthetic``; starts no process."""

    live = False

    def __init__(self, case: EvalCase) -> None:
        self.case = case
        self.events: list[FixtureEvent] = []

    def covers(self, entry_id: str) -> bool:
        return entry_id in self.case.execution

    def for_entry(self, entry_id: str) -> RunSide:
        def run(runtime: RuntimeConfigEntry, surface: ReviewSurface, side: str) -> SurfaceRunResult:
            raw = _script_for(self.case, entry_id, side)
            status = str(raw.get("status") or "passed")
            if status not in {"passed", "failed", "unavailable"}:
                raise ValueError(f"case {self.case.id} execution.{entry_id}.{side}.status is invalid")
            default_code = None if status == "unavailable" else 0 if status == "passed" else 1
            value = raw.get("exit_code", default_code)
            exit_code = value if isinstance(value, int) and not isinstance(value, bool) else default_code
            data = dict(raw.get("data") or {}) if isinstance(raw.get("data"), Mapping) else {}
            if "report" in raw:
                report = raw["report"]
                if isinstance(report, (Mapping, list)):
                    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    # Artifact analyses consume the same sealed-driver envelope as
                    # production: hex-encoded UTF-8 JSON. Check runners consume
                    # their native report text (JUnit/Jest/etc.).
                    data["report"] = encoded.encode("utf-8").hex() if surface.provider == "analysis" else encoded
                else:
                    data["report"] = str(report)
            if isinstance(raw.get("observation"), Mapping):
                data["observation"] = dict(raw["observation"])
            if exit_code is not None:
                data["execution"] = _execution_wire(runtime, surface, side, status, exit_code)
            return SurfaceRunResult(
                surface_id=surface.id,
                provider=surface.provider,
                side=side,
                status=status,
                summary=str(raw.get("summary") or f"synthetic {entry_id} {side} {status}"),
                runner="fixture",
                runtime=runtime.id,
                exit_code=exit_code,
                duration_ms=max(0, int(raw.get("duration_ms") or 1)),
                output=str(raw.get("output") or ""),
                data=data,
            )

        return run


class RecordingExecutionRunner:
    """Record exact product ``RunSide`` requests while answering them synthetically."""

    live = False

    def __init__(self, case: EvalCase, underlying: ExecutionSource) -> None:
        self.case = case
        self.underlying = underlying
        self.calls: dict[str, ExecutionFixtureCall] = {}
        self.events: list[FixtureEvent] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()

    def covers(self, entry_id: str) -> bool:
        return self.underlying.covers(entry_id)

    def bind_context(self, **kwargs: Any) -> None:
        binder = getattr(self.underlying, "bind_context", None)
        if callable(binder):
            binder(**kwargs)

    def for_entry(self, entry_id: str) -> RunSide:
        inner = self.underlying.for_entry(entry_id)

        def run(runtime: RuntimeConfigEntry, surface: ReviewSurface, side: str) -> SurfaceRunResult:
            key = ExecutionKey.for_request(self.case, entry_id, surface, side)
            with self._lock:
                previous = self.calls.get(key.key)
            if previous is not None:
                return _surface_result(previous.result)
            try:
                result = inner(runtime, surface, side)
            except Exception as exc:
                self.errors.append(f"{entry_id} {side}: {type(exc).__name__}: {exc}")
                raise
            row = ExecutionFixtureCall(key=key, result=result.to_payload())
            with self._lock:
                previous = self.calls.get(key.key)
                if previous is not None and previous.result != row.result:
                    self.errors.append(f"{entry_id} {side}: the same execution request returned different results")
                self.calls[key.key] = row
            return result

        return run


__all__ = [
    "EXECUTION_FIXTURE_VERSION",
    "CaseExecutionFixtures",
    "ExecutionFixtureCall",
    "ExecutionFixtureFormatError",
    "ExecutionFixtureReplayer",
    "ExecutionKey",
    "LocalExecutionRunner",
    "RecordingExecutionRunner",
    "SyntheticExecutionRunner",
    "execution_fixture_path",
    "load_execution_fixtures",
    "write_execution_fixtures",
]
