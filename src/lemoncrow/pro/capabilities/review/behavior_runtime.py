"""Built-in behavior producers over existing Review execution evidence.

The two executors Review already has -- runtime validation of configured
surfaces (``runtime_validation.py``/``runtimes.py``) and browser regression
checks (``browser_checks.py``) -- persist one ReviewEvidence row per scenario
and Snapshot side. These producers pair the base (``old``) and head (``new``)
rows of the exact Snapshot and turn every observed difference into a
:class:`~lemoncrow.pro.capabilities.review.behavior.BehaviorDelta`. Identical
sides produce no delta. A row whose artifact bytes do not match its recorded
digest is skipped visibly; nothing here trusts a reporter's PASS.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from lemoncrow.pro.capabilities.review.behavior import (
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorContext,
    BehaviorDelta,
    BehaviorEvidenceRef,
    BehaviorObservation,
    BehaviorProducer,
    BehaviorProduction,
    BehaviorSeverity,
    BehaviorSide,
    ExecutedScope,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence

RUNTIME_VALIDATION_PRODUCER_ID = "lemoncrow.runtime_validation"
BROWSER_CHECK_PRODUCER_ID = "lemoncrow.browser_checks"

_OUTCOMES = frozenset({"PASS", "FAIL"})
_SIDES: dict[str, BehaviorSide] = {"old": "base", "new": "head"}


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _check_ref(item: ReviewEvidence) -> str:
    return f"check:{item.source}:{item.source_ref}" if item.source_ref else f"check:{item.id}"


def _read_record(context: BehaviorContext, item: ReviewEvidence) -> Mapping[str, Any] | None:
    payload = context.read_artifact(item)
    if payload is None or not item.content_hash or hashlib.sha256(payload).hexdigest() != item.content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _newest(
    rows: Sequence[tuple[ReviewEvidence, Mapping[str, Any]]],
    key: Any,
) -> dict[tuple[str, str], tuple[ReviewEvidence, Mapping[str, Any]]]:
    newest: dict[tuple[str, str], tuple[ReviewEvidence, Mapping[str, Any]]] = {}
    for item, record in sorted(rows, key=lambda row: (row[0].created_at, row[0].id), reverse=True):
        newest.setdefault(key(record), (item, record))
    return newest


def _record_refs(item: ReviewEvidence, side: BehaviorSide, title: str) -> tuple[BehaviorEvidenceRef, ...]:
    return (
        BehaviorEvidenceRef(
            ref=f"artifact:{item.id}",
            kind="artifact",
            label=f"{title} · {side} run record",
            digest=item.content_hash,
            side=side,
            role="record",
            evidence_id=item.id,
            media_type=item.mime_type,
        ),
        BehaviorEvidenceRef(
            ref=_check_ref(item),
            kind="check",
            label=f"{title} · {side} result {item.verification_status or 'baseline'}".strip(),
            digest=item.content_hash,
            side=side,
            role="check",
            evidence_id=item.id,
        ),
    )


def _outcome_delta(
    *,
    producer: str,
    subject: str,
    title: str,
    base_status: str,
    head_status: str,
    base_ref: str,
    head_ref: str,
    base_reason: str,
    head_reason: str,
    evidence: tuple[BehaviorEvidenceRef, ...],
    paths: tuple[str, ...],
    target_ids: tuple[str, ...],
    base_digest: str,
    head_digest: str,
    execution: Mapping[str, Any],
) -> BehaviorDelta | None:
    """A PASS<->FAIL flip between the two sides; UNKNOWN/NOT_RUN never flip."""

    if base_status not in _OUTCOMES or head_status not in _OUTCOMES or base_status == head_status:
        return None
    regression = head_status == "FAIL"
    return BehaviorDelta(
        kind="test_outcome",
        producer=producer,
        subject=subject,
        title=title,
        summary=(
            f"Head run fails where the base run did not: {head_reason}"
            if regression
            else "Base run fails; the head run no longer does"
        ),
        base=BehaviorObservation("base", base_status, base_digest, base_reason or base_status, base_ref),
        head=BehaviorObservation("head", head_status, head_digest, head_reason or head_status, head_ref),
        evidence=evidence,
        severity="high" if regression else "medium",
        critical=regression,
        paths=paths,
        target_ids=target_ids,
        facts=(("Outcome", f"{base_status} → {head_status}"),),
        **execution,
    )


def _side_execution(item: ReviewEvidence, record: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """``(environment fingerprint, executor class, configured unverified integrations)`` of one side.

    The executor class is the dispatcher's stamp on the evidence row, never
    the runner's own claim. The fingerprint and integrations come from the
    typed execution record; an untyped or unreadable record contributes none.
    Only integrations the trusted base declared are reported: the built-in
    floor (payments, third-party APIs, production data) applies to every run
    and would turn each delta into the same three manual checks.
    """

    from lemoncrow.pro.environment.execution import (
        DEFAULT_UNVERIFIED_INTEGRATIONS,
        ExecutionResult,
        parse_executor_class,
    )

    executor = parse_executor_class(item.executor_class).value if item.executor_class else ""
    data = record.get("data")
    raw = data.get("execution") if isinstance(data, Mapping) else record.get("execution")
    if not isinstance(raw, Mapping):
        return "", executor, ()
    try:
        result = ExecutionResult.from_wire(raw)
    except (TypeError, ValueError):
        return "", executor, ()
    floor = {integration.name for integration in DEFAULT_UNVERIFIED_INTEGRATIONS}
    configured = tuple(row.name for row in result.verification.not_verified if row.name not in floor)
    return result.environment_fingerprint, executor or result.executor_class.value, configured


def _pair_execution(
    old: tuple[ReviewEvidence, Mapping[str, Any]] | None,
    new: tuple[ReviewEvidence, Mapping[str, Any]],
) -> dict[str, Any]:
    """``BehaviorDelta`` environment fields for a base/head pair.

    Equal fingerprints pass through; differing ones become one opaque pair
    digest, stable while neither side's environment changes. The executor is
    the least trusted of the two sides, so a self-attested base run cannot
    ride on a sandboxed head run. Any change to either side changes the
    behavior fingerprint and reopens a judgment.
    """

    base = _side_execution(*old) if old is not None else ("", "", ())
    head = _side_execution(*new)
    if base[0] == head[0]:
        environment = head[0]
    else:
        environment = "envpair:sha256:" + _digest({"base": base[0], "head": head[0]})
    classes = {base[1], head[1]} - {""}
    executor = "author_machine" if "author_machine" in classes else head[1] or base[1]
    return {
        "environment_fingerprint": environment,
        "executor_class": executor,
        "unverified_integrations": tuple(dict.fromkeys((*head[2], *base[2]))),
    }


def _first_reason(record: Mapping[str, Any]) -> str:
    reasons = record.get("status_reasons")
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])[:200]
    return str(record.get("summary") or "")[:200]


class RuntimeValidationBehaviorProducer:
    """Base-vs-head deltas of trusted runtime-validation surface runs."""

    producer_id = RUNTIME_VALIDATION_PRODUCER_ID
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE

        rows: list[tuple[ReviewEvidence, Mapping[str, Any]]] = []
        degraded: list[str] = []
        for item in context.evidence:
            if item.kind != "verification" or item.mime_type != RUNTIME_VALIDATION_MEDIA_TYPE:
                continue
            if item.revision_id != context.revision.id or item.execution_intent == "explicit_historical":
                continue
            record = _read_record(context, item)
            if record is None:
                degraded.append(f"runtime validation record {item.id} is missing or does not match its digest")
                continue
            if str(record.get("side") or "") not in _SIDES:
                continue
            request = record.get("request")
            if isinstance(request, Mapping) and request.get("scenario_kind") == "recipe":
                # Check recipes have their own differential producer (``differential.py``).
                continue
            rows.append((item, record))

        def scenario_id(record: Mapping[str, Any]) -> str:
            request = record.get("request")
            return str(request.get("scenario_id") or "") if isinstance(request, Mapping) else ""

        newest = _newest(rows, lambda record: (scenario_id(record), str(record.get("side") or "")))
        executed = []
        for (_scenario, side), (item, record) in newest.items():
            request = record.get("request")
            target_ids = (
                tuple(str(value) for value in request.get("target_ids") or () if str(value))
                if isinstance(request, Mapping)
                else ()
            )
            executed.append(
                ExecutedScope(
                    producer=self.producer_id,
                    run_ref=_check_ref(item),
                    side=_SIDES[side],
                    paths=(item.path,) if item.path else (),
                    target_ids=target_ids,
                )
            )

        deltas: list[BehaviorDelta] = []
        unchanged = 0
        for scenario in sorted({key[0] for key in newest if key[0]}):
            old = newest.get((scenario, "old"))
            new = newest.get((scenario, "new"))
            if old is None or new is None:
                continue
            found = self._pair(scenario, old, new)
            if found:
                deltas.extend(found)
            else:
                unchanged += 1
        return BehaviorProduction(
            deltas=tuple(deltas), executed=tuple(executed), unchanged=unchanged, degraded=tuple(degraded)
        )

    def _pair(
        self,
        scenario: str,
        old: tuple[ReviewEvidence, Mapping[str, Any]],
        new: tuple[ReviewEvidence, Mapping[str, Any]],
    ) -> list[BehaviorDelta]:
        from lemoncrow.pro.environment.runners import (
            _VOLATILE_HTTP_HEADERS,
            SurfaceRunResult,
            api_response_diff_score,
        )

        old_item, old_record = old
        new_item, new_record = new
        raw_scenario = new_record.get("scenario")
        title = str(raw_scenario.get("title") or scenario) if isinstance(raw_scenario, Mapping) else scenario
        evidence = (*_record_refs(old_item, "base", title), *_record_refs(new_item, "head", title))
        paths = tuple(dict.fromkeys(path for path in (new_item.path, old_item.path) if path))
        request = new_record.get("request")
        target_ids = (
            tuple(str(value) for value in request.get("target_ids") or () if str(value))
            if isinstance(request, Mapping)
            else ()
        )
        found: list[BehaviorDelta] = []
        execution = _pair_execution(old, new)

        def checks_digest(record: Mapping[str, Any]) -> str:
            return _digest({"status": record.get("status"), "checks": record.get("deterministic_checks") or []})

        outcome = _outcome_delta(
            producer=self.producer_id,
            subject=scenario,
            title=title,
            base_status=str(old_record.get("status") or ""),
            head_status=str(new_record.get("status") or ""),
            base_ref=f"artifact:{old_item.id}",
            head_ref=f"artifact:{new_item.id}",
            base_reason=_first_reason(old_record),
            head_reason=_first_reason(new_record),
            evidence=evidence,
            paths=paths,
            target_ids=target_ids,
            base_digest=checks_digest(old_record),
            head_digest=checks_digest(new_record),
            execution=execution,
        )
        if outcome is not None:
            found.append(outcome)

        old_data = old_record.get("data")
        new_data = new_record.get("data")
        if isinstance(old_data, Mapping) and isinstance(new_data, Mapping):
            try:
                score = api_response_diff_score(
                    SurfaceRunResult(scenario, "", "old", "", "", data=dict(old_data)),
                    SurfaceRunResult(scenario, "", "new", "", "", data=dict(new_data)),
                )
            except ValueError:
                score = None
            if score is not None and (
                score.status_changed or score.body_changed_ratio > 0 or score.header_changed_ratio > 0
            ):

                def response_digest(data: Mapping[str, Any]) -> str:
                    headers = data.get("headers")
                    stable = (
                        {
                            str(name).lower(): str(value)
                            for name, value in headers.items()
                            if str(name).lower() not in _VOLATILE_HTTP_HEADERS
                        }
                        if isinstance(headers, Mapping)
                        else {}
                    )
                    return _digest(
                        {"status_code": data.get("status_code"), "body": data.get("body"), "headers": stable}
                    )

                severity: BehaviorSeverity = (
                    "high" if score.status_changed else "medium" if score.body_changed_ratio >= 0.25 else "low"
                )
                label = (
                    f"HTTP {score.old_status_code} → {score.new_status_code}"
                    if score.status_changed
                    else f"HTTP {score.new_status_code}, response changed"
                )
                found.append(
                    BehaviorDelta(
                        kind="api_response",
                        producer=self.producer_id,
                        subject=scenario,
                        title=title,
                        summary=f"{label} · body Δ {score.body_changed_ratio * 100:.1f}%"
                        + (
                            f" · headers Δ {score.header_changed_ratio * 100:.1f}%"
                            if score.header_changed_ratio
                            else ""
                        ),
                        base=BehaviorObservation(
                            "base",
                            "",
                            response_digest(old_data),
                            f"HTTP {score.old_status_code}",
                            f"artifact:{old_item.id}",
                        ),
                        head=BehaviorObservation(
                            "head",
                            "",
                            response_digest(new_data),
                            f"HTTP {score.new_status_code}",
                            f"artifact:{new_item.id}",
                        ),
                        evidence=evidence,
                        severity=severity,
                        critical=score.status_changed,
                        paths=paths,
                        target_ids=target_ids,
                        facts=(
                            ("HTTP status", f"{score.old_status_code} → {score.new_status_code}"),
                            ("Body changed", f"{score.body_changed_ratio * 100:.1f}%"),
                            ("Stable headers changed", f"{score.header_changed_ratio * 100:.1f}%"),
                        ),
                        **execution,
                    )
                )
        return found


class BrowserCheckBehaviorProducer:
    """Base-vs-head deltas of browser regression scenario runs."""

    producer_id = BROWSER_CHECK_PRODUCER_ID
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        from lemoncrow.pro.capabilities.review.browser_checks import is_browser_evidence

        rows: list[tuple[ReviewEvidence, Mapping[str, Any]]] = []
        degraded: list[str] = []
        for item in context.evidence:
            if not is_browser_evidence(item) or item.revision_id != context.revision.id:
                continue
            record = _read_record(context, item)
            if record is None:
                degraded.append(f"browser check record {item.id} is missing or does not match its digest")
                continue
            if str(record.get("side") or "") not in _SIDES or not str(record.get("scenario") or ""):
                continue
            rows.append((item, record))

        def key(record: Mapping[str, Any]) -> tuple[str, str]:
            subject = f"{record.get('scenario')}@{record.get('viewport_key') or 'default'}"
            return subject, str(record.get("side") or "")

        newest = _newest(rows, key)
        executed = [
            ExecutedScope(
                producer=self.producer_id,
                run_ref=_check_ref(item),
                side=_SIDES[side],
                paths=(item.path,) if item.path else (),
                path_globs=tuple(context.scenario_paths.get(str(record.get("scenario") or ""), ())),
            )
            for (_subject, side), (item, record) in newest.items()
        ]
        deltas: list[BehaviorDelta] = []
        unchanged = 0
        for subject in sorted({value[0] for value in newest}):
            old = newest.get((subject, "old"))
            new = newest.get((subject, "new"))
            if new is None:
                continue
            found = self._deltas(context, subject, old, new)
            if found:
                deltas.extend(found)
            elif old is not None or _diff_ratio(new[1]) is not None:
                unchanged += 1
        return BehaviorProduction(
            deltas=tuple(deltas), executed=tuple(executed), unchanged=unchanged, degraded=tuple(degraded)
        )

    def _deltas(
        self,
        context: BehaviorContext,
        subject: str,
        old: tuple[ReviewEvidence, Mapping[str, Any]] | None,
        new: tuple[ReviewEvidence, Mapping[str, Any]],
    ) -> list[BehaviorDelta]:
        new_item, new_record = new
        title = str(new_record.get("name") or new_record.get("title") or subject)
        scenario = str(new_record.get("scenario") or "")
        globs = tuple(context.scenario_paths.get(scenario, ()))
        paths = tuple(path for path in context.changed_paths if _matches(path, globs))
        evidence: list[BehaviorEvidenceRef] = list(_record_refs(new_item, "head", title))
        evidence.extend(_artifact_refs(new_record, "head", title))
        if old is not None:
            evidence.extend(_record_refs(old[0], "base", title))
            evidence.extend(_artifact_refs(old[1], "base", title))
        refs = tuple(evidence)
        found: list[BehaviorDelta] = []
        execution = _pair_execution(old, new)
        if old is not None:
            old_item, old_record = old
            outcome = _outcome_delta(
                producer=self.producer_id,
                subject=subject,
                title=title,
                base_status=str(old_record.get("status") or ""),
                head_status=str(new_record.get("status") or ""),
                base_ref=f"artifact:{old_item.id}",
                head_ref=f"artifact:{new_item.id}",
                base_reason=_first_reason(old_record),
                head_reason=_first_reason(new_record),
                evidence=refs,
                paths=paths,
                target_ids=(),
                base_digest=_digest({"status": old_record.get("status"), "checks": old_record.get("assertions")}),
                head_digest=_digest({"status": new_record.get("status"), "checks": new_record.get("assertions")}),
                execution=execution,
            )
            if outcome is not None:
                found.append(outcome)

        ratio = _diff_ratio(new_record)
        threshold = _threshold(new_record)
        new_shot = _artifact_digest(new_record, "screenshot")
        old_shot = _artifact_digest(old[1], "screenshot") if old is not None else ""
        bytes_differ = bool(new_shot and old_shot and new_shot != old_shot)
        if (ratio is not None and ratio > 0) or (ratio is None and bytes_differ):
            exceeds = ratio is not None and threshold is not None and ratio > threshold
            severity: BehaviorSeverity = "high" if exceeds else "medium" if ratio is None or ratio >= 0.01 else "low"
            summary = (
                f"{ratio * 100:.2f}% of pixels differ from the baseline"
                + (f" (configured threshold {threshold * 100:.2f}%)" if threshold is not None else "")
                if ratio is not None
                else "Base and head screenshots differ; no diff ratio was reported"
            )
            facts: list[tuple[str, str]] = []
            if ratio is not None:
                facts.append(("Pixels changed", f"{ratio * 100:.2f}%"))
            if threshold is not None:
                facts.append(("Threshold", f"{threshold * 100:.2f}%"))
            viewport = str(new_record.get("viewport_key") or "")
            if viewport:
                facts.append(("Viewport", viewport))
            found.append(
                BehaviorDelta(
                    kind="visual",
                    producer=self.producer_id,
                    subject=subject,
                    title=title,
                    summary=summary,
                    base=(
                        BehaviorObservation("base", "", old_shot, "baseline screenshot", f"artifact:{old[0].id}")
                        if old is not None and old_shot
                        else None
                    ),
                    head=BehaviorObservation(
                        "head",
                        "",
                        _digest({"screenshot": new_shot, "diff": _artifact_digest(new_record, "diff"), "ratio": ratio}),
                        "head screenshot" + (f" · {ratio * 100:.2f}% changed" if ratio is not None else ""),
                        f"artifact:{new_item.id}",
                    ),
                    evidence=refs,
                    severity=severity,
                    critical=exceeds,
                    paths=paths,
                    facts=tuple(facts),
                    **execution,
                )
            )
        return found


def _matches(path: str, globs: Sequence[str]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatchcase(path, pattern) for pattern in globs)


def _diff_ratio(record: Mapping[str, Any]) -> float | None:
    visual = record.get("visual")
    raw = visual.get("diff_ratio") if isinstance(visual, Mapping) else None
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def _threshold(record: Mapping[str, Any]) -> float | None:
    visual = record.get("visual")
    raw = visual.get("threshold") if isinstance(visual, Mapping) else None
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def _artifact_digest(record: Mapping[str, Any], role: str) -> str:
    artifacts = record.get("artifacts")
    entry = artifacts.get(role) if isinstance(artifacts, Mapping) else None
    return str(entry.get("digest") or "") if isinstance(entry, Mapping) else ""


def _artifact_refs(record: Mapping[str, Any], side: BehaviorSide, title: str) -> list[BehaviorEvidenceRef]:
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return []
    refs: list[BehaviorEvidenceRef] = []
    for role in ("screenshot", "diff", "trace", "video"):
        entry = artifacts.get(role)
        if not isinstance(entry, Mapping) or not str(entry.get("evidence_id") or ""):
            continue
        evidence_id = str(entry["evidence_id"])
        refs.append(
            BehaviorEvidenceRef(
                ref=f"artifact:{evidence_id}",
                kind="artifact",
                label=f"{title} · {side} {role}",
                digest=str(entry.get("digest") or ""),
                side=side,
                role=role,
                evidence_id=evidence_id,
                media_type=str(entry.get("mime_type") or ""),
            )
        )
    return refs


_BUILTINS: tuple[BehaviorProducer, ...] = (RuntimeValidationBehaviorProducer(), BrowserCheckBehaviorProducer())


def builtin_behavior_producers() -> tuple[BehaviorProducer, ...]:
    return _BUILTINS


__all__ = [
    "BROWSER_CHECK_PRODUCER_ID",
    "RUNTIME_VALIDATION_PRODUCER_ID",
    "BrowserCheckBehaviorProducer",
    "RuntimeValidationBehaviorProducer",
    "builtin_behavior_producers",
]
