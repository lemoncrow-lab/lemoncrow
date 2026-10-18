"""Recorded model outputs: record once, replay offline, never reuse a stale one.

Every structured model call the AI review makes passes through one
``structured_executor`` seam (``ai_review.run_independent_review``). A fixture
is keyed by the exact call identity::

    (stage, driver, model, sha256(system prompt), sha256(output schema), sha256(prompt))

:class:`FixtureReplayer` (CI mode) answers only an exact key match. When there
is none it reports *why* -- the case inputs changed since recording, the model
changed, the prompt template changed, the output schema changed, or the prompt
input changed -- raises, and the AI stage degrades exactly as a failed provider
call would. A near-miss is never served.

Recording (:class:`RecordingExecutor`) wraps a responder: the
:class:`SyntheticReviewer` (scripted per case, no model, free) or
:func:`live_responder` (a real provider, refused in CI and under pytest, and
capped by a mandatory :class:`LiveBudget`). The live driver is imported only
inside :func:`live_responder`; replay never constructs one.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.ai_review import DEDUPE_SCHEMA, HYPOTHESIS_SCHEMA, INVESTIGATION_SCHEMA
from lemoncrow.pro.capabilities.review.evaluation.corpus import EvalCase, canonical_json, digest
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution

FIXTURE_VERSION: Final[int] = 1
Stage = Literal["hypothesis_generation", "finding_investigation", "finding_dedupe"]
RecordingMode = Literal["synthetic", "live"]
EventStatus = Literal["stale", "missing"]


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def schema_digest(schema: Mapping[str, Any]) -> str:
    return digest(dict(schema))


#: Output schema digest -> stage; the executor seam does not pass the capability.
STAGE_BY_SCHEMA: Final[Mapping[str, Stage]] = {
    schema_digest(HYPOTHESIS_SCHEMA): "hypothesis_generation",
    schema_digest(INVESTIGATION_SCHEMA): "finding_investigation",
    schema_digest(DEDUPE_SCHEMA): "finding_dedupe",
}
#: Which catalog entry a stage's model call is spent on (per-entry cost attribution).
ENTRY_BY_STAGE: Final[Mapping[str, str]] = {
    "hypothesis_generation": "ai.hypotheses",
    "finding_investigation": "ai.investigators",
    "finding_dedupe": "ai.investigators",
}


def stage_for(output_schema: Mapping[str, Any]) -> str:
    return STAGE_BY_SCHEMA.get(schema_digest(output_schema), "unknown")


@dataclass(frozen=True, slots=True)
class CallKey:
    stage: str
    driver: str
    model: str
    system_digest: str
    schema_digest: str
    prompt_digest: str

    @classmethod
    def of(cls, kwargs: Mapping[str, Any]) -> CallKey:
        schema = cast(Mapping[str, Any], kwargs.get("output_schema") or {})
        return cls(
            stage=stage_for(schema),
            driver=str(kwargs.get("driver") or ""),
            model=str(kwargs.get("model") or ""),
            system_digest=text_digest(str(kwargs.get("system") or "")),
            schema_digest=schema_digest(schema),
            prompt_digest=text_digest(str(kwargs.get("prompt") or "")),
        )

    @property
    def key(self) -> str:
        return digest(self.to_wire())

    def to_wire(self) -> dict[str, str]:
        return {
            "stage": self.stage,
            "driver": self.driver,
            "model": self.model,
            "system_digest": self.system_digest,
            "schema_digest": self.schema_digest,
            "prompt_digest": self.prompt_digest,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> CallKey:
        return cls(**{name: str(raw[name]) for name in cls.__slots__})


@dataclass(frozen=True, slots=True)
class FixtureCall:
    key: CallKey
    response: Mapping[str, Any]
    usage: Mapping[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {"key": self.key.key, **self.key.to_wire(), "response": dict(self.response), "usage": dict(self.usage)}


@dataclass(frozen=True, slots=True)
class CaseFixtures:
    case_id: str
    case_input_digest: str
    recorded_with: Mapping[str, Any]
    calls: tuple[FixtureCall, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": FIXTURE_VERSION,
            "case_id": self.case_id,
            "case_input_digest": self.case_input_digest,
            "recorded_with": dict(self.recorded_with),
            "calls": [call.to_wire() for call in sorted(self.calls, key=lambda item: (item.key.stage, item.key.key))],
        }

    def identities(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted({(call.key.driver, call.key.model) for call in self.calls}))


class FixtureFormatError(ValueError):
    pass


def fixture_path(fixtures_dir: Path, case_id: str) -> Path:
    return fixtures_dir / f"{case_id}.json"


def load_case_fixtures(fixtures_dir: Path, case_id: str) -> CaseFixtures | None:
    path = fixture_path(fixtures_dir, case_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != FIXTURE_VERSION or raw.get("case_id") != case_id:
            raise FixtureFormatError(f"{path}: unsupported fixture version or case id")
        calls = []
        for row in raw.get("calls") or ():
            key = CallKey.from_wire(row)
            if str(row.get("key") or "") != key.key:
                raise FixtureFormatError(f"{path}: a call's key does not match its identity")
            calls.append(FixtureCall(key=key, response=dict(row["response"]), usage=dict(row.get("usage") or {})))
        return CaseFixtures(
            case_id=case_id,
            case_input_digest=str(raw["case_input_digest"]),
            recorded_with=dict(raw.get("recorded_with") or {}),
            calls=tuple(calls),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise FixtureFormatError(f"{path}: {exc}") from exc


def write_case_fixtures(fixtures_dir: Path, fixtures: CaseFixtures) -> Path:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    path = fixture_path(fixtures_dir, fixtures.case_id)
    path.write_text(
        json.dumps(fixtures.to_wire(), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------- #
# replay (CI mode)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FixtureEvent:
    status: EventStatus
    stage: str
    reason: str
    prompt_digest: str

    def to_wire(self) -> dict[str, str]:
        return {"status": self.status, "stage": self.stage, "reason": self.reason, "prompt_digest": self.prompt_digest}


@dataclass(frozen=True, slots=True)
class ServedCall:
    """One model call answered (replay) or made (recording), for cost accounting."""

    stage: str
    driver: str
    model: str
    usage: ModelCallUsage

    def to_wire(self) -> dict[str, Any]:
        return {"stage": self.stage, "driver": self.driver, "model": self.model, "usage": self.usage.to_wire()}


class FixtureUnavailable(RuntimeError):
    """No exact fixture answers this call; the reason is recorded as a FixtureEvent."""


def _usage(raw: Mapping[str, Any]) -> ModelCallUsage:
    usage = ModelCallUsage.from_wire(raw)
    return (
        usage
        if usage.calls
        else ModelCallUsage(
            calls=1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_write_input_tokens=usage.cache_write_input_tokens,
            provider_cost=usage.provider_cost,
            currency=usage.currency,
            request_id=usage.request_id,
        )
    )


class FixtureReplayer:
    """A ``structured_executor`` that answers only from exact recorded fixtures."""

    live = False

    def __init__(self, case: EvalCase, fixtures: CaseFixtures | None) -> None:
        self._case = case
        self._fixtures = fixtures
        self._by_key = {call.key.key: call for call in fixtures.calls} if fixtures is not None else {}
        self._lock = threading.Lock()
        self.served: list[ServedCall] = []
        self.events: list[FixtureEvent] = []

    def _miss(self, key: CallKey) -> FixtureEvent:
        fixtures = self._fixtures
        if fixtures is None:
            return FixtureEvent("missing", key.stage, "no fixtures were recorded for this case", key.prompt_digest)
        if fixtures.case_input_digest != self._case.input_digest():
            return FixtureEvent("stale", key.stage, "case inputs changed since recording", key.prompt_digest)
        same_stage = [call.key for call in fixtures.calls if call.key.stage == key.stage]
        if not same_stage:
            return FixtureEvent(
                "missing", key.stage, f"no {key.stage} call was recorded for this case", key.prompt_digest
            )
        checks: tuple[tuple[Callable[[CallKey], bool], str], ...] = (
            (
                lambda other: (other.driver, other.model) == (key.driver, key.model),
                "model changed: recorded with "
                + ", ".join(sorted({f"{item.driver}/{item.model}" for item in same_stage}))
                + f", replay uses {key.driver}/{key.model}",
            ),
            (lambda other: other.system_digest == key.system_digest, "prompt template (system prompt) changed"),
            (lambda other: other.schema_digest == key.schema_digest, "output schema changed"),
        )
        candidates = same_stage
        for matches, reason in checks:
            narrowed = [other for other in candidates if matches(other)]
            if not narrowed:
                return FixtureEvent("stale", key.stage, reason, key.prompt_digest)
            candidates = narrowed
        return FixtureEvent(
            "stale",
            key.stage,
            "prompt input changed (capture, candidates or context assembly differ from the recording)",
            key.prompt_digest,
        )

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        key = CallKey.of(kwargs)
        call = self._by_key.get(key.key)
        if call is None:
            event = self._miss(key)
            with self._lock:
                self.events.append(event)
            raise FixtureUnavailable(f"{event.status} fixture for {event.stage}: {event.reason}")
        usage = _usage(call.usage)
        with self._lock:
            self.served.append(ServedCall(key.stage, key.driver, key.model, usage))
        return StructuredExecution(
            result=json.loads(canonical_json(dict(call.response))),
            driver=cast(Any, key.driver),
            model=key.model,
            usage=usage,
        )


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #


Responder = Callable[..., StructuredExecution]


class RecordingExecutor:
    """Wraps a responder and keeps every exact call it answered."""

    live = False

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self._lock = threading.Lock()
        self.calls: dict[str, FixtureCall] = {}
        self.served: list[ServedCall] = []
        self.errors: list[str] = []
        self.events: list[FixtureEvent] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        key = CallKey.of(kwargs)
        with self._lock:
            known = self.calls.get(key.key)
        if known is not None:
            usage = _usage(known.usage)
            with self._lock:
                self.served.append(ServedCall(key.stage, key.driver, key.model, usage))
            return StructuredExecution(
                result=dict(known.response), driver=cast(Any, key.driver), model=key.model, usage=usage
            )
        try:
            execution = self._responder(**kwargs)
        except Exception as exc:
            with self._lock:
                self.errors.append(f"{key.stage}: {type(exc).__name__}: {exc}")
            raise
        usage = execution.usage if execution.usage.calls else ModelCallUsage(calls=1)
        call = FixtureCall(
            key=CallKey(
                stage=key.stage,
                driver=str(execution.driver),
                model=execution.model,
                system_digest=key.system_digest,
                schema_digest=key.schema_digest,
                prompt_digest=key.prompt_digest,
            ),
            response=json.loads(canonical_json(dict(execution.result))),
            usage=usage.to_wire(),
        )
        with self._lock:
            self.calls[call.key.key] = call
            self.served.append(ServedCall(key.stage, str(execution.driver), execution.model, usage))
        return execution


def _prompt_document(kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        value = json.loads(str(kwargs.get("prompt") or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, Mapping) else {}


def _block(document: Mapping[str, Any], name: str) -> Any:
    block = document.get(name)
    return block.get("value") if isinstance(block, Mapping) else None


class SyntheticReviewer:
    """Deterministic scripted responder used to author fixtures without a model.

    * hypothesis synthesis: one hypothesis per supplied deterministic candidate;
    * investigation: ``confirmed`` when a ``synthetic_reviewer.confirm`` entry
      names the hypothesis category and the path of one of its targets, citing
      the canonical ``target:`` ref the registry supplied; otherwise
      ``not_confirmed``;
    * semantic dedupe: no merge decisions.

    Its outputs are labelled ``synthetic`` in the fixture; they measure the
    pipeline (planning, budgets, candidate selection, validation), not a model.
    """

    def __init__(self, case: EvalCase, *, driver: str, model: str) -> None:
        self._case = case
        self._driver = driver
        self._model = model

    def _execution(self, result: Mapping[str, Any]) -> StructuredExecution:
        return StructuredExecution(
            result=dict(result), driver=cast(Any, self._driver), model=self._model, usage=ModelCallUsage(calls=1)
        )

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        stage = stage_for(cast(Mapping[str, Any], kwargs.get("output_schema") or {}))
        document = _prompt_document(kwargs)
        if stage == "hypothesis_generation":
            limit = int(document.get("max_hypotheses") or 1)
            candidates = _block(document, "candidates") or []
            hypotheses = [
                {
                    "category": str(row.get("category") or ""),
                    "target_ids": [str(item) for item in row.get("target_ids") or ()],
                    "failure_mode": f"Synthetic hypothesis for {row.get('scope') or 'the changed scope'}",
                    "investigation_rationale": "; ".join(str(item) for item in row.get("rationale") or ()),
                    "priority_rationale": f"Deterministic candidate score {row.get('score')}",
                    "evidence_needed": "Trace the changed behavior through the bound target.",
                    "rule_refs": [str(item) for item in row.get("rule_refs") or ()],
                }
                for row in candidates
                if isinstance(row, Mapping)
            ][:limit]
            return self._execution({"hypotheses": hypotheses})
        if stage == "finding_dedupe":
            return self._execution({"decisions": []})
        hypothesis = _block(document, "hypothesis") or {}
        sources = _block(document, "snapshot_source") or {}
        registry = _block(document, "evidence_registry") or []
        category = str(hypothesis.get("category") or "")
        target_ids = [str(item) for item in hypothesis.get("target_ids") or ()]
        paths = {
            str(row.get("path") or ""): target_id
            for target_id in target_ids
            if isinstance(row := sources.get(target_id), Mapping)
        }
        for confirmation in self._case.synthetic_reviewer:
            if confirmation.category != category or confirmation.path not in paths:
                continue
            target_id = paths[confirmation.path]
            refs = [
                str(row.get("ref"))
                for row in registry
                if isinstance(row, Mapping)
                and row.get("kind") == "target"
                and [str(item) for item in row.get("target_ids") or ()] == [target_id]
            ]
            if not refs:
                continue
            return self._execution(
                {
                    "status": "confirmed",
                    "category": category,
                    "target_ids": [target_id],
                    "title": confirmation.title,
                    "explanation": confirmation.explanation,
                    "consequence": confirmation.consequence,
                    "evidence_refs": refs[:1],
                    "rule_refs": [],
                }
            )
        return self._execution(
            {
                "status": "not_confirmed",
                "category": category,
                "target_ids": target_ids,
                "title": "",
                "explanation": "",
                "consequence": "",
                "evidence_refs": [],
                "rule_refs": [],
            }
        )


# --------------------------------------------------------------------------- #
# live recording guard
# --------------------------------------------------------------------------- #


class LiveRecordingRefused(RuntimeError):
    pass


class LiveBudgetExhausted(RuntimeError):
    pass


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in {"", "0", "false", "no"}


def ensure_live_recording_allowed(env: Mapping[str, str] | None = None) -> None:
    """Live model recording never runs in CI or under pytest, whatever the flags say."""

    values = os.environ if env is None else env
    if values.get("PYTEST_CURRENT_TEST"):
        raise LiveRecordingRefused("live model recording never runs under pytest")
    if _truthy(values.get("CI")):
        raise LiveRecordingRefused("live model recording never runs in CI (CI is set)")


@dataclass
class LiveBudget:
    """Hard cap for one live recording; checked before every provider call.

    ``max_calls`` is mandatory. ``max_usd`` additionally requires every call to
    report its provider cost; a call that does not stops the recording. The
    cap is enforced between calls, so the last call may exceed the remainder.
    """

    max_calls: int
    max_usd: float | None = None
    calls: int = 0
    spent_usd: float = 0.0
    stopped: str = ""

    def __post_init__(self) -> None:
        if self.max_calls < 1:
            raise ValueError("a live recording needs a positive --max-calls budget")
        if self.max_usd is not None and self.max_usd <= 0:
            raise ValueError("--max-usd must be positive")

    def to_wire(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_usd": self.max_usd,
            "calls": self.calls,
            "spent_usd": round(self.spent_usd, 6),
            "stopped": self.stopped,
        }


def live_responder(budget: LiveBudget, *, underlying: Responder | None = None) -> Responder:
    """A budget-capped responder over the real provider driver.

    The provider runtime is imported here and nowhere on the replay path.
    *underlying* exists for unit tests of the cap; it is never a real driver
    in the test suite.
    """

    ensure_live_recording_allowed()
    if underlying is None:
        from lemoncrow.pro.capabilities.review.intelligence_runtime import execute_structured

        underlying = execute_structured
    call_underlying = underlying
    lock = threading.Lock()

    def respond(**kwargs: Any) -> StructuredExecution:
        with lock:
            if budget.stopped:
                raise LiveBudgetExhausted(budget.stopped)
            if budget.calls >= budget.max_calls:
                budget.stopped = f"model-call budget of {budget.max_calls} reached"
                raise LiveBudgetExhausted(budget.stopped)
            if budget.max_usd is not None and budget.spent_usd >= budget.max_usd:
                budget.stopped = f"cost budget of ${budget.max_usd:.2f} reached"
                raise LiveBudgetExhausted(budget.stopped)
            budget.calls += 1
        execution = call_underlying(**kwargs)
        cost = execution.usage.provider_cost
        with lock:
            if budget.max_usd is not None:
                if cost is None:
                    budget.stopped = "the provider did not report a cost, so --max-usd cannot be enforced"
                    raise LiveBudgetExhausted(budget.stopped)
                budget.spent_usd += float(cost)
        return execution

    return respond


@dataclass(frozen=True, slots=True)
class RecordedWith:
    mode: RecordingMode
    driver: str
    model: str
    recorded_at: str = ""
    budget: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode, "driver": self.driver, "model": self.model}
        # Synthetic recordings omit the time so re-recording identical outputs is byte-identical.
        if self.recorded_at:
            out["recorded_at"] = self.recorded_at
        if self.budget:
            out["budget"] = dict(self.budget)
        if self.mode == "synthetic":
            out["note"] = "scripted responses; no model was called and no provider was contacted"
        return out


__all__ = [
    "ENTRY_BY_STAGE",
    "FIXTURE_VERSION",
    "STAGE_BY_SCHEMA",
    "CallKey",
    "CaseFixtures",
    "FixtureCall",
    "FixtureEvent",
    "FixtureFormatError",
    "FixtureReplayer",
    "FixtureUnavailable",
    "LiveBudget",
    "LiveBudgetExhausted",
    "LiveRecordingRefused",
    "RecordedWith",
    "RecordingExecutor",
    "ServedCall",
    "SyntheticReviewer",
    "ensure_live_recording_allowed",
    "fixture_path",
    "live_responder",
    "load_case_fixtures",
    "schema_digest",
    "stage_for",
    "text_digest",
    "write_case_fixtures",
]
