"""Reviewer suggestions: advisory, explainable and deterministic (Phase 9 item 7).

A suggestion names a candidate reviewer -- a person **or a team** -- and says why,
per signal and per changed path:

* ``rule`` -- explicit reviewer rules from operator/organization policy or the
  trusted-base ``.lemoncrow/review.yaml`` (``review.reviewers``);
* ``codeowners`` -- CODEOWNERS read only from the **trusted base** (local Snapshot
  base, or the hosted provider-proven trusted-base capture). A head-side
  CODEOWNERS edit never feeds suggestions for the Review that contains it; it is
  reported as a reviewable change instead (:func:`codeowners_head_changes`);
* ``ownership`` -- who reviewed these paths in earlier LemonCrow Reviews;
* ``history`` -- recent meaningful authors/reviewers of these paths in git
  history at the immutable base commit (``reviewer_history``).

Path/criticality policy (P9-PRIO) weights every covered path and critical/high
paths are named in the explanation. The Change author, authoring agents and
principals without access are excluded; equal evidence orders by kind, label
and durable key so the list is stable.

Nothing here requests, assigns, or touches readiness, merge eligibility or
permissions. Callers render suggestions; only an explicit action, or an
organization routing rule that opted into automatic routing
(:class:`ReviewerRouting`), may act on one.

Pure: no store, no I/O.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review.criticality import (
    BUILT_IN_ORIGIN,
    CRITICALITY_RANK,
    CRITICALITY_VALUES,
    Criticality,
    PolicyOrigin,
)

CandidateKind = Literal["user", "team"]
SignalKind = Literal["rule", "codeowners", "ownership", "history"]
AccessState = Literal["granted", "unknown", "denied", "not_applicable"]
"""``not_applicable``: a single-user local Review has no access model to verify."""
ExclusionReason = Literal["author", "authoring_agent", "no_access"]
SignalState = Literal["available", "absent", "provisional", "unavailable", "invalid"]
CodeOwnersState = Literal["present", "absent", "provisional", "invalid"]

SIGNAL_ORDER: Final[tuple[str, ...]] = ("rule", "codeowners", "ownership", "history")
#: Explicit policy outranks CODEOWNERS, which outranks LemonCrow review history,
#: which outranks git history. Weights multiply the criticality weight of each
#: distinct covered path, so a critical path counts double a normal one.
SIGNAL_WEIGHT: Final[Mapping[str, int]] = {"rule": 100, "codeowners": 50, "ownership": 20, "history": 10}
CRITICALITY_WEIGHT: Final[Mapping[str, int]] = {"critical": 4, "high": 3, "normal": 2, "low": 1}
#: Repeated evidence (more commits / more Reviews) adds a small, capped bonus.
_REPEAT_BONUS: Final[int] = 2
_REPEAT_CAP: Final[int] = 5
DEFAULT_SUGGESTION_LIMIT: Final[int] = 8
MAX_WIRE_PATHS: Final[int] = 50
_KIND_RANK: Final[Mapping[str, int]] = {"user": 0, "team": 1}

CODEOWNERS_PATHS: Final[tuple[str, ...]] = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
"""GitHub's CODEOWNERS locations in precedence order: the first present file is the only one used."""
MAX_CODEOWNERS_BYTES: Final[int] = 3 * 1024 * 1024
MAX_CODEOWNERS_RULES: Final[int] = 5000
MAX_REVIEWER_RULES: Final[int] = 128
_MAX_RULE_ENTRIES: Final[int] = 64

_USER_OWNER = re.compile(r"^@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_TEAM_OWNER = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_EMAIL_OWNER = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


# --------------------------------------------------------------------------- #
# identities and evidence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReviewerIdentity:
    """One candidate reviewer keyed by a durable identity.

    ``key`` is the merge and routing identity: ``principal:<id>`` for a bound
    LemonCrow principal, ``<provider>:user:<durable id>`` /
    ``<provider>:team:<durable id>`` for a provider identity (a login or team
    slug rename keeps the key), and handle/email keys only where no durable
    identity exists (local CODEOWNERS, git history). ``label`` is display text.
    """

    kind: CandidateKind
    key: str
    label: str
    principal_id: str = ""
    handle: str = ""
    provider: str = ""
    durable_id: str = ""

    def to_wire(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "principal_id": self.principal_id,
            "handle": self.handle,
            "provider": self.provider,
            "durable_id": self.durable_id,
        }


@dataclass(frozen=True, slots=True)
class ReviewerEvidence:
    """One signal naming one candidate for some changed paths."""

    signal: SignalKind
    identity: ReviewerIdentity
    paths: tuple[str, ...]
    source: str
    detail: str = ""
    rule_id: str = ""
    count: int = 1
    automatic: bool = False
    """An operator rule that opted into automatic routing (only meaningful for ``rule``)."""


@dataclass(frozen=True, slots=True)
class SuggestionReason:
    """Why, per signal: which source and which changed paths."""

    signal: SignalKind
    paths: tuple[str, ...]
    sources: tuple[str, ...]
    detail: str
    rule_ids: tuple[str, ...] = ()
    count: int = 1

    def to_wire(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "paths": list(self.paths[:MAX_WIRE_PATHS]),
            "path_count": len(self.paths),
            "sources": list(self.sources),
            "detail": self.detail,
            "rule_ids": list(self.rule_ids),
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class ReviewerSuggestion:
    identity: ReviewerIdentity
    score: int
    reasons: tuple[SuggestionReason, ...]
    paths: tuple[str, ...]
    critical_paths: tuple[str, ...]
    access: AccessState
    requested: bool = False
    automatic_rules: tuple[str, ...] = ()
    """Operator rule ids that may route this candidate automatically (when the org opted in)."""

    @property
    def explanation(self) -> str:
        return " · ".join(reason.detail for reason in self.reasons)

    def to_wire(self) -> dict[str, Any]:
        return {
            **self.identity.to_wire(),
            "score": self.score,
            "explanation": self.explanation,
            "reasons": [reason.to_wire() for reason in self.reasons],
            "paths": list(self.paths[:MAX_WIRE_PATHS]),
            "path_count": len(self.paths),
            "critical_paths": list(self.critical_paths[:MAX_WIRE_PATHS]),
            "access": self.access,
            "status": "requested" if self.requested else "suggested",
            "automatic_rules": list(self.automatic_rules),
        }


@dataclass(frozen=True, slots=True)
class SignalStatus:
    signal: SignalKind
    state: SignalState
    source: str = ""
    detail: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"signal": self.signal, "state": self.state, "source": self.source, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class SuggestionSet:
    """The advisory projection one Review renders (``to_wire`` is the API contract)."""

    suggestions: tuple[ReviewerSuggestion, ...] = ()
    excluded: Mapping[str, int] = field(default_factory=dict)
    truncated: int = 0
    signals: tuple[SignalStatus, ...] = ()
    head_changes: tuple[Mapping[str, str], ...] = ()
    revision_id: str = ""
    automatic_routing: bool = False
    errors: tuple[str, ...] = ()

    def find(self, key: str) -> ReviewerSuggestion | None:
        return next((row for row in self.suggestions if row.identity.key == key), None)

    def to_wire(self) -> dict[str, Any]:
        return {
            "advisory": True,
            "revision_id": self.revision_id,
            "suggestions": [row.to_wire() for row in self.suggestions],
            "excluded": dict(sorted(self.excluded.items())),
            "truncated": self.truncated,
            "signals": [row.to_wire() for row in self.signals],
            "head_changes": [dict(row) for row in self.head_changes],
            "automatic_routing": self.automatic_routing,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# CODEOWNERS (trusted base only)
# --------------------------------------------------------------------------- #


def _codeowners_regex(pattern: str) -> re.Pattern[str]:
    """GitHub CODEOWNERS pattern -> anchored regex over repository-relative paths.

    gitignore-style: a leading or inner ``/`` anchors to the root, otherwise the
    pattern matches at any depth; a trailing ``/`` (or a last segment without
    wildcards) also matches everything below that directory; ``*`` stays within
    one segment, ``**`` crosses segments. ``docs/*`` matches direct children
    only, exactly as GitHub documents.
    """

    raw = pattern.replace("\\#", "#")
    anchored = raw.startswith("/") or "/" in raw.strip("/")
    dir_only = raw.endswith("/")
    body = raw.strip("/")
    last = body.rsplit("/", 1)[-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        if body.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif body.startswith("/**", index) and index + 3 == len(body):
            out.append("/.*")
            index += 3
        elif body.startswith("**", index):
            out.append(".*")
            index += 2
        elif body[index] == "*":
            out.append("[^/]*")
            index += 1
        elif body[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(body[index]))
            index += 1
    prefix = "" if anchored else "(?:.*/)?"
    if dir_only:
        suffix = "/.*"
    elif last == "**" or "*" in last or "?" in last:
        suffix = ""
    else:
        suffix = "(?:/.*)?"
    return re.compile("^" + prefix + "".join(out) + suffix + "$")


@dataclass(frozen=True, slots=True)
class CodeOwnersRule:
    pattern: str
    owners: tuple[str, ...]
    line: int
    regex: re.Pattern[str] = field(repr=False, compare=False)

    def matches(self, path: str) -> bool:
        return bool(self.regex.match(path))


@dataclass(frozen=True, slots=True)
class CodeOwners:
    path: str
    rules: tuple[CodeOwnersRule, ...] = ()
    errors: tuple[str, ...] = ()

    def owners_for(self, path: str) -> tuple[tuple[str, ...], CodeOwnersRule | None]:
        """The last matching rule wins; a matching rule without owners leaves the path unowned."""

        for rule in reversed(self.rules):
            if rule.matches(path):
                return rule.owners, rule
        return (), None


def owner_kind(owner: str) -> CandidateKind | Literal["email"] | None:
    if _TEAM_OWNER.match(owner):
        return "team"
    if _USER_OWNER.match(owner):
        return "user"
    if _EMAIL_OWNER.match(owner):
        return "email"
    return None


def parse_codeowners(text: str, *, path: str) -> CodeOwners:
    """Parse CODEOWNERS text; malformed owners/lines are reported, never guessed."""

    rules: list[CodeOwnersRule] = []
    errors: list[str] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        pattern = tokens[0]
        owners: list[str] = []
        for token in tokens[1:]:
            if token.startswith("#"):
                break  # inline comment
            if owner_kind(token) is None:
                errors.append(f"{path}:{number}: invalid owner {token[:80]!r}")
                continue
            owners.append(token)
        if pattern.startswith("!") or "[" in pattern:
            errors.append(f"{path}:{number}: unsupported pattern {pattern[:80]!r}")
            continue
        if len(rules) >= MAX_CODEOWNERS_RULES:
            errors.append(f"{path}: more than {MAX_CODEOWNERS_RULES} rules; the rest are ignored")
            break
        rules.append(CodeOwnersRule(pattern, tuple(dict.fromkeys(owners)), number, _codeowners_regex(pattern)))
    return CodeOwners(path=path, rules=tuple(rules), errors=tuple(errors[:50]))


@dataclass(frozen=True, slots=True)
class CodeOwnersAuthority:
    """The trusted-base CODEOWNERS for one Snapshot.

    ``provisional`` means a higher-precedence candidate could not be read: no
    lower file is promoted past it and no CODEOWNERS signal is produced.
    """

    state: CodeOwnersState
    path: str = ""
    source: str = ""
    codeowners: CodeOwners | None = None
    detail: str = ""

    def status(self) -> SignalStatus:
        state: SignalState = "available" if self.state == "present" else self.state
        return SignalStatus("codeowners", state, self.source or self.path, self.detail)


def resolve_codeowners(read_base_document: Callable[[str], Any]) -> CodeOwnersAuthority:
    """Trusted-base CODEOWNERS via a ``TrustedBaseDocument`` reader; head bytes never enter."""

    for candidate in CODEOWNERS_PATHS:
        document = read_base_document(candidate)
        state = str(getattr(document, "state", "provisional") or "provisional")
        source = str(getattr(document, "source", "") or "")
        if state == "provisional":
            return CodeOwnersAuthority(
                "provisional", candidate, source, None, f"trusted-base {candidate} could not be read"
            )
        if state != "present":
            continue
        content = str(getattr(document, "content", "") or "")
        if len(content.encode("utf-8")) > MAX_CODEOWNERS_BYTES:
            return CodeOwnersAuthority("invalid", candidate, source, None, f"{candidate} exceeds 3 MiB")
        parsed = parse_codeowners(content, path=candidate)
        detail = f"{len(parsed.errors)} invalid line(s) ignored" if parsed.errors else ""
        return CodeOwnersAuthority("present", candidate, source or f"base {candidate}", parsed, detail)
    return CodeOwnersAuthority("absent", detail="no CODEOWNERS at the trusted base")


def codeowners_head_changes(files: Iterable[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    """CODEOWNERS files this Change edits: reviewable changes, never this Review's routing authority."""

    out: list[dict[str, str]] = []
    for item in files:
        path = str(item.get("path") or "")
        old_path = str(item.get("old_path") or "")
        touched = next((name for name in (path, old_path) if name in CODEOWNERS_PATHS), "")
        if not touched:
            continue
        out.append(
            {
                "path": touched,
                "change": str(item.get("status") or "modified"),
                "note": (
                    f"{touched} changes in this Change. Suggestions use the trusted base's CODEOWNERS; "
                    "review the ownership change itself -- it applies only to later Reviews once landed."
                ),
            }
        )
    return tuple(out)


def codeowners_evidence(
    authority: CodeOwnersAuthority,
    paths: Sequence[str],
    *,
    resolve_owner: Callable[[str], ReviewerIdentity | None],
) -> tuple[ReviewerEvidence, ...]:
    """One evidence row per owner: the changed paths it owns and the rule pattern(s)."""

    if authority.state != "present" or authority.codeowners is None:
        return ()
    owned: dict[str, tuple[ReviewerIdentity, list[str], list[str]]] = {}
    for path in dict.fromkeys(paths):
        owners, rule = authority.codeowners.owners_for(path)
        if rule is None:
            continue
        for owner in owners:
            identity = resolve_owner(owner)
            if identity is None:
                continue
            _held, held_paths, patterns = owned.setdefault(identity.key, (identity, [], []))
            if path not in held_paths:
                held_paths.append(path)
            if rule.pattern not in patterns:
                patterns.append(rule.pattern)
    source = authority.source or authority.path
    return tuple(
        ReviewerEvidence(
            signal="codeowners",
            identity=identity,
            paths=tuple(held_paths),
            source=source,
            detail=f"{authority.path} {', '.join(patterns[:3])}" + (" …" if len(patterns) > 3 else ""),
        )
        for identity, held_paths, patterns in owned.values()
    )


# --------------------------------------------------------------------------- #
# explicit reviewer rules and routing policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReviewerRule:
    """One explicit ``paths -> users/teams`` reviewer rule.

    ``automatic`` is honored only for an operator-layer rule while the
    effective routing policy opted in (:attr:`ReviewerRouting.automatic`); a
    repository's own trusted-base rule is always advisory.
    """

    id: str
    paths: tuple[str, ...]
    origin: PolicyOrigin
    users: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    min_criticality: Criticality | None = None
    automatic: bool = False

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.paths)

    @property
    def operator(self) -> bool:
        return self.origin.layer in {"organization", "repository", "team"}

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "paths": list(self.paths),
            "users": list(self.users),
            "teams": list(self.teams),
            "min_criticality": self.min_criticality,
            "automatic": self.automatic,
            "origin": self.origin.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class RoutingLayer:
    origin: PolicyOrigin
    rules: tuple[ReviewerRule, ...] = ()
    automatic: bool | None = None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewerRouting:
    """Effective explicit reviewer rules plus the organization's automatic-routing opt-in."""

    rules: tuple[ReviewerRule, ...] = ()
    automatic: bool = False
    automatic_origin: PolicyOrigin = BUILT_IN_ORIGIN
    errors: tuple[str, ...] = ()

    def rule(self, rule_id: str) -> ReviewerRule | None:
        return next((rule for rule in self.rules if rule.id == rule_id), None)

    def routes_automatically(self, rule: ReviewerRule) -> bool:
        return self.automatic and rule.automatic and rule.operator

    def to_wire(self) -> dict[str, Any]:
        return {
            "automatic": self.automatic,
            "automatic_origin": self.automatic_origin.to_wire(),
            "rules": [rule.to_wire() for rule in self.rules],
            "errors": list(self.errors),
        }


def _entries(raw: object, where: str, errors: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        errors.append(f"{where} must be a list of strings")
        return ()
    out: list[str] = []
    for item in raw[:_MAX_RULE_ENTRIES]:
        if not isinstance(item, str) or not item.strip() or len(item) > 256:
            errors.append(f"{where} entries must be non-empty strings")
            continue
        out.append(item.strip())
    if len(raw) > _MAX_RULE_ENTRIES:
        errors.append(f"{where} exceeds {_MAX_RULE_ENTRIES} entries")
    return tuple(dict.fromkeys(out))


_RULE_KEYS: Final[frozenset[str]] = frozenset({"id", "paths", "users", "teams", "min_criticality", "automatic"})


def parse_reviewer_rules(raw: object, *, origin: PolicyOrigin, where: str = "reviewers") -> RoutingLayer:
    """``[{id, paths, users?, teams?, min_criticality?, automatic?}]``; every error is visible."""

    errors: list[str] = []
    rules: list[ReviewerRule] = []
    if raw is None:
        return RoutingLayer(origin=origin)
    if not isinstance(raw, list):
        return RoutingLayer(origin=origin, errors=(f"{where} must be a list of rules",))
    seen: set[str] = set()
    for index, item in enumerate(raw[:MAX_REVIEWER_RULES]):
        at = f"{where}[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{at} must be a mapping")
            continue
        for key in sorted(set(item) - _RULE_KEYS):
            errors.append(f"{at} has unknown field {key!r}")
        rule_id = str(item.get("id") or "").strip()
        if not _RULE_ID.match(rule_id):
            errors.append(f"{at}.id must be a stable identifier")
            continue
        if rule_id in seen:
            errors.append(f"{at}.id {rule_id!r} is duplicated")
            continue
        paths = _entries(item.get("paths"), f"{at}.paths", errors)
        users = _entries(item.get("users"), f"{at}.users", errors)
        teams = _entries(item.get("teams"), f"{at}.teams", errors)
        bad_teams = [team for team in teams if not _TEAM_OWNER.match(team if team.startswith("@") else f"@{team}")]
        if bad_teams:
            errors.append(f"{at}.teams entries must be org/team handles")
            teams = tuple(team for team in teams if team not in bad_teams)
        if not paths:
            errors.append(f"{at} needs paths")
            continue
        if not users and not teams:
            errors.append(f"{at} names no users or teams")
            continue
        floor = item.get("min_criticality")
        if floor is not None and floor not in CRITICALITY_VALUES:
            errors.append(f"{at}.min_criticality must be critical, high, normal or low")
            continue
        automatic = item.get("automatic", False)
        if not isinstance(automatic, bool):
            errors.append(f"{at}.automatic must be a boolean")
            automatic = False
        seen.add(rule_id)
        rules.append(
            ReviewerRule(
                id=rule_id,
                paths=paths,
                origin=origin,
                users=users,
                teams=tuple(team if team.startswith("@") else f"@{team}" for team in teams),
                min_criticality=cast("Criticality | None", floor),
                automatic=automatic,
            )
        )
    if len(raw) > MAX_REVIEWER_RULES:
        errors.append(f"{where} exceeds {MAX_REVIEWER_RULES} rules")
    return RoutingLayer(origin=origin, rules=tuple(rules), errors=tuple(errors))


def parse_routing_settings(raw: object, *, origin: PolicyOrigin) -> RoutingLayer:
    """An operator ``routing: {automatic?, rules?}`` section."""

    if not isinstance(raw, Mapping):
        return RoutingLayer(origin=origin, errors=("routing must be an object",))
    errors: list[str] = [f"routing has unknown field {key!r}" for key in sorted(set(raw) - {"automatic", "rules"})]
    automatic = raw.get("automatic")
    if automatic is not None and not isinstance(automatic, bool):
        errors.append("routing.automatic must be a boolean")
        automatic = None
    layer = parse_reviewer_rules(raw.get("rules"), origin=origin, where="routing.rules")
    return RoutingLayer(
        origin=origin,
        rules=layer.rules,
        automatic=automatic,
        errors=(*errors, *layer.errors),
    )


def trusted_base_routing(content: str, *, source: str, commit: str = "") -> RoutingLayer | None:
    """``review.reviewers`` from trusted-base ``.lemoncrow/review.yaml`` bytes (advisory only).

    A repository document may name reviewers but can never enable automatic
    routing: ``automatic`` on its rules is ignored by :meth:`ReviewerRouting.routes_automatically`.
    """

    origin = PolicyOrigin("trusted_base_repository", source or "base revision .lemoncrow/review.yaml", commit)
    try:
        decoded = yaml.safe_load(content) if content.strip() else None
    except yaml.YAMLError as exc:
        return RoutingLayer(origin=origin, errors=(f"invalid review configuration: {exc}",))
    if not isinstance(decoded, Mapping):
        return None
    section = decoded.get("review")
    if not isinstance(section, Mapping) or "reviewers" not in section:
        return None
    return parse_reviewer_rules(section.get("reviewers"), origin=origin, where="review.reviewers")


def operator_routing_layers(effective: object) -> tuple[RoutingLayer, ...]:
    """Operator layers from ``effective["routing"]["layers"]`` (each ``{origin, settings}``)."""

    if not isinstance(effective, Mapping):
        return ()
    routing = effective.get("routing")
    if routing is None:
        return ()
    if not isinstance(routing, Mapping) or not isinstance(routing.get("layers") or [], list):
        return (RoutingLayer(PolicyOrigin("organization", "operator_review_policy"), errors=("routing is malformed",)),)
    out: list[RoutingLayer] = []
    for raw in routing.get("layers") or ():
        if not isinstance(raw, Mapping):
            continue
        origin = PolicyOrigin.from_wire(raw.get("origin"))
        if origin.layer not in {"organization", "team", "repository"}:
            origin = PolicyOrigin("organization", origin.source or "operator_review_policy")
        out.append(parse_routing_settings(raw.get("settings") or {}, origin=origin))
    return tuple(out)


_ROUTING_ORDER: Final[Mapping[str, int]] = {"organization": 0, "team": 1, "repository": 2, "trusted_base_repository": 3}


def resolve_routing(layers: Iterable[RoutingLayer | None]) -> ReviewerRouting:
    """Rules accumulate across layers; ``automatic`` is the highest operator layer that set it.

    Automatic routing is off unless an operator layer turned it on, and a
    narrower operator layer (repository) can turn it back off. A rule id
    defined by two layers keeps the higher-precedence definition.
    """

    present = sorted(
        (layer for layer in layers if layer is not None), key=lambda layer: _ROUTING_ORDER.get(layer.origin.layer, 0)
    )
    automatic = False
    automatic_origin = BUILT_IN_ORIGIN
    by_id: dict[str, ReviewerRule] = {}
    errors: list[str] = []
    for layer in present:
        errors.extend(f"{layer.origin.label}: {error}" for error in layer.errors)
        if layer.automatic is not None and layer.origin.layer in {"organization", "team", "repository"}:
            automatic, automatic_origin = layer.automatic, layer.origin
        for rule in layer.rules:
            by_id[rule.id] = rule
    return ReviewerRouting(
        rules=tuple(by_id[key] for key in sorted(by_id)),
        automatic=automatic,
        automatic_origin=automatic_origin,
        errors=tuple(dict.fromkeys(errors)),
    )


def rule_evidence(
    routing: ReviewerRouting,
    paths: Sequence[str],
    *,
    criticality: Callable[[str], str],
    resolve_user: Callable[[str], ReviewerIdentity | None],
    resolve_team: Callable[[str], ReviewerIdentity | None],
) -> tuple[ReviewerEvidence, ...]:
    """Evidence from explicit rules whose paths (and criticality floor) match changed paths."""

    out: list[ReviewerEvidence] = []
    for rule in routing.rules:
        matched = [
            path
            for path in dict.fromkeys(paths)
            if rule.matches(path)
            and (
                rule.min_criticality is None
                or CRITICALITY_RANK.get(criticality(path), 1) >= CRITICALITY_RANK[rule.min_criticality]
            )
        ]
        if not matched:
            continue
        floor = f" (criticality ≥ {rule.min_criticality})" if rule.min_criticality else ""
        automatic = routing.routes_automatically(rule)
        for name, resolver in (
            *((user, resolve_user) for user in rule.users),
            *((t, resolve_team) for t in rule.teams),
        ):
            identity = resolver(name)
            if identity is None:
                continue
            out.append(
                ReviewerEvidence(
                    signal="rule",
                    identity=identity,
                    paths=tuple(matched),
                    source=rule.origin.label,
                    detail=f"reviewer rule {rule.id}{floor}",
                    rule_id=rule.id,
                    automatic=automatic,
                )
            )
    return tuple(out)


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #

_SIGNAL_LABEL: Final[Mapping[str, str]] = {
    "rule": "Reviewer rule",
    "codeowners": "CODEOWNERS",
    "ownership": "Reviewed these paths before",
    "history": "Recent git history",
}


def _paths_text(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:3])
    return shown + (f" +{len(paths) - 3} more" if len(paths) > 3 else "")


def _reason(signal: SignalKind, rows: Sequence[ReviewerEvidence], paths: Sequence[str]) -> SuggestionReason:
    sources = tuple(dict.fromkeys(row.source for row in rows if row.source))
    rule_ids = tuple(sorted({row.rule_id for row in rows if row.rule_id}))
    count = sum(max(1, row.count) for row in rows)
    details = tuple(dict.fromkeys(row.detail for row in rows if row.detail))
    if signal == "history":
        head = f"{_SIGNAL_LABEL[signal]}: {count} commit{'s' if count != 1 else ''}"
    elif signal == "ownership":
        head = f"{_SIGNAL_LABEL[signal]} in {count} Review{'s' if count != 1 else ''}"
    elif signal == "rule":
        head = f"{_SIGNAL_LABEL[signal]} {', '.join(rule_ids)}"
    else:
        head = f"{_SIGNAL_LABEL[signal]} ({'; '.join(details)})" if details else _SIGNAL_LABEL[signal]
    return SuggestionReason(
        signal=signal,
        paths=tuple(paths),
        sources=sources,
        detail=f"{head} · {_paths_text(paths)}",
        rule_ids=rule_ids,
        count=count,
    )


def rank_suggestions(
    evidence: Iterable[ReviewerEvidence],
    *,
    criticality: Callable[[str], str],
    exclude: Callable[[ReviewerIdentity], ExclusionReason | None],
    access: Callable[[ReviewerIdentity], AccessState],
    requested: Collection[str] = (),
    limit: int = DEFAULT_SUGGESTION_LIMIT,
) -> tuple[tuple[ReviewerSuggestion, ...], dict[str, int], int]:
    """Merge evidence per durable key, drop excluded candidates, score and order deterministically.

    Returns ``(suggestions, excluded counts by reason, truncated count)``.
    """

    grouped: dict[str, tuple[ReviewerIdentity, dict[str, list[ReviewerEvidence]]]] = {}
    for row in evidence:
        if not row.paths:
            continue
        identity, by_signal = grouped.setdefault(row.identity.key, (row.identity, {}))
        # Keep the richest view of one durable identity (bound principal, then provider handle).
        if (not identity.principal_id and row.identity.principal_id) or (
            not identity.handle and row.identity.handle and row.identity.principal_id == identity.principal_id
        ):
            grouped[row.identity.key] = (row.identity, by_signal)
        by_signal.setdefault(row.signal, []).append(row)

    excluded: dict[str, int] = {}
    ranked: list[tuple[tuple[Any, ...], ReviewerSuggestion]] = []
    for identity, by_signal in grouped.values():
        reason = exclude(identity)
        decision = access(identity)
        if reason is None and decision == "denied":
            reason = "no_access"
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        reasons: list[SuggestionReason] = []
        covered: list[str] = []
        score = 0
        automatic: set[str] = set()
        for signal in SIGNAL_ORDER:
            rows = by_signal.get(signal)
            if not rows:
                continue
            paths = list(dict.fromkeys(path for row in rows for path in row.paths))
            weight = sum(CRITICALITY_WEIGHT.get(criticality(path), 2) for path in paths)
            repeats = min(_REPEAT_CAP, sum(max(1, row.count) for row in rows) - 1)
            score += SIGNAL_WEIGHT[signal] * weight + _REPEAT_BONUS * max(0, repeats)
            reasons.append(_reason(cast(SignalKind, signal), rows, paths))
            covered.extend(path for path in paths if path not in covered)
            automatic.update(row.rule_id for row in rows if row.automatic and row.rule_id)
        critical = tuple(
            path for path in covered if CRITICALITY_RANK.get(criticality(path), 1) >= CRITICALITY_RANK["high"]
        )
        suggestion = ReviewerSuggestion(
            identity=identity,
            score=score,
            reasons=tuple(reasons),
            paths=tuple(covered),
            critical_paths=critical,
            access="unknown" if decision == "denied" else decision,
            requested=identity.key in requested,
            automatic_rules=tuple(sorted(automatic)),
        )
        order = (-score, -len(critical), _KIND_RANK[identity.kind], identity.label.lower(), identity.key)
        ranked.append((order, suggestion))
    ranked.sort(key=lambda item: item[0])
    kept = tuple(item[1] for item in ranked[: max(0, limit)])
    return kept, excluded, max(0, len(ranked) - len(kept))


__all__ = [
    "CODEOWNERS_PATHS",
    "CRITICALITY_WEIGHT",
    "DEFAULT_SUGGESTION_LIMIT",
    "SIGNAL_ORDER",
    "SIGNAL_WEIGHT",
    "AccessState",
    "CandidateKind",
    "CodeOwners",
    "CodeOwnersAuthority",
    "CodeOwnersRule",
    "ExclusionReason",
    "ReviewerEvidence",
    "ReviewerIdentity",
    "ReviewerRouting",
    "ReviewerRule",
    "ReviewerSuggestion",
    "RoutingLayer",
    "SignalKind",
    "SignalStatus",
    "SuggestionReason",
    "SuggestionSet",
    "codeowners_evidence",
    "codeowners_head_changes",
    "operator_routing_layers",
    "owner_kind",
    "parse_codeowners",
    "parse_reviewer_rules",
    "parse_routing_settings",
    "rank_suggestions",
    "resolve_codeowners",
    "resolve_routing",
    "rule_evidence",
    "trusted_base_routing",
]
