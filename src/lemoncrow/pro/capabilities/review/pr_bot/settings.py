"""The pull request reviewer's noise controls: what is posted, listed, held back or left out, and who decides.

The settings live in ``review:`` of the **trusted-base** ``.lemoncrow/review.yaml`` (the merge base's copy, the same
authority as the rules), so a pull request cannot loosen its own review by editing the file::

    review:
      strictness: 3             # 1 shows everything, 2 is the default, 3 holds back P2 (alias of finding_threshold)
      finding_threshold: high   # critical | high | medium | suggestions: what is surfaced at all
      inline_comments: false    # summary only: no inline review comments
      max_files: 60             # above this many files, review the highest-risk ones first and say so
      ignore: ["docs/**", "**/*.lock"]   # no findings on these paths, and the model never reads them
      rules:                    # a rule's severity: it can lower a critical rule, and raise an advisory one to P1
        no-eval: {severity: p1}

``strictness`` and ``finding_threshold`` are parsed by ``criticality.parse_triage_settings`` (the Reader reads them
too); this module reads the rest and joins them in one :class:`NoisePolicy`. Three of the settings also have a
workflow-level default (:class:`NoiseInputs`, set by the Action's inputs): the file wins when it sets the key.

Nothing here can hide a P0. P0 findings are always surfaced and always inline (unless the repository asks for a summary
only), a threshold only holds back what is below it, and a held-back finding is counted in the summary, never dropped
silently. A rule's severity can lift a violation to P0 only for a critical rule, which is a P0 already.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

import yaml

from lemoncrow.pro.capabilities.review.criticality import (
    DEFAULT_FINDING_THRESHOLD,
    FINDING_THRESHOLDS,
    STRICTNESS_THRESHOLDS,
    severity_for,
    surfaced,
)
from lemoncrow.pro.capabilities.review.pr_bot.findings import SEVERITY_RANK
from lemoncrow.pro.capabilities.review.rules import (
    ContentAccessRule,
    ReviewGovernance,
    ReviewRule,
    _safe_path,
    cites_critical_rule,
    path_matches,
)

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.criticality import EffectiveTriagePolicy

#: Files one review looks at before the highest-risk ones are chosen and the rest are said to be left for later.
DEFAULT_MAX_FILES: Final[int] = 100
MAX_FILES_CEILING: Final[int] = 100_000
MAX_IGNORE: Final[int] = 64
SEVERITIES: Final[tuple[str, ...]] = ("p0", "p1", "p2")

STRICTNESS_ENV: Final[str] = "LEMONCROW_REVIEW_STRICTNESS"
INLINE_ENV: Final[str] = "LEMONCROW_REVIEW_INLINE_COMMENTS"
MAX_FILES_ENV: Final[str] = "LEMONCROW_REVIEW_MAX_FILES"
#: ``threshold -> strictness`` for the ones that have a number; ``critical`` has none.
THRESHOLD_STRICTNESS: Final[Mapping[str, int]] = {value: key for key, value in STRICTNESS_THRESHOLDS.items()}
#: Where a finding of each severity goes: ``row`` (the findings list, and an inline comment when those are on),
#: ``listed`` (the collapsed low-priority block) or ``held`` (counted, below the threshold).
TIERS: Final[tuple[str, ...]] = ("row", "listed", "held")
_CONFIG: Final[str] = ".lemoncrow/review.yaml"
_IGNORE_ORIGIN: Final[str] = "review.ignore in the trusted-base repository Review policy"


# --------------------------------------------------------------------------- #
# the workflow's defaults
# --------------------------------------------------------------------------- #

# lc-debt: the workflow's defaults reach the reviewer only through the LEMONCROW_REVIEW_STRICTNESS, _INLINE_COMMENTS and
# _MAX_FILES variables the Action sets, so `lc review --pr N --ai` has no flag for them; upgrade: `--strictness`,
# `--inline-comments` and `--max-files` options of `lc review` that fill `RunOptions.noise`.


@dataclass(frozen=True, slots=True)
class NoiseInputs:
    """Workflow-level defaults (the Action's ``strictness``, ``inline-comments`` and ``max-files`` inputs).

    ``None`` is "not set". A key the trusted-base ``.lemoncrow/review.yaml`` sets wins over these: the file is the
    repository's policy, an input only fills in for a repository that has none. On a ``pull_request`` run GitHub reads
    the workflow from the pull request's own merge commit, so a rule that must hold against the pull request's
    author belongs in the file.
    """

    strictness: int | None = None
    inline_comments: bool | None = None
    max_files: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> NoiseInputs:
        """The inputs as the Action passes them in the environment; empty or absent means not set.

        Raises ``ValueError`` naming the setting when a value is not one this accepts: a typo in the workflow is the
        workflow author's to fix, and a review that quietly ignored it would be tuned to nothing.
        """

        def text(name: str) -> str:
            return str(env.get(name) or "").strip()

        strictness: int | None = None
        if text(STRICTNESS_ENV):
            if text(STRICTNESS_ENV) not in {"1", "2", "3"}:
                raise ValueError("strictness must be 1, 2 or 3")
            strictness = int(text(STRICTNESS_ENV))
        inline: bool | None = None
        if text(INLINE_ENV):
            if text(INLINE_ENV).lower() not in {"true", "false"}:
                raise ValueError("inline-comments must be true or false")
            inline = text(INLINE_ENV).lower() == "true"
        limit: int | None = None
        if text(MAX_FILES_ENV):
            if not text(MAX_FILES_ENV).isdigit() or int(text(MAX_FILES_ENV)) > MAX_FILES_CEILING:
                raise ValueError("max-files must be a whole number, 0 for no limit")
            limit = int(text(MAX_FILES_ENV))
        return cls(strictness, inline, limit)


# --------------------------------------------------------------------------- #
# the file
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FileSettings:
    """What ``review:`` in the trusted-base file says about noise, apart from the threshold. ``None``: not set."""

    ignore: tuple[str, ...] = ()
    inline_comments: bool | None = None
    max_files: int | None = None
    #: ``{rule stable_key: severity}`` as the file wrote it, in the vocabulary (``p0``, ``p1`` or ``p2``).
    rule_severity: Mapping[str, str] = field(default_factory=dict)
    #: Problems found while reading, said out loud rather than dropped.
    notes: tuple[str, ...] = ()


def parse_file_settings(content: str, *, source: str = _CONFIG) -> FileSettings:
    """The noise settings of the trusted-base ``review.yaml`` text. Never raises: a bad value is a note and is not applied."""

    if not content.strip():
        return FileSettings()
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError:
        return FileSettings(notes=(f"{source} could not be parsed, so its noise settings were not applied",))
    section = raw.get("review") if isinstance(raw, Mapping) else None
    if not isinstance(section, Mapping):
        return FileSettings()

    notes: list[str] = []
    ignore = _globs(section.get("ignore"), notes) if "ignore" in section else ()

    inline: bool | None = None
    if "inline_comments" in section:
        if isinstance(section["inline_comments"], bool):
            inline = section["inline_comments"]
        else:
            notes.append("review.inline_comments must be true or false; not applied")

    limit: int | None = None
    if "max_files" in section:
        value = section["max_files"]
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_FILES_CEILING:
            limit = value
        else:
            notes.append("review.max_files must be a whole number, 0 for no limit; not applied")

    severities: dict[str, str] = {}
    if "rules" in section:
        severities = _rule_severities(section["rules"], notes)
    return FileSettings(ignore, inline, limit, severities, tuple(notes))


def _globs(raw: object, notes: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    rows: Sequence[object] = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    if not rows:
        notes.append("review.ignore must be a path glob or a list of them; not applied")
        return ()
    kept: list[str] = []
    for index, item in enumerate(rows[:MAX_IGNORE]):
        if not isinstance(item, str):
            notes.append(f"review.ignore[{index}] must be a path glob; skipped")
            continue
        try:
            kept.append(_safe_path(item, field=f"review.ignore[{index}]"))
        except ValueError:
            notes.append(f"review.ignore[{index}] must be a repository-relative glob; skipped")
    if len(rows) > MAX_IGNORE:
        notes.append(f"review.ignore keeps the first {MAX_IGNORE} globs; the rest were skipped")
    return tuple(dict.fromkeys(kept))


def _rule_severities(raw: object, notes: list[str]) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        notes.append("review.rules must map a rule key to {severity: p0|p1|p2}; not applied")
        return {}
    found: dict[str, str] = {}
    for key, row in raw.items():
        label = f"review.rules.{key}"
        level = row.get("severity") if isinstance(row, Mapping) else None
        stray = sorted(str(name) for name in row if name != "severity") if isinstance(row, Mapping) else []
        if stray:
            notes.append(f"{label}: unknown field {stray[0]!r}; only severity is read")
        if not isinstance(level, str) or level.strip().lower() not in SEVERITIES:
            notes.append(f"{label}.severity must be p0, p1 or p2; not applied")
            continue
        found[str(key)] = level.strip().lower()
    return found


# --------------------------------------------------------------------------- #
# the policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NoisePolicy:
    """Everything the noise controls decide, resolved for one review. The defaults are the behavior without any setting."""

    threshold: str = DEFAULT_FINDING_THRESHOLD
    threshold_source: str = "default"
    inline: bool = True
    max_files: int = DEFAULT_MAX_FILES
    ignore: tuple[str, ...] = ()
    #: ``{rule ref: severity}`` for the rules the file gave a severity.
    rule_severity: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    # ----- what a finding of each severity gets ---------------------------- #

    def tier(self, severity: str) -> str:
        """``row``, ``listed`` or ``held`` for a finding of *severity*.

        ``held`` is exactly what ``criticality.surfaced`` refuses under the threshold (P0 is never refused). Of what
        is surfaced, a P2 is ``listed`` (collapsed, never inline) unless the threshold is ``suggestions``, which
        lifts it to a row like the rest.
        """

        if not surfaced(severity if severity in SEVERITIES else None, self.threshold):  # type: ignore[arg-type]
            return "held"
        if severity == "p2" and self.threshold != "suggestions":
            return "listed"
        return "row"

    def inline_eligible(self, severity: str) -> bool:
        """Whether a finding of *severity* gets an inline comment at all."""

        return self.inline and self.tier(severity) == "row"

    # ----- words ------------------------------------------------------------ #

    @property
    def strictness(self) -> int | None:
        return THRESHOLD_STRICTNESS.get(self.threshold)

    @property
    def label(self) -> str:
        """``strictness 3`` for a threshold that has a number, else ``threshold critical``."""

        number = self.strictness
        return f"strictness {number}" if number is not None else f"threshold {self.threshold}"

    # ----- paths ------------------------------------------------------------ #

    def ignores(self, path: str) -> bool:
        if not self.ignore or not path:
            return False
        try:
            return path_matches(path, self.ignore)
        except ValueError:
            return False  # an odd path is never a reason to hide a finding

    def view_code(self) -> str:
        """What the summary shows because of these settings, as the state keeps it; ``""`` for the defaults.

        A re-run on the same commit compares it with the last summary's, so a changed threshold or ``inline_comments``
        rewrites the comment instead of leaving the old one; the budget notice rebuilds the presentation from it.
        """

        if self.threshold == DEFAULT_FINDING_THRESHOLD and self.inline:
            return ""
        return f"{self.threshold}:{'inline' if self.inline else 'summary'}"

    @classmethod
    def from_view(cls, code: str) -> NoisePolicy:
        threshold, _, mode = code.partition(":")
        if threshold in FINDING_THRESHOLDS and mode in {"inline", "summary"}:
            return cls(threshold=threshold, threshold_source="the last review", inline=mode == "inline")
        return cls()


def _source_of(origin_layer: str) -> str:
    return _CONFIG if origin_layer == "trusted_base_repository" else "organization policy"


def resolve_policy(
    *,
    config: object,
    triage: EffectiveTriagePolicy | None,
    rules: Sequence[ReviewRule],
    inputs: NoiseInputs | None = None,
) -> NoisePolicy:
    """The policy for one review from the Snapshot's trusted-base config, its triage policy and the workflow's inputs.

    *config* is the Snapshot's ``TrustedBaseDocument`` for ``.lemoncrow/review.yaml`` (its ``content`` is the base's
    bytes, never the pull request's), *triage* the policy resolved from it and the operator layers, *rules* the rules
    in force. An absent piece is the default.
    """

    given = inputs or NoiseInputs()
    parsed = parse_file_settings(str(getattr(config, "content", "") or ""))
    notes = list(parsed.notes)

    threshold, source = DEFAULT_FINDING_THRESHOLD, "default"
    if triage is not None:
        notes.extend(triage.errors)
        if triage.threshold_origin.layer != "built_in_default":
            threshold, source = triage.finding_threshold, _source_of(triage.threshold_origin.layer)
    if source == "default" and given.strictness is not None:
        threshold, source = STRICTNESS_THRESHOLDS[given.strictness], "the strictness input"

    inline = parsed.inline_comments if parsed.inline_comments is not None else given.inline_comments
    limit = parsed.max_files if parsed.max_files is not None else given.max_files

    by_ref: dict[str, str] = {}
    for key, level in parsed.rule_severity.items():
        matching = [rule for rule in rules if rule.stable_key == key]
        if not matching:
            notes.append(f"review.rules.{key}: no rule has this key; not applied")
            continue
        for rule in matching:
            effective = level
            if level == "p0" and rule.severity != "critical":
                # Only a critical rule is a gate; raising an advisory (imported) one stops at P1.
                effective = "p1"
                notes.append(f"review.rules.{key}: only a critical rule can be P0; this advisory rule is P1")
            by_ref[rule.ref] = effective

    return NoisePolicy(
        threshold=threshold,
        threshold_source=source,
        inline=True if inline is None else inline,
        max_files=DEFAULT_MAX_FILES if limit is None else limit,
        ignore=parsed.ignore,
        rule_severity=by_ref,
        notes=tuple(dict.fromkeys(notes)),
    )


def with_ignored_paths(governance: ReviewGovernance, policy: NoisePolicy, config: object) -> ReviewGovernance:
    """*governance* plus an ``ai_denied`` content-access row for every ``ignore`` glob.

    The existing mechanism for keeping a path from the model: such a row stops the path from being a target of a
    model investigation, from reaching a prompt as an excerpt and from the repository context pack (callers and
    tests of other files included), and it joins the policy digest, so a changed ``ignore`` re-reviews once.
    """

    if not policy.ignore:
        return governance
    state = str(getattr(config, "state", "present") or "present")
    where = str(getattr(config, "source", "") or "") or "trusted-base"
    path = str(getattr(config, "path", "") or _CONFIG)
    row = ContentAccessRule(
        mode="ai_denied",
        path_scope=policy.ignore,
        origin=_IGNORE_ORIGIN,
        trust_class="trusted_base_repository",
        source_identity=f"{state}:{where}:{path}",
        source_location=path,
    )
    return replace(governance, content_access=(*governance.content_access, row))


# --------------------------------------------------------------------------- #
# severity
# --------------------------------------------------------------------------- #


def severity_with_overrides(
    category: str, *, impact: str, verified: bool, rule_refs: Sequence[str], overrides: Mapping[str, str]
) -> str:
    """A finding's severity, honoring the severities the file gave to the rules it cites.

    A finding that cites no overridden rule gets exactly what ``severity_for`` says. One that cites overridden rules
    gets the most severe of their severities and, when it cites other rules too, of what those earn by the ordinary
    ladder. A model's wording is never read, and P0 only ever comes from a critical rule: ``resolve_policy`` already
    stopped an advisory rule's override at P1.
    """

    named = [ref for ref in rule_refs if ref in overrides]
    rest = [ref for ref in rule_refs if ref not in overrides]
    base = severity_for(category, impact=impact, verified=verified, rule_cited=cites_critical_rule(tuple(rest)))
    if not named:
        return base
    levels = [overrides[ref] for ref in named]
    if rest:
        levels.append(base)
    return min(levels, key=lambda level: SEVERITY_RANK.get(level, 1))


def capped(severity: str, ceiling: str) -> str:
    """*severity* no more severe than *ceiling* (``p0`` is the most severe)."""

    return severity if SEVERITY_RANK.get(severity, 1) >= SEVERITY_RANK.get(ceiling, 1) else ceiling


__all__ = [
    "DEFAULT_MAX_FILES",
    "THRESHOLD_STRICTNESS",
    "FileSettings",
    "NoiseInputs",
    "NoisePolicy",
    "capped",
    "parse_file_settings",
    "resolve_policy",
    "severity_with_overrides",
    "with_ignored_paths",
]
