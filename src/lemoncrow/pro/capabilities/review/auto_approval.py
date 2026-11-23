"""Auto-approval eligibility: a deterministic, auditable evaluator (Phase 10 items 8-9, P10-LAND).

LemonCrow may approve an eligible low-risk Change after deterministic policy
gates pass; the SCM's native auto-merge or merge queue then merges once every
requirement it owns is satisfied. Nothing here calls a model, merges, or
bypasses branch protection.

:func:`evaluate_auto_approval` evaluates **every** gate (never short-circuits),
so the decision explains each passed and failed gate. A gate that fails only
because a fact could not be read is ``transient``: it blocks approving but, on
revalidation, defers instead of withdrawing a standing approval.

Gate F preconditions (``docs/planning/2026-10-04-gate-f-qualification.md``):

* **L1** ``provider_sync``: the provider subject is ``synced`` and its head is
  the Snapshot's exact head.
* **L2** the decision names the policy generation digest and the readiness
  conditions hash it was taken under (:class:`AutoApprovalDecision`); a
  standing approval is revalidated only through the Review API's
  ``_revalidate_armed_landing`` (which calls :func:`revalidate_auto_approval`)
  and the hosted sweep.
* **L3** ``attention``: request progress is evaluated directly on the latest
  Snapshot (no completed request may have pending work), not inferred from
  whether the Snapshot's attention reconcile ran.
* **L4** ``trusted_base``: at decision time the default branch's review
  policy is the one this Snapshot was captured against.

Hard-deny defaults (item 9) cannot be configured away: sensitive path classes,
unresolved P0/P1 findings, failed/unknown/stale required evidence, waivers,
human blockers, verification weakening and agent-instruction changes, Gate H
unqualified LemonCrow evidence, and incomplete change sets.

Durable state: the standing approval is one ``landing`` relationship per
Change (relation ``auto_approval``); the latest eligibility decision of a
Snapshot is one ``review_derived`` row (kind :data:`DECISION_KIND`). Writers
are :func:`record_auto_approval` / :func:`store_decision`; closers are
:func:`revalidate_auto_approval` (withdrawal) and the Review/Snapshot deletion
cascade (retention).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from lemoncrow_client.review_words import part_count

from lemoncrow.pro.capabilities.review.landing_policy import CRITICALITY_RANK, LandingPolicy
from lemoncrow.pro.capabilities.review.readiness import SATISFIED_STATES, TRANSIENT_REASONS
from lemoncrow.pro.capabilities.review.rules import path_matches

AUTO_APPROVAL_ACTOR: Final[str] = "lemoncrow-policy"
"""The principal an auto-approval is evaluated and recorded as (never a person)."""
AUTO_APPROVAL_RELATION: Final[str] = "auto_approval"
DECISION_KIND: Final[str] = "auto_approval_decision"
DECISION_SCOPE: Final[str] = "latest"
DECISION_VERSION: Final[int] = 1
#: Approval states in force: ``approving`` (provider delivery owed) and ``approved``.
IN_FORCE_STATES: Final[frozenset[str]] = frozenset({"approving", "approved"})
#: The review-progress condition auto-approval stands in for (as the policy principal).
STANDS_IN_FOR: Final[frozenset[str]] = frozenset({"latest-snapshot-reviewed"})
MERGE_RESULT_CONDITION: Final[str] = "merge-result"
CHANGE_SET_PREFIX: Final[str] = "change-set:"
MAX_PATHS_NAMED: Final[int] = 8
"""Paths named per failed gate detail; the count is always exact."""

# --------------------------------------------------------------------------- #
# hard-deny path classes (item 9): typed rules over a normalized path
# --------------------------------------------------------------------------- #

#: Word tokens (alphanumeric runs of a normalized path, plus their camelCase parts) per hard-deny class.
_CLASS_TOKENS: Final[Mapping[str, frozenset[str]]] = {
    "auth": frozenset(
        {
            "auth",
            "authn",
            "authz",
            "authentication",
            "authorization",
            "authorize",
            "oauth",
            "oauth2",
            "oidc",
            "saml",
            "sso",
            "login",
            "logout",
            "signin",
            "signup",
            "password",
            "passwords",
            "permission",
            "permissions",
            "rbac",
            "acl",
            "acls",
            "iam",
            "mfa",
            "totp",
            "jwt",
        }
    ),
    "secrets_crypto": frozenset(
        {
            "secret",
            "secrets",
            "credential",
            "credentials",
            "crypto",
            "cryptography",
            "encrypt",
            "encryption",
            "decrypt",
            "cipher",
            "kms",
            "hsm",
            "keystore",
            "keyring",
            "certificate",
            "certificates",
            "certs",
            "tls",
            "ssl",
            "signing",
            "gpg",
            "pgp",
            "vault",
            "pem",
            "p12",
            "pfx",
            "env",
            "npmrc",
            "pypirc",
        }
    ),
    "billing": frozenset(
        {
            "billing",
            "payment",
            "payments",
            "invoice",
            "invoices",
            "stripe",
            "checkout",
            "subscription",
            "subscriptions",
            "pricing",
            "refund",
            "refunds",
            "paypal",
            "braintree",
        }
    ),
    "migrations_persistence": frozenset(
        {"migration", "migrations", "migrate", "alembic", "flyway", "liquibase", "sql", "ddl", "seed", "seeds"}
    ),
    "infra_ci_release": frozenset(
        {
            "workflows",
            "jenkinsfile",
            "circleci",
            "buildkite",
            "dockerfile",
            "docker",
            "terraform",
            "tf",
            "tfvars",
            "helm",
            "charts",
            "k8s",
            "kubernetes",
            "kustomize",
            "kustomization",
            "deploy",
            "deployment",
            "deployments",
            "infra",
            "infrastructure",
            "ansible",
            "pulumi",
            "cloudformation",
            "release",
            "releases",
            "goreleaser",
            "ci",
            "pipeline",
            "pipelines",
        }
    ),
    "public_contracts": frozenset(
        {
            "openapi",
            "swagger",
            "asyncapi",
            "proto",
            "protos",
            "protobuf",
            "graphql",
            "gql",
            "avsc",
            "thrift",
            "schema",
            "schemas",
            "contract",
            "contracts",
        }
    ),
    "review_policy": frozenset({"codeowners", "security"}),
}

#: Directory names (any depth) whose every path is in a class (CI/provider config, Review policy).
_SEGMENT_CLASSES: Final[Mapping[str, str]] = {
    ".github": "infra_ci_release",
    ".gitlab": "infra_ci_release",
    ".circleci": "infra_ci_release",
    ".buildkite": "infra_ci_release",
    ".lemoncrow": "review_policy",
}
#: Exact file names (any directory) in a class.
_NAME_CLASSES: Final[Mapping[str, str]] = {
    ".gitlab-ci.yml": "infra_ci_release",
    ".gitlab-ci.yaml": "infra_ci_release",
    "azure-pipelines.yml": "infra_ci_release",
    "bitbucket-pipelines.yml": "infra_ci_release",
    ".travis.yml": "infra_ci_release",
    ".goreleaser.yml": "infra_ci_release",
    ".goreleaser.yaml": "infra_ci_release",
    ".envrc": "secrets_crypto",
}
_NAME_PREFIX_CLASSES: Final[Mapping[str, str]] = {"id_rsa": "secrets_crypto"}
_NAME_SUFFIX_CLASSES: Final[Mapping[str, str]] = {
    ".key": "secrets_crypto",
    ".keystore": "secrets_crypto",
    ".jks": "secrets_crypto",
}

HARD_DENY_LABELS: Final[Mapping[str, str]] = {
    "auth": "Authentication or authorization",
    "secrets_crypto": "Secrets, credentials or cryptography",
    "billing": "Billing or payments",
    "migrations_persistence": "Database migrations or destructive persistence",
    "infra_ci_release": "Infrastructure, CI or release machinery",
    "public_contracts": "Public API, schema or contract",
    "review_policy": "Review or security policy",
    "configured_sensitive": "Sensitive paths named by policy",
    "unsafe_path": "A path that does not normalize to one repository-relative file",
}
HARD_DENY_CLASSES: Final[tuple[str, ...]] = tuple(HARD_DENY_LABELS)

_WORD = re.compile(r"[^\W_]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
#: Latin look-alikes folded before matching (over-matching only withholds auto-approval).
# lc-debt: a small Cyrillic/Greek confusable fold, not the full Unicode confusables table;
# upgrade path: fold with the UTS #39 skeleton when a maintained table ships with the runtime.
_CONFUSABLES: Final[dict[int, str]] = str.maketrans(
    {
        "\u0430": "a", "\u0432": "b", "\u0441": "c", "\u0501": "d", "\u0435": "e", "\u04bb": "h", "\u0456": "i", "\u0458": "j", "\u043a": "k",
        "\u04cf": "l", "\u043c": "m", "\u043d": "h", "\u043e": "o", "\u0440": "p", "\u051b": "q", "\u0455": "s", "\u0442": "t", "\u0443": "y",
        "\u0445": "x", "\u051d": "w", "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v", "\u03bf": "o", "\u03c1": "p",
        "\u03c4": "t", "\u03c5": "u", "\u03c7": "x",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class PolicyPath:
    """One changed path as the hard-deny policy sees it (pure; built by :func:`policy_path`).

    ``segments`` are the lexically resolved, case-folded, compatibility-normalized
    directory/file names (``\\`` is a separator, ``.`` segments vanish, ``..``
    pops). ``folded`` are the same names *before* resolution and ``popped`` the
    ones a ``..`` removed: directory and name rules match them too, so a
    ``..`` look-alike never hides a class. ``tokens`` are every word of every
    segment, of the folded path *and* of the raw lowercased path, plus camelCase
    parts -- so normalization can only add matches, never remove one. ``unsafe``
    marks a path that is empty, absolute, has any segment that folds to ``..``
    (Git stores no such component: it is a traversal or a look-alike of one) or
    carries a NUL: it is never proof of insensitivity.
    """

    raw: str
    segments: tuple[str, ...]
    tokens: frozenset[str]
    unsafe: bool
    folded: tuple[str, ...] = ()
    popped: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.segments[-1] if self.segments else ""

    @property
    def normalized(self) -> str:
        return "/".join(self.segments)


def _fold(text: str) -> str:
    """NFKC + case fold, with format/control characters and combining marks dropped."""

    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(
        char for char in decomposed if unicodedata.category(char) not in {"Mn", "Me", "Cf", "Cc", "Co", "Cs"}
    )
    return unicodedata.normalize("NFKC", kept).casefold().translate(_CONFUSABLES)


def _words(text: str) -> set[str]:
    out: set[str] = set()
    for word in _WORD.findall(text):
        out.add(word.casefold())
        out.update(part.casefold() for part in _CAMEL.findall(word))
    return out


def policy_path(raw: str) -> PolicyPath:
    """Normalize one changed path for the typed hard-deny rules (see :class:`PolicyPath`)."""

    text = str(raw)
    unsafe = not text.strip() or "\x00" in text
    stripped = "".join(
        char for char in unicodedata.normalize("NFKD", text) if unicodedata.category(char) not in {"Cf", "Cc"}
    ).replace("\\", "/")
    if stripped.startswith("/") or re.match(r"^[A-Za-z]:/", stripped):
        unsafe = True
    segments: list[str] = []
    folded: list[str] = []
    popped: list[str] = []
    for part in stripped.split("/"):
        name = _fold(part).strip()
        if name in {"", "."}:
            continue
        if name == "..":
            unsafe = True  # any traversal, or a look-alike Git would store as a real name
            if segments:
                popped.append(segments.pop())
            continue
        segments.append(name)
        folded.append(name)
    if not segments:
        unsafe = True
    # Words of the camelCase-preserving text, of the folded segments and of the old lowercased form.
    tokens = _words(stripped) | _words(" ".join(folded)) | _words(text.lower())
    return PolicyPath(
        raw=text,
        segments=tuple(segments),
        tokens=frozenset(tokens),
        unsafe=unsafe,
        folded=tuple(folded),
        popped=tuple(popped),
    )


def _path_classes(path: PolicyPath) -> set[str]:
    classes = {klass for klass, words in _CLASS_TOKENS.items() if path.tokens & words}
    # Directory classes over every folded name, resolved or not: a popped `.lemoncrow` still counts.
    classes.update(
        _SEGMENT_CLASSES[segment] for segment in (*path.segments, *path.folded) if segment in _SEGMENT_CLASSES
    )
    for name in {path.name, *path.popped}:
        if name in _NAME_CLASSES:
            classes.add(_NAME_CLASSES[name])
        classes.update(klass for prefix, klass in _NAME_PREFIX_CLASSES.items() if name.startswith(prefix))
        classes.update(klass for suffix, klass in _NAME_SUFFIX_CLASSES.items() if name.endswith(suffix))
    if path.unsafe:
        classes.add("unsafe_path")
    return classes


def _configured(path: PolicyPath, patterns: Sequence[str]) -> bool:
    """A policy-named sensitive path: the raw path, the normalized one or either case-folded."""

    if not patterns:
        return False
    folded = tuple(_fold(pattern) for pattern in patterns)
    candidates = {path.raw, path.normalized}
    return any(_matches(candidate, patterns) for candidate in candidates if candidate) or any(
        _matches(_fold(candidate), folded) for candidate in candidates if candidate
    )


def hard_deny_classes(paths: Iterable[str], *, sensitive_paths: Sequence[str] = ()) -> dict[str, list[str]]:
    """Every hard-deny class a changed path falls in, with the paths (deterministic order).

    Typed rules over :func:`policy_path`: a class word token (``src/auth/login.py``
    and ``src/AuthService.ts`` are ``auth``; ``docs/authoring.md`` is not), a
    class directory at any depth (``.github``), an exact file name, a file-name
    prefix/suffix, or a policy-named glob. Unicode compatibility forms, case,
    ``\\`` separators, ``.``/``..`` segments and invisible characters are
    normalized first, and the raw form is matched too, so normalization only
    ever adds a class. An unsafe path is its own class. Over-matching only
    withholds auto-approval.

    lc-debt: names and words, not meaning; upgrade path: classify by CODEOWNERS
    ownership and the semantic facts (symbols, side effects) per path.
    """

    found: dict[str, list[str]] = {}
    for raw in dict.fromkeys(item for item in paths if item):
        path = policy_path(raw)
        classes = _path_classes(path)
        if sensitive_paths and _configured(path, sensitive_paths):
            classes.add("configured_sensitive")
        for klass in classes:
            found.setdefault(klass, []).append(raw)
    return {klass: found[klass] for klass in HARD_DENY_CLASSES if klass in found}


def _matches(path: str, patterns: Sequence[str], *, on_error: bool = True) -> bool:
    """*on_error* is the fail-safe answer for an unsafe path: excluded (``True``), never included."""

    try:
        return bool(patterns) and path_matches(path, patterns)
    except ValueError:
        return on_error


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderSyncFact:
    """L1: the provider subject's sync state and head against the Snapshot head."""

    state: str
    provider_head: str


@dataclass(frozen=True, slots=True)
class RequestProgressFact:
    """L3: review requests evaluated directly against the latest Snapshot."""

    stale_completed: tuple[str, ...] = ()
    """Completed requests whose scope has pending work on the latest revision."""
    open_requests: tuple[str, ...] = ()
    """Active requests: a person was asked to review and has not completed."""


@dataclass(frozen=True, slots=True)
class TrustedBaseFact:
    """L4: the default branch's review policy now, against the one this Snapshot was captured with."""

    state: str  # current | stale | unknown | not_rechecked
    detail: str = ""
    tip_commit: str = ""


@dataclass(frozen=True, slots=True)
class FindingsFact:
    p0_p1: int = 0
    unrated: int = 0
    other_unresolved: int = 0
    stale_unresolved: int = 0


@dataclass(frozen=True, slots=True)
class AutoApprovalFacts:
    """Everything one evaluation reads; ``None`` means the fact could not be read (unknown)."""

    review_id: str
    snapshot_id: str
    tree_fingerprint: str
    head_revision: str
    policy: LandingPolicy
    readiness: Mapping[str, Any]
    changed_paths: tuple[str, ...]
    criticality: str | None
    labels: tuple[str, ...] | None = None
    target_branch: str | None = None
    findings: FindingsFact | None = None
    human_needs_changes: int | None = None
    provider_blockers: tuple[str, ...] | None = ()
    carried_blockers: tuple[Mapping[str, Any], ...] | None = ()
    unqualified_evidence: tuple[str, ...] | None = ()
    provider_sync: ProviderSyncFact | None = None
    requests: RequestProgressFact | None = None
    trusted_base: TrustedBaseFact | None = None
    errors: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Gate:
    id: str
    label: str
    passed: bool
    detail: str
    reason: str = ""
    transient: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "passed": self.passed,
            "detail": self.detail,
            "reason": self.reason,
            "transient": self.transient,
        }


@dataclass(frozen=True, slots=True)
class AutoApprovalDecision:
    review_id: str
    snapshot_id: str
    tree_fingerprint: str
    head_revision: str
    policy_generation: str
    conditions_hash: str
    evaluated_at: str
    gates: tuple[Gate, ...]

    @property
    def eligible(self) -> bool:
        return bool(self.gates) and all(gate.passed for gate in self.gates)

    @property
    def failed(self) -> tuple[Gate, ...]:
        return tuple(gate for gate in self.gates if not gate.passed)

    @property
    def definite_failures(self) -> tuple[Gate, ...]:
        """Failed gates that are a definite "no" (only these may withdraw a standing approval)."""

        return tuple(gate for gate in self.failed if not gate.transient)

    @property
    def digest(self) -> str:
        """Stable identity of what was decided (never the wording or the instant)."""

        document = {
            "version": DECISION_VERSION,
            "snapshot_id": self.snapshot_id,
            "tree_fingerprint": self.tree_fingerprint,
            "head_revision": self.head_revision,
            "policy_generation": self.policy_generation,
            "conditions_hash": self.conditions_hash,
            "gates": [[gate.id, gate.passed, gate.reason] for gate in self.gates],
        }
        raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "decision:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": DECISION_VERSION,
            "review_id": self.review_id,
            "snapshot_id": self.snapshot_id,
            "tree_fingerprint": self.tree_fingerprint,
            "head_revision": self.head_revision,
            "eligible": self.eligible,
            "policy_generation": self.policy_generation,
            "conditions_hash": self.conditions_hash,
            "evaluated_at": self.evaluated_at,
            "decision_digest": self.digest,
            "gates": [gate.to_wire() for gate in self.gates],
            "blocking": [gate.label for gate in self.failed],
        }


def _named(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:MAX_PATHS_NAMED])
    return shown + (f" and {len(paths) - MAX_PATHS_NAMED} more" if len(paths) > MAX_PATHS_NAMED else "")


def _unknown(gate_id: str, label: str, facts: AutoApprovalFacts, what: str) -> Gate:
    error = facts.errors.get(gate_id, "")
    return Gate(
        gate_id,
        label,
        False,
        f"{what} could not be read{': ' + error[:200] if error else ''}; auto-approval waits until it can.",
        reason="unknown",
        transient=True,
    )


def _policy_gate(facts: AutoApprovalFacts) -> Gate:
    policy = facts.policy
    if policy.errors:
        return Gate(
            "policy",
            "Auto-approval policy",
            False,
            "The landing policy is invalid, so auto-approval is off: " + "; ".join(policy.errors[:3]),
            reason="invalid_policy",
        )
    if not policy.auto_approve.enabled:
        return Gate(
            "policy",
            "Auto-approval policy",
            False,
            "Organization policy does not enable auto-approval for this repository.",
            reason="not_enabled",
        )
    return Gate("policy", "Auto-approval policy", True, "Organization policy enables auto-approval.", "enabled")


def _authority_gate(facts: AutoApprovalFacts) -> Gate:
    generation = facts.readiness.get("policy_generation")
    authority = str(generation.get("authority") or "") if isinstance(generation, Mapping) else ""
    if authority == "final":
        return Gate("policy_authority", "Policy authority", True, "The effective Review policy is final.", "final")
    return Gate(
        "policy_authority",
        "Policy authority",
        False,
        f"The effective Review policy is {authority or 'unknown'}; auto-approval needs a final policy.",
        reason=authority or "unknown",
        transient=authority in {"", "provisional"},
    )


def _provider_sync_gate(facts: AutoApprovalFacts) -> Gate:
    label = "Provider in sync"
    sync = facts.provider_sync
    if sync is None:
        return _unknown("provider_sync", label, facts, "The provider's sync state")
    if sync.state != "synced":
        return Gate(
            "provider_sync",
            label,
            False,
            f"The provider is {sync.state.replace('_', ' ') or 'not linked'}; LemonCrow approves only what it has "
            "fully synced.",
            reason=sync.state or "not_linked",
            # A provider that is ahead or delayed is a moving target, not a regression.
            transient=sync.state in {"provider_delayed", "local_pending", "sync_failed"},
        )
    if not facts.head_revision or sync.provider_head != facts.head_revision:
        return Gate(
            "provider_sync",
            label,
            False,
            f"The provider head {sync.provider_head[:12] or 'unknown'} is not this revision's head "
            f"{facts.head_revision[:12] or 'unknown'}.",
            reason="head_mismatch",
        )
    return Gate("provider_sync", label, True, f"The provider head is this revision's head {sync.provider_head[:12]}.")


def _requests_gate(facts: AutoApprovalFacts) -> Gate:
    label = "Requested reviews current"
    requests = facts.requests
    if requests is None:
        return _unknown("requests", label, facts, "Review request progress on the latest revision")
    if requests.stale_completed:
        return Gate(
            "requests",
            label,
            False,
            f"{len(requests.stale_completed)} completed review request(s) have new work on the latest revision.",
            reason="request_reopened",
        )
    if requests.open_requests:
        return Gate(
            "requests",
            label,
            False,
            f"{len(requests.open_requests)} requested review(s) are still open: a person was asked to review.",
            reason="review_requested",
        )
    return Gate("requests", label, True, "No requested review has pending work on the latest revision.")


def _trusted_base_gate(facts: AutoApprovalFacts) -> Gate:
    label = "Trusted base current"
    base = facts.trusted_base
    if base is None:
        return _unknown("trusted_base", label, facts, "The default branch's review policy")
    if base.state == "current":
        return Gate("trusted_base", label, True, base.detail or "The default branch's review policy is unchanged.")
    if base.state == "not_rechecked":
        # Revalidation: L4 is a decision-time precondition; it was proven when the approval was made.
        return Gate(
            "trusted_base",
            label,
            True,
            base.detail or "Checked against the default branch when deciding; not re-read here.",
            reason="not_rechecked",
        )
    return Gate(
        "trusted_base",
        label,
        False,
        base.detail or "The default branch's review policy changed after this revision was captured.",
        reason=base.state or "unknown",
        transient=base.state != "stale",
    )


def _hard_deny_gate(facts: AutoApprovalFacts) -> Gate:
    label = "Not a sensitive change"
    classes = hard_deny_classes(facts.changed_paths, sensitive_paths=facts.policy.auto_approve.sensitive_paths)
    if not facts.changed_paths:
        return Gate(
            "hard_deny", label, False, "No changed path is known, so sensitivity cannot be ruled out.", "unknown"
        )
    if classes:
        parts = [f"{HARD_DENY_LABELS[klass]} ({_named(paths)})" for klass, paths in classes.items()]
        return Gate(
            "hard_deny",
            label,
            False,
            "Never auto-approved: " + "; ".join(parts) + ".",
            reason=",".join(classes),
        )
    return Gate("hard_deny", label, True, "No changed path is in a sensitive class.")


def _scope_gate(facts: AutoApprovalFacts) -> Gate:
    label = "In auto-approval scope"
    scope = facts.policy.auto_approve
    outside = [
        path
        for path in facts.changed_paths
        if scope.include_paths and not _matches(path, scope.include_paths, on_error=False)
    ]
    excluded = [path for path in facts.changed_paths if _configured(policy_path(path), scope.exclude_paths)]
    problems: list[str] = []
    reasons: list[str] = []
    if outside:
        problems.append(f"{len(outside)} path(s) outside the included paths ({_named(outside)})")
        reasons.append("outside_include")
    if excluded:
        problems.append(f"{len(excluded)} excluded path(s) ({_named(excluded)})")
        reasons.append("excluded_path")
    transient = False
    if scope.exclude_labels:
        if facts.labels is None:
            problems.append("the pull request's labels could not be read")
            reasons.append("labels_unknown")
            transient = True
        else:
            hit = sorted({label.lower() for label in facts.labels} & {label.lower() for label in scope.exclude_labels})
            if hit:
                problems.append(f"excluded label(s) {', '.join(hit)}")
                reasons.append("excluded_label")
    if scope.exclude_branches:
        if facts.target_branch is None:
            problems.append("the target branch could not be read")
            reasons.append("branch_unknown")
            transient = True
        elif _matches(facts.target_branch, scope.exclude_branches):
            problems.append(f"target branch {facts.target_branch} is excluded")
            reasons.append("excluded_branch")
    if problems:
        definite = any(reason not in {"labels_unknown", "branch_unknown"} for reason in reasons)
        return Gate(
            "scope",
            label,
            False,
            "Outside the auto-approval scope: " + "; ".join(problems) + ".",
            reason=",".join(reasons),
            transient=transient and not definite,
        )
    return Gate("scope", label, True, "Every changed path, label and the target branch are in scope.")


def _criticality_gate(facts: AutoApprovalFacts) -> Gate:
    label = "Criticality"
    ceiling = facts.policy.auto_approve.max_criticality
    if facts.criticality is None or facts.criticality not in CRITICALITY_RANK:
        return _unknown("criticality", label, facts, "The Change's criticality")
    if facts.criticality == "critical" or CRITICALITY_RANK[facts.criticality] > CRITICALITY_RANK[ceiling]:
        return Gate(
            "criticality",
            label,
            False,
            f"The Change is {facts.criticality}; policy auto-approves at most {ceiling}.",
            reason=facts.criticality,
        )
    return Gate("criticality", label, True, f"The Change is {facts.criticality} (policy allows {ceiling}).")


def _conditions(facts: AutoApprovalFacts) -> list[Mapping[str, Any]]:
    return [row for row in facts.readiness.get("conditions") or () if isinstance(row, Mapping)]


def _readiness_gate(facts: AutoApprovalFacts) -> Gate:
    """Every LemonCrow-owned condition holds, except the review progress auto-approval stands in for.

    SCM-owned conditions stay with the SCM (its auto-merge waits for them). A
    waived condition is a human exception, never something policy approves on.
    The merge result and change set have gates of their own.
    """

    label = "Review readiness"
    rows = [
        row
        for row in _conditions(facts)
        if str(row.get("owner") or "lemoncrow") != "scm"
        and str(row.get("id") or "") not in STANDS_IN_FOR
        and str(row.get("id") or "") != MERGE_RESULT_CONDITION
        and not str(row.get("id") or "").startswith(CHANGE_SET_PREFIX)
        and str(row.get("id") or "") != "policy-generation"
    ]
    if not facts.readiness.get("conditions_hash"):
        return _unknown("readiness", label, facts, "Review readiness")
    blocked = [row for row in rows if str(row.get("state") or "") not in SATISFIED_STATES]
    waived = [row for row in rows if str(row.get("state") or "") == "waived"]
    if blocked:
        names = "; ".join(str(row.get("label") or row.get("id")) for row in blocked[:6])
        return Gate(
            "readiness",
            label,
            False,
            f"{len(blocked)} readiness condition(s) are not satisfied: {names}.",
            reason="not_ready",
            transient=all(str(row.get("reason") or "") in TRANSIENT_REASONS for row in blocked),
        )
    if waived:
        names = "; ".join(str(row.get("label") or row.get("id")) for row in waived[:6])
        return Gate(
            "readiness",
            label,
            False,
            f"{len(waived)} condition(s) hold only by a human waiver ({names}); a waiver is never auto-approved.",
            reason="waived",
        )
    return Gate("readiness", label, True, "Every LemonCrow-owned readiness condition holds.")


def _human_gate(facts: AutoApprovalFacts) -> Gate:
    label = "No human blocker"
    problems: list[str] = []
    if facts.human_needs_changes is None or facts.provider_blockers is None:
        return _unknown("human_blockers", label, facts, "Human review decisions")
    if facts.human_needs_changes:
        problems.append(f"{part_count(facts.human_needs_changes)} judged as needing changes")
    problems.extend(facts.provider_blockers)
    if problems:
        return Gate("human_blockers", label, False, "; ".join(problems) + ".", reason="human_blocker")
    return Gate("human_blockers", label, True, "No reviewer has asked for changes.")


def _weakening_gate(facts: AutoApprovalFacts) -> Gate:
    label = "No weakened verification"
    if facts.carried_blockers is None:
        return _unknown("verification_weakening", label, facts, "Verification-weakening analysis")
    if facts.carried_blockers:
        names = "; ".join(str(row.get("title") or row.get("code")) for row in facts.carried_blockers[:4])
        more = len(facts.carried_blockers) - 4
        return Gate(
            "verification_weakening",
            label,
            False,
            f"Accepting these is a human decision: {names}" + (f" and {more} more" if more > 0 else "") + ".",
            reason=",".join(sorted({str(row.get("code") or "") for row in facts.carried_blockers})),
        )
    return Gate(
        "verification_weakening",
        label,
        True,
        "No weakened verification, agent instruction or critical side-effect change.",
    )


def _findings_gate(facts: AutoApprovalFacts) -> Gate:
    label = "No unresolved findings"
    findings = facts.findings
    if findings is None:
        return _unknown("findings", label, facts, "Review findings")
    problems: list[str] = []
    if findings.p0_p1:
        problems.append(f"{findings.p0_p1} unresolved P0/P1 finding(s)")
    if findings.unrated:
        problems.append(f"{findings.unrated} unresolved finding(s) without a severity")
    if facts.policy.auto_approve.require_clean_review:
        if findings.other_unresolved:
            problems.append(f"{findings.other_unresolved} other unresolved finding(s)")
        if findings.stale_unresolved:
            problems.append(f"{findings.stale_unresolved} unresolved finding(s) from an earlier revision")
    if problems:
        return Gate("findings", label, False, "; ".join(problems) + ".", reason="unresolved_findings")
    return Gate("findings", label, True, "No unresolved finding blocks approval.")


def _qualification_gate(facts: AutoApprovalFacts) -> Gate:
    """Gate H: LemonCrow-executed evidence feeds auto-approval only once qualified.

    Readiness marks a condition (``evidence_qualification``) only when an
    advisory LemonCrow-executed result touched it -- in enforce mode (not
    counted) or report mode (counted today, advisory once enforced). Either
    way the condition rests on unqualified evidence, so it never approves.
    """

    label = "Qualified LemonCrow evidence"
    if facts.unqualified_evidence is None:
        return _unknown("evidence_qualification", label, facts, "Evidence qualification")
    marked = [row for row in _conditions(facts) if isinstance(row.get("evidence_qualification"), Mapping)]
    unqualified = list(facts.unqualified_evidence)
    if unqualified or marked:
        parts = []
        if unqualified:
            parts.append(f"{len(unqualified)} LemonCrow-executed result(s) not yet qualified ({_named(unqualified)})")
        if marked:
            names = _named([str(row.get("label") or row.get("id")) for row in marked])
            parts.append(f"{len(marked)} readiness condition(s) rest on advisory LemonCrow-executed results ({names})")
        return Gate(
            "evidence_qualification",
            label,
            False,
            "; ".join(parts) + ". Gate H must qualify them before they can feed auto-approval.",
            reason="unqualified",
        )
    return Gate("evidence_qualification", label, True, "No unqualified LemonCrow-executed evidence is relied on.")


def _change_set_gate(facts: AutoApprovalFacts) -> Gate:
    """A change set is read as a principal; a request-free (sweep) read is incomplete and never approves."""

    label = "Change set complete"
    rows = [row for row in _conditions(facts) if str(row.get("id") or "").startswith(CHANGE_SET_PREFIX)]
    if not rows:
        return Gate("change_set", label, True, "Not part of a change set.", reason="not_in_set")
    blocked = [
        row
        for row in rows
        if str(row.get("state") or "") not in SATISFIED_STATES
        or (str(row.get("id") or "") == f"{CHANGE_SET_PREFIX}members" and row.get("set_state") not in {None, "ready"})
    ]
    if blocked:
        incomplete = any(row.get("set_state") == "incomplete" for row in blocked)
        return Gate(
            "change_set",
            label,
            False,
            (
                "The change set could not be read completely as this principal; it is never auto-approved "
                "from a partial view."
                if incomplete
                else "The change set is not ready: "
                + "; ".join(str(row.get("label") or row.get("id")) for row in blocked[:4])
                + "."
            ),
            reason="incomplete" if incomplete else "not_ready",
            transient=all(str(row.get("reason") or "") in TRANSIENT_REASONS for row in blocked),
        )
    return Gate("change_set", label, True, "Every change-set member is ready.")


def _merge_result_gate(facts: AutoApprovalFacts) -> Gate:
    label = "Merge result"
    row = next((row for row in _conditions(facts) if row.get("id") == MERGE_RESULT_CONDITION), None)
    if row is None or str(row.get("state") or "") != "fail":
        return Gate("merge_result", label, True, "No new failure on the merge result.")
    return Gate("merge_result", label, False, str(row.get("detail") or "The merge result fails."), "merge_fail")


_GATES: Final[tuple[Callable[[AutoApprovalFacts], Gate], ...]] = (
    _policy_gate,
    _authority_gate,
    _provider_sync_gate,
    _requests_gate,
    _trusted_base_gate,
    _hard_deny_gate,
    _scope_gate,
    _criticality_gate,
    _readiness_gate,
    _human_gate,
    _weakening_gate,
    _findings_gate,
    _qualification_gate,
    _change_set_gate,
    _merge_result_gate,
)
GATE_IDS: Final[tuple[str, ...]] = (
    "policy",
    "policy_authority",
    "provider_sync",
    "requests",
    "trusted_base",
    "hard_deny",
    "scope",
    "criticality",
    "readiness",
    "human_blockers",
    "verification_weakening",
    "findings",
    "evidence_qualification",
    "change_set",
    "merge_result",
)


def evaluate_auto_approval(facts: AutoApprovalFacts, *, evaluated_at: str) -> AutoApprovalDecision:
    """Evaluate every gate (no short-circuit); a gate that raises fails closed as unknown."""

    gates: list[Gate] = []
    for gate_id, evaluate in zip(GATE_IDS, _GATES, strict=True):
        try:
            gates.append(evaluate(facts))
        except Exception as exc:  # fail closed: a broken gate never passes
            gates.append(
                Gate(
                    gate_id,
                    gate_id.replace("_", " ").capitalize(),
                    False,
                    f"Could not be evaluated: {(str(exc) or exc.__class__.__name__)[:200]}",
                    reason="evaluation_error",
                    transient=True,
                )
            )
    generation = facts.readiness.get("policy_generation")
    return AutoApprovalDecision(
        review_id=facts.review_id,
        snapshot_id=facts.snapshot_id,
        tree_fingerprint=facts.tree_fingerprint,
        head_revision=facts.head_revision,
        policy_generation=str(generation.get("digest") or "") if isinstance(generation, Mapping) else "",
        conditions_hash=str(facts.readiness.get("conditions_hash") or ""),
        evaluated_at=evaluated_at,
        gates=tuple(gates),
    )


# --------------------------------------------------------------------------- #
# durable decision and standing approval
# --------------------------------------------------------------------------- #


def store_decision(store: Any, decision: AutoApprovalDecision) -> bool:
    """Keep the Snapshot's latest eligibility decision (one row; rewritten only when it changed)."""

    from lemoncrow.pro.capabilities.review.store import DerivedResult

    existing = store.derived_results(DECISION_KIND, [decision.snapshot_id]).get((decision.snapshot_id, DECISION_SCOPE))
    if existing is not None and existing[0] == decision.digest:
        return False
    store.put_derived_results(
        [
            DerivedResult(
                review_id=decision.review_id,
                revision_id=decision.snapshot_id,
                kind=DECISION_KIND,
                scope_key=DECISION_SCOPE,
                stamp=decision.digest,
                payload=decision.to_wire(),
            )
        ]
    )
    return True


def stored_decision(store: Any, snapshot_id: str) -> Mapping[str, Any] | None:
    found = store.derived_results(DECISION_KIND, [snapshot_id]).get((snapshot_id, DECISION_SCOPE))
    if found is None or not isinstance(found[1], Mapping) or found[1].get("version") != DECISION_VERSION:
        return None
    return found[1]


def auto_approval_relationship(store: Any, review_id: str) -> Any | None:
    """The standing auto-approval of one Change, if any (the newest when the provider link changed)."""

    rows = store.list_relationships(review_id, kind="landing")
    return next((item for item in reversed(rows) if item.relation == AUTO_APPROVAL_RELATION), None)


def approval_metadata(item: Any | None) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review.landing import landing_metadata

    return landing_metadata(item)


def record_auto_approval(
    store: Any,
    *,
    decision: AutoApprovalDecision,
    revision_number: int,
    policy_layers: Sequence[Mapping[str, Any]],
    valid_until: str,
    provider: str,
    external_id: str,
    url: str = "",
) -> Any:
    """Record an eligible decision as the Change's standing approval (delivery owed).

    The relationship's metadata uses the armed-landing keys (``policy_generation``,
    ``policy_layers``, ``readiness_valid_until``, ``provider_cancel``) so the
    one revalidation path and the hosted sweep read both the same way.
    """

    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

    if not decision.eligible:
        raise ValueError("only an eligible decision can be recorded as an auto-approval")
    metadata = {
        "revision_id": decision.snapshot_id,
        "revision_number": revision_number,
        "tree_fingerprint": decision.tree_fingerprint,
        "head_revision": decision.head_revision,
        "conditions_hash": decision.conditions_hash,
        "policy_generation": decision.policy_generation,
        "policy_layers": [dict(layer) for layer in policy_layers],
        "readiness_valid_until": valid_until,
        "decision_digest": decision.digest,
        "decision": decision.to_wire(),
        "decided_at": decision.evaluated_at,
        "decided_by": AUTO_APPROVAL_ACTOR,
        "delivery": "pending",
        "detail": "Approved by organization policy; delivering the approval to the provider.",
    }
    return store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=decision.review_id,
            kind="landing",
            relation=AUTO_APPROVAL_RELATION,
            provider=provider,
            external_id=external_id or "auto_approval",
            url=url,
            title="Auto-approval",
            status="approving",
            source="policy",
            metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
    )


def _write(store: Any, item: Any, *, status: str, updates: Mapping[str, Any]) -> Any | None:
    metadata = approval_metadata(item)
    metadata.update(dict(updates))
    return store.update_relationship_if_unchanged(
        item, status=status, metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    )


def settle_delivery(store: Any, item: Any, result: Mapping[str, Any]) -> Any | None:
    """Record the provider's answer to an owed approval delivery (CAS; ``None`` when it raced)."""

    state = str(result.get("state") or "failed")
    detail = str(result.get("detail") or result.get("error") or "")[:500]
    if state == "sent":
        return _write(
            store,
            item,
            status="approved",
            updates={
                "delivery": "sent",
                "remote_ref": str(result.get("remote_ref") or ""),
                "delivery_error": "",
                "detail": "Approved by organization policy; the approval is on the provider.",
            },
        )
    # Retryable or failed: the approval stays owed (never shown as delivered).
    return _write(
        store,
        item,
        status="approving",
        updates={
            "delivery": "pending",
            "delivery_error": detail,
            "detail": f"Approved by organization policy; delivering it to the provider failed ({detail}). Retrying.",
        },
    )


def claim_withdrawal(
    store: Any, item: Any, *, detail: str, blocked_by: Sequence[str], policy_generation: str, cause: str
) -> Any | None:
    """Claim one withdrawal compare-and-set (exactly one caller wins); the provider withdrawal is then owed."""

    metadata = approval_metadata(item)
    if item.status not in IN_FORCE_STATES or metadata.get("provider_cancel") == "pending":
        return None
    return _write(
        store,
        item,
        status=item.status,
        updates={
            "provider_cancel": "pending",
            "cancel_reason": cause,
            "withdraw_detail": detail,
            "blocked_by": list(blocked_by),
            "policy_generation_at_cancel": policy_generation,
            "detail": f"{detail} Withdrawing the approval.",
        },
    )


def complete_withdrawal(
    store: Any, item: Any, withdraw: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
) -> Any:
    """Perform an owed withdrawal; a provider failure leaves it owed (retried by the next pass)."""

    metadata = approval_metadata(item)
    if metadata.get("provider_cancel") != "pending":
        return item
    delivered = metadata.get("delivery") == "sent"
    if delivered and withdraw is not None:
        try:
            result = withdraw(metadata)
            state = str(result.get("state") or "failed")
            detail = str(result.get("detail") or "")
        except Exception as exc:  # provider boundary: preserve the exact failure
            state, detail = "failed", str(exc) or exc.__class__.__name__
    elif delivered:
        state, detail = "unavailable", "No connected provider can withdraw the approval; dismiss it in the provider."
    else:
        state, detail = "done", ""
    if state == "failed":
        return (
            _write(
                store,
                item,
                status=item.status,
                updates={
                    "withdraw_error": detail[:500],
                    "detail": f"{metadata.get('withdraw_detail') or ''} Withdrawing the provider approval FAILED: "
                    f"{detail[:300]}; LemonCrow retries it.".strip(),
                },
            )
            or item
        )
    return (
        _write(
            store,
            item,
            status="withdrawn",
            updates={
                "provider_cancel": "done" if state != "unavailable" else "unavailable",
                "withdraw_error": "",
                "detail": f"{metadata.get('withdraw_detail') or ''} {detail}".strip()
                or "The auto-approval was withdrawn.",
            },
        )
        or item
    )


AuditSink = Callable[[Any, str, str, Mapping[str, Any]], None]


def revalidate_auto_approval(
    store: Any,
    *,
    review_id: str,
    latest_tree_fingerprint: str,
    origin: str,
    evaluate: Callable[[], AutoApprovalDecision],
    valid_until: Callable[[], str],
    withdraw: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    deliver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    audit: AuditSink,
) -> str:
    """Revalidate the standing auto-approval; the only path that withdraws it (L2).

    Called by the Review API's ``_revalidate_armed_landing`` (Reader actions,
    readiness reads, the hosted sweep, the local watch). Never creates an
    approval: still eligible records the current generation/hash (and retries
    an owed delivery of the *same* decision basis); a definite failed gate
    claims the withdrawal compare-and-set and performs it through the
    provider; only transient failures defer. Returns ``none``, ``stale``,
    ``unchanged``, ``revalidated``, ``deferred``, ``raced`` or ``withdrawn``.
    """

    item = auto_approval_relationship(store, review_id)
    if item is None:
        return "none"
    metadata = approval_metadata(item)
    if metadata.get("provider_cancel") == "pending":
        settled = complete_withdrawal(store, item, withdraw)
        return "withdrawn" if getattr(settled, "status", "") == "withdrawn" else "deferred"
    if item.status not in IN_FORCE_STATES:
        return "none"
    if str(metadata.get("tree_fingerprint") or "") != latest_tree_fingerprint:
        claimed = claim_withdrawal(
            store,
            item,
            detail="A new revision was captured after the auto-approval.",
            blocked_by=["snapshot"],
            policy_generation=str(metadata.get("policy_generation") or ""),
            cause="snapshot",
        )
        if claimed is None:
            return "raced"
        audit(claimed, "landing.auto_approval_withdrawn", "Auto-approval withdrawn: a new revision", {"origin": origin})
        complete_withdrawal(store, claimed, withdraw)
        return "stale"

    def _defer(cause: str) -> str:
        if metadata.get("revalidation_deferred") == cause:
            return "deferred"
        written = _write(store, item, status=item.status, updates={"revalidation_deferred": cause})
        if written is not None:
            audit(
                written,
                "landing.revalidation_deferred",
                "Auto-approval eligibility could not be evaluated; the approval was kept, not withdrawn",
                {"origin": origin, "cause": cause},
            )
        return "deferred"

    try:
        decision = evaluate()
    except Exception as exc:  # a read failure is not a regression of the decision
        return _defer(f"eligibility evaluation failed: {(str(exc) or exc.__class__.__name__)[:300]}")
    recorded = str(metadata.get("policy_generation") or "")
    if decision.eligible:
        until = valid_until()
        unchanged = (
            recorded == decision.policy_generation
            and str(metadata.get("conditions_hash") or "") == decision.conditions_hash
            and str(metadata.get("readiness_valid_until") or "") == until
            and not metadata.get("revalidation_deferred")
        )
        current = item
        outcome = "unchanged"
        if not unchanged:
            written = _write(
                store,
                item,
                status=item.status,
                updates={
                    "policy_generation": decision.policy_generation,
                    "conditions_hash": decision.conditions_hash,
                    "readiness_valid_until": until,
                    "revalidation_deferred": "",
                    "revalidated_decision_digest": decision.digest,
                    **({"policy_revalidated_from": recorded} if recorded != decision.policy_generation else {}),
                },
            )
            if written is None:
                return "raced"
            if recorded != decision.policy_generation:
                audit(
                    written,
                    "landing.revalidated",
                    "Review policy changed; the auto-approval still satisfies it",
                    {"origin": origin, "previous_generation": recorded, "generation": decision.policy_generation},
                )
            current, outcome = written, "revalidated"
        if approval_metadata(current).get("delivery") == "pending" and deliver is not None:
            # The same decision basis (Snapshot and head unchanged): retrying delivery is not a new approval.
            try:
                result = deliver(approval_metadata(current))
            except Exception as exc:  # provider boundary
                result = {"state": "retryable", "detail": str(exc) or exc.__class__.__name__}
            settle_delivery(store, current, result)
        return outcome
    definite = decision.definite_failures
    if not definite:
        return _defer("; ".join(gate.label for gate in decision.failed) or "eligibility could not be evaluated")
    labels = [gate.label for gate in definite]
    changed = bool(recorded) and recorded != decision.policy_generation
    detail = (
        (
            "Review policy changed after the auto-approval and it no longer qualifies ("
            if changed
            else "An auto-approval gate no longer holds ("
        )
        + "; ".join(labels)
        + ")."
    )
    claimed = claim_withdrawal(
        store,
        item,
        detail=detail,
        blocked_by=labels,
        policy_generation=decision.policy_generation,
        cause="readiness",
    )
    if claimed is None:
        return "raced"
    audit(
        claimed,
        "landing.auto_approval_withdrawn",
        f"Auto-approval withdrawn: {'; '.join(labels)}"[:500],
        {
            "origin": origin,
            "blocked_by": [{"id": gate.id, "label": gate.label, "reason": gate.reason} for gate in definite],
            "previous_generation": recorded,
            "generation": decision.policy_generation,
            "decision_digest": decision.digest,
        },
    )
    complete_withdrawal(store, claimed, withdraw)
    return "withdrawn"


def approval_wire(item: Any | None, *, latest_tree_fingerprint: str) -> dict[str, Any]:
    """The standing approval as readiness shows it (``state: none`` without one)."""

    if item is None:
        return {"state": "none"}
    metadata = approval_metadata(item)
    stale = str(metadata.get("tree_fingerprint") or "") != latest_tree_fingerprint
    state = str(item.status or "")
    if stale and state in IN_FORCE_STATES:
        state = "stale"
    return {
        "state": state,
        "revision_id": str(metadata.get("revision_id") or ""),
        "decision_digest": str(metadata.get("decision_digest") or ""),
        "policy_generation": str(metadata.get("policy_generation") or ""),
        "conditions_hash": str(metadata.get("conditions_hash") or ""),
        "decided_at": str(metadata.get("decided_at") or ""),
        "delivery": str(metadata.get("delivery") or ""),
        "provider_cancel": str(metadata.get("provider_cancel") or ""),
        "detail": str(metadata.get("detail") or ""),
    }


def decision_for_record(item: Any) -> dict[str, str]:
    """The signed-record decision detail of a standing approval (strings only)."""

    metadata = approval_metadata(item)
    return {
        "basis": "auto_approval",
        "decision_digest": str(metadata.get("decision_digest") or ""),
        "policy_generation": str(metadata.get("policy_generation") or ""),
        "conditions_hash": str(metadata.get("conditions_hash") or ""),
        "head_revision": str(metadata.get("head_revision") or ""),
    }


def with_errors(facts: AutoApprovalFacts, errors: Mapping[str, str]) -> AutoApprovalFacts:
    return replace(facts, errors={**facts.errors, **errors})


__all__ = [
    "AUTO_APPROVAL_ACTOR",
    "AUTO_APPROVAL_RELATION",
    "DECISION_KIND",
    "GATE_IDS",
    "HARD_DENY_CLASSES",
    "HARD_DENY_LABELS",
    "IN_FORCE_STATES",
    "AutoApprovalDecision",
    "AutoApprovalFacts",
    "FindingsFact",
    "Gate",
    "PolicyPath",
    "ProviderSyncFact",
    "RequestProgressFact",
    "TrustedBaseFact",
    "approval_metadata",
    "approval_wire",
    "auto_approval_relationship",
    "claim_withdrawal",
    "complete_withdrawal",
    "decision_for_record",
    "evaluate_auto_approval",
    "hard_deny_classes",
    "policy_path",
    "record_auto_approval",
    "revalidate_auto_approval",
    "settle_delivery",
    "store_decision",
    "stored_decision",
    "with_errors",
]
