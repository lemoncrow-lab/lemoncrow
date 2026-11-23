"""Review capability catalog (Phase 9R item 40).

Every runnable thing a Review can spend compute on has one stable catalog ID
with a family, a cost class, an applicability predicate derived from the
trusted base, and dependencies. Presets (Quick/Auto/Thorough/Deep), Custom
selections, organization profiles and per-Review picks are all selections over
this one registry; :mod:`run_plan` resolves them into the effective run plan.

An entry whose executor does not exist in this build is catalogued with an
empty ``executor`` and is planned as ``not_available``. It is never reported as
run.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

CapabilityFamily = Literal["static", "checks", "behavior_diff", "visual", "deep_runtime", "ai"]
CostClass = Literal["minimal", "low", "medium", "high", "very_high"]
# The preset that first introduces an entry; budget degradation drops Deep
# extras first, then Thorough behavior work, then AI escalation, then the rest.
DegradeTier = Literal["deep_extra", "thorough_behavior", "ai_escalation", "base"]
Preset = Literal["quick", "auto", "thorough", "deep"]
When = Literal["open", "push", "manual"]
Sides = Literal["head", "base+head"]
# ``review_host`` is never selectable: it is where the planner places work that
# only reads content the Review store already holds (static analysis), i.e. the
# process that holds the Snapshot -- the local server, or the hosted Review
# server. It never executes reviewed code and needs no sandbox enablement.
ExecutorLocus = Literal["local", "worker", "managed", "review_host"]
FamilyMode = Literal["off", "auto", "always"]

REVIEW_HOST: Final[ExecutorLocus] = "review_host"
# Documented minimum compute estimate of the static side-effect inventory, used
# until this Review has measured runs: a bounded base-register scan (at most
# 20 s, see ``side_effects.ScanLimits``) plus parsing the changed files.
STATIC_MINIMUM_MINUTES: Final[float] = 0.1

FAMILIES: Final[tuple[CapabilityFamily, ...]] = ("static", "checks", "behavior_diff", "visual", "deep_runtime", "ai")
PRESETS: Final[tuple[Preset, ...]] = ("quick", "auto", "thorough", "deep")
PRESET_RANK: Final[Mapping[str, int]] = {"quick": 0, "auto": 1, "thorough": 2, "deep": 3}
WHEN_VALUES: Final[frozenset[str]] = frozenset({"open", "push", "manual"})
SIDES_VALUES: Final[frozenset[str]] = frozenset({"head", "base+head"})
EXECUTOR_LOCI: Final[tuple[ExecutorLocus, ...]] = ("local", "worker", "managed")
FAMILY_MODES: Final[frozenset[str]] = frozenset({"off", "auto", "always"})
COST_RANK: Final[Mapping[str, int]] = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "very_high": 4}
DEGRADE_ORDER: Final[tuple[DegradeTier, ...]] = ("deep_extra", "thorough_behavior", "ai_escalation", "base")

# Default compute estimate per side until measured run costs exist. These are
# display/budget estimates, never billed amounts.
_MINUTES_BY_COST: Final[Mapping[str, float]] = {
    "minimal": 0.0,
    "low": 1.0,
    "medium": 3.0,
    "high": 8.0,
    "very_high": 20.0,
}
# Model calls per AI entry and depth. ``ai.investigators`` mirrors the Phase 4
# effort profiles (investigations plus one semantic-dedupe call at
# Thorough/Deep); a test pins this table to ``ai_review._EFFORT_PROFILES``.
AI_INVESTIGATOR_CALLS: Final[Mapping[str, int]] = {"quick": 1, "auto": 2, "thorough": 5, "deep": 9}

#: Phase 10: model-evaluated pre-merge checks (custom NL checks, AI scope
#: alignment). Applicable only when the trusted base or an operator layer
#: configures one; one model call per such check and run.
PREMERGE_CHECKS_ENTRY: Final[str] = "ai.premerge_checks"
PREMERGE_CHECKS_FACT: Final[str] = "premerge_ai_checks"
#: P11-ESC: focused per-rule model checks. Applicable only when an authoritative
#: Review rule applies to the Change; one bounded model call per rule question.
RULE_CHECKS_ENTRY: Final[str] = "ai.rule_checks"
RULE_CHECKS_FACT: Final[str] = "review_rule_checks"

DYNAMIC_PREFIXES: Final[tuple[str, ...]] = ("checks.recipe:", "browser:", "surface:")
_DYNAMIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One addressable capability. ``executor`` empty means not available yet."""

    id: str
    family: CapabilityFamily
    title: str
    cost_class: CostClass
    degrade_tier: DegradeTier
    requires: str = ""
    executor: str = ""
    dependencies: tuple[str, ...] = ()
    dependency_sides: Sides = "head"
    setup_hint: str = ""
    external: bool = False
    # Loci its executor can run on (empty: any the deployment allows).
    loci: tuple[ExecutorLocus, ...] = ()
    # What to do when the deployment places the entry on a locus outside ``loci`` (shown with the reason).
    locus_hint: str = ""
    # Documented minimum estimate per run, whatever the cost class says.
    minimum_minutes: float = 0.0
    # A preset/family selection leaves the entry out entirely (instead of
    # listing it inapplicable) when the trusted base gives it nothing to act on,
    # so plans of Reviews that never configure it keep their shape and digest.
    omit_when_inapplicable: bool = False
    # Model calls per run when the trusted base decides the count (pre-merge checks).
    calls_per_run: int = 0

    def minutes_per_side(self) -> float:
        return 0.0 if self.external or self.family == "ai" else _MINUTES_BY_COST[self.cost_class]

    def model_calls(self, depth: str) -> int:
        if self.id == "ai.investigators":
            return AI_INVESTIGATOR_CALLS.get(depth, AI_INVESTIGATOR_CALLS["auto"])
        if self.id in {"ai.hypotheses", "ai.brief_synthesis"}:
            return 1
        if self.id in {PREMERGE_CHECKS_ENTRY, RULE_CHECKS_ENTRY}:
            return self.calls_per_run
        return 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "title": self.title,
            "cost_class": self.cost_class,
            "degrade_tier": self.degrade_tier,
            "dependencies": list(self.dependencies),
            "dependency_sides": self.dependency_sides,
            "available": bool(self.executor),
            "external": self.external,
        }


#: Why render diffs do not run on this locus; the planner adds what this deployment's reviewer can do about it
#: (``run_plan._locus_action``: a hosted deployment has no local executor to switch to).
_RENDER_LOCUS_HINT: Final[str] = (
    "Rendered diffs build the revision and run a headless browser against it, which this executor is not set up "
    "to do; an operator enables that with `reviewWorker.renderDiffs` and a worker image carrying a browser and "
    "Node.js."
)
_CHECK_RECIPES: Final[str] = "check_recipes"
_ANALYSES: Final[str] = "artifact_analyses"
_TEST_HINT = (
    "No test command was discovered in the trusted base. Add a test runner config "
    "(pytest/tox, package.json test script, Makefile test target, Cargo, Go) to enable it."
)

STATIC_CATALOG: Final[Mapping[str, CatalogEntry]] = {
    entry.id: entry
    for entry in (
        CatalogEntry(
            "side_effects.static",
            "static",
            "Static side-effect inventory",
            "minimal",
            "base",
            executor="static_side_effects",
            loci=(REVIEW_HOST,),
            minimum_minutes=STATIC_MINIMUM_MINUTES,
        ),
        # Trusted-base check recipes execute these (``recipes.py``/``check_runs.py``).
        CatalogEntry(
            "checks.test",
            "checks",
            "Affected tests",
            "medium",
            "base",
            requires="test_command",
            executor=_CHECK_RECIPES,
            setup_hint=_TEST_HINT,
        ),
        CatalogEntry(
            "checks.lint",
            "checks",
            "Lint",
            "low",
            "base",
            requires="lint",
            executor=_CHECK_RECIPES,
            setup_hint="No linter configuration was discovered in the trusted base.",
        ),
        CatalogEntry(
            "checks.format",
            "checks",
            "Format check",
            "low",
            "base",
            requires="format",
            executor=_CHECK_RECIPES,
            setup_hint="No formatter configuration was discovered in the trusted base.",
        ),
        CatalogEntry(
            "checks.typecheck",
            "checks",
            "Type check",
            "medium",
            "base",
            requires="typecheck",
            executor=_CHECK_RECIPES,
            setup_hint="No type-checker configuration was discovered in the trusted base.",
        ),
        CatalogEntry(
            "checks.precommit",
            "checks",
            "Pre-commit hooks",
            "medium",
            "thorough_behavior",
            requires="precommit",
            executor=_CHECK_RECIPES,
            setup_hint="No .pre-commit-config.yaml was discovered in the trusted base.",
        ),
        CatalogEntry(
            "behavior.test_diff",
            "behavior_diff",
            "Base-vs-head test behavior diff",
            "high",
            "thorough_behavior",
            requires="test_command",
            executor=_CHECK_RECIPES,
            dependencies=("checks.test",),
            dependency_sides="base+head",
            setup_hint=_TEST_HINT,
        ),
        CatalogEntry(
            "behavior.api_diff",
            "behavior_diff",
            "Base-vs-head API response diff",
            "high",
            "thorough_behavior",
            requires="api_surface",
        ),
        # Rendered by the Reader's web preview, on request or when planned.
        CatalogEntry(
            "visual.screenshot",
            "visual",
            "Base-vs-head screenshots",
            "high",
            "thorough_behavior",
            requires="web_app",
            executor="web_preview",
        ),
        # Phase 9R item 16: reviewer-opened base and head running side by side
        # (``interactive_preview.py``). Manual only: it starts when a reviewer
        # opens it, never on a Snapshot. The estimate is one default-length
        # session (5 minutes per side, both sides).
        CatalogEntry(
            "visual.interactive_preview",
            "visual",
            "Interactive side-by-side preview",
            "medium",
            "deep_extra",
            requires="previewable_app",
            executor="interactive_preview",
            setup_hint=(
                "No previewable app was detected in the trusted base. Add an interactive_preview "
                "command and port to .lemoncrow/review.yaml, or use a Next.js, Astro or Vite project."
            ),
            minimum_minutes=10.0,
        ),
        # Sandboxed headless-browser render diffs (``render_diff.py``). The catalog states what
        # every deployment runs: the local locus. A deployment whose worker image carries a
        # browser and a build toolchain adds its own locus (``PlanInputs.executor_loci``).
        CatalogEntry(
            "visual.dom_a11y",
            "visual",
            "DOM and accessibility-tree diff",
            "high",
            "deep_extra",
            requires="web_app",
            executor="render_diff",
            dependencies=("visual.screenshot",),
            dependency_sides="base+head",
            loci=("local",),
            locus_hint=_RENDER_LOCUS_HINT,
        ),
        CatalogEntry(
            "visual.matrix",
            "visual",
            "Viewport x theme visual matrix",
            "very_high",
            "deep_extra",
            requires="web_app",
            executor="render_diff",
            dependencies=("visual.screenshot",),
            dependency_sides="base+head",
            loci=("local",),
            locus_hint=_RENDER_LOCUS_HINT,
        ),
        # Observed base/head runs of the trusted-base tests (``runtime_observation_runs.py``).
        CatalogEntry(
            "side_effects.runtime",
            "deep_runtime",
            "Runtime side-effect capture",
            "high",
            "thorough_behavior",
            requires="test_command",
            executor="runtime_observation",
            dependencies=("checks.test",),
            dependency_sides="base+head",
            setup_hint=_TEST_HINT,
        ),
        # Trusted-base artifact/infrastructure analyses execute these (``artifact_analyses.py``).
        CatalogEntry(
            "migrations.execute",
            "deep_runtime",
            "Migration execution",
            "high",
            "deep_extra",
            requires="migrations",
            executor=_ANALYSES,
            setup_hint=(
                "Plain SQL migration directories run on SQLite in the sandbox; Alembic, Django and Rails "
                "migrations are not executable yet. Configure analyses.migrations (with a schema fixture) "
                "in the trusted-base .lemoncrow/review.yaml."
            ),
        ),
        CatalogEntry(
            "artifacts.regenerate",
            "deep_runtime",
            "Regenerate and compare generated files",
            "medium",
            "deep_extra",
            requires="generators",
            executor=_ANALYSES,
            setup_hint=(
                "Declare generators (command, outputs, generator inputs) under analyses.regenerate in the "
                "trusted-base .lemoncrow/review.yaml; head-only generators are never run."
            ),
        ),
        CatalogEntry(
            "artifacts.shipped",
            "deep_runtime",
            "Shipped artifact diff",
            "medium",
            "deep_extra",
            requires="package_artifact",
            executor=_ANALYSES,
            setup_hint=(
                "Python packages need python3 with the package's PEP 517 build backend (e.g. setuptools) in the sandbox; "
                "npm packages need npm. Container images are not built in the sandbox yet."
            ),
        ),
        CatalogEntry(
            "infra.render",
            "deep_runtime",
            "Infrastructure render diff",
            "medium",
            "deep_extra",
            requires="infrastructure",
            executor=_ANALYSES,
            setup_hint=(
                "Needs helm, kustomize (or kubectl) or terraform in the sandbox; terraform also needs a "
                "trusted-base provider mirror listed in execution.fixtures. Nothing is ever applied."
            ),
        ),
        # Phase 9R item 41: old/new code, clients and consumers paired across the Change
        # (``compat_mixed_version.py``); Deep or a policy floor only.
        CatalogEntry(
            "compat.mixed_version",
            "deep_runtime",
            "Mixed-version compatibility",
            "high",
            "deep_extra",
            requires="compat_surface",
            executor=_ANALYSES,
            setup_hint=(
                "Needs a contract surface in the trusted base: plain SQL migrations (executed on SQLite in the "
                "sandbox), OpenAPI documents, or proto/Avro/JSON Schema message formats; configure more under "
                "analyses.compatibility in the trusted-base .lemoncrow/review.yaml."
            ),
        ),
        # P10-7: request-data flows Opengrep finds on base and head, and how they changed (``data_path.py``).
        # Experimental and on request only; it runs a local, pinned binary, so only the local locus runs it.
        # lc-debt: local locus only; worker and managed loci plan not_available. Upgrade: ship the pinned
        # Opengrep v2.0.0 in the worker image with the same digest check.
        CatalogEntry(
            "data_path.diff",
            "static",
            "Data path difference (experimental)",
            "medium",
            "deep_extra",
            executor="data_path",
            loci=("local",),
            locus_hint="The data path runs the pinned Opengrep v2.0.0 binary of the machine that holds the revision.",
            setup_hint=(
                "Needs the pinned Opengrep v2.0.0 release on this machine: set LEMONCROW_DATA_PATH_OPENGREP_BIN "
                "to it (or put it on PATH as opengrep). A review never downloads it. It runs only sandboxed: "
                "sandbox-exec on macOS, bubblewrap on Linux."
            ),
        ),
        # Phase 9R item 44: behavior diffs with each touched feature flag on and off (``flag_matrix.py``).
        CatalogEntry(
            "behavior.flag_matrix",
            "behavior_diff",
            "Feature-flag state matrix",
            "high",
            "thorough_behavior",
            requires="flag_overrides",
            executor=_ANALYSES,
            setup_hint=(
                "Configure analyses.flags (a test command and the flag variable, e.g. env: FEATURE_{FLAG}) in "
                "the trusted-base .lemoncrow/review.yaml."
            ),
        ),
        CatalogEntry("ai.brief_synthesis", "ai", "AI Brief synthesis", "low", "base", executor="brief_synthesis"),
        CatalogEntry(
            "ai.hypotheses",
            "ai",
            "AI hypothesis synthesis",
            "low",
            "ai_escalation",
            executor="ai_review",
            dependencies=("ai.investigators",),
        ),
        CatalogEntry("ai.investigators", "ai", "AI focused investigators", "medium", "base", executor="ai_review"),
        CatalogEntry(
            PREMERGE_CHECKS_ENTRY,
            "ai",
            "AI pre-merge checks",
            "low",
            "base",
            requires=PREMERGE_CHECKS_FACT,
            executor="premerge_checks",
            setup_hint=(
                "Configure a model-evaluated pre-merge check (premerge.checks[].instruction, or "
                "scope-alignment with ai: true) in the trusted base or operator policy."
            ),
            omit_when_inapplicable=True,
        ),
        CatalogEntry(
            RULE_CHECKS_ENTRY,
            "ai",
            "AI rule checks",
            "low",
            "base",
            requires=RULE_CHECKS_FACT,
            executor="ai_review",
            setup_hint="Add a trusted Review rule (review.rules in .lemoncrow/review.yaml or operator policy).",
            omit_when_inapplicable=True,
        ),
    )
}


@dataclass(frozen=True, slots=True)
class PresetEntry:
    id: str
    when: When = "push"
    sides: Sides = "head"
    escalate: bool = False


# Presets are pure selections. ``when: push`` means every Snapshot (open and
# later pushes), which preserves the pre-catalog behavior of automatic work;
# ``manual`` entries are available on request only. Wildcards expand to the
# trusted base's addressable recipes/scenarios/surfaces.
_QUICK: Final[tuple[PresetEntry, ...]] = (
    PresetEntry("side_effects.static"),
    PresetEntry("checks.test"),
    PresetEntry("checks.lint"),
    PresetEntry("checks.typecheck"),
    PresetEntry("checks.recipe:*"),
    PresetEntry("ai.investigators"),
    PresetEntry("ai.brief_synthesis", when="manual"),
    PresetEntry("surface:*", when="manual"),
    PresetEntry(PREMERGE_CHECKS_ENTRY),
)
_AUTO: Final[tuple[PresetEntry, ...]] = (
    *_QUICK,
    PresetEntry("checks.format"),
    PresetEntry("behavior.test_diff", sides="base+head", escalate=True),
    PresetEntry("behavior.api_diff", sides="base+head", escalate=True),
    PresetEntry("browser:*"),
    PresetEntry("ai.hypotheses", escalate=True),
    PresetEntry(RULE_CHECKS_ENTRY),
)
_THOROUGH: Final[tuple[PresetEntry, ...]] = (
    *(item for item in _AUTO if item.id not in {"behavior.test_diff", "behavior.api_diff", "ai.hypotheses"}),
    PresetEntry("checks.precommit"),
    PresetEntry("behavior.test_diff", sides="base+head"),
    PresetEntry("behavior.api_diff", sides="base+head"),
    PresetEntry("visual.screenshot", sides="base+head"),
    PresetEntry("side_effects.runtime", sides="base+head"),
    PresetEntry("behavior.flag_matrix", sides="base+head"),
    PresetEntry("ai.hypotheses"),
)
_DEEP: Final[tuple[PresetEntry, ...]] = (
    *_THOROUGH,
    PresetEntry("visual.dom_a11y", sides="base+head"),
    PresetEntry("visual.matrix", sides="base+head"),
    PresetEntry("migrations.execute"),
    PresetEntry("artifacts.regenerate", sides="base+head"),
    PresetEntry("artifacts.shipped", sides="base+head"),
    PresetEntry("infra.render", sides="base+head"),
    PresetEntry("compat.mixed_version"),
    # Offered on request at Deep; any depth may add it as a per-Review pick.
    PresetEntry("visual.interactive_preview", when="manual", sides="base+head"),
    # Experimental (P10-7): offered on request at Deep, never run on a Snapshot by itself.
    PresetEntry("data_path.diff", when="manual", sides="base+head"),
)
PRESET_SELECTIONS: Final[Mapping[Preset, tuple[PresetEntry, ...]]] = {
    "quick": _QUICK,
    "auto": _AUTO,
    "thorough": _THOROUGH,
    "deep": _DEEP,
}


def preset_entry(preset: str, entry_id: str) -> PresetEntry | None:
    """The preset's own defaults for an ID (wildcards cover dynamic IDs)."""

    rows: tuple[PresetEntry, ...] = PRESET_SELECTIONS[preset] if preset in PRESET_SELECTIONS else ()  # type: ignore[index]
    for row in rows:
        if row.id == entry_id:
            return row
    for prefix in DYNAMIC_PREFIXES:
        if entry_id.startswith(prefix):
            wildcard = prefix + "*"
            return next((row for row in rows if row.id == wildcard), None)
    return None


# --------------------------------------------------------------------------- #
# trusted-base detection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DynamicTarget:
    """One trusted-base addressable recipe, browser scenario or surface."""

    id: str
    title: str = ""
    required: bool = False
    applicability: Literal["applies", "not_applicable", "unknown"] = "applies"
    reason: str = ""
    executable: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "required": self.required,
            "applicability": self.applicability,
            "reason": self.reason,
            "executable": self.executable,
        }


DETECTION_FACTS: Final[tuple[str, ...]] = (
    "test_command",
    "lint",
    "format",
    "typecheck",
    "precommit",
    "web_app",
    "api_surface",
    "migrations",
    "generators",
    "package_artifact",
    "infrastructure",
    # A trusted-base interactive_preview recipe, or a statically buildable web
    # project (Next.js, Astro, Vite): base and head can run side by side.
    "previewable_app",
    # A trusted-base contract surface mixed-version pairings act on (migrations, API, message formats).
    "compat_surface",
    # A trusted-base flag-override command (``analyses.flags``).
    "flag_overrides",
)
_STATIC_PREVIEW_FRAMEWORKS: Final[frozenset[str]] = frozenset({"next", "astro", "vite"})


def _declares_previews(text: str | None) -> bool:
    """The trusted-base environment.toml is valid and declares at least one preview."""

    from lemoncrow_client.environment import DefinitionError, parse_environment

    if not text or "previews" not in text:
        return False
    try:
        return bool(parse_environment(text).previews)
    except DefinitionError:
        return False


def _declares_render_root(text: str | None) -> bool:
    """The trusted-base review config names a static site to render (``render.static_root``)."""

    if not text or "static_root" not in text:
        return False
    import yaml

    try:
        decoded = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    block = decoded.get("render") if isinstance(decoded, Mapping) else None
    return isinstance(block, Mapping) and bool(block.get("static_root"))


def _declares_interactive_preview(text: str | None) -> bool:
    """The trusted-base review config declares an interactive preview command."""

    if not text or "interactive_preview" not in text:
        return False
    import yaml

    try:
        decoded = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    block = decoded.get("interactive_preview") if isinstance(decoded, Mapping) else None
    return isinstance(block, Mapping) and bool(block.get("command"))


def _static_preview_framework(text: str | None) -> bool:
    try:
        decoded = json.loads(text) if text else {}
    except ValueError:
        return False
    if not isinstance(decoded, Mapping):
        return False
    for key in ("dependencies", "devDependencies"):
        deps = decoded.get(key)
        if isinstance(deps, Mapping) and _STATIC_PREVIEW_FRAMEWORKS & set(deps):
            return True
    return False


@dataclass(frozen=True, slots=True)
class DetectedCapabilities:
    """Facts derived from the trusted base only; head content never contributes."""

    facts: frozenset[str] = frozenset()
    evidence: Mapping[str, str] = field(default_factory=dict)
    recipes: tuple[DynamicTarget, ...] = ()
    browser_scenarios: tuple[DynamicTarget, ...] = ()
    surfaces: tuple[DynamicTarget, ...] = ()
    base_state: Literal["present", "absent", "provisional"] = "present"
    truncated: bool = False
    #: Model-evaluated pre-merge checks the trusted base and operator policy
    #: configure (never head-side policy); set by the run-plan service.
    premerge_checks: int = 0
    #: P11-ESC: authoritative Review rules that apply to the Change (one question
    #: each); set by the run-plan service from trusted governance only.
    rule_checks: int = 0
    #: Fact -> typed reason for an entry the trusted base configures but none of whose
    #: specs can run (``Command too large``): planned ``not_available``, never ``run``.
    unavailable: Mapping[str, str] = field(default_factory=dict)

    def has(self, fact: str) -> bool:
        if fact == PREMERGE_CHECKS_FACT:
            return self.premerge_checks > 0
        if fact == RULE_CHECKS_FACT:
            return self.rule_checks > 0
        return not fact or fact in self.facts

    def dynamic(self, entry_id: str) -> DynamicTarget | None:
        for prefix, rows in (
            ("checks.recipe:", self.recipes),
            ("browser:", self.browser_scenarios),
            ("surface:", self.surfaces),
        ):
            if entry_id.startswith(prefix):
                wanted = entry_id[len(prefix) :]
                return next((row for row in rows if row.id == wanted), None)
        return None

    def digest_document(self) -> dict[str, Any]:
        # Present only when configured, so every other plan keeps its digest.
        premerge = {"premerge_checks": self.premerge_checks} if self.premerge_checks > 0 else {}
        if self.rule_checks > 0:
            premerge["rule_checks"] = self.rule_checks
        return {
            "facts": sorted(self.facts),
            "recipes": [row.to_wire() for row in self.recipes],
            "browser": [row.to_wire() for row in self.browser_scenarios],
            "surfaces": [row.to_wire() for row in self.surfaces],
            "base_state": self.base_state,
            # Present only when detection hit its bound: then absence is unknown.
            **({"truncated": True} if self.truncated else {}),
            **premerge,
            # Present only when an entry cannot run, so every other plan keeps its digest.
            **({"unavailable": dict(sorted(self.unavailable.items()))} if self.unavailable else {}),
        }

    def to_wire(self) -> dict[str, Any]:
        return {**self.digest_document(), "evidence": dict(self.evidence), "truncated": self.truncated}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> DetectedCapabilities:
        def targets(key: str) -> tuple[DynamicTarget, ...]:
            rows = raw.get(key) or ()
            out: list[DynamicTarget] = []
            for row in rows if isinstance(rows, Sequence) else ():
                if isinstance(row, Mapping):
                    applicability = str(row.get("applicability") or "applies")
                    out.append(
                        DynamicTarget(
                            id=str(row.get("id") or ""),
                            title=str(row.get("title") or ""),
                            required=row.get("required") is True,
                            applicability=(
                                applicability  # type: ignore[arg-type]
                                if applicability in {"applies", "not_applicable", "unknown"}
                                else "unknown"
                            ),
                            reason=str(row.get("reason") or ""),
                            executable=row.get("executable") is not False,
                        )
                    )
            return tuple(out)

        state = str(raw.get("base_state") or "present")
        evidence = raw.get("evidence")
        unavailable = raw.get("unavailable")
        premerge = raw.get("premerge_checks")
        rule_checks = raw.get("rule_checks")
        return cls(
            facts=frozenset(str(item) for item in raw.get("facts") or () if str(item) in DETECTION_FACTS),
            evidence={str(k): str(v) for k, v in evidence.items()} if isinstance(evidence, Mapping) else {},
            recipes=targets("recipes"),
            browser_scenarios=targets("browser"),
            surfaces=targets("surfaces"),
            base_state=state if state in {"present", "absent", "provisional"} else "provisional",  # type: ignore[arg-type]
            truncated=raw.get("truncated") is True,
            premerge_checks=(max(0, premerge) if isinstance(premerge, int) and not isinstance(premerge, bool) else 0),
            rule_checks=(
                max(0, rule_checks) if isinstance(rule_checks, int) and not isinstance(rule_checks, bool) else 0
            ),
            unavailable=(
                {str(k): str(v) for k, v in unavailable.items() if str(k) in DETECTION_FACTS}
                if isinstance(unavailable, Mapping)
                else {}
            ),
        )


_WEB_FRAMEWORKS: Final[tuple[str, ...]] = (
    '"react"',
    '"vue"',
    '"svelte"',
    '"next"',
    '"vite"',
    '"@angular/core"',
    '"nuxt"',
    '"solid-js"',
    '"preact"',
    '"astro"',
)
_MIGRATION_DIRS: Final[tuple[str, ...]] = (
    "migrations/",
    "alembic/versions/",
    "db/migrate/",
    "prisma/migrations/",
    "migrate/",
)
_API_FILES = re.compile(r"(^|/)(openapi|swagger)\.(ya?ml|json)$|\.proto$|(^|/)schema\.graphql$", re.IGNORECASE)
_INFRA_FILES = re.compile(
    r"\.tf$|(^|/)Chart\.ya?ml$|(^|/)kustomization\.ya?ml$|(^|/)helmfile\.ya?ml$",
    re.IGNORECASE,
)


def _package_scripts(text: str | None) -> tuple[dict[str, str], bool, str]:
    if not text:
        return {}, False, ""
    try:
        decoded = json.loads(text)
    except ValueError:
        return {}, False, text
    if not isinstance(decoded, Mapping):
        return {}, False, text
    scripts = decoded.get("scripts")
    rows = {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, Mapping) else {}
    return rows, decoded.get("private") is True, text


def detect_capabilities(
    base_paths: Iterable[str] | None,
    read_base: Callable[[str], str | None] | None = None,
    *,
    base_state: Literal["present", "absent", "provisional"] = "present",
    recipes: Sequence[DynamicTarget] = (),
    browser_scenarios: Sequence[DynamicTarget] = (),
    surfaces: Sequence[DynamicTarget] = (),
    max_paths: int = 200_000,
) -> DetectedCapabilities:
    """Derive capability facts from trusted-base file names plus a few manifests.

    Only the trusted base is consulted: a Change cannot make a capability
    applicable for its own Review. ``base_paths=None`` means the base tree is
    unavailable, so nothing is detected and the state is provisional.
    """

    if base_paths is None:
        return DetectedCapabilities(
            base_state="provisional",
            recipes=tuple(recipes),
            browser_scenarios=tuple(browser_scenarios),
            surfaces=tuple(surfaces),
        )
    facts: set[str] = set()
    evidence: dict[str, str] = {}

    def mark(fact: str, why: str) -> None:
        if fact not in facts:
            facts.add(fact)
            evidence[fact] = why

    paths: list[str] = []
    truncated = False
    for index, path in enumerate(base_paths):
        if index >= max_paths:
            # lc-debt: detection scans at most max_paths base entries; a larger
            # tree reports truncated=true instead of silently claiming absence.
            truncated = True
            break
        paths.append(path)
    names = set(paths)
    reader = read_base or (lambda _path: None)

    for path in paths:
        lower = path.lower()
        name = lower.rsplit("/", 1)[-1]
        if name in {"pytest.ini", "tox.ini", "noxfile.py", "conftest.py"} or name in {"cargo.toml", "go.mod"}:
            mark("test_command", path)
        if name.endswith("_test.go") or (name.startswith("test_") and name.endswith(".py")):
            mark("test_command", path)
        if name == ".pre-commit-config.yaml":
            mark("precommit", path)
        if (
            name.startswith(".eslintrc")
            or name.startswith("eslint.config.")
            or name
            in {
                "ruff.toml",
                ".ruff.toml",
                ".flake8",
                ".golangci.yml",
                ".golangci.yaml",
                "clippy.toml",
            }
        ):
            mark("lint", path)
        if (
            name.startswith(".prettierrc")
            or name.startswith("prettier.config.")
            or name
            in {
                "rustfmt.toml",
                ".rustfmt.toml",
                ".editorconfig-checker.json",
            }
        ):
            mark("format", path)
        if name in {"tsconfig.json", "mypy.ini", ".mypy.ini", "pyrightconfig.json"}:
            mark("typecheck", path)
        if name in {"go.mod", "cargo.toml"}:
            mark("format", path)
            mark("typecheck", path)
        if any(f"/{marker}" in f"/{lower}" for marker in _MIGRATION_DIRS) and not lower.endswith("/"):
            mark("migrations", path)
        if _API_FILES.search(path):
            mark("api_surface", path)
        if _INFRA_FILES.search(path):
            mark("infrastructure", path)
        if name in {"dockerfile", "setup.py"}:
            mark("package_artifact", path)

    for manifest in sorted(path for path in names if path.rsplit("/", 1)[-1] == "package.json")[:20]:
        scripts, private, text = _package_scripts(reader(manifest))
        test_script = scripts.get("test", "")
        if test_script and "no test specified" not in test_script:
            mark("test_command", f"{manifest} scripts.test")
        for script, fact in (("lint", "lint"), ("format", "format"), ("typecheck", "typecheck")):
            if scripts.get(script):
                mark(fact, f"{manifest} scripts.{script}")
        if text and any(marker in text for marker in _WEB_FRAMEWORKS):
            mark("web_app", manifest)
        if _static_preview_framework(text):
            mark("previewable_app", f"{manifest} static web build")
        if text and not private:
            mark("package_artifact", manifest)

    for manifest in sorted(path for path in names if path.rsplit("/", 1)[-1] == "pyproject.toml")[:20]:
        text = reader(manifest) or ""
        if "[tool.pytest" in text:
            mark("test_command", f"{manifest} [tool.pytest]")
        if "[tool.ruff" in text or "[tool.flake8" in text or "[tool.pylint" in text:
            mark("lint", f"{manifest} linter config")
        if "[tool.black" in text or "[tool.ruff.format" in text:
            mark("format", f"{manifest} formatter config")
        if "[tool.mypy" in text or "[tool.pyright" in text:
            mark("typecheck", f"{manifest} type-checker config")
        if "[project]" in text or "[tool.poetry]" in text:
            mark("package_artifact", f"{manifest} [project]")

    if "Makefile" in names:
        text = reader("Makefile") or ""
        for target, fact in (
            ("test", "test_command"),
            ("lint", "lint"),
            ("format", "format"),
            ("typecheck", "typecheck"),
        ):
            if re.search(rf"^{target}\s*:", text, re.MULTILINE):
                mark(fact, f"Makefile {target} target")

    if ".lemoncrow/environment.toml" in names and _declares_previews(reader(".lemoncrow/environment.toml")):
        mark("previewable_app", ".lemoncrow/environment.toml previews")
    if ".lemoncrow/review.yaml" in names and _declares_interactive_preview(reader(".lemoncrow/review.yaml")):
        mark("previewable_app", ".lemoncrow/review.yaml interactive_preview")
    if ".lemoncrow/review.yaml" in names and _declares_render_root(reader(".lemoncrow/review.yaml")):
        mark("web_app", ".lemoncrow/review.yaml render.static_root")
    if any(path in names for path in ("index.html", "public/index.html", "src/index.html")):
        mark("web_app", "index.html")
    if any(row.applicability != "not_applicable" or row.required for row in browser_scenarios):
        mark("web_app", "configured browser_checks")

    return DetectedCapabilities(
        facts=frozenset(facts),
        evidence=evidence,
        recipes=tuple(recipes),
        browser_scenarios=tuple(browser_scenarios),
        surfaces=tuple(surfaces),
        base_state=base_state,
        truncated=truncated,
    )


# --------------------------------------------------------------------------- #
# lookup and applicability
# --------------------------------------------------------------------------- #

_DYNAMIC_SHAPES: Final[Mapping[str, CatalogEntry]] = {
    "checks.recipe:": CatalogEntry(
        "checks.recipe:", "checks", "Trusted-base check recipe", "medium", "base", executor=_CHECK_RECIPES
    ),
    # Browser scenarios execute outside LemonCrow (Playwright/CI/agent) and are
    # reported as evidence; the plan schedules which results are owed.
    "browser:": CatalogEntry(
        "browser:", "visual", "Browser scenario", "medium", "base", executor="external_browser_evidence", external=True
    ),
    "surface:": CatalogEntry(
        "surface:", "deep_runtime", "Runtime surface validation", "medium", "base", executor="runtime_validation"
    ),
}


def is_dynamic(entry_id: str) -> bool:
    return any(entry_id.startswith(prefix) for prefix in DYNAMIC_PREFIXES)


def valid_entry_id(entry_id: str) -> bool:
    """Syntactic validity: a static ID, or a well-formed dynamic/wildcard ID."""

    if entry_id in STATIC_CATALOG:
        return True
    for prefix in DYNAMIC_PREFIXES:
        if entry_id.startswith(prefix):
            rest = entry_id[len(prefix) :]
            return rest == "*" or bool(_DYNAMIC_ID.fullmatch(rest))
    return False


def resolve_entry(entry_id: str, detected: DetectedCapabilities) -> CatalogEntry | None:
    """Catalog entry for an ID; dynamic IDs must exist in the trusted base."""

    static = STATIC_CATALOG.get(entry_id)
    if static is not None:
        if entry_id == PREMERGE_CHECKS_ENTRY:
            return replace(static, calls_per_run=detected.premerge_checks)
        if entry_id == RULE_CHECKS_ENTRY:
            return replace(static, calls_per_run=detected.rule_checks)
        return static
    for prefix, shape in _DYNAMIC_SHAPES.items():
        if entry_id.startswith(prefix):
            target = detected.dynamic(entry_id)
            if target is None:
                return None
            return CatalogEntry(
                id=entry_id,
                family=shape.family,
                title=target.title or f"{shape.title} {target.id}",
                cost_class=shape.cost_class,
                degrade_tier=shape.degrade_tier,
                executor=shape.executor if target.executable else "",
                external=shape.external,
            )
    return None


def expand_wildcard(entry_id: str, detected: DetectedCapabilities) -> tuple[str, ...]:
    if not entry_id.endswith(":*"):
        return (entry_id,)
    prefix = entry_id[:-1]
    rows = {
        "checks.recipe:": detected.recipes,
        "browser:": detected.browser_scenarios,
        "surface:": detected.surfaces,
    }.get(prefix, ())
    return tuple(prefix + row.id for row in rows)


Applicability = Literal["applicable", "inapplicable", "unknown", "unavailable"]
#: The reason prefix of an entry whose applicability a bounded scan could not decide.
COVERAGE_UNKNOWN: Final[str] = "coverage_unknown"
#: Facts set from configuration, not from scanning base paths: a path bound cannot hide them.
_CONFIGURED_FACTS: Final[frozenset[str]] = frozenset({PREMERGE_CHECKS_FACT, RULE_CHECKS_FACT})


def applicability(entry: CatalogEntry, detected: DetectedCapabilities) -> tuple[Applicability, str]:
    """Whether the trusted base gives this entry anything to act on.

    ``unknown`` (reason prefixed :data:`COVERAGE_UNKNOWN`): detection stopped at its
    path bound before finding the fact, so absence is not established.
    """

    if is_dynamic(entry.id):
        target = detected.dynamic(entry.id)
        if target is None:
            return "inapplicable", "not present in the trusted base"
        if target.applicability == "not_applicable":
            return "inapplicable", target.reason or "no changed path or route reaches it"
        return "applicable", target.reason
    if detected.has(entry.requires):
        return "applicable", detected.evidence.get(entry.requires, "")
    if entry.requires in detected.unavailable:
        # Configured, but nothing it configures can run: shown with why, never planned to run.
        return "unavailable", detected.unavailable[entry.requires]
    if detected.base_state == "provisional":
        return "inapplicable", "trusted base is not readable yet, so applicability is unknown"
    labels = {
        "test_command": "no discoverable test command",
        "lint": "no linter configured",
        "format": "no formatter configured",
        "typecheck": "no type checker configured",
        "precommit": "no pre-commit configuration",
        "web_app": "no web app",
        "api_surface": "no API contract (OpenAPI/proto/GraphQL)",
        "migrations": "no migrations",
        "generators": "no trusted-base generator to regenerate",
        "package_artifact": "no shipped package or image",
        "infrastructure": "no infrastructure or deploy config",
        "previewable_app": "no previewable app",
        "compat_surface": "no migrations, API contract or message format to pair",
        "flag_overrides": "no flag-override command configured (analyses.flags)",
        PREMERGE_CHECKS_FACT: "no model-evaluated pre-merge check is configured",
        RULE_CHECKS_FACT: "no trusted Review rule applies to the changed paths",
    }
    absent = labels.get(entry.requires, f"{entry.requires} not detected")
    if detected.truncated and entry.requires not in _CONFIGURED_FACTS:
        return "unknown", (
            f"{COVERAGE_UNKNOWN}: trusted-base detection stopped at its path bound, "
            f"so '{absent}' is not established"
        )
    return "inapplicable", absent


def paths_match(patterns: Sequence[str], changed_paths: Sequence[str]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns for path in changed_paths)


__all__ = [
    "AI_INVESTIGATOR_CALLS",
    "COST_RANK",
    "COVERAGE_UNKNOWN",
    "DEGRADE_ORDER",
    "DETECTION_FACTS",
    "DYNAMIC_PREFIXES",
    "EXECUTOR_LOCI",
    "FAMILIES",
    "FAMILY_MODES",
    "PREMERGE_CHECKS_ENTRY",
    "PREMERGE_CHECKS_FACT",
    "PRESETS",
    "PRESET_RANK",
    "PRESET_SELECTIONS",
    "REVIEW_HOST",
    "RULE_CHECKS_ENTRY",
    "RULE_CHECKS_FACT",
    "SIDES_VALUES",
    "STATIC_CATALOG",
    "STATIC_MINIMUM_MINUTES",
    "WHEN_VALUES",
    "CapabilityFamily",
    "CatalogEntry",
    "CostClass",
    "DetectedCapabilities",
    "DynamicTarget",
    "ExecutorLocus",
    "FamilyMode",
    "Preset",
    "PresetEntry",
    "Sides",
    "When",
    "applicability",
    "detect_capabilities",
    "expand_wildcard",
    "is_dynamic",
    "paths_match",
    "preset_entry",
    "resolve_entry",
    "valid_entry_id",
]
