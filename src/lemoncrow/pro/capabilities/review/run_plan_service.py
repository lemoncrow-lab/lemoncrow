"""Bind the run planner to one Review store and its trusted-base readers.

The API composes one service per Review app. Every executor seam (AI review,
Brief synthesis, runtime validation, browser-check scheduling) asks it for the
Snapshot's persisted effective run plan, so what executes is exactly what the
Review displays (same digest).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from itertools import islice
from typing import Any, Final, cast

import yaml

from lemoncrow.pro.capabilities.review.capability_catalog import (
    PRESETS,
    DetectedCapabilities,
    DynamicTarget,
    ExecutorLocus,
    detect_capabilities,
)
from lemoncrow.pro.capabilities.review.check_ingest import (
    IngestedSatisfaction,
    ci_equivalence_enabled,
    ingested_satisfactions,
    parse_ci_equivalents,
)
from lemoncrow.pro.capabilities.review.compute_ledger import ComputeLedger
from lemoncrow.pro.capabilities.review.criticality import parse_trusted_base_triage, triage_policy_for
from lemoncrow.pro.capabilities.review.depth_policy import explain_depth_request
from lemoncrow.pro.capabilities.review.economy import SavingsLedger, economy_projection
from lemoncrow.pro.capabilities.review.escalation import EscalationFacts, collect_escalation_facts
from lemoncrow.pro.capabilities.review.fast_lane import FastLaneSignals
from lemoncrow.pro.capabilities.review.independence import IndependencePolicy, resolve_independence
from lemoncrow.pro.capabilities.review.rule_checks import RuleQuestion, rule_questions
from lemoncrow.pro.capabilities.review.rules import organization_block
from lemoncrow.pro.capabilities.review.run_plan import (
    BudgetUsage,
    EffectiveRunSettings,
    FloorRule,
    ManualPick,
    PlanInputs,
    RunPlan,
    RunSettingsLayer,
    SettingOrigin,
    amend_run_plan,
    catalog_wire,
    depth_preview,
    execution_class,
    layer_from_wire,
    load_run_plan,
    parse_review_config_layer,
    persist_run_plan,
    preview_picks,
    rebuild_run_plan,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.run_plan_reverification import reverification_summary
from lemoncrow.pro.capabilities.review.track_record import track_record_assessment

_POLICY_LAYERS = frozenset({"organization", "team", "repository", "trusted_base_repository", "directory"})
_CACHE_LIMIT = 128
# detect_capabilities' default max_paths; one more entry lets it report truncation.
_MAX_DETECTED_PATHS = 200_000


def effective_policy_layers(effective: Any) -> tuple[list[RunSettingsLayer], dict[str, Any]]:
    """Run-settings layers from an effective Review policy document; malformed input fails closed.

    Shared by the per-Review planner and the hosted settings projection so both
    resolve the same operator layers.
    """

    layers: list[RunSettingsLayer] = []
    execution: dict[str, Any] = {}
    if effective is None:
        return layers, execution
    if not isinstance(effective, Mapping):
        raise RuntimeError("effective Review policy must be an object")
    behavior = effective.get("behavior")
    # An invalid trusted-base config withholds ``behavior``; the plan then runs
    # nothing (``policy_invalid_reason``), so only the operator layers resolve.
    withheld = behavior is None and bool(policy_invalid_reason(effective))
    if not withheld and not isinstance(behavior, Mapping):
        raise RuntimeError("effective Review policy is missing behavior")
    effort = behavior.get("effort") if isinstance(behavior, Mapping) else None
    if not withheld and not isinstance(effort, Mapping):
        raise RuntimeError("effective Review policy is missing behavior.effort")
    value = str(effort.get("effective_value") or "").lower() if isinstance(effort, Mapping) else ""
    if not withheld and value not in (*PRESETS, "custom"):
        raise RuntimeError("effective Review policy has an invalid effort")
    origin = effort.get("origin") if isinstance(effort, Mapping) and isinstance(effort.get("origin"), Mapping) else {}
    assert isinstance(origin, Mapping)
    layer_name = str(origin.get("layer") or "built_in_default")
    if not withheld and layer_name != "built_in_default":
        layers.append(
            RunSettingsLayer(
                origin=SettingOrigin(
                    layer_name if layer_name in _POLICY_LAYERS else "organization",
                    str(origin.get("source") or layer_name),
                ),
                effort=value,
            )
        )
    run_plan = effective.get("run_plan")
    if run_plan is not None:
        if not isinstance(run_plan, Mapping):
            raise RuntimeError("effective Review policy run_plan must be an object")
        for raw in run_plan.get("layers") or ():
            if not isinstance(raw, Mapping):
                raise RuntimeError("effective Review policy run_plan layers must be objects")
            layers.append(layer_from_wire(raw))
        execution = {
            "default_run_on": run_plan.get("default_run_on"),
            "allowed_run_on": run_plan.get("allowed_run_on"),
            "allowed_run_on_by_class": run_plan.get("allowed_run_on_by_class"),
            "unavailable_run_on_by_class": run_plan.get("unavailable_run_on_by_class"),
            "executor_loci": run_plan.get("executor_loci"),
        }
    blocked = organization_block(effective)
    if blocked:
        execution["unavailable_run_on_by_class"] = _with_organization_block(
            execution.get("unavailable_run_on_by_class"), blocked
        )
    return layers, execution


#: Execution classes each organization block kind stops (``run_plan.execution_class``).
#: Static analysis only reads content the Review already holds, so neither kind stops it.
#: ``default`` holds only ``check:<key>``: a required LemonCrow-run verification check
#: that no catalog executor maps, so it never runs. ``execution`` stops it anyway, so a
#: future entry outside the ``ai``/``runtime``/``static`` classes is blocked, not missed.
BLOCKED_CLASSES: Final[Mapping[str, tuple[str, ...]]] = {"ai": ("ai",), "execution": ("runtime", "default")}
_EXECUTING_LOCI: Final[tuple[ExecutorLocus, ...]] = ("local", "worker", "managed")


def _with_organization_block(raw: Any, blocked: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """``unavailable_run_on_by_class`` with every locus of each blocked class unavailable.

    The block reason replaces any wiring reason: the plan then shows every entry
    of the class ``not_available`` -- required floors and manual picks included
    -- because ``build_run_plan`` checks unavailability before either.
    """

    merged: dict[str, dict[str, str]] = {
        name: {str(locus): reason for locus, reason in reasons.items()}
        for name, reasons in unavailable_loci(raw).items()
    }
    for kind, reason in blocked.items():
        for name in BLOCKED_CLASSES.get(kind, ()):
            merged[name] = {**merged.get(name, {}), **{locus: reason for locus in _EXECUTING_LOCI}}
    return merged


def policy_invalid_reason(effective: Any) -> str:
    """The trusted-base config error when the effective policy marks it ``invalid``, else ``""``.

    A ``provisional`` trusted base (unreadable, no provider source) is not
    invalid: its operator policy still applies and the plan says provisional.
    """

    if not isinstance(effective, Mapping):
        return ""
    trusted = effective.get("trusted_base")
    if not isinstance(trusted, Mapping) or trusted.get("policy_state") != "invalid":
        return ""
    path = str(trusted.get("path") or ".lemoncrow/review.yaml")
    detail = str(trusted.get("detail") or "").strip() or "configuration could not be read"
    return f"trusted-base {path} is invalid: {detail}"


def required_check_floors(effective: Any) -> tuple[FloorRule, ...]:
    """Typed LemonCrow-owned required checks as plan floors; malformed input fails closed.

    Only ``lemoncrow_verification`` requirements are planned: LemonCrow executes
    them. ``provider_ci`` and other attestation classes are observed, never run.
    Branch/environment scoping is not narrowed here, so a scoped requirement is
    planned on every Review of the repository (over-planning, never omission).
    """

    if not isinstance(effective, Mapping):
        return ()
    requirements = effective.get("requirements")
    if requirements is None:
        return ()
    if not isinstance(requirements, Mapping):
        raise RuntimeError("effective Review policy requirements must be an object")
    settings = requirements.get("settings")
    field = settings.get("required_checks") if isinstance(settings, Mapping) else None
    if field is None:
        return ()
    rows = field.get("effective_value") if isinstance(field, Mapping) else None
    if not isinstance(rows, list):
        raise RuntimeError("effective Review policy required_checks must be a list")
    assert isinstance(field, Mapping)
    origin = field.get("origin") if isinstance(field.get("origin"), Mapping) else {}
    assert isinstance(origin, Mapping)
    layer = str(origin.get("layer") or "organization")
    floor_origin = SettingOrigin(
        layer if layer in _POLICY_LAYERS else "organization",
        f"{layer.replace('_', ' ')} Review policy required_checks",
    )
    floors: dict[str, FloorRule] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("effective Review policy required_checks entries must be objects")
        if row.get("attestation_class") != "lemoncrow_verification":
            continue
        key = str(row.get("stable_key") or "").strip()
        if not key:
            raise RuntimeError("a lemoncrow_verification required check has no stable_key")
        floors.setdefault(key, FloorRule("check", key, floor_origin))
    return tuple(floors.values())


def git_tree_paths(repo: Any, tree: Any, prefix: str = "") -> Iterator[str]:
    """File paths of a pygit2 tree without loading blob objects."""

    for entry in tree:
        path = f"{prefix}/{entry.name}" if prefix else entry.name
        mode = int(getattr(entry, "filemode", 0) or 0)
        if mode == 0o160000:
            continue
        if getattr(entry, "type_str", "") == "tree" or mode == 0o040000:
            try:
                subtree = repo[entry.id]
            except KeyError:
                continue
            yield from git_tree_paths(repo, subtree, path)
        else:
            yield path


def created_since(item: Any, since: float) -> bool:
    """Whether a Review was created at or after ``since`` (unix seconds)."""

    raw = str(getattr(item, "created_at", "") or "")
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created.timestamp() >= since


def resolve_execution_loci(
    raw: Mapping[str, Any],
    *,
    default_run_on: ExecutorLocus = "local",
    allowed_run_on: Sequence[ExecutorLocus] = ("local",),
) -> tuple[ExecutorLocus, tuple[ExecutorLocus, ...], dict[str, tuple[ExecutorLocus, ...]]]:
    """The deployment's executor loci from an effective policy's ``run_plan`` execution block.

    Returns ``(default_run_on, allowed_run_on, allowed_run_on_by_class)``; the
    given defaults apply where the block names nothing. Shared by the planner
    (``RunPlanService``) and the data-flow projection (``data_flow``).
    """

    fallback_allowed: tuple[ExecutorLocus, ...] = tuple(allowed_run_on) or ("local",)

    def loci(value: Any, *, static: bool = False) -> tuple[ExecutorLocus, ...]:
        if not isinstance(value, list):
            return ()
        # ``review_host`` (the process holding the Snapshot) is a locus for
        # static analysis only; executing work never runs there.
        names = {"local", "worker", "managed", "review_host"} if static else {"local", "worker", "managed"}
        return tuple(cast(ExecutorLocus, item) for item in value if item in names)

    allowed = loci(raw.get("allowed_run_on")) or fallback_allowed
    default = raw.get("default_run_on")
    resolved_default = cast(ExecutorLocus, default) if default in allowed else default_run_on
    # Per execution class: hosted AI review and runtime validation may run on
    # different deployment loci.
    by_class_raw = raw.get("allowed_run_on_by_class")
    by_class = (
        {
            name: loci(value, static=name == "static")
            for name, value in by_class_raw.items()
            if name in {"ai", "runtime", "static", "default"} and loci(value, static=name == "static")
        }
        if isinstance(by_class_raw, Mapping)
        else {}
    )
    return resolved_default, allowed, by_class


def unavailable_loci(raw: Any) -> dict[str, dict[ExecutorLocus, str]]:
    """Deployment-stated loci with no wired executor, per execution class, with the reason."""

    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, dict[ExecutorLocus, str]] = {}
    for name, reasons in raw.items():
        if name not in {"ai", "runtime", "default"} or not isinstance(reasons, Mapping):
            continue
        parsed = {
            cast(ExecutorLocus, locus): reason.strip()[:512]
            for locus, reason in reasons.items()
            if locus in {"local", "worker", "managed"} and isinstance(reason, str) and reason.strip()
        }
        if parsed:
            out[str(name)] = parsed
    return out


#: Executors whose catalog locus a deployment may extend, and only onto these loci.
_EXTENSIBLE_EXECUTORS: Final[frozenset[str]] = frozenset({"render_diff"})


def executor_loci(raw: Any) -> dict[str, tuple[ExecutorLocus, ...]]:
    """Deployment-stated extra loci per executor; anything else in the document is ignored."""

    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, tuple[ExecutorLocus, ...]] = {}
    for name, loci in raw.items():
        if name not in _EXTENSIBLE_EXECUTORS or not isinstance(loci, (list, tuple)):
            continue
        parsed = tuple(sorted({cast(ExecutorLocus, locus) for locus in loci if locus in {"worker", "managed"}}))
        if parsed:
            out[str(name)] = parsed
    return out


def _config_targets(content: str) -> tuple[tuple[DynamicTarget, ...], tuple[DynamicTarget, ...]]:
    """Trusted-base ``check_recipes`` and configured ``surfaces`` as catalog targets.

    A ``check_recipes`` row is executable only when it parses as a recipe
    (``recipes.parse_config_recipes``); an invalid row stays addressable and
    is never executed.
    """

    from lemoncrow.pro.capabilities.review.recipes import parse_config_recipes

    valid = {recipe.id for recipe in parse_config_recipes(content)[0]}

    try:
        decoded = yaml.safe_load(content) if content.strip() else None
    except yaml.YAMLError:
        return (), ()
    if not isinstance(decoded, Mapping):
        return (), ()

    def rows(key: str) -> list[Mapping[str, Any]]:
        raw = decoded.get(key)
        return [row for row in raw if isinstance(row, Mapping)] if isinstance(raw, list) else []

    recipes = tuple(
        DynamicTarget(
            id=str(row["id"]).strip(),
            title=str(row.get("title") or row["id"]),
            executable=str(row["id"]).strip() in valid,
            reason="" if str(row["id"]).strip() in valid else "check recipe is invalid in the trusted base",
        )
        for row in rows("check_recipes")[:256]
        if str(row.get("id") or "").strip()
    )
    surfaces = tuple(
        DynamicTarget(
            id=str(row["id"]).strip(),
            title=str(row.get("title") or row["id"]),
            executable=bool(str(row.get("runtime") or "").strip()),
            reason="" if str(row.get("runtime") or "").strip() else "surface has no configured runtime",
        )
        for row in rows("surfaces")[:256]
        if str(row.get("id") or "").strip()
    )
    return recipes, surfaces


#: The catalog target of a trusted-base environment.toml that is invalid: shown, never run.
_INVALID_ENVIRONMENT_TARGET: Final[str] = "environment.toml"


def _environment_targets(
    text: str,
    changed: Sequence[str],
    attributes: Callable[[], Mapping[str, str] | None] = lambda: None,
) -> tuple[tuple[DynamicTarget, ...] | None, tuple[tuple[str, str], ...]]:
    """Trusted-base ``environment.toml`` checks as catalog targets (``None`` when it defines none),
    and ``(id, why)`` for each check the change does not touch.

    A check applies only when the change touches its ``paths``. An invalid file is one
    target that never runs, so ``check_recipes`` never stands in for it; its reason is
    the parse error. *attributes* gives the trusted base's ``.gitattributes`` files, read
    only when a check selects by ``attr:``.
    """

    from lemoncrow.pro.capabilities.review.recipes import (
        ENVIRONMENT_FILE,
        environment_recipes,
        touched_checks,
        uses_attributes,
    )

    recipes, errors = environment_recipes(text)
    if errors:
        invalid = DynamicTarget(
            id=_INVALID_ENVIRONMENT_TARGET,
            title=ENVIRONMENT_FILE,
            executable=False,
            reason=f"invalid in the trusted base: {errors[0]}"[:500],
        )
        return (invalid,), ()
    if not recipes:
        return None, ()
    touched, problem = touched_checks(
        recipes, changed, attributes() if any(uses_attributes(recipe) for recipe in recipes) else None
    )
    targets: list[DynamicTarget] = []
    untouched: list[tuple[str, str]] = []
    for recipe in recipes:
        if recipe.id not in touched and recipe.paths:
            # Unknown applies: a check whose paths cannot be matched runs rather than being skipped.
            targets.append(DynamicTarget(id=recipe.id, title=recipe.title, applicability="unknown", reason=problem))
        elif touched.get(recipe.id):
            targets.append(DynamicTarget(id=recipe.id, title=recipe.title))
        else:
            why = f"the change touches none of its paths ({', '.join(recipe.paths)})"[:500]
            targets.append(DynamicTarget(id=recipe.id, title=recipe.title, applicability="not_applicable", reason=why))
            untouched.append((recipe.id, why))
    return tuple(targets), tuple(untouched)


class RunPlanService:
    """Effective run plans for one Review store."""

    def __init__(
        self,
        *,
        store: Any,
        policy: Callable[[Any, Any], Mapping[str, Any]],
        review_config: Callable[[Any], Any],
        read_base: Callable[[Any, str], str | None],
        read_head: Callable[[Any, str], str | None],
        base_paths: Callable[[Any], Iterable[str] | None],
        changed_paths: Callable[[Any], Sequence[str]],
        browser_policy: Callable[[Any], Any],
        usage: Callable[[Any, Any], Mapping[str, Any] | None] | None = None,
        default_run_on: ExecutorLocus = "local",
        allowed_run_on: Sequence[ExecutorLocus] = ("local",),
        required_paths: Callable[[Any], Sequence[str]] | None = None,
        premerge_checks: Callable[[Any, Any], int] | None = None,
        changed_files: Callable[[Any], Sequence[tuple[str, str, str]]] | None = None,
        review_rules: Callable[[Any, Any], Sequence[Any]] | None = None,
        environment_config: Callable[[Any], Any] | None = None,
    ) -> None:
        """``changed_paths`` scopes optional path-scoped work; ``required_paths``
        (default: the same) scopes policy floors and required browser scenarios,
        so a narrower re-run scope never drops required work.

        ``premerge_checks`` counts the model-evaluated pre-merge checks the
        trusted base and operator policy configure for a Snapshot (never
        head-side policy); it makes ``ai.premerge_checks`` applicable. It is
        read per plan, not cached with detection: operator policy can change
        without a new Snapshot.

        ``changed_files`` lists the whole Change as ``(path, old_path, status)``
        rows (the packet's files); test-impact selection needs deletions and
        rename sources. Without it, selection runs every test.

        ``review_rules`` returns the Snapshot's Review rules from authoritative
        governance (trusted base and operator policy, never head-side policy);
        each authoritative rule applying to a changed path is one
        ``ai.rule_checks`` question (P11-ESC item 5).

        ``environment_config`` returns the Snapshot's trusted-base
        ``.lemoncrow/environment.toml`` document, read like ``review_config``
        (never head bytes). Its checks, when it defines any, replace
        ``check_recipes``; without it the recipes come from ``review.yaml``.
        """
        self._store = store
        self._policy = policy
        self._review_config = review_config
        self._read_base = read_base
        self._read_head = read_head
        self._base_paths = base_paths
        self._changed_paths = changed_paths
        self._required_paths = required_paths
        self._browser_policy = browser_policy
        self._usage = usage
        self._premerge_checks = premerge_checks
        self._review_rules = review_rules
        self._environment_config = environment_config
        self._default_run_on = default_run_on
        self._allowed_run_on = tuple(allowed_run_on) or ("local",)
        self._detected: dict[str, DetectedCapabilities] = {}
        self._recipes: dict[str, Any] = {}
        self._analyses: dict[str, Any] = {}
        self._changed_files = changed_files
        self._impact: dict[str, dict[str, Any]] = {}
        self._impact_maps: dict[str, Any] = {}
        self._lock = threading.Lock()
        #: In-process executions report here (``recorder(...).record_compute``).
        self.compute = ComputeLedger(store)
        #: Measured compute avoided by shared base results and carried-forward runs.
        self.savings = SavingsLedger(store)

    # ------------------------------------------------------------------ #
    # settings and detection
    # ------------------------------------------------------------------ #

    def _policy_layers(self, session: Any, revision: Any) -> tuple[list[RunSettingsLayer], dict[str, Any]]:
        return effective_policy_layers(self._policy(session, revision).get("effective_review"))

    def _execution(
        self, raw: Mapping[str, Any]
    ) -> tuple[ExecutorLocus, tuple[ExecutorLocus, ...], dict[str, tuple[ExecutorLocus, ...]]]:
        return resolve_execution_loci(raw, default_run_on=self._default_run_on, allowed_run_on=self._allowed_run_on)

    @staticmethod
    def _unavailable(raw: Any) -> dict[str, dict[ExecutorLocus, str]]:
        return unavailable_loci(raw)

    def settings(self, session: Any, revision: Any) -> EffectiveRunSettings:
        layers, execution = self._policy_layers(session, revision)
        config = self._review_config(revision)
        if str(getattr(config, "content", "") or ""):
            layers.append(
                parse_review_config_layer(str(config.content), source=str(config.source or config.path))
                or RunSettingsLayer(origin=SettingOrigin("trusted_base_repository", str(config.source)))
            )
        default_run_on, _allowed, _by_class = self._execution(execution)
        return resolve_run_settings(layers, default_run_on=default_run_on)

    def future_settings(self, revision: Any) -> dict[str, Any]:
        """Head-side ``review:`` edits are proposed future policy, never current authority."""

        config = self._review_config(revision)
        head = self._read_head(revision, str(config.path))
        base_layer = parse_review_config_layer(str(config.content or ""), source="base")
        head_layer = parse_review_config_layer(head or "", source="head") if head is not None else None
        base_doc = base_layer.to_document() if base_layer is not None else {}
        head_doc = head_layer.to_document() if head_layer is not None else {}
        # Head-side focus/criticality/threshold edits are future policy too.
        base_triage = parse_trusted_base_triage(str(config.content or ""), source="base")
        head_triage = parse_trusted_base_triage(head or "", source="head") if head is not None else None
        base_doc = {**base_doc, **(base_triage.to_document() if base_triage is not None else {})}
        head_doc = {**head_doc, **(head_triage.to_document() if head_triage is not None else {})}
        changed = head is not None and base_doc != head_doc
        errors = [
            *(head_layer.errors if head_layer is not None else ()),
            *(head_triage.errors if head_triage is not None else ()),
        ]
        return {
            "changed": changed,
            "applies_to": "later Reviews after this Change lands" if changed else "",
            "proposed": head_doc if changed else None,
            "errors": errors if changed else [],
        }

    def _scopes(self, revision: Any) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
        """(paths optional work is scoped to, paths required work is scoped to when different)."""

        changed = tuple(self._changed_paths(revision))
        if self._required_paths is None:
            return changed, None
        required = tuple(self._required_paths(revision))
        return changed, (None if set(required) == set(changed) else required)

    def environment_text(self, revision: Any) -> str | None:
        """The trusted base's environment.toml text; ``None`` when absent or not known (review.yaml applies)."""

        if self._environment_config is None:
            return None
        document = self._environment_config(revision)
        if str(getattr(document, "state", "")) != "present":
            return None
        return str(getattr(document, "content", "") or "") or None

    def detected(self, revision: Any) -> DetectedCapabilities:
        with self._lock:
            cached = self._detected.get(revision.id)
        if cached is not None:
            return cached
        from lemoncrow.pro.capabilities.review.browser_checks import scenario_applicability

        config = self._review_config(revision)
        changed, required = self._scopes(revision)
        browser = self._browser_policy(revision)
        scenarios = tuple(
            DynamicTarget(
                id=scenario.id,
                title=scenario.title,
                required=scenario.required,
                applicability=cast(Any, state),
                reason=reason,
                executable=True,
            )
            for scenario in getattr(browser, "scenarios", ())
            for state, reason in (
                scenario_applicability(
                    scenario, required if scenario.required and required is not None else changed, None
                ),
            )
        )
        content = str(getattr(config, "content", "") or "")
        recipes, surfaces = _config_targets(content)
        state = str(getattr(config, "state", "present"))
        raw_paths = self._base_paths(revision) if state != "provisional" else None
        # One bounded listing feeds detection and recipe discovery (detection
        # reports truncation past its own cap).
        paths = list(islice(raw_paths, _MAX_DETECTED_PATHS + 1)) if raw_paths is not None else None
        environment = self.environment_text(revision)
        untouched: tuple[tuple[str, str], ...] = ()
        if environment is not None:
            from lemoncrow.pro.capabilities.review.recipes import attribute_files

            def attributes() -> Mapping[str, str] | None:
                # A truncated listing may miss a .gitattributes file: unknown, never a guess.
                if paths is None or len(paths) > _MAX_DETECTED_PATHS:
                    return None
                return attribute_files(paths, lambda path: self._read_base(revision, path))

            defined, untouched = _environment_targets(environment, changed, attributes)
            if defined is not None:
                recipes = defined
        detected = detect_capabilities(
            paths,
            lambda path: self._read_base(revision, path),
            base_state=cast(Any, state if paths is not None else "provisional"),
            recipes=recipes,
            browser_scenarios=scenarios,
            surfaces=surfaces,
        )
        from lemoncrow.pro.capabilities.review.recipes import KIND_FACT, build_recipe_catalog

        catalog = build_recipe_catalog(
            content,
            paths,
            lambda path: self._read_base(revision, path),
            base_state=cast(Any, detected.base_state),
            environment=environment,
        )
        if untouched:
            catalog = replace(catalog, untouched=untouched)
        # A trusted-base recipe makes its check kind applicable even where file
        # names alone did not (a configured recipe, a Makefile target). An
        # environment.toml check the change does not touch does not.
        extra = {
            KIND_FACT[recipe.kind]: f"check recipe {recipe.id} ({recipe.source or recipe.origin})"
            for recipe in reversed(catalog.recipes)
            if recipe.kind in KIND_FACT
            and KIND_FACT[recipe.kind] not in detected.facts
            and not catalog.skipped(recipe.id)
        }
        if extra:
            detected = replace(
                detected, facts=detected.facts | frozenset(extra), evidence={**extra, **dict(detected.evidence)}
            )
        from lemoncrow.pro.capabilities.review.artifact_analyses import apply_analysis_facts, build_analysis_catalog

        # Artifact/infrastructure analyses act only on what the trusted base gives them.
        analyses = build_analysis_catalog(
            content,
            paths,
            lambda path: self._read_base(revision, path),
            base_state=detected.base_state,
        )
        detected = apply_analysis_facts(detected, analyses)
        if detected.base_state != "provisional":
            with self._lock:
                # lc-debt: per-process FIFO of 128 Snapshot detections; a base-tree
                # listing is recomputed per process. Upgrade: persist beside the plan.
                self._detected.pop(revision.id, None)
                self._detected[revision.id] = detected
                self._recipes.pop(revision.id, None)
                self._recipes[revision.id] = catalog
                self._analyses.pop(revision.id, None)
                self._analyses[revision.id] = analyses
                while len(self._detected) > _CACHE_LIMIT:
                    self._detected.pop(next(iter(self._detected)))
                while len(self._recipes) > _CACHE_LIMIT:
                    self._recipes.pop(next(iter(self._recipes)))
                while len(self._analyses) > _CACHE_LIMIT:
                    self._analyses.pop(next(iter(self._analyses)))
        else:
            with self._lock:
                self._recipes[revision.id] = catalog
                self._analyses[revision.id] = analyses
                while len(self._recipes) > _CACHE_LIMIT:
                    self._recipes.pop(next(iter(self._recipes)))
                while len(self._analyses) > _CACHE_LIMIT:
                    self._analyses.pop(next(iter(self._analyses)))
        return detected

    def _with_premerge(self, session: Any, revision: Any, detected: DetectedCapabilities) -> DetectedCapabilities:
        if self._premerge_checks is None:
            return detected
        try:
            count = max(0, int(self._premerge_checks(session, revision)))
        except Exception:
            # Unreadable policy schedules nothing; the checks stay NOT_RUN and block.
            count = 0
        return replace(detected, premerge_checks=count) if count != detected.premerge_checks else detected

    def rule_questions(self, session: Any, revision: Any) -> tuple[RuleQuestion, ...]:
        """One question per authoritative Review rule applying to the changed paths (P11-ESC)."""

        if self._review_rules is None:
            return ()
        try:
            rules = tuple(self._review_rules(session, revision))
        except Exception:
            # Unreadable governance plans no rule question; the rules stay unenforced by AI.
            return ()
        changed, _required = self._scopes(revision)
        return rule_questions(rules, changed)

    def _with_rule_checks(
        self, detected: DetectedCapabilities, questions: Sequence[RuleQuestion]
    ) -> DetectedCapabilities:
        count = len(questions)
        return replace(detected, rule_checks=count) if count != detected.rule_checks else detected

    def escalation_facts(self, session: Any, revision: Any) -> EscalationFacts:
        """The deterministic deep-review escalation signals of this Snapshot's Change (P11-ESC).

        Path criticality from triage policy, the R3 side-effect inventory of each
        changed file (base vs head) and current P11-FED security chains.
        """

        from lemoncrow.pro.capabilities.review.static_findings import (
            dispositions,
            security_chains,
            snapshot_findings,
        )

        effective = self._policy(session, revision).get("effective_review")
        triage = triage_policy_for(effective, self._review_config(revision))
        changed, required = self._scopes(revision)
        scope = tuple(dict.fromkeys((*changed, *(required or ()))))

        def classify(path: str) -> tuple[str, str]:
            decided = triage.classify(path)
            rule = decided.criticality_rule
            return decided.criticality, (str(rule.origin.label) if rule is not None else "")

        chains: tuple[tuple[str, str, str], ...] = ()
        try:
            current, _stale, _checks = snapshot_findings(self._store, revision)
            dismissed = {
                key
                for key, value in dispositions(self._store, revision.review_id).items()
                if value["disposition"] == "dismissed"
            }
            chains = tuple(
                (str(chain.severity), chain.path, chain.rule.id)
                for chain in security_chains(current, excluded=dismissed)
            )
        except Exception:
            # A store without readable ingestion has no chain signal; the other classes still apply.
            chains = ()
        facts = collect_escalation_facts(
            scope,
            read_base=lambda path: self._read_base(revision, path),
            read_head=lambda path: self._read_head(revision, path),
            classify=classify,
            chains=chains,
        )
        # P11 item 16: the authoring agent/model's track record (never a human's).
        try:
            assessment = track_record_assessment(self._store, session, revision)
        except Exception as exc:
            logging.getLogger(__name__).warning("author track record unavailable for %s: %s", revision.id, exc)
            assessment = None
        if assessment is None or not (assessment.rows or assessment.unknown):
            return facts
        return replace(facts, author_track_record=assessment.rows, track_record_unknown=assessment.unknown)

    def independence_policy(self, session: Any, revision: Any) -> IndependencePolicy:
        """The effective ``independent_ai_reviewer`` mode for this Change, with its origin (P11 item 14)."""

        settings = self.settings(session, revision)
        changed, required = self._scopes(revision)
        return resolve_independence(settings.independence, tuple(dict.fromkeys((*changed, *(required or ())))))

    def explain_request(
        self, session: Any, revision: Any, plan: RunPlan, requested: str, *, entry_id: str = "ai.investigators"
    ) -> dict[str, Any]:
        """How *plan* answered a per-Review depth request (floors are never lowered; P11-ESC item 11)."""

        inputs = self.inputs(session, revision)
        return explain_depth_request(
            requested,
            plan,
            (*inputs.settings.floors, *inputs.extra_floors),
            inputs.required_scope,
            entry_id=entry_id,
        )

    def recipe_catalog(self, revision: Any) -> Any:
        """The Snapshot's trusted-base check recipes (``recipes.RecipeCatalog``)."""

        with self._lock:
            cached = self._recipes.get(revision.id)
            detected = self._detected.get(revision.id)
        if cached is not None and detected is not None:
            return cached
        with self._lock:
            self._detected.pop(revision.id, None)
        self.detected(revision)
        with self._lock:
            return self._recipes[revision.id]

    def base_paths(self, revision: Any) -> Any:
        """The trusted base's file listing of one Snapshot (``None`` while unreadable)."""

        return self._base_paths(revision)

    def analysis_catalog(self, revision: Any) -> Any:
        """The Snapshot's trusted-base artifact/infrastructure analyses (``analysis_common.AnalysisCatalog``)."""

        with self._lock:
            cached = self._analyses.get(revision.id)
            detected = self._detected.get(revision.id)
        if cached is not None and detected is not None:
            return cached
        with self._lock:
            self._detected.pop(revision.id, None)
        self.detected(revision)
        with self._lock:
            return self._analyses[revision.id]

    def head_recipe_catalog(self, revision: Any) -> Any:
        """Recipes as the head would define them: proposed future policy only, never executed."""

        from lemoncrow.pro.capabilities.review.recipes import ENVIRONMENT_FILE, build_recipe_catalog

        config = self._review_config(revision)
        head_config = self._read_head(revision, str(getattr(config, "path", "") or ".lemoncrow/review.yaml"))
        base = self.recipe_catalog(revision)
        changed = set(self._changed_paths(revision))

        def read_head(path: str) -> str | None:
            return self._read_head(revision, path) if path in changed else self._read_base(revision, path)

        raw = self._base_paths(revision)
        if raw is None:
            return base
        names = set(islice(raw, _MAX_DETECTED_PATHS + 1))
        # One head read per changed file: on a 10,000-file change each read is a store round trip.
        present = {path for path in changed if self._read_head(revision, path) is not None}
        names |= present
        names -= changed - present
        environment = self.environment_text(revision)
        if self._environment_config is not None and ENVIRONMENT_FILE in changed:
            environment = self._read_head(revision, ENVIRONMENT_FILE) or None
        return build_recipe_catalog(head_config or "", sorted(names), read_head, environment=environment)

    def _trigger(self, revision: Any) -> str:
        revisions = tuple(self._store.list_revisions(revision.review_id))
        if not revisions:
            return "open"
        first = min(revisions, key=lambda item: (item.revision_number, item.created_at, item.id))
        return "open" if first.id == revision.id else "push"

    def _ingested(self, revision: Any) -> tuple[IngestedSatisfaction, ...]:
        """Entries an equivalent current-Snapshot result satisfies (trusted-base ``ci_equivalents``)."""

        config = self._review_config(revision)
        if str(getattr(config, "state", "present")) == "provisional":
            return ()  # no trusted authority yet: nothing is satisfied by ingestion
        equivalents, _errors = parse_ci_equivalents(str(getattr(config, "content", "") or ""))
        if not equivalents:
            return ()
        from lemoncrow.pro.capabilities.review.evidence_adapters import withhold_untrusted_signed
        from lemoncrow.pro.capabilities.review.recipes import entry_recipe_digests

        # The trusted base's current recipe digests pin LemonCrow's own verdicts:
        # a verdict for a recipe the base has since changed never satisfies. A signed
        # result counts only while that base still trusts its key.
        return ingested_satisfactions(
            withhold_untrusted_signed(self._store.list_evidence(revision.review_id), lambda: config),
            revision,
            equivalents,
            recipe_digests=entry_recipe_digests(self.recipe_catalog(revision)),
        )

    def _fast_lane(self, revision: Any) -> FastLaneSignals | None:
        """Whether the previous Snapshot's persisted plan ran the broad AI pass."""

        earlier = [
            item
            for item in self._store.list_revisions(revision.review_id)
            if item.revision_number < revision.revision_number
        ]
        if not earlier:
            return None
        previous = max(earlier, key=lambda item: (item.revision_number, item.created_at, item.id))
        plan = self.persisted(previous)
        return FastLaneSignals(
            prior_snapshot_id=previous.id,
            prior_broad_pass=plan is not None and plan.runs("ai.investigators"),
        )

    def inputs(self, session: Any, revision: Any, *, ai_candidates: int = 0) -> PlanInputs:
        settings = self.settings(session, revision)
        effective = self._policy(session, revision).get("effective_review")
        _layers, execution = effective_policy_layers(effective)
        _default, allowed, by_class = self._execution(execution)
        questions = self.rule_questions(session, revision)
        detected = self._with_rule_checks(self._with_premerge(session, revision, self.detected(revision)), questions)
        config = self._review_config(revision)
        floors = (
            *(
                FloorRule(
                    "capability",
                    f"browser:{target.id}",
                    SettingOrigin("trusted_base_repository", str(config.source or "base revision")),
                )
                for target in detected.browser_scenarios
                if target.required and target.applicability != "not_applicable"
            ),
            *required_check_floors(effective),
            # Criticality raises the depth floor of its paths; it is never a severity.
            *triage_policy_for(effective, config).effort_floors(),
        )
        changed, required = self._scopes(revision)
        return PlanInputs(
            review_id=revision.review_id,
            revision_id=revision.id,
            trigger=cast(Any, self._trigger(revision)),
            settings=settings,
            detected=detected,
            changed_paths=changed,
            floor_paths=required,
            ai_candidates=ai_candidates,
            # Auto escalates execution work on a deterministic risk signal:
            # triage found consequential AI candidates.
            risk_signal=ai_candidates > 0,
            allowed_run_on=allowed,
            extra_floors=floors,
            ingested=self._ingested(revision),
            equivalence_disabled=not ci_equivalence_enabled(),
            fast_lane=self._fast_lane(revision),
            policy_error=policy_invalid_reason(effective),
            allowed_run_on_by_class=by_class,
            measured_minutes=self._measured(revision),
            unavailable_run_on_by_class=self._unavailable(execution.get("unavailable_run_on_by_class")),
            executor_loci=executor_loci(execution.get("executor_loci")),
            impact=self.impact(revision),
            rule_questions=questions,
        )

    # ------------------------------------------------------------------ #
    # R6: test-impact selection and per-recipe input digests
    # ------------------------------------------------------------------ #

    def _head_changes(self, revision: Any) -> tuple[list[Any], bool]:
        """The whole Change as ``test_impact.HeadChange`` rows, and whether the list is complete."""

        from lemoncrow.pro.capabilities.review.test_impact import MAX_CHANGED, HeadChange

        if self._changed_files is None:
            paths = tuple(self._required_paths(revision) if self._required_paths else self._changed_paths(revision))
            # No status: a deletion and a rename source are indistinguishable from
            # an unreadable file, so the list is not complete.
            rows = [(path, "", "modified") for path in paths]
            complete = False
        else:
            rows = [(str(row[0]), str(row[1]), str(row[2])) for row in self._changed_files(revision)]
            complete = True
        changes: dict[str, Any] = {}
        for path, old_path, status in rows[: MAX_CHANGED + 1]:
            if not path:
                continue
            if status == "deleted":
                changes[path] = HeadChange.of(path, None, deleted=True)
                continue
            changes[path] = HeadChange.of(path, self._read_head(revision, path))
            if status == "renamed" and old_path and old_path != path and old_path not in changes:
                changes[old_path] = HeadChange.of(old_path, None, deleted=True)
        return list(changes.values()), complete and len(rows) <= MAX_CHANGED

    def _impact_map(self, revision: Any) -> tuple[Any, str]:
        """The trusted base's import graph for this base commit: memory, store, else built once."""

        from lemoncrow.pro.capabilities.review.base_results import load_impact_map, store_impact_map
        from lemoncrow.pro.capabilities.review.test_impact import (
            IMPACT_MAP_ENTRY,
            IMPACT_MAP_EXECUTOR,
            IMPACT_MAP_STAGE,
            build_impact_map,
        )

        base_sha = str(getattr(revision, "base_sha", "") or "")
        if not base_sha:
            return None, "the revision has no base commit"
        with self._lock:
            cached = self._impact_maps.get(base_sha)
        if cached is not None:
            return cached, ""
        impact = load_impact_map(self._store, base_sha)
        if impact is None:
            paths = self._base_paths(revision)
            if paths is None:
                return None, "the trusted base tree is not readable"
            # Built from the trusted base only; the head never contributes to the stored map.
            started = time.monotonic()
            impact = build_impact_map(base_sha, paths, lambda path: self._read_base(revision, path))
            # The build is real compute: metered (and counted against per-Review
            # minutes) on the Snapshot that paid for it, never invisible.
            self.compute.record(
                review_id=revision.review_id,
                revision_id=revision.id,
                entry_id=IMPACT_MAP_ENTRY,
                executor_class=IMPACT_MAP_EXECUTOR,
                stage=IMPACT_MAP_STAGE,
                seconds=max(0.0, time.monotonic() - started),
            )
            store_impact_map(self._store, review_id=revision.review_id, revision_id=revision.id, impact=impact)
        with self._lock:
            self._impact_maps.pop(base_sha, None)
            self._impact_maps[base_sha] = impact
            while len(self._impact_maps) > 8:
                self._impact_maps.pop(next(iter(self._impact_maps)))
        return impact, ""

    def impact(self, revision: Any) -> dict[str, Any]:
        """Check entry id -> ``test_impact.EntryImpact`` for this Snapshot (cached per Snapshot)."""

        from lemoncrow.pro.capabilities.review.recipes import KIND_ENTRY, TEST_DIFF_ENTRY
        from lemoncrow.pro.capabilities.review.test_impact import EntryImpact, select_recipe_tests

        with self._lock:
            cached = self._impact.get(revision.id)
        if cached is not None:
            return cached
        catalog = self.recipe_catalog(revision)
        if catalog.base_state == "provisional" or not catalog.recipes:
            return {}
        impact_map, unavailable = self._impact_map(revision)
        changes, complete = self._head_changes(revision)
        base_sha = str(getattr(revision, "base_sha", "") or "")
        entry_ids = [*KIND_ENTRY.values(), TEST_DIFF_ENTRY, *(recipe.entry_id for recipe in catalog.recipes)]
        out: dict[str, Any] = {}
        for entry_id in dict.fromkeys(entry_ids):
            recipes = catalog.for_entry(entry_id)
            if not recipes:
                continue
            out[entry_id] = EntryImpact(
                entry_id,
                tuple(
                    select_recipe_tests(
                        impact_map, recipe, changes, base_sha=base_sha, unavailable=unavailable, complete=complete
                    )
                    for recipe in recipes
                ),
                impact_map.digest if impact_map is not None else "",
            )
        with self._lock:
            # lc-debt: per-process FIFO like detection; re-derived from the stored map per process.
            self._impact.pop(revision.id, None)
            self._impact[revision.id] = out
            while len(self._impact) > _CACHE_LIMIT:
                self._impact.pop(next(iter(self._impact)))
        return out

    def reach_inputs(self, revision: Any) -> tuple[Any, str, list[Any], bool]:
        """``(impact map | None, why none, head changes, list complete)`` for per-file test reach."""

        impact_map, unavailable = self._impact_map(revision)
        changes, complete = self._head_changes(revision)
        return impact_map, unavailable, changes, complete

    def _measured(self, revision: Any) -> dict[str, float]:
        """Average in-process compute per run of static entries on this Review's earlier Snapshots."""

        earlier = [item.id for item in self._store.list_revisions(revision.review_id) if item.id != revision.id]
        if not earlier:
            return {}
        totals: dict[str, tuple[float, int]] = {}
        for row in self.compute.rows(earlier):
            if execution_class(row.entry_id) == "static" and row.runs > 0:
                seconds, runs = totals.get(row.entry_id, (0.0, 0))
                totals[row.entry_id] = (seconds + row.seconds, runs + row.runs)
        return {entry: seconds / 60.0 / runs for entry, (seconds, runs) in totals.items()}

    def _revision_ids(self, review_id: str) -> list[str]:
        return [item.id for item in self._store.list_revisions(review_id)]

    def compute_projection(self, session: Any) -> dict[str, Any]:
        """In-process compute metered for this Review, per entry, executor and stage."""

        return self.compute.projection(self._revision_ids(session.id))

    def usage(self, session: Any, revision: Any) -> BudgetUsage:
        calls = int(self._store.intelligence_calls(session.id))
        # In-process minutes add to externally metered (job lease) minutes: the
        # two sources never meter the same execution.
        local_minutes = self.compute.minutes(self._revision_ids(session.id))
        raw = self._usage(session, revision) if self._usage is not None else None
        if not raw:
            return BudgetUsage(review_minutes=local_minutes, review_model_calls=calls)
        observed = BudgetUsage.from_wire(raw)
        return BudgetUsage(
            review_minutes=observed.review_minutes + local_minutes,
            review_model_calls=max(calls, observed.review_model_calls),
            monthly_minutes=observed.monthly_minutes,
            monthly_model_calls=observed.monthly_model_calls,
            monthly_managed_usd=observed.monthly_managed_usd,
        )

    # ------------------------------------------------------------------ #
    # plans
    # ------------------------------------------------------------------ #

    def persisted(self, revision: Any) -> RunPlan | None:
        try:
            return load_run_plan(self._store, revision.id)
        except (ValueError, KeyError, TypeError):
            # A tampered or unreadable row is never executed; it is rebuilt.
            return None

    def peek(self, session: Any, revision: Any) -> RunPlan:
        """The persisted plan when current, else an unpersisted preview.

        Used by projections that cannot cheaply compute AI escalation signals;
        entries outside the AI family never depend on those signals.
        """

        inputs = self.inputs(session, revision)
        persisted = self.persisted(revision)
        if persisted is not None and persisted.inputs_digest == inputs.digest():
            return persisted
        return rebuild_run_plan(persisted, inputs, usage=self.usage(session, revision))

    def current(
        self,
        session: Any,
        revision: Any,
        *,
        ai_candidates: int | Callable[[], int],
        persist: bool = True,
    ) -> RunPlan:
        """The Snapshot's effective plan, persisting a fresh one when inputs changed."""

        inputs = self.inputs(session, revision)
        persisted = self.persisted(revision)
        if persisted is not None and persisted.inputs_digest == inputs.digest():
            return persisted
        count = ai_candidates() if callable(ai_candidates) else ai_candidates
        # Still-valid manual picks of the superseded plan survive; the rest are
        # dropped with their reason on the plan.
        plan = rebuild_run_plan(
            persisted,
            replace(
                inputs,
                ai_candidates=count,
                risk_signal=count > 0,
                # P11-ESC: observed once, frozen on the plan like the candidate count.
                escalation=self.escalation_facts(session, revision),
            ),
            usage=self.usage(session, revision),
        )
        if not persist:
            return plan
        # lc-debt: two concurrent first computations both upsert; the later write
        # wins and the earlier caller's digest is recorded on its own execution.
        persist_run_plan(self._store, plan)
        return self.persisted(revision) or plan

    def amend(
        self,
        session: Any,
        revision: Any,
        picks: Sequence[ManualPick],
        *,
        ai_candidates: int | Callable[[], int],
    ) -> RunPlan:
        """Apply per-Review picks (add-only) and persist the amended plan."""

        base = self.current(session, revision, ai_candidates=ai_candidates)
        inputs = self.inputs(session, revision)
        plan = amend_run_plan(base, inputs, usage=self.usage(session, revision), manual=picks)
        if plan is not base:
            persist_run_plan(self._store, plan)
        return plan

    def preview(
        self,
        session: Any,
        revision: Any,
        picks: Sequence[ManualPick],
        *,
        ai_candidates: int | Callable[[], int],
    ) -> dict[str, Any]:
        base = self.current(session, revision, ai_candidates=ai_candidates)
        inputs = self.inputs(session, revision)
        return preview_picks(base, inputs, usage=self.usage(session, revision), manual=picks)

    def projection(
        self,
        session: Any,
        revision: Any,
        plan: RunPlan,
        *,
        monthly_reviews: int | None = None,
    ) -> dict[str, Any]:
        settings = self.settings(session, revision)
        detected = self._with_rule_checks(
            self._with_premerge(session, revision, self.detected(revision)), self.rule_questions(session, revision)
        )
        equivalents, equivalence_errors = parse_ci_equivalents(
            str(getattr(self._review_config(revision), "content", "") or "")
        )
        return {
            "ci_equivalents": {
                "rows": [row.to_wire() for row in equivalents],
                "errors": list(equivalence_errors),
            },
            "plan": plan.to_wire(),
            "settings": settings.to_wire(),
            "detected": detected.to_wire(),
            "catalog": catalog_wire(detected),
            "depths": depth_preview(settings, detected, monthly_reviews=monthly_reviews),
            "future_settings": self.future_settings(revision),
            "compute": self.compute_projection(session),
            # R6: compute saved (shared base, carried forward, CI reuse, impact selection) vs spent.
            "economy": economy_projection(
                plan,
                self.savings.rows(self._revision_ids(session.id)),
                spent_minutes=self.usage(session, revision).review_minutes,
            ),
            "reverification": self.reverification(revision, plan),
        }

    def reverification(self, revision: Any, plan: RunPlan) -> dict[str, list[dict[str, Any]]]:
        """Trust T3 item 9: what runs, is reused, skipped, broadened or unknown, and why."""

        from lemoncrow.pro.capabilities.review.check_api import latest_results
        from lemoncrow.pro.capabilities.review.check_runs import is_check_entry

        # lc-debt: reads every check differential artifact on the Snapshot per projection;
        # index the head attempt's provenance in a derived row if run-plan reads get hot.
        differentials: dict[str, dict[str, Any]] = {}
        if any(row.status == "run" and is_check_entry(row.id) for row in plan.entries):
            try:
                differentials, _verdicts = latest_results(self._store, revision)
            except Exception as exc:
                # Display only: without readable results every check recipe shows as planned.
                logging.getLogger(__name__).warning("check results unavailable for %s: %s", revision.id, exc)
        return reverification_summary(
            plan,
            differentials,
            recipes_for=lambda entry_id: [recipe.id for recipe in self.recipe_catalog(revision).for_entry(entry_id)],
            is_check=is_check_entry,
        )


__all__ = [
    "RunPlanService",
    "created_since",
    "effective_policy_layers",
    "executor_loci",
    "git_tree_paths",
    "policy_invalid_reason",
    "required_check_floors",
    "resolve_execution_loci",
    "unavailable_loci",
]
