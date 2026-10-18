"""Explicit, scoped Review rules and AI content-access policy.

Authority is resolved before model context exists, path applicability is
fully deterministic, and suggestions/head content are typed but never silently
promoted to policy.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml

from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

RuleTrustClass = Literal[
    "organization_platform",
    "trusted_base_repository",
    "provider_authoritative_base",
    "head_change_candidate",
    "learned_suggestion",
]
ContentAccessMode = Literal["external_allowed", "customer_local_only", "ai_denied"]
ModelRouteTrust = Literal["external", "customer_local", "unknown"]

AUTHORITATIVE_RULE_TRUST: frozenset[str] = frozenset(
    {"organization_platform", "trusted_base_repository", "provider_authoritative_base"}
)
CONTENT_ACCESS_MODES: tuple[ContentAccessMode, ...] = (
    "external_allowed",
    "customer_local_only",
    "ai_denied",
)
_RESTRICTIVENESS: Mapping[ContentAccessMode, int] = {
    "external_allowed": 0,
    "customer_local_only": 1,
    "ai_denied": 2,
}
_MAX_RULES = 256
_MAX_PATTERNS = 64


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_path(value: object, *, field: str) -> str:
    path = str(value or "").strip().replace("\\", "/")
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"{field} must be a safe repository-relative path")
    return str(pure)


def _patterns(raw: object, *, field: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    if raw is None:
        return tuple(default)
    if isinstance(raw, str):
        rows: Sequence[object] = (raw,)
    elif isinstance(raw, Sequence):
        rows = raw
    else:
        raise ValueError(f"{field} must be a string or array")
    if len(rows) > _MAX_PATTERNS:
        raise ValueError(f"{field} exceeds {_MAX_PATTERNS} entries")
    out: list[str] = []
    for index, item in enumerate(rows):
        pattern = _safe_path(item, field=f"{field}[{index}]")
        if pattern not in out:
            out.append(pattern)
    return tuple(out)


def path_matches(path: str, patterns: Sequence[str]) -> bool:
    """Match one repository-relative path against a deterministic glob scope."""

    normalized = _safe_path(path, field="path")
    if not patterns:
        return True
    for raw in patterns:
        pattern = _safe_path(raw, field="pattern")
        if pattern.endswith("/"):
            pattern += "**"
        if fnmatch.fnmatchcase(normalized, pattern):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ReviewRule:
    stable_key: str
    instruction: str
    path_scope: tuple[str, ...]
    origin: str
    trust_class: RuleTrustClass
    source_identity: str
    source_location: str = ""

    @property
    def authoritative(self) -> bool:
        return self.trust_class in AUTHORITATIVE_RULE_TRUST

    @property
    def digest(self) -> str:
        return _sha(
            {
                "stable_key": self.stable_key,
                "instruction": self.instruction,
                "path_scope": list(self.path_scope),
                "origin": self.origin,
                "trust_class": self.trust_class,
                "source_identity": self.source_identity,
                "source_location": self.source_location,
            }
        )

    @property
    def ref(self) -> str:
        return f"rule:{self.trust_class}:{self.stable_key}@sha256:{self.digest}"

    def applies_to(self, path: str) -> bool:
        return self.authoritative and path_matches(path, self.path_scope)

    def to_wire(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "stable_key": self.stable_key,
            "instruction": self.instruction,
            "paths": list(self.path_scope),
            "origin": self.origin,
            "trust_class": self.trust_class,
            "source_identity": self.source_identity,
            "source_location": self.source_location,
            "digest": self.digest,
            "authoritative": self.authoritative,
        }


@dataclass(frozen=True, slots=True)
class ContentAccessRule:
    mode: ContentAccessMode
    path_scope: tuple[str, ...]
    origin: str
    trust_class: RuleTrustClass
    source_identity: str
    source_location: str = ""

    @property
    def authoritative(self) -> bool:
        return self.trust_class in AUTHORITATIVE_RULE_TRUST

    @property
    def digest(self) -> str:
        return _sha(
            {
                "mode": self.mode,
                "path_scope": list(self.path_scope),
                "origin": self.origin,
                "trust_class": self.trust_class,
                "source_identity": self.source_identity,
                "source_location": self.source_location,
            }
        )

    def applies_to(self, path: str) -> bool:
        return self.authoritative and path_matches(path, self.path_scope)

    def to_wire(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "paths": list(self.path_scope),
            "origin": self.origin,
            "trust_class": self.trust_class,
            "source_identity": self.source_identity,
            "source_location": self.source_location,
            "digest": self.digest,
            "authoritative": self.authoritative,
        }


@dataclass(frozen=True, slots=True)
class ReviewGovernance:
    """Current-Review governance resolved from authoritative layers only."""

    rules: tuple[ReviewRule, ...] = ()
    content_access: tuple[ContentAccessRule, ...] = ()
    rule_documents: tuple[str, ...] = ()
    base_state: str = "absent"
    errors: tuple[str, ...] = ()

    def rules_for_path(self, path: str) -> tuple[ReviewRule, ...]:
        return tuple(rule for rule in self.rules if rule.applies_to(path))

    def rules_for_paths(self, paths: Sequence[str]) -> tuple[ReviewRule, ...]:
        refs: dict[str, ReviewRule] = {}
        for path in paths:
            for rule in self.rules_for_path(path):
                refs.setdefault(rule.ref, rule)
        return tuple(refs[key] for key in sorted(refs))

    def rule_digest_for_paths(self, paths: Sequence[str]) -> str:
        return rule_subset_digest(self.rules_for_paths(paths))

    def access_digest_for_paths(self, paths: Sequence[str]) -> str:
        relevant = {row.digest: row for path in paths for row in self.content_access if row.applies_to(path)}
        return "access-" + _sha([relevant[key].to_wire() for key in sorted(relevant)])


@dataclass(frozen=True, slots=True)
class ContentAccessDecision:
    path: str
    mode: ContentAccessMode
    allowed: bool
    route_trust: ModelRouteTrust
    reason: str
    policy_refs: tuple[str, ...]
    digest: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "mode": self.mode,
            "allowed": self.allowed,
            "route_trust": self.route_trust,
            "reason": self.reason,
            "policy_refs": list(self.policy_refs),
            "digest": self.digest,
        }


def rule_subset_digest(rules: Sequence[ReviewRule]) -> str:
    unique = {rule.ref: rule for rule in rules if rule.authoritative}
    return "rules-" + _sha([unique[key].to_wire() for key in sorted(unique)])


def model_route_trust(driver: str, *, explicit: ModelRouteTrust | None = None) -> ModelRouteTrust:
    """The trust a driver name alone proves: ``external`` for hosted providers, else ``unknown``.

    A driver name never proves where its endpoint runs (an ``ollama`` route may
    point at a remote host), so ``customer_local`` comes only from an explicit
    attestation: the operator's data-governance document or a customer worker's
    attested route.
    """

    if explicit is not None:
        return explicit
    normalized = driver.strip().lower()
    if normalized in {"openai", "anthropic", "openai_compatible", "claude", "codex", "opencode"}:
        return "external"
    return "unknown"


def content_access_for_path(
    path: str,
    policy: Sequence[ContentAccessRule],
    *,
    route_trust: ModelRouteTrust,
) -> ContentAccessDecision:
    # RC6 fail-safe: a restricting row also matches case-insensitively (``Secrets/x``
    # under ``secrets/**`` on a case-insensitive checkout); allow rows stay exact.
    folded = path.casefold()
    matching = tuple(
        row
        for row in policy
        if row.applies_to(path)
        or (
            row.mode != "external_allowed"
            and row.authoritative
            and path_matches(folded, tuple(pattern.casefold() for pattern in row.path_scope))
        )
    )
    if matching:
        mode = max((row.mode for row in matching), key=lambda value: _RESTRICTIVENESS[value])
    else:
        mode = "external_allowed"
    if mode == "ai_denied":
        allowed = False
        reason = "AI content access policy denies this path"
    elif mode == "customer_local_only" and route_trust != "customer_local":
        allowed = False
        reason = (
            "AI content access policy requires an explicitly customer-local model route"
            if route_trust != "unknown"
            else "AI content access policy requires a known customer-local model route; route trust is unknown"
        )
    else:
        allowed = True
        reason = ""
    refs = tuple(sorted(f"access:{row.digest}" for row in matching))
    digest = "access-" + _sha(
        {
            "path": _safe_path(path, field="path"),
            "mode": mode,
            "route_trust": route_trust,
            "policy": [row.to_wire() for row in sorted(matching, key=lambda item: item.digest)],
        }
    )
    return ContentAccessDecision(
        path=_safe_path(path, field="path"),
        mode=mode,
        allowed=allowed,
        route_trust=route_trust,
        reason=reason,
        policy_refs=refs,
        digest=digest,
    )


def _yaml_mapping(content: str, *, source: str) -> Mapping[str, Any]:
    if not content.strip():
        return {}
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"{source}: invalid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{source}: expected a YAML mapping")
    return raw


def _rule_rows(
    raw: object,
    *,
    trust_class: RuleTrustClass,
    origin: str,
    source_identity: str,
    source_location: str,
    default_paths: Sequence[str] = (),
) -> tuple[ReviewRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{source_location}: rules must be an array")
    if len(raw) > _MAX_RULES:
        raise ValueError(f"{source_location}: rules exceeds {_MAX_RULES} entries")
    out: list[ReviewRule] = []
    keys: set[str] = set()
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise ValueError(f"{source_location}: rules[{index}] must be an object")
        key = str(row.get("stable_key") or row.get("key") or row.get("id") or "").strip()
        instruction = str(row.get("instruction") or row.get("statement") or "").strip()
        if not key or len(key) > 160:
            raise ValueError(f"{source_location}: rules[{index}].stable_key is required and must be <=160 characters")
        if not instruction or len(instruction) > 4000:
            raise ValueError(f"{source_location}: rules[{index}].instruction is required and must be <=4000 characters")
        if key in keys:
            raise ValueError(f"{source_location}: duplicate rule stable_key {key!r}")
        keys.add(key)
        paths = _patterns(row.get("paths"), field=f"{source_location}.rules[{index}].paths", default=default_paths)
        out.append(
            ReviewRule(
                stable_key=key,
                instruction=instruction,
                path_scope=paths,
                origin=origin,
                trust_class=trust_class,
                source_identity=source_identity,
                source_location=source_location,
            )
        )
    return tuple(out)


def _access_rows(
    raw: object,
    *,
    trust_class: RuleTrustClass,
    origin: str,
    source_identity: str,
    source_location: str,
) -> tuple[ContentAccessRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{source_location}: ai_content_access must be an array")
    out: list[ContentAccessRule] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise ValueError(f"{source_location}: ai_content_access[{index}] must be an object")
        mode = str(row.get("mode") or "").strip()
        if mode not in CONTENT_ACCESS_MODES:
            raise ValueError(f"{source_location}: ai_content_access[{index}].mode is invalid")
        paths = _patterns(row.get("paths"), field=f"{source_location}.ai_content_access[{index}].paths")
        if not paths:
            raise ValueError(f"{source_location}: ai_content_access[{index}].paths must not be empty")
        out.append(
            ContentAccessRule(
                mode=mode,
                path_scope=paths,
                origin=origin,
                trust_class=trust_class,
                source_identity=source_identity,
                source_location=source_location,
            )
        )
    return tuple(out)


def _source_identity(document: TrustedBaseDocument) -> str:
    # Keep document identity stable when an unrelated row in the same policy
    # document changes. Each ReviewRule/ContentAccessRule digest already binds
    # its exact semantic row; the Review Snapshot identity binds the base bytes.
    source = document.source.strip() or "trusted-base"
    return f"{document.state}:{source}:{document.path}"


def _configured_rule_documents(raw: object, *, source: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{source}: rule_documents must be an array")
    out: list[tuple[str, tuple[str, ...]]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            path = _safe_path(item, field=f"{source}.rule_documents[{index}]")
            paths: tuple[str, ...] = ()
        elif isinstance(item, Mapping):
            path = _safe_path(item.get("path"), field=f"{source}.rule_documents[{index}].path")
            paths = _patterns(item.get("paths"), field=f"{source}.rule_documents[{index}].paths")
        else:
            raise ValueError(f"{source}: rule_documents[{index}] must be a path or object")
        if all(existing[0] != path for existing in out):
            out.append((path, paths))
    return tuple(out)


#: Key of the organization execution block in an effective Review policy document.
ORGANIZATION_BLOCK_KEY = "organization_block"
ORGANIZATION_BLOCK_KINDS: tuple[str, ...] = ("ai", "execution")
#: Origin of the content-access row the organization AI block becomes.
ORGANIZATION_AI_BLOCK_ORIGIN = "organization AI block"


def organization_block(effective: object) -> dict[str, str]:
    """What the organization blocks (``ai``: model calls, ``execution``: running reviewed code), with why.

    The block lives in the effective Review policy document under
    ``organization_block``. It is above every repository, trusted-base and
    personal layer: nothing below may lift it. A malformed block blocks both
    kinds (fail closed); its contents are never echoed.
    """

    if not isinstance(effective, Mapping):
        return {}
    raw = effective.get(ORGANIZATION_BLOCK_KEY)
    if raw is None:
        return {}
    if (
        not isinstance(raw, Mapping)
        or any(key not in (*ORGANIZATION_BLOCK_KINDS, "origin") for key in raw)
        or any(not isinstance(raw.get(kind, False), bool) for kind in ORGANIZATION_BLOCK_KINDS)
    ):
        return {
            kind: "the organization execution block is malformed, so it blocks this"
            for kind in ORGANIZATION_BLOCK_KINDS
        }
    return {
        kind: f"blocked by organization policy (execution_block.{kind})"
        for kind in ORGANIZATION_BLOCK_KINDS
        if raw.get(kind) is True
    }


def parse_organization_governance(raw: Mapping[str, Any] | None) -> ReviewGovernance:
    if not isinstance(raw, Mapping):
        return ReviewGovernance()
    source_identity = "organization-platform-policy:" + _sha(raw)
    blocked = organization_block(raw).get("ai")
    # The organization AI block denies every path to every model route, so each
    # consumer of content access (Ask, briefs, AI review, rule checks) inherits it.
    block_rows = (
        (
            ContentAccessRule(
                mode="ai_denied",
                path_scope=("**",),
                origin=ORGANIZATION_AI_BLOCK_ORIGIN,
                trust_class="organization_platform",
                source_identity="organization-ai-block",
                source_location="organization policy execution_block.ai",
            ),
        )
        if blocked
        else ()
    )
    try:
        rules = _rule_rows(
            raw.get("rules"),
            trust_class="organization_platform",
            origin="organization/platform Review policy",
            source_identity=source_identity,
            source_location="organization policy",
        )
        access = _access_rows(
            raw.get("ai_content_access"),
            trust_class="organization_platform",
            origin="organization/platform Review policy",
            source_identity=source_identity,
            source_location="organization policy",
        )
        return ReviewGovernance(rules=rules, content_access=(*block_rows, *access))
    except ValueError as exc:
        deny = ContentAccessRule(
            mode="ai_denied",
            path_scope=("**",),
            origin="invalid organization/platform Review policy",
            trust_class="organization_platform",
            source_identity=source_identity,
            source_location="organization policy",
        )
        return ReviewGovernance(content_access=(*block_rows, deny), errors=(str(exc),), base_state="provisional")


#: Origin of the fail-closed row a malformed trusted-base content-access policy becomes.
INVALID_TRUSTED_BASE_ACCESS_ORIGIN = "invalid trusted-base repository Review policy"


def _invalid_base_access(config: TrustedBaseDocument, identity: str) -> ContentAccessRule:
    # A trusted-base policy that cannot be read may restrict content we cannot
    # see, so model access fails closed for every path instead of open.
    return ContentAccessRule(
        mode="ai_denied",
        path_scope=("**",),
        origin=INVALID_TRUSTED_BASE_ACCESS_ORIGIN,
        trust_class="trusted_base_repository",
        source_identity=identity,
        source_location=config.path,
    )


def _resolved_governance(
    rules: Sequence[ReviewRule],
    access: Sequence[ContentAccessRule],
    *,
    docs: Sequence[str],
    base_state: str,
    errors: Sequence[str],
) -> ReviewGovernance:
    unique_rules = {row.ref: row for row in rules if row.authoritative}
    unique_access = {row.digest: row for row in access if row.authoritative}
    return ReviewGovernance(
        rules=tuple(unique_rules[key] for key in sorted(unique_rules)),
        content_access=tuple(unique_access[key] for key in sorted(unique_access)),
        rule_documents=tuple(docs),
        base_state=base_state,
        errors=tuple(errors),
    )


def _unreadable_base_access(config: TrustedBaseDocument) -> ContentAccessRule:
    # The repository's restrictions on model access are unknown, so they are
    # assumed to cover every path: model access fails closed instead of open.
    return ContentAccessRule(
        mode="ai_denied",
        path_scope=("**",),
        origin=UNREADABLE_TRUSTED_BASE_ACCESS_ORIGIN,
        trust_class="trusted_base_repository",
        source_identity="unreadable-trusted-base:" + config.path,
        source_location=config.path,
    )


#: Origin of the fail-closed row an unreadable (provisional) trusted base becomes.
UNREADABLE_TRUSTED_BASE_ACCESS_ORIGIN = "unreadable trusted-base repository Review policy"


def resolve_review_governance(
    config: TrustedBaseDocument,
    *,
    read_base_document: Callable[[str], TrustedBaseDocument],
    organization: ReviewGovernance | None = None,
) -> ReviewGovernance:
    """Resolve current rules without ever consulting change/head content.

    Organization rows always apply, whatever the trusted base's state. A
    trusted base that cannot be read (``provisional``), or whose YAML or
    ``ai_content_access`` cannot be parsed, contributes an ``ai_denied`` row
    for every path (fail closed); a malformed ``rules``/``rule_documents``
    block never drops the access rows that parsed.
    """

    org = organization or ReviewGovernance()
    rules = list(org.rules)
    access = list(org.content_access)
    errors = list(org.errors)
    docs: list[str] = []
    if config.state != "present":
        if config.state == "provisional":
            access.append(_unreadable_base_access(config))
        return ReviewGovernance(
            rules=tuple(rules),
            content_access=tuple(access),
            base_state=config.state,
            errors=tuple(errors),
        )

    identity = _source_identity(config)
    try:
        raw = _yaml_mapping(config.content, source=config.path)
    except ValueError as exc:
        access.append(_invalid_base_access(config, identity))
        return _resolved_governance(rules, access, docs=(), base_state=config.state, errors=(*errors, str(exc)))
    try:
        access.extend(
            _access_rows(
                raw.get("ai_content_access"),
                trust_class="trusted_base_repository",
                origin="trusted-base repository Review policy",
                source_identity=identity,
                source_location=config.path,
            )
        )
    except ValueError as exc:
        access.append(_invalid_base_access(config, identity))
        errors.append(str(exc))
    try:
        rules.extend(
            _rule_rows(
                raw.get("rules"),
                trust_class="trusted_base_repository",
                origin="trusted-base repository Review policy",
                source_identity=identity,
                source_location=config.path,
            )
        )
        configured_docs = _configured_rule_documents(raw.get("rule_documents"), source=config.path)
    except ValueError as exc:
        return _resolved_governance(rules, access, docs=(), base_state=config.state, errors=(*errors, str(exc)))

    for path, default_paths in configured_docs:
        docs.append(path)
        document = read_base_document(path)
        if document.state == "provisional":
            errors.append(f"{path}: trusted-base content unavailable")
            continue
        if document.state == "absent":
            errors.append(f"{path}: configured trusted-base rule document is absent")
            continue
        doc_identity = _source_identity(document)
        try:
            raw_doc = _yaml_mapping(document.content, source=path)
            rules.extend(
                _rule_rows(
                    raw_doc.get("rules"),
                    trust_class="trusted_base_repository",
                    origin="trusted-base repository rule document",
                    source_identity=doc_identity,
                    source_location=path,
                    default_paths=default_paths,
                )
            )
        except ValueError as exc:
            errors.append(str(exc))

    return _resolved_governance(rules, access, docs=docs, base_state=config.state, errors=errors)


FuturePolicyCategory = Literal["review_policy", "review_rules", "owner_routing", "instruction_context"]


@dataclass(frozen=True, slots=True)
class FuturePolicyChange:
    path: str
    change: Literal["added", "changed", "removed"]
    category: FuturePolicyCategory
    summary: str
    effects: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change": self.change,
            "category": self.category,
            "summary": self.summary,
            "effects": list(self.effects),
            "current_authority": False,
            "future_policy": True,
        }


def _semantic_rules(
    content: str,
    *,
    source_location: str,
    default_paths: Sequence[str] = (),
) -> dict[str, tuple[str, tuple[str, ...]]]:
    if not content.strip():
        return {}
    raw = _yaml_mapping(content, source=source_location)
    rows = _rule_rows(
        raw.get("rules"),
        trust_class="head_change_candidate",
        origin="candidate comparison",
        source_identity="candidate-comparison",
        source_location=source_location,
        default_paths=default_paths,
    )
    return {row.stable_key: (row.instruction, row.path_scope) for row in rows}


def _semantic_access(content: str, *, source_location: str) -> dict[tuple[str, ...], ContentAccessMode]:
    if not content.strip():
        return {}
    raw = _yaml_mapping(content, source=source_location)
    rows = _access_rows(
        raw.get("ai_content_access"),
        trust_class="head_change_candidate",
        origin="candidate comparison",
        source_identity="candidate-comparison",
        source_location=source_location,
    )
    return {row.path_scope: row.mode for row in rows}


def _rule_effects(
    before: str,
    after: str,
    *,
    source_location: str,
    default_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    try:
        left = _semantic_rules(before, source_location=source_location, default_paths=default_paths)
        right = _semantic_rules(after, source_location=source_location, default_paths=default_paths)
    except ValueError:
        return ("rule document changes; candidate details are not structurally parseable",)
    effects: list[str] = []
    for key in sorted(set(left) | set(right)):
        old = left.get(key)
        new = right.get(key)
        if old is None and new is not None:
            scope = ", ".join(new[1]) or "all paths"
            effects.append(f"adds rule {key} for {scope}")
        elif new is None and old is not None:
            effects.append(f"removes rule {key}")
        elif old != new and new is not None:
            scope = ", ".join(new[1]) or "all paths"
            effects.append(f"changes rule {key} for {scope}")
    return tuple(effects)


def _access_effects(before: str, after: str, *, source_location: str) -> tuple[str, ...]:
    try:
        left = _semantic_access(before, source_location=source_location)
        right = _semantic_access(after, source_location=source_location)
    except ValueError:
        return ()
    effects: list[str] = []
    for scope in sorted(set(left) | set(right)):
        old = left.get(scope)
        new = right.get(scope)
        label = ", ".join(scope) or "all paths"
        if old is None and new is not None:
            effects.append(f"adds AI content access {new} for {label}")
        elif new is None and old is not None:
            effects.append(f"removes AI content access {old} for {label}")
        elif old != new and new is not None:
            effects.append(f"changes AI content access for {label}: {old} -> {new}")
    return tuple(effects)


def project_future_policy(
    base_config: TrustedBaseDocument,
    *,
    head_config_content: str | None,
    read_base_document: Callable[[str], TrustedBaseDocument],
    read_head_document: Callable[[str], str | None],
) -> tuple[FuturePolicyChange, ...]:
    """Describe policy files changed by the head without granting them authority."""

    configured: dict[str, tuple[str, ...]] = {}
    for content, source in (
        (base_config.content if base_config.state == "present" else "", base_config.path),
        (head_config_content or "", base_config.path),
    ):
        if not content.strip():
            continue
        try:
            raw = _yaml_mapping(content, source=source)
            for path, paths in _configured_rule_documents(raw.get("rule_documents"), source=source):
                configured.setdefault(path, paths)
        except ValueError:
            continue

    tracked: dict[str, tuple[FuturePolicyCategory, tuple[str, ...]]] = {
        base_config.path: ("review_policy", ()),
        ".github/CODEOWNERS": ("owner_routing", ()),
        "CODEOWNERS": ("owner_routing", ()),
        "docs/CODEOWNERS": ("owner_routing", ()),
        "AGENTS.md": ("instruction_context", ()),
        "CLAUDE.md": ("instruction_context", ()),
        "REVIEW.md": ("instruction_context", ()),
    }
    for path, paths in configured.items():
        tracked[path] = ("review_rules", paths)

    out: list[FuturePolicyChange] = []
    for path, (category, default_paths) in tracked.items():
        base = base_config if path == base_config.path else read_base_document(path)
        head = head_config_content if path == base_config.path else read_head_document(path)
        if head is None:
            continue
        before = base.content if base.state == "present" else ""
        if before == head:
            continue
        change: Literal["added", "changed", "removed"]
        if not before and head:
            change = "added"
        elif before and not head:
            change = "removed"
        else:
            change = "changed"

        effects: tuple[str, ...] = ()
        if category in {"review_policy", "review_rules"}:
            effects = _rule_effects(
                before,
                head,
                source_location=path,
                default_paths=default_paths,
            )
            if category == "review_policy":
                from lemoncrow.pro.capabilities.review.premerge_checks import future_premerge_effects

                effects = (
                    *effects,
                    *_access_effects(before, head, source_location=path),
                    *future_premerge_effects(before, head),
                )
        elif category == "owner_routing":
            effects = ("changes owner routing for later Reviews",)
        else:
            effects = ("changes repository instruction/context for later Reviews",)

        out.append(
            FuturePolicyChange(
                path=path,
                change=change,
                category=category,
                summary=(
                    f"{path} is {change} in this Change. It does not govern the current Review; "
                    "if landed, it may affect later Reviews."
                ),
                effects=effects[:32],
            )
        )
    return tuple(out)


def candidate_rule(
    *,
    stable_key: str,
    instruction: str,
    paths: Sequence[str],
    source_identity: str,
    source_location: str = "",
    learned: bool = False,
) -> ReviewRule:
    """Build an inert typed rule for future-policy or suggestion projection."""

    return ReviewRule(
        stable_key=stable_key,
        instruction=instruction,
        path_scope=_patterns(paths, field="candidate.paths"),
        origin="learned suggestion" if learned else "head/change candidate",
        trust_class="learned_suggestion" if learned else "head_change_candidate",
        source_identity=source_identity,
        source_location=source_location,
    )


__all__ = [
    "AUTHORITATIVE_RULE_TRUST",
    "CONTENT_ACCESS_MODES",
    "INVALID_TRUSTED_BASE_ACCESS_ORIGIN",
    "ORGANIZATION_AI_BLOCK_ORIGIN",
    "ORGANIZATION_BLOCK_KEY",
    "ORGANIZATION_BLOCK_KINDS",
    "ContentAccessDecision",
    "ContentAccessMode",
    "ContentAccessRule",
    "FuturePolicyChange",
    "ModelRouteTrust",
    "ReviewGovernance",
    "ReviewRule",
    "RuleTrustClass",
    "candidate_rule",
    "content_access_for_path",
    "model_route_trust",
    "organization_block",
    "parse_organization_governance",
    "path_matches",
    "project_future_policy",
    "resolve_review_governance",
    "rule_subset_digest",
]
