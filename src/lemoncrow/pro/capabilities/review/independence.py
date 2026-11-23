"""Independent AI reviewer assurance (Phase 11 item 14).

Phase 4 prefers a reviewer route distinct from the change's author model. This
module makes that an explicit, provenance-backed assurance property:

* **policy** -- ``review.independent_ai_reviewer: off | prefer | require`` at
  the organization, team, repository, trusted-base and path layers (a path
  rule: ``{mode, paths: [{paths, mode}]}``). It is a floor: the strictest
  applicable rule wins, so a lower layer can never weaken a higher one, and
  the effective mode carries the origin of the rule that set it.
* **distinctness** (conservative) -- independent only when the author's model
  is known from *exact* provenance with a known model family, and every
  reviewer identity is observed or explicitly pinned (never an unreported CLI
  default) with a known family different from the author's. Same family with a
  different model is *not* independent; an unknown or ambiguous family is not
  independent.
* **requirement** -- ``prefer`` labels the result (``preferred_met`` /
  ``preferred_unmet``); ``require`` is ``satisfied`` only when independent and
  is otherwise visibly ``unsatisfied``: the ``independent-ai-review``
  readiness condition stays pending. A human-authored change is
  ``not_applicable`` (the policy covers agent-authored changes).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.capability_catalog import paths_match
from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity, clean_model_name, model_family

IndependenceMode = Literal["off", "prefer", "require"]
MODES: Final[tuple[IndependenceMode, ...]] = ("off", "prefer", "require")
_MODE_RANK: Final[Mapping[str, int]] = {"off": 0, "prefer": 1, "require": 2}
POLICY_KEY: Final[str] = "independent_ai_reviewer"
CONDITION_KEY: Final[str] = "independent_ai_review"
CONDITION_ID: Final[str] = "independent-ai-review"
_MAX_PATH_RULES: Final[int] = 64
_MAX_GLOBS: Final[int] = 64
#: Reviewer identities listed on the wire; the count beyond it is reported.
_SHOWN_REVIEWERS: Final[int] = 8
#: ``LEMONCROW_REVIEW_REVIEWER_ROUTES``: alternative ``driver=model`` routes a
#: ``prefer``/``require`` policy may select. Entries past the bound are
#: reported (``routes_ignored``), never silently used or dropped.
REVIEWER_ROUTES_ENV: Final[str] = "LEMONCROW_REVIEW_REVIEWER_ROUTES"
MAX_REVIEWER_ROUTES: Final[int] = 8
_HOST_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

IndependenceStatus = Literal[
    "independent",
    "not_independent",
    "unknown_author",
    "unreported_reviewer",
    "unverified_reviewer",
    "unknown_reviewer",
    "not_applicable",
    "not_run",
]
RequirementState = Literal[
    "not_required", "not_applicable", "preferred_met", "preferred_unmet", "satisfied", "unsatisfied"
]


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IndependenceRule:
    """One layer's mode, optionally path-scoped (``origin``: ``run_plan.SettingOrigin``)."""

    mode: IndependenceMode
    origin: Any
    paths: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {"mode": self.mode, "paths": list(self.paths), "origin": self.origin.to_wire()}


def _mode(raw: object) -> IndependenceMode | None:
    # YAML 1.1 reads a bare ``off`` as False.
    if raw is False:
        return "off"
    return raw if isinstance(raw, str) and raw in MODES else None


def _globs(raw: object, where: str, errors: list[str]) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        errors.append(f"{where} must be a non-empty list of path globs")
        return ()
    valid = [item.strip() for item in raw[:_MAX_GLOBS] if isinstance(item, str) and item.strip() and len(item) <= 512]
    if len(valid) != len(raw[:_MAX_GLOBS]) or len(raw) > _MAX_GLOBS:
        errors.append(f"{where} entries must be at most {_MAX_GLOBS} non-empty strings")
    return tuple(dict.fromkeys(valid))


def parse_independence_policy(raw: object, origin: Any, errors: list[str]) -> tuple[IndependenceRule, ...]:
    """One layer's ``independent_ai_reviewer``: a mode, or ``{mode, paths: [{paths, mode}]}``."""

    mode = _mode(raw)
    if mode is not None:
        return (IndependenceRule(mode, origin),)
    if not isinstance(raw, Mapping):
        errors.append(f"{POLICY_KEY} must be off, prefer, require or a mapping with mode/paths")
        return ()
    rules: list[IndependenceRule] = []
    unknown = sorted(str(key) for key in set(raw) - {"mode", "paths"})
    if unknown:
        errors.append(f"{POLICY_KEY} has unknown field {unknown[0]!r}")
    if "mode" in raw:
        base = _mode(raw["mode"])
        if base is None:
            errors.append(f"{POLICY_KEY}.mode must be off, prefer or require")
        else:
            rules.append(IndependenceRule(base, origin))
    path_rules = raw.get("paths", [])
    if not isinstance(path_rules, list):
        errors.append(f"{POLICY_KEY}.paths must be a list of {{paths, mode}} rules")
        return tuple(rules)
    for index, item in enumerate(path_rules[:_MAX_PATH_RULES]):
        where = f"{POLICY_KEY}.paths[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{where} must be an object with paths and mode")
            continue
        extra = sorted(str(key) for key in set(item) - {"paths", "mode"})
        if extra:
            errors.append(f"{where} has unknown field {extra[0]!r}")
        scoped = _mode(item.get("mode"))
        if scoped is None:
            errors.append(f"{where}.mode must be off, prefer or require")
            continue
        globs = _globs(item.get("paths"), f"{where}.paths", errors)
        if globs:
            rules.append(IndependenceRule(scoped, origin, globs))
    if len(path_rules) > _MAX_PATH_RULES:
        errors.append(f"{POLICY_KEY}.paths exceeds {_MAX_PATH_RULES} rules")
    return tuple(rules)


def independence_document(rules: Sequence[IndependenceRule]) -> object:
    """``independent_ai_reviewer``-shaped document of one layer; parsing it yields equal rules."""

    base = [rule for rule in rules if not rule.paths]
    scoped = [rule for rule in rules if rule.paths]
    if base and not scoped and len(base) == 1:
        return base[0].mode
    out: dict[str, Any] = {}
    if base:
        out["mode"] = base[-1].mode
    if scoped:
        out["paths"] = [{"paths": list(rule.paths), "mode": rule.mode} for rule in scoped]
    return out


@dataclass(frozen=True, slots=True)
class IndependencePolicy:
    """The effective mode for one Change and the rule (origin, matched paths) that set it."""

    mode: IndependenceMode = "off"
    rule: IndependenceRule | None = None
    matched_paths: tuple[str, ...] = ()

    @property
    def origin_label(self) -> str:
        if self.rule is None:
            return "built-in default"
        return str(self.rule.origin.source or self.rule.origin.layer)

    def to_wire(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "origin": (
                self.rule.origin.to_wire()
                if self.rule is not None
                else {"layer": "built_in_default", "source": "built_in_default"}
            ),
            "paths": [] if self.rule is None else list(self.rule.paths),
            "matched_paths": list(self.matched_paths[:8]),
            "matched_path_count": len(self.matched_paths),
        }


def resolve_independence(rules: Sequence[IndependenceRule], changed_paths: Sequence[str]) -> IndependencePolicy:
    """Strictest applicable rule wins (a floor); ties go to the highest-precedence layer."""

    applicable = [rule for rule in rules if not rule.paths or paths_match(rule.paths, changed_paths)]
    if not applicable:
        return IndependencePolicy()
    top = max(_MODE_RANK[rule.mode] for rule in applicable)
    chosen = [rule for rule in applicable if _MODE_RANK[rule.mode] == top][-1]
    matched = (
        tuple(path for path in dict.fromkeys(changed_paths) if paths_match(chosen.paths, (path,)))
        if chosen.paths
        else ()
    )
    return IndependencePolicy(chosen.mode, chosen, matched)


# --------------------------------------------------------------------------- #
# author and distinctness
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AgentAuthor:
    """An agent author known from exact provenance (never a human)."""

    host: str
    model: str

    @property
    def family(self) -> str:
        return model_family(self.model)

    @property
    def label(self) -> str:
        return f"{self.host}/{self.model}"

    def to_wire(self) -> dict[str, Any]:
        return {"host": self.host, "model": self.model, "family": self.family, "label": self.label}


def clean_host(value: object) -> str:
    text = str(value or "").strip().lower() if isinstance(value, str) else ""
    return text if _HOST_RE.match(text) else ""


def exact_agent_author(host: object, model: object, certainty: object) -> AgentAuthor | None:
    """The agent author when provenance is *exact* and names both host and model."""

    if certainty != "exact":
        return None
    clean_h, clean_m = clean_host(host), clean_model_name(model)
    if not clean_h or not clean_m:
        return None
    return AgentAuthor(clean_h, clean_m)


Authorship = Literal["agent", "human", "unknown"]


def authorship(session: Any, revision: Any) -> tuple[Authorship, AgentAuthor | None]:
    """``agent`` (exact provenance), ``human`` (no agent signal at all), else ``unknown``."""

    author = exact_agent_author(
        getattr(revision, "provenance_host", ""),
        getattr(revision, "provenance_model", ""),
        getattr(revision, "provenance_certainty", "none"),
    )
    if author is not None:
        return "agent", author
    actor = str(getattr(session, "actor_type", "") or "")
    if actor == "human" and not str(getattr(revision, "provenance_host", "") or ""):
        return "human", None
    return "unknown", None


def reviewer_families(reviewer: ModelIdentity) -> frozenset[str]:
    return frozenset(model_family(model) for model in reviewer.effective_models)


def provably_distinct(author_family: str, reviewer: ModelIdentity) -> bool:
    """The conservative distinctness rule: known author family, provable reviewer, other family."""

    if not author_family or not reviewer.provable:
        return False
    families = reviewer_families(reviewer)
    if not families or "" in families:
        return False
    return author_family not in families


# --------------------------------------------------------------------------- #
# assessment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IndependenceAssessment:
    status: IndependenceStatus
    reason: str
    author: AgentAuthor | None = None
    reviewers: tuple[ModelIdentity, ...] = ()

    @property
    def independent(self) -> bool:
        return self.status == "independent"

    def to_wire(self) -> dict[str, Any]:
        shown = self.reviewers[:_SHOWN_REVIEWERS]
        return {
            "status": self.status,
            "independent": self.independent,
            "reason": self.reason,
            **({"author": self.author.to_wire()} if self.author is not None else {}),
            "reviewers": [
                {**row.to_wire(), "families": sorted(family for family in reviewer_families(row) if family)}
                for row in shown
            ],
            "reviewers_not_shown": len(self.reviewers) - len(shown),
        }


def assess_independence(
    kind: Authorship, author: AgentAuthor | None, reviewers: Sequence[ModelIdentity]
) -> IndependenceAssessment:
    """Is every reviewer identity provably distinct from the author? Never labeled without proof."""

    unique = tuple(dict.fromkeys(reviewers))
    if kind == "human":
        return IndependenceAssessment(
            "not_applicable", "human-authored change: the policy covers agent-authored changes", None, unique
        )
    if author is None:
        return IndependenceAssessment(
            "unknown_author", "the author's model is not known from exact provenance", None, unique
        )
    if not author.family:
        return IndependenceAssessment(
            "unknown_author", f"the author model {author.label} has no known model family", author, unique
        )
    if not unique:
        return IndependenceAssessment("not_run", "no AI review has run on this revision", author, unique)
    for reviewer in unique:
        if reviewer.source == "unreported_default":
            return IndependenceAssessment(
                "unreported_reviewer",
                f"{reviewer.driver} ran on its unreported default model, which cannot prove independence",
                author,
                unique,
            )
        if reviewer.source == "unverified":
            return IndependenceAssessment(
                "unverified_reviewer",
                f"{reviewer.driver} model {reviewer.label} was not observed or pinned by LemonCrow",
                author,
                unique,
            )
        families = reviewer_families(reviewer)
        if reviewer.observed_truncated or not families or "" in families:
            return IndependenceAssessment(
                "unknown_reviewer",
                f"reviewer model {reviewer.label} has no single known model family",
                author,
                unique,
            )
        if not provably_distinct(author.family, reviewer):
            return IndependenceAssessment(
                "not_independent",
                f"reviewer {reviewer.driver} {reviewer.label} is the author's model family "
                f"({author.family}, author {author.label})",
                author,
                unique,
            )
    return IndependenceAssessment(
        "independent",
        f"every reviewer model is outside the author's model family ({author.family}, author {author.label})",
        author,
        unique,
    )


def requirement_state(mode: IndependenceMode, assessment: IndependenceAssessment) -> RequirementState:
    if mode == "off":
        return "not_required"
    if assessment.status == "not_applicable":
        return "not_applicable"
    if mode == "prefer":
        return "preferred_met" if assessment.independent else "preferred_unmet"
    return "satisfied" if assessment.independent else "unsatisfied"


def reviewer_identities(rows: Sequence[Any], trace: Mapping[str, Any] | None = None) -> tuple[ModelIdentity, ...]:
    """Identities of the ready AI review rows; without rows, a worker trace's claim (unverified)."""

    from lemoncrow.pro.capabilities.review.intelligence_runtime import run_identity

    ready = tuple(dict.fromkeys(run_identity(row) for row in rows if row.status == "ready"))
    if ready:
        return ready
    if isinstance(trace, Mapping) and str(trace.get("provider") or ""):
        # A customer worker's trace names its model; LemonCrow observed nothing.
        return (ModelIdentity(str(trace["provider"]), unverified_model=clean_model_name(trace.get("model"))),)
    return ()


def assess_revision(
    session: Any, revision: Any, rows: Sequence[Any], trace: Mapping[str, Any] | None = None
) -> IndependenceAssessment:
    kind, author = authorship(session, revision)
    return assess_independence(kind, author, reviewer_identities(rows, trace))


def independence_facts(policy: IndependencePolicy, assessment: IndependenceAssessment) -> IndependenceFacts:
    return IndependenceFacts(
        mode=policy.mode,
        requirement=requirement_state(policy.mode, assessment),
        status=assessment.status,
        reason=assessment.reason,
        origin=policy.origin_label,
    )


def independence_block(policy: IndependencePolicy, assessment: IndependenceAssessment) -> dict[str, Any]:
    """The ``ai_review.assurance.independence`` wire block."""

    return {
        **assessment.to_wire(),
        "policy": policy.to_wire(),
        "requirement": requirement_state(policy.mode, assessment),
    }


# --------------------------------------------------------------------------- #
# reviewer routes
# --------------------------------------------------------------------------- #


def configured_reviewer_routes(environ: Mapping[str, str] | None = None) -> tuple[tuple[tuple[str, str], ...], int]:
    """``driver=model`` alternatives from :data:`REVIEWER_ROUTES_ENV`, and how many were ignored."""

    raw = (environ if environ is not None else os.environ).get(REVIEWER_ROUTES_ENV, "")
    entries = [item.strip() for item in raw.split(",") if item.strip()]
    routes: list[tuple[str, str]] = []
    ignored = 0
    for item in entries:
        driver, _, model = item.partition("=")
        clean_driver, clean_model = clean_host(driver), clean_model_name(model)
        if not clean_driver or not clean_model or len(routes) >= MAX_REVIEWER_ROUTES:
            ignored += 1
            continue
        routes.append((clean_driver, clean_model))
    if ignored:
        logging.getLogger(__name__).warning(
            "%s: %d route(s) ignored (malformed or past %d)", REVIEWER_ROUTES_ENV, ignored, MAX_REVIEWER_ROUTES
        )
    return tuple(dict.fromkeys(routes)), ignored


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IndependenceFacts:
    mode: IndependenceMode
    requirement: RequirementState
    status: IndependenceStatus
    reason: str
    origin: str


def independence_condition(inputs: Any) -> Iterable[Any]:
    """``require`` is visible: pass when independent, pending (never PASS) otherwise."""

    from lemoncrow.pro.capabilities.review.readiness import ReadinessCondition

    facts = inputs.fact(CONDITION_KEY)
    if facts is None:
        return ()
    if not isinstance(facts, IndependenceFacts):
        raise ValueError("independent AI review state is unavailable")
    if facts.mode != "require" or facts.requirement == "not_applicable":
        return ()
    satisfied = facts.requirement == "satisfied"
    return (
        ReadinessCondition(
            id=CONDITION_ID,
            label="Independent AI review",
            state="pass" if satisfied else "pending",
            detail=(
                f"Required by {facts.origin}: "
                + ("satisfied; " if satisfied else "not satisfied; ")
                + facts.reason
                + "."
            ),
            category="policy",
            reason="independent" if satisfied else facts.status,
            extra={"requirement": facts.requirement, "independence_status": facts.status},
        ),
    )


__all__ = [
    "CONDITION_ID",
    "CONDITION_KEY",
    "MAX_REVIEWER_ROUTES",
    "MODES",
    "POLICY_KEY",
    "REVIEWER_ROUTES_ENV",
    "AgentAuthor",
    "IndependenceAssessment",
    "IndependenceFacts",
    "IndependenceMode",
    "IndependencePolicy",
    "IndependenceRule",
    "assess_independence",
    "assess_revision",
    "authorship",
    "configured_reviewer_routes",
    "exact_agent_author",
    "independence_block",
    "independence_condition",
    "independence_document",
    "independence_facts",
    "parse_independence_policy",
    "provably_distinct",
    "requirement_state",
    "resolve_independence",
    "reviewer_identities",
]
