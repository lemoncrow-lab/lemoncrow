"""Read burden: how many parts a person is asked to read again when a coding agent pushes again.

OWNER-14 asks that Review be measured against the two rules every other review tool uses, on real data, so the
number is not a claim. For each revision of a real, public, agent-written pull request after the first, this module
counts what a reviewer who read the whole of the previous revision is asked to read under three rule sets:

``file level``
    GitHub's "Viewed" semantics. A file viewed earlier is unmarked when its diff changes; a changed or new file is one
    part to read.
``hunk level``
    What Tidewave and tuicr do. A hunk whose content is not among the previous revision's hunks of that file is one
    part to read.
``LemonCrow``
    The engine's own parts (symbols, hunks, dependency components) after :func:`revisions.reconcile` carried what it
    could prove unchanged, with the moved-symbol carry of :func:`revisions.detect_symbol_moves`, and the queue's own
    answer categories. Nothing here re-implements those rules: every revision is captured into a real git repository
    and derived through the packet, ``derive_units`` and ``derive_review_targets`` the product uses.

**The assumption.** The reviewer read every part of revision *n-1* and judged it ``reviewed``, at the fingerprint they
saw. That is the most favourable honest reading for every rule set alike: nothing is left over from earlier rounds,
so each count is exactly the cost of the push itself.

**What a revision is** and how the corpus was collected is in ``evals/review/read_burden/README.md``. The corpus is
never fetched here; this module only reads what the private corpus collector stored.

The LemonCrow count takes the routine batch off the parts to read: :func:`split_routine_batch` asks P11-2's own
proof (``since_review.since_review_proofs``) which reopened parts are only formatting, a comment or a lone rename.
"""

from __future__ import annotations

import difflib
import gzip
import hashlib
import json
import re
import statistics
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, cast

from lemoncrow.pro.capabilities.review.config_names import ConfigFile
from lemoncrow.pro.capabilities.review.impact import symbol_windows
from lemoncrow.pro.capabilities.review.models import ChangedFile, ReviewPacket
from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewRevision, ReviewUnit
from lemoncrow.pro.capabilities.review.targets import ReviewTarget

CORPUS_FORMAT = 1

#: Licences a public corpus may hold; the same set as ``evaluation/corpus.PERMISSIVE_LICENSES``.
PERMISSIVE = frozenset({"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"})

#: Corpus size cap, in stored bytes: every pull request file, the licence texts and the manifest.
CORPUS_MAX_BYTES = 10_000_000

#: Decompressed size cap of one stored record (the collector keeps at most 600 KB of text per pull request; the
#: largest stored record is 660 KB). A gzip bomb is refused rather than inflated.
RECORD_MAX_BYTES = 4_000_000

#: Where each repository's licence text is kept, next to the pull request records.
LICENCES_FILE = "licences.json.gz"

#: What a fetcher needs to rebuild the corpus from GitHub: for every pull request its record without the file texts
#: (every revision's base and head commit, every changed file's path and git blob id), and where each repository's licence
#: files are. Derived from the stored corpus by :func:`build_sources`, so it carries no third-party text. It is kept out
#: of :func:`corpus_bytes`, so the stored size the report prints does not depend on it.
SOURCES_FILE = "sources.json"
SOURCES_FORMAT = 1

#: Author login of a pull request -> how it counts as an agent. The collector checks every entry against the author's
#: GraphQL ``__typename`` (``Bot``); the loader checks that each record names a login from this table and the agent
#: the table gives for it.
AGENT_BY_LOGIN = {
    "Copilot": "GitHub Copilot coding agent",
    "copilot-swe-agent": "GitHub Copilot coding agent",
    "devin-ai-integration": "Devin",
    "claude": "Claude Code (claude[bot])",
    "google-labs-jules": "Jules",
    "cursor": "Cursor background agent",
}
_IDENTIFIED_LOGIN = re.compile(r"account '([^']+)'")

REVIEW_ID = "read-burden"
REVIEWER_ID = "reviewer"


class CorpusError(ValueError):
    """The stored corpus is malformed, tampered with, or outside its licence or size bounds."""


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FileChange:
    path: str
    old_path: str
    status: str
    base_blob: str | None
    head_blob: str | None


@dataclass(frozen=True)
class Revision:
    index: int
    head_sha: str
    base_sha: str
    observed_at: str
    observed_by: tuple[str, ...]
    files: tuple[FileChange, ...]


@dataclass(frozen=True)
class PullRequest:
    id: str
    repository: str
    number: int
    url: str
    title: str
    license: str
    attribution: str
    agent: str
    identified_by: str
    revisions: tuple[Revision, ...]
    blobs: Mapping[str, str]


def git_blob_id(text: str) -> str:
    raw = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


# --------------------------------------------------------------------------- #
# licence texts
# --------------------------------------------------------------------------- #

#: Phrases that must all be present (and those that must be absent) in the text of each permissive licence, after
#: lower-casing and collapsing whitespace. Enough to tell the five licences apart; not a legal classifier.
_LICENCE_SIGNATURES: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "MIT": (
        ("permission is hereby granted, free of charge", "the above copyright notice and this permission notice"),
        (),
    ),
    "Apache-2.0": (
        ("apache license", "version 2.0, january 2004", "terms and conditions for use, reproduction, and distribution"),
        (),
    ),
    "BSD-2-Clause": (
        ("redistribution and use in source and binary forms", "redistributions of source code must retain"),
        ("neither the name", "endorse or promote"),
    ),
    "BSD-3-Clause": (
        (
            "redistribution and use in source and binary forms",
            "redistributions of source code must retain",
            "neither the name",
        ),
        (),
    ),
    "ISC": (("permission to use, copy, modify", "for any purpose with or without fee"), ()),
}


def licence_matches(spdx: str, text: str) -> bool:
    """True when *text* is the text of the SPDX licence *spdx* (one of :data:`PERMISSIVE`)."""

    signature = _LICENCE_SIGNATURES.get(spdx)
    if signature is None:
        return False
    required, forbidden = signature
    flat = " ".join(text.lower().split())
    return all(phrase in flat for phrase in required) and not any(phrase in flat for phrase in forbidden)


@dataclass(frozen=True)
class RepositoryLicence:
    """One repository's licence files at the commit the corpus was collected from."""

    repository: str
    spdx: str
    sha: str
    files: Mapping[str, str]
    """``path -> text`` of the LICENSE and, where the repository has one, NOTICE file."""


def parse_licences(raw: Mapping[str, Any]) -> dict[str, RepositoryLicence]:
    """Validate the stored licence texts: present, non-empty, blob ids intact, and the text matches the SPDX id."""

    out: dict[str, RepositoryLicence] = {}
    try:
        for repository, row in raw["repositories"].items():
            spdx = str(row["license"])
            sha = str(row["sha"])
            if spdx not in PERMISSIVE:
                raise CorpusError(f"{repository}: licence {spdx!r} is not permissive")
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise CorpusError(f"{repository}: licence texts name no commit")
            files: dict[str, str] = {}
            for path, item in row["files"].items():
                text = str(item["text"])
                if not text.strip():
                    raise CorpusError(f"{repository}: {path} is empty")
                if git_blob_id(text) != str(item["blob"]):
                    raise CorpusError(f"{repository}: {path} does not match its blob id")
                files[str(path)] = text
            if not any(
                licence_matches(spdx, text) for path, text in files.items() if not Path(path).name.startswith("NOTICE")
            ):
                raise CorpusError(f"{repository}: no stored licence file is the text of {spdx}")
            out[str(repository)] = RepositoryLicence(str(repository), spdx, sha, files)
    except (KeyError, TypeError, AttributeError) as exc:
        raise CorpusError(f"malformed licences file: {exc!r}") from exc
    return out


def _read_gzip_json(path: Path, limit: int) -> Any:
    """The JSON in a gzip file, inflating at most *limit* bytes (a bomb is refused, not expanded)."""

    with gzip.GzipFile(filename=str(path)) as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise CorpusError(f"{path.name} inflates past {limit} bytes")
    return json.loads(raw)


def load_licences(directory: Path) -> dict[str, RepositoryLicence]:
    path = directory / LICENCES_FILE
    if not path.exists():
        raise CorpusError(f"{LICENCES_FILE} is missing: every repository's licence text must be stored")
    return parse_licences(_read_gzip_json(path, RECORD_MAX_BYTES))


def parse_pull_request(raw: Mapping[str, Any]) -> PullRequest:
    """Validate one stored pull request: licence, attribution, agent identity and every blob's git id."""

    try:
        pr_id = str(raw["id"])
        licence = str(raw["license"])
        if licence not in PERMISSIVE:
            raise CorpusError(f"{pr_id}: licence {licence!r} is not permissive")
        if not str(raw["attribution"]).strip():
            raise CorpusError(f"{pr_id}: no attribution line")
        agent = raw["agent"]
        if not str(agent["name"]).strip() or not str(agent["identified_by"]).strip():
            raise CorpusError(f"{pr_id}: agent authorship is not recorded")
        found = _IDENTIFIED_LOGIN.search(str(agent["identified_by"]))
        if found is None or found.group(1) not in AGENT_BY_LOGIN:
            raise CorpusError(f"{pr_id}: identified_by names no login from the agent table")
        if AGENT_BY_LOGIN[found.group(1)] != str(agent["name"]):
            raise CorpusError(f"{pr_id}: login {found.group(1)!r} does not map to agent {agent['name']!r}")
        blobs = {str(key): str(value) for key, value in raw["blobs"].items()}
        for key, text in blobs.items():
            if git_blob_id(text) != key:
                raise CorpusError(f"{pr_id}: blob {key} does not match its text")
        revisions = tuple(
            Revision(
                index=int(item["index"]),
                head_sha=str(item["head_sha"]),
                base_sha=str(item["base_sha"]),
                observed_at=str(item["observed_at"]),
                observed_by=tuple(str(reason) for reason in item["observed_by"]),
                files=tuple(
                    FileChange(
                        path=str(row["path"]),
                        old_path=str(row["old_path"] or ""),
                        status=str(row["status"]),
                        base_blob=row["base_blob"],
                        head_blob=row["head_blob"],
                    )
                    for row in item["files"]
                ),
            )
            for item in raw["revisions"]
        )
        if len(revisions) < 2:
            raise CorpusError(f"{pr_id}: fewer than two revisions")
        for revision in revisions:
            for change in revision.files:
                for blob in (change.base_blob, change.head_blob):
                    if blob is not None and blob not in blobs:
                        raise CorpusError(f"{pr_id}: revision {revision.index} names a blob that is not stored")
        return PullRequest(
            id=pr_id,
            repository=str(raw["repository"]),
            number=int(raw["number"]),
            url=str(raw["url"]),
            title=str(raw["title"]),
            license=licence,
            attribution=str(raw["attribution"]),
            agent=str(agent["name"]),
            identified_by=str(agent["identified_by"]),
            revisions=revisions,
            blobs=blobs,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, CorpusError):
            raise
        raise CorpusError(f"malformed pull request record: {exc!r}") from exc


def load_corpus(directory: Path) -> tuple[PullRequest, ...]:
    """Every stored pull request, in id order. Raises :class:`CorpusError` over the size cap or a bad record."""

    files = sorted((directory / "prs").glob("*.json.gz"))
    if corpus_bytes(directory) > CORPUS_MAX_BYTES:
        raise CorpusError(f"corpus is {corpus_bytes(directory)} bytes, over the {CORPUS_MAX_BYTES} cap")
    licences = load_licences(directory)
    out = [parse_pull_request(_read_gzip_json(path, RECORD_MAX_BYTES)) for path in files]
    for pr in out:
        stored = licences.get(pr.repository)
        if stored is None:
            raise CorpusError(f"{pr.id}: no licence text is stored for {pr.repository}")
        if stored.spdx != pr.license:
            raise CorpusError(f"{pr.id}: record says {pr.license} but the stored licence text is {stored.spdx}")
    return tuple(sorted(out, key=lambda pr: pr.id))


def corpus_bytes(directory: Path) -> int:
    """Stored size of the corpus: every pull request file, the licence texts and the manifest."""

    total = sum(path.stat().st_size for path in (directory / "prs").glob("*.json.gz"))
    for name in ("manifest.json", LICENCES_FILE):
        extra = directory / name
        total += extra.stat().st_size if extra.exists() else 0
    return total


# --------------------------------------------------------------------------- #
# capturing a revision through the real engine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Capture:
    """One revision as the engine derives it: packet, new-side texts, units and the (unmarked) targets."""

    revision: Revision
    pr_id: str
    packet: ReviewPacket
    packet_wire: dict[str, Any]
    blobs_new: Mapping[str, str]
    units: tuple[ReviewUnit, ...]
    targets: tuple[ReviewTarget, ...]
    head_text: Mapping[str, str]
    """New-side text of each changed file, straight from the corpus (used by the independent oracle only)."""
    base_text: Mapping[str, str]
    """Base-side text of each changed file, keyed by its new path (oracle only)."""


def _commit_files(repo: Any, files: Mapping[str, str], parents: Sequence[Any], when: int, ref: str | None) -> Any:
    import pygit2
    from pygit2.enums import FileMode

    index = pygit2.Index()
    for path, text in sorted(files.items()):
        index.add(pygit2.IndexEntry(path, repo.create_blob(text.encode("utf-8")), FileMode.BLOB))
    tree = index.write_tree(repo)
    signature = pygit2.Signature("LemonCrow Read Burden", "read-burden@lemoncrow.invalid", when, 0)
    return repo.create_commit(ref, signature, signature, "revision", tree, list(parents))


def capture_revision(pr: PullRequest, revision: Revision, root: Path) -> Capture:
    """Capture *revision* into a fresh git repository at *root* and derive its units and targets.

    Only the changed files exist in the repository: the base commit holds each file's base text, the head commit its
    head text. Nothing after the packet is simulated: the packet, units, answer stamps and targets are the ones
    ``record_revision`` and the ``/targets`` reader build.
    """

    import pygit2

    from lemoncrow.pro.capabilities.review.dependencies import derive_dependency_units
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.packet import attach_changed_symbols, build_review_packet_with_blobs
    from lemoncrow.pro.capabilities.review.revisions import compute_frontier
    from lemoncrow.pro.capabilities.review.targets import derive_review_targets
    from lemoncrow.pro.capabilities.review.units import derive_units, stamp_answers

    base_files: dict[str, str] = {}
    head_files: dict[str, str] = {}
    base_by_new_path: dict[str, str] = {}
    for change in revision.files:
        if change.base_blob is not None:
            base_files[change.old_path or change.path] = pr.blobs[change.base_blob]
            base_by_new_path[change.path] = pr.blobs[change.base_blob]
        if change.head_blob is not None:
            head_files[change.path] = pr.blobs[change.head_blob]
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    base = _commit_files(repo, base_files, (), 1_700_000_000, None)
    head = _commit_files(repo, head_files, (base,), 1_700_000_001, "refs/heads/main")
    rng = resolve_rev_range(root, base=str(base), head=str(head))
    build = build_review_packet_with_blobs(
        root,
        rng,
        store_root=root / ".store",
        with_impact=False,
        with_provenance=False,
        with_patch_text=True,
        unbounded_patch_text=True,
    )
    build = attach_changed_symbols(root, build)
    packet = build.packet
    units = derive_units(packet, build.blobs.new, old_blobs=build.blobs.old)
    changed = tuple(item.path for item in packet.files)
    dependency_units = derive_dependency_units(
        build.blobs.old,
        build.blobs.new,
        changed,
        base_expected=tuple(item.path for item in packet.files if item.status != "added"),
        head_expected=tuple(item.path for item in packet.files if item.status != "deleted"),
    )
    units = stamp_answers((*units, *dependency_units), packet)
    wire = packet.to_dict()
    frontier = compute_frontier(REVIEW_ID, REVIEWER_ID, _revision_row(revision, pr.id), units, ())
    targets = derive_review_targets(units, frontier.entries, wire)
    return Capture(
        revision=revision,
        pr_id=pr.id,
        base_text=base_by_new_path,
        packet=packet,
        packet_wire=wire,
        blobs_new=dict(build.blobs.new),
        units=units,
        targets=targets,
        head_text=head_files,
    )


def _revision_row(revision: Revision, pr_id: str = "") -> ReviewRevision:
    """The revision row the engine reads: the real diff base and head, so base movement is judged as in production."""

    return ReviewRevision(
        id=f"rev-{revision.index}",
        review_id=REVIEW_ID,
        revision_number=revision.index,
        range_mode="commit_range",
        base_sha=revision.base_sha,
        merge_base_sha=revision.base_sha,
        head_sha=revision.head_sha,
        tree_fingerprint="",
        packet_schema_version=1,
        # ``rebase_classification._patch_cache`` is keyed by (base, merge base, head, packet digest); the head sha is
        # unique per revision, and the digest names the pull request too so two corpora never share an entry.
        packet_sha256=hashlib.sha256(f"{pr_id}@{revision.head_sha}".encode()).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# what a change looks like to the two baseline rule sets
# --------------------------------------------------------------------------- #


def change_lines(patch: str) -> tuple[str, ...]:
    """The ``+``/``-`` lines of a hunk body, context dropped.

    Context and the ``@@`` header are left out on purpose: both move when something unrelated above them changes
    (a rebase, an edit elsewhere in the file), and a baseline that re-read a hunk for that would be easier to beat.
    This is the most favourable honest reading for the baselines.
    """

    return tuple(line for line in patch.split("\n") if line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- ")))


def _file_signature(item: ChangedFile, change: FileChange | None) -> tuple[Any, ...]:
    if item.hunks:
        return ("hunks", tuple(change_lines(hunk.patch) for hunk in item.hunks))
    # Binary, mode-only or pure-rename entries have no body: what identifies them is the content on each side.
    return (
        "bodiless",
        item.status,
        item.old_path or "",
        change.base_blob if change else None,
        change.head_blob if change else None,
    )


def _changes_by_path(revision: Revision) -> dict[str, FileChange]:
    return {change.path: change for change in revision.files}


def window_lines(patch: str) -> int:
    """Every body line of a hunk (context included, headers and "no newline" markers not): the window a reader opens."""

    return sum(1 for line in patch.split("\n") if line[:1] in (" ", "+", "-"))


@dataclass(frozen=True)
class BaselineRead:
    """What one baseline asks of a reviewer, counted three ways."""

    parts: int
    """Files (file level) or hunks (hunk level) to read."""
    lines: int
    """Their changed (``+``/``-``) lines."""
    window: int
    """Their whole body, context included: the most a reader can be asked to look at (an upper bound)."""
    files: frozenset[str]
    """Distinct files holding a part to read."""


def file_level_read(previous: Capture, current: Capture) -> BaselineRead:
    """Files whose diff changed since the previous revision, or that were not in it (GitHub "Viewed")."""

    before = {
        item.path: _file_signature(item, _changes_by_path(previous.revision).get(item.path))
        for item in previous.packet.files
    }
    current_changes = _changes_by_path(current.revision)
    changed = [
        item
        for item in current.packet.files
        if before.get(item.path) != _file_signature(item, current_changes.get(item.path))
    ]
    return BaselineRead(
        parts=len(changed),
        lines=sum(len(change_lines(hunk.patch)) for item in changed for hunk in item.hunks),
        window=sum(window_lines(hunk.patch) for item in changed for hunk in item.hunks),
        files=frozenset(item.path for item in changed),
    )


def hunk_level_read(previous: Capture, current: Capture) -> BaselineRead:
    """Hunks whose content is not among the previous revision's hunks of the same file (Tidewave, tuicr)."""

    before: dict[str, Counter[tuple[str, ...]]] = {}
    before_bodiless: dict[str, tuple[Any, ...]] = {}
    previous_changes = _changes_by_path(previous.revision)
    for item in previous.packet.files:
        if item.hunks:
            before[item.path] = Counter(change_lines(hunk.patch) for hunk in item.hunks)
        else:
            before_bodiless[item.path] = _file_signature(item, previous_changes.get(item.path))
    current_changes = _changes_by_path(current.revision)
    parts = lines = window = 0
    files: set[str] = set()
    for item in current.packet.files:
        if not item.hunks:
            if before_bodiless.get(item.path) != _file_signature(item, current_changes.get(item.path)):
                parts += 1
                files.add(item.path)
            continue
        remaining = Counter(before.get(item.path, Counter()))
        for hunk in item.hunks:
            changed = change_lines(hunk.patch)
            if remaining[changed] > 0:
                remaining[changed] -= 1
            else:
                parts += 1
                lines += len(changed)
                window += window_lines(hunk.patch)
                files.add(item.path)
    return BaselineRead(parts=parts, lines=lines, window=window, files=frozenset(files))


def file_level_parts(previous: Capture, current: Capture) -> int:
    return file_level_read(previous, current).parts


def hunk_level_parts(previous: Capture, current: Capture) -> int:
    return hunk_level_read(previous, current).parts


# --------------------------------------------------------------------------- #
# LemonCrow's rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _BlobStore:
    """The one method ``sources.local._symbol_body_digests`` asks of a store: the previous revision's frozen text."""

    previous: Mapping[str, str]

    def read_blob_artifact(self, review_id: str, revision_id: str) -> Mapping[str, str]:
        return self.previous


@dataclass(frozen=True)
class LemonCrowCount:
    to_read: tuple[ReviewTarget, ...]
    """Outstanding parts a person is asked to read (decisions and quick checks)."""
    to_confirm_in_one_step: tuple[ReviewTarget, ...]
    """Routine parts the person confirms together (P11-2): the product's ``since_review`` proof says so."""
    accepted_by_rule: tuple[ReviewTarget, ...]
    """Outstanding parts the queue lists with the rule that accepted them (generated files, lockfiles)."""
    unchanged_since_review: int
    """Parts carried as reviewed: not asked again."""
    total_parts: int
    reopened: Mapping[str, Reopened] = field(default_factory=dict)
    """Why each part a person is asked about (``to_read`` and the routine batch) is outstanding, by unit key."""
    base_moved: bool = False
    """The base branch moved between the two revisions, as ``classify_movement`` judges it."""


#: Reopen reasons that are not a ``reconcile`` reason code: no mark of the reviewer's could have decided these parts.
NEW_UNIT = "new_unit"
NEVER_A_TARGET = "never_a_target"
NO_REASON = "no_reason"


@dataclass(frozen=True)
class Reopened:
    """Why one outstanding part is outstanding."""

    category: str
    """A ``reconcile`` reason code (``content_changed``, ``hunks_renumbered``, ``symbol_renamed``...), or one of
    :data:`NEW_UNIT` (no such unit before), :data:`NEVER_A_TARGET` (the unit existed but was not a part to judge) and
    :data:`NO_REASON` (a marked unit with no recorded reason: not expected)."""
    identical: bool = False
    """The part's content is exactly content the reviewer was shown: for ``hunks_renumbered`` an already-seen hunk of
    that file, for ``never_a_target`` the same unit unchanged. Asked again for a reason other than a change."""


def _reopened(
    asked: Sequence[ReviewTarget], reason: Mapping[str, str], previous: Capture, current: Capture
) -> dict[str, Reopened]:
    """Why each of *asked* is outstanding: the ``reconcile`` reason recorded for its unit, else what the units say."""

    before = {unit.unit_key: unit for unit in previous.units}
    now = {unit.unit_key: unit for unit in current.units}
    judged = {target.unit_key for target in previous.targets}
    hunks_seen: dict[str, set[str]] = {}
    for unit in previous.units:
        if unit.kind == "hunk":
            hunks_seen.setdefault(unit.path, set()).add(unit.content_fingerprint)
    out: dict[str, Reopened] = {}
    for target in asked:
        key = target.unit_key
        code = reason.get(key)
        if code is not None:
            seen = now[key].content_fingerprint in hunks_seen.get(target.path, ())
            out[key] = Reopened(code, code == "hunks_renumbered" and seen)
        elif key not in before:
            out[key] = Reopened(NEW_UNIT)
        elif key not in judged:
            out[key] = Reopened(NEVER_A_TARGET, before[key].content_fingerprint == now[key].content_fingerprint)
        else:
            out[key] = Reopened(NO_REASON)
    return out


#: Quick check never batches a dependency part or a lockfile; ``frontend/src/review/quickCheck/quickCheckModel.ts``
#: ``NON_BATCH_PATH`` is the source and the gate compares the two.
NON_BATCH_PATH = (
    r"(?:^|/)(?:package-lock\.json|npm-shrinkwrap\.json|poetry\.lock|uv\.lock|pdm\.lock|Cargo\.lock|yarn\.lock|"
    r"pnpm-lock\.yaml|bun\.lockb?|go\.sum|Gemfile\.lock|composer\.lock|Pipfile\.lock|Podfile\.lock|mix\.lock|"
    r"gradle\.lockfile|packages\.lock\.json)$"
)

#: The proof rules the queue accepts for a part reopened since the reviewer's review
#: (``frontend/src/review/sinceReview.ts`` ``SINCE_REVIEW_RULES``; the gate compares the two). A re-hunked hunk is
#: batched on its own rule, ``since_review.REHUNKED_RULE``.
BATCHED_RULES = frozenset({"comment_only", "formatting_only", "rename_only"})


class _CorpusStore:
    """The reads ``since_review.since_review_proofs`` makes of a store, answered from two captured revisions.

    The proof is the product's own; this only supplies what the product's store would hold once the reviewer had
    judged every part of the previous revision and the current one had been reconciled against those judgments:
    the units and frozen new-side text of both revisions, the packet of each, the marks ``reconcile`` left, and the
    audit events (the reviewer's Looks good, and the reason ``reconcile`` reopened each mark).
    """

    def __init__(self, previous: Capture, current: Capture, reconciled: Any) -> None:
        from lemoncrow.pro.capabilities.review.session_models import ReviewMarkEvent
        from lemoncrow.pro.capabilities.review.validity import mark_event_reason

        self._rows = {
            row.id: row
            for row in (
                _revision_row(previous.revision, previous.pr_id),
                _revision_row(current.revision, current.pr_id),
            )
        }
        previous_id, current_id = (f"rev-{previous.revision.index}", f"rev-{current.revision.index}")
        self._units = {previous_id: previous.units, current_id: current.units}
        self._blobs = {previous_id: previous.blobs_new, current_id: current.blobs_new}
        self._packets = {
            self._rows[previous_id].packet_sha256: previous.packet_wire,
            self._rows[current_id].packet_sha256: current.packet_wire,
        }
        self.marks = tuple(reconciled.marks)
        fingerprints = {unit.unit_key: unit.content_fingerprint for unit in previous.units}
        events = [
            ReviewMarkEvent(
                id=f"judged-{target.unit_key}",
                review_id=REVIEW_ID,
                reviewer_id=REVIEWER_ID,
                unit_key=target.unit_key,
                revision_id=previous_id,
                reviewed_revision_id=previous_id,
                event_kind="judgment",
                from_state="",
                to_state="reviewed",
                content_fingerprint=fingerprints[target.unit_key],
                actor_type="human",
                created_at="1",
            )
            for target in previous.targets
            if target.unit_key in fingerprints
        ]
        # What ``sources.local.refresh`` writes for a reopened mark: the reason code, and the key it came from.
        reason = {key: code for _reviewer, key, code in reconciled.reasons}
        moves = {(move.reviewer_id, move.new_key): move for move in reconciled.moves}
        followed = {(move.old_key, move.new_key) for move in reconciled.moves}
        alias = {new: old for old, new in reconciled.aliases if (old, new) not in followed}
        for mark in reconciled.reopened:
            move = moves.get((mark.reviewer_id, mark.unit_key))
            prior = alias.get(mark.unit_key, "") or (move.old_key if move is not None else "")
            code = reason.get(mark.unit_key, "")
            if code and move is not None:
                text = mark_event_reason(code, symbol_move=(move.kind, move.previous_path, move.previous_symbol))
            elif code:
                text = mark_event_reason(code, renamed=bool(prior))
            else:
                text = "revision reconciliation changed the judgment state"
            events.append(
                ReviewMarkEvent(
                    id=f"reopened-{mark.unit_key}",
                    review_id=REVIEW_ID,
                    reviewer_id=mark.reviewer_id,
                    unit_key=mark.unit_key,
                    revision_id=current_id,
                    reviewed_revision_id=mark.reviewed_revision_id,
                    event_kind="reconciled",
                    from_state="reviewed",
                    to_state=mark.state,
                    content_fingerprint=mark.content_fingerprint,
                    previous_unit_key=prior,
                    reason=text,
                    created_at="2",
                )
            )
        self._events = tuple(events)

    def list_marks(self, review_id: str, *, reviewer_id: str = "local") -> tuple[ReviewMark, ...]:
        return tuple(mark for mark in self.marks if mark.reviewer_id == reviewer_id)

    def list_units(self, revision_id: str) -> tuple[ReviewUnit, ...]:
        return self._units.get(revision_id, ())

    def list_mark_events(self, review_id: str, *, reviewer_id: str = "local", **_: Any) -> tuple[Any, ...]:
        return tuple(event for event in self._events if event.reviewer_id == reviewer_id)

    def read_blob_artifact(self, review_id: str, revision_id: str) -> Mapping[str, str] | None:
        return self._blobs.get(revision_id)

    def list_revisions(self, review_id: str) -> tuple[ReviewRevision, ...]:
        return tuple(sorted(self._rows.values(), key=lambda row: row.revision_number))

    def get_revision(self, revision_id: str) -> ReviewRevision | None:
        return self._rows.get(revision_id)

    def read_packet_artifact(self, revision: ReviewRevision) -> bytes | None:
        packet = self._packets.get(revision.packet_sha256)
        return None if packet is None else gzip.compress(json.dumps(packet).encode("utf-8"))


def _corpus_config_files(current: Capture) -> Iterator[ConfigFile]:
    """The text files the product would search at the head, as far as the corpus has them.

    The corpus holds the files each revision changed and nothing else, so a config, manifest or script file that the
    pull request did not touch is not here: the search sees less than the product's (whole-tree) search and can only batch more.
    """

    for path, text in sorted(current.head_text.items()):
        payload = text.encode("utf-8")
        yield ConfigFile(path, len(payload), partial(bytes, payload))


def split_routine_batch(
    outstanding: Sequence[ReviewTarget], previous: Capture, current: Capture, reconciled: Any
) -> tuple[tuple[ReviewTarget, ...], tuple[ReviewTarget, ...]]:
    """``(to read, to confirm in one step)`` for the parts still outstanding after the carry.

    The product's own proof decides: ``since_review.since_review_proofs`` is asked, as ``api._since_review`` asks it,
    about the quick-check parts reopened since the reviewer's review and the unanswered hunks of a re-hunked file, and
    the queue (``frontend/src/review/queue.ts``) batches a part when the proof says routine under one of the rules it
    accepts. Everything else stays to read. The proof's own bound (``MAX_PARTS_PROVED``, in target order) applies to
    every reopened part, dependency and lockfile parts included, as in ``api._since_review``: the queue drops those
    from the batch only afterwards, so a part past the bound is read whichever parts came before it.

    What the corpus does not have, and what is assumed so that a part not proved is read:

    * **Hosted.** A hosted server never sends a proof and so never batches; the corpus models the local reviewer, whose
      server does.
    * **Frozen patches.** Quick check also takes a part out of the batch when its patch was read and is unusable.
      The corpus has every patch and reads none, so none is unusable.
    * **AI hints.** Quick check also holds a part with an anchored AI hint that raises a possible change to the result.
      The corpus has no AI review, so no part is held for one.
    * **The base-diff guard (``batch_guard``)** clears quick checks that were never reopened. It is not run here: a
      part that was not reopened since the review is read, which can only raise the count.
    """

    from lemoncrow.pro.capabilities.review.since_review import REHUNKED_RULE, since_review_proofs

    pattern = re.compile(NON_BATCH_PATH, re.IGNORECASE)
    candidates = [
        target
        for target in outstanding
        if target.answer.category == "quick_check"
        and (
            (target.state == "changed_since_review" and target.changed_since_mark)
            or (target.kind == "hunk" and target.state == "unreviewed")
        )
    ]

    def batchable(target: ReviewTarget) -> bool:
        # The queue refuses a dependency or lockfile part after the proof, so the proof's bound counted it first.
        return target.kind != "dependency" and not pattern.search(target.path)

    # lc-debt: batch_guard is not run, so a quick check never reopened is always read; run the server guard over the corpus's base diffs to batch those too
    if not any(batchable(target) for target in candidates):
        return tuple(outstanding), ()
    store = _CorpusStore(previous, current, reconciled)
    proofs = since_review_proofs(
        cast(Any, store),
        REVIEW_ID,
        REVIEWER_ID,
        f"rev-{current.revision.index}",
        candidates,
        lambda path: current.blobs_new.get(path),
        head_config_files=lambda _budget: _corpus_config_files(current),
    )

    def routine(target: ReviewTarget) -> bool:
        if not batchable(target):
            return False
        proof = proofs.get(target.target_id)
        if proof is None or not proof.routine:
            return False
        if target.kind == "hunk" and target.state == "unreviewed":
            return proof.rule == REHUNKED_RULE
        return proof.rule in BATCHED_RULES

    batched = {target.target_id for target in candidates if routine(target)}
    return (
        tuple(target for target in outstanding if target.target_id not in batched),
        tuple(target for target in outstanding if target.target_id in batched),
    )


def lemoncrow_parts(previous: Capture, current: Capture) -> LemonCrowCount:
    """What the engine asks of a reviewer who judged all of *previous* ``reviewed`` once *current* arrives."""

    from lemoncrow.pro.capabilities.review.rebase_classification import classify_movement
    from lemoncrow.pro.capabilities.review.revisions import compute_frontier, reconcile
    from lemoncrow.pro.capabilities.review.sources.local import _renames, _symbol_body_digests
    from lemoncrow.pro.capabilities.review.targets import derive_review_targets

    previous_row = _revision_row(previous.revision, previous.pr_id)
    current_row = _revision_row(current.revision, current.pr_id)
    movement = classify_movement(
        previous_row, previous.units, previous.packet_wire, current_row, current.units, current.packet_wire
    )
    fingerprints = {unit.unit_key: unit.content_fingerprint for unit in previous.units}
    marks = tuple(
        ReviewMark(
            review_id=REVIEW_ID,
            unit_key=target.unit_key,
            state="reviewed",
            reviewed_revision_id=previous_row.id,
            content_fingerprint=fingerprints[target.unit_key],
            reviewer_id=REVIEWER_ID,
        )
        for target in previous.targets
        if target.unit_key in fingerprints
    )
    texts = _symbol_body_digests(
        cast(Any, _BlobStore(previous.blobs_new)),
        REVIEW_ID,
        previous_row.id,
        previous.units,
        current.blobs_new,
        current.units,
    )
    result = reconcile(
        previous.units,
        current.units,
        marks,
        renames=_renames(current.packet),
        base_only=movement.base_only_unit_keys,
        body_digests=texts.digests,
        headers=texts.headers,
    )
    frontier = compute_frontier(REVIEW_ID, REVIEWER_ID, current_row, current.units, result.marks)
    targets = derive_review_targets(current.units, frontier.entries, current.packet_wire)
    outstanding = [target for target in targets if target.priority.outstanding]
    by_rule = tuple(target for target in outstanding if target.answer.category == "accepted_by_rule")
    asked = [target for target in outstanding if target.answer.category != "accepted_by_rule"]
    to_read, to_confirm = split_routine_batch(asked, previous, current, result)
    reason = {key: code for _reviewer, key, code in result.reasons}
    return LemonCrowCount(
        to_read=to_read,
        to_confirm_in_one_step=to_confirm,
        accepted_by_rule=by_rule,
        unchanged_since_review=len(targets) - len(outstanding),
        total_parts=len(targets),
        reopened=_reopened(asked, reason, previous, current),
        base_moved=movement.base_moved,
    )


# --------------------------------------------------------------------------- #
# the fail-open oracle
# --------------------------------------------------------------------------- #


def _normal(path: str, line: str) -> str:
    """A line as the oracle compares it: indentation kept for Python (it is syntax there), whitespace collapsed
    elsewhere. Nothing is stripped as a "comment": ``#`` is a preprocessor directive in C and ``*x`` is unpacking in
    Python, so a comment-only change is a change the reviewer is asked about like any other."""

    if path.endswith(".py"):
        return line.rstrip()
    return " ".join(line.split())


@dataclass(frozen=True)
class _Block:
    """A run of changed new-side lines, or a deletion's join point, that the reviewer has not seen."""

    lines: tuple[int, ...]
    """1-based new-side line numbers of the non-blank changed lines; for a deletion, the two lines it sits between."""
    text: tuple[str, ...]
    deletion: bool = False
    removed_from: tuple[tuple[int, ...], ...] = ()
    """For a deletion: per deleted line the old-side line numbers of the diff's ``-`` lines with that text."""
    old_only: bool = False
    """A deletion only the part owning its ``-`` line covers (the base text a replacement took away)."""
    alternatives: tuple[_Block, ...] = ()
    """The same insertion aligned one or more lines up or down, where a repeated line makes the alignment ambiguous."""


def _seen_text(previous: Capture, current: Capture, path: str, old_path: str | None) -> str:
    """What the reviewer has of *path*: the previous revision's head when the file was in it, else the base text."""

    for name in (path, old_path):
        if name and name in previous.head_text:
            return previous.head_text[name]
    return current.base_text.get(path, "")


def _changes_since_seen(
    previous: Capture, current: Capture
) -> tuple[dict[str, list[_Block]], dict[str, list[tuple[str, ...]]]]:
    """Per file of *current*, the changed new-side runs the reviewer has not seen, and per file the runs that left it.

    ``seen`` is exact text, not a count of lines: a file whose head text equals what the reviewer saw has nothing to
    read, however its diff moved with the base. Otherwise the new text is aligned to the seen text line by line
    (``difflib``, no junk heuristic) and every inserted or replaced non-blank line that is also an added line of the
    *current diff* is required (a line the diff does not show is base drift, not the agent's). A deletion is required
    when the diff has a changed line at its join point. Blank lines are not required.
    """

    required: dict[str, list[_Block]] = {}
    removed: dict[str, list[tuple[str, ...]]] = {}
    in_packet = {item.path for item in current.packet.files}
    for path, text in previous.head_text.items():
        if path not in in_packet:
            # The file left the diff (deleted, or reverted to the base): its text is no longer in this revision.
            removed.setdefault(path, []).append(
                tuple(x for x in (_normal(path, line) for line in text.splitlines()) if x)
            )
    for item in current.packet.files:
        head = current.head_text.get(item.path)
        seen = _seen_text(previous, current, item.path, item.old_path)
        if head is None:
            # Deleted file: its text left the diff entirely; a copy of it elsewhere is a relocation, not new code.
            removed.setdefault(item.path, []).append(
                tuple(x for x in (_normal(item.path, line) for line in seen.splitlines()) if x)
            )
            continue
        if seen == head:
            continue
        diffset, minus = _diff_lines(item)
        earlier = next((f for f in previous.packet.files if f.path in (item.path, item.old_path)), None)
        minus_before = _diff_lines(earlier)[1] if earlier is not None else {}
        base_moved = previous.revision.base_sha != current.revision.base_sha
        in_base = Counter(
            _normal(item.path, line)
            for line in current.base_text.get(item.path, current.base_text.get(item.old_path or "", "")).splitlines()
        )
        old_lines = [_normal(item.path, line) for line in seen.splitlines()]
        new_lines = [_normal(item.path, line) for line in head.splitlines()]
        in_seen, in_head = Counter(old_lines), Counter(new_lines)
        changed_windows = [
            (window.start_line, window.end_line)
            for window in symbol_windows(head, item.path)
            if any(window.start_line <= line <= window.end_line for line in diffset)
        ]
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            gone = tuple(x for x in old_lines[i1:i2] if x)
            if gone:
                removed.setdefault(item.path, []).append(gone)
            if tag == "replace":
                # Text the diff newly shows as removed is base text this revision replaced, and only the part that owns
                # its ``-`` line shows it: the new line beside it is another part's. On one base that is a ``-`` line
                # at a base position the diff the reviewer saw did not remove (so a deletion that moved between two
                # copies of the same text counts, and a line the agent added is not credited to a twin). When the base
                # moved the positions are not comparable, so there are only the ``-`` lines of that text to count.
                fresh = tuple(
                    (line, tuple(minus[line]))
                    for line in gone
                    if minus.get(line)
                    and (
                        len(minus[line]) > len(minus_before.get(line, ()))
                        if base_moved
                        else set(minus[line]) - set(minus_before.get(line, ()))
                    )
                )
                if fresh:
                    required.setdefault(item.path, []).append(
                        _Block(
                            (j1, j1 + 1),
                            tuple(line for line, _ in fresh),
                            deletion=True,
                            removed_from=tuple(homes for _, homes in fresh),
                            old_only=True,
                        )
                    )
            if tag in ("insert", "replace"):
                block = _insertion(new_lines, diffset, j1, j2, slide=tag == "insert")
                if block.lines:
                    required.setdefault(item.path, []).append(block)
            if tag == "delete":
                homes = tuple(tuple(minus.get(line, ())) for line in gone)
                if any(homes):
                    required.setdefault(item.path, []).append(
                        _Block((j1, j1 + 1), gone, deletion=True, removed_from=tuple(h for h in homes if h))
                    )
                # A line the diff does not show as removed that the reviewer saw more often than the head or the base
                # has it is the agent's own, added in an earlier revision and deleted now: nothing in this diff owns
                # it, so the place it left must be covered. That is so when the place is a changed line or lies in a
                # definition the diff changes; elsewhere the change went back to the base and nothing is left to read.
                own = tuple(
                    line
                    for line, home in zip(gone, homes, strict=True)
                    if not home and in_seen[line] > max(in_head[line], in_base[line])
                )
                inside = any(low <= j1 <= high or low <= j1 + 1 <= high for low, high in changed_windows)
                if own and (j1 in diffset or j1 + 1 in diffset or inside):
                    required.setdefault(item.path, []).append(_Block((j1, j1 + 1), own, deletion=True))
    return required, removed


def _insertion(new_lines: list[str], diffset: set[int], start: int, end: int, *, slide: bool) -> _Block:
    """The required lines of an insertion at ``new_lines[start:end]`` (0-based), plus its equally valid alignments.

    A pure insertion next to a repeated line (``}`` and a blank) can be aligned several ways; each is a correct diff, so
    a part covering any one of them covers the insertion.
    """

    def make(low: int, high: int) -> _Block:
        lines = tuple(n + 1 for n in range(low, high) if new_lines[n] and n + 1 in diffset)
        return _Block(lines, tuple(new_lines[n - 1] for n in lines))

    options: list[_Block] = []
    if slide:
        low, high = start, end
        while low > 0 and new_lines[low - 1] == new_lines[high - 1]:
            low, high = low - 1, high - 1
            options.append(make(low, high))
        low, high = start, end
        while high < len(new_lines) and new_lines[low] == new_lines[high]:
            low, high = low + 1, high + 1
            options.append(make(low, high))
    primary = make(start, end)
    return _Block(primary.lines, primary.text, alternatives=tuple(options))


def _diff_lines(item: ChangedFile) -> tuple[set[int], dict[str, list[int]]]:
    """The current diff's changed lines: new-side numbers of its ``+`` lines, and ``text -> old-side numbers`` of its
    ``-`` lines. Context lines are not changes, and neither are the join points ``new_ranges`` adds around a deletion.
    """

    added: set[int] = set()
    removed: dict[str, list[int]] = {}
    for hunk in item.hunks:
        old, new = hunk.old_start, hunk.new_start
        for line in hunk.patch.split("\n"):
            marker = line[:1]
            if marker == "+":
                added.add(new)
                new += 1
            elif marker == "-":
                text = _normal(item.path, line[1:])
                if text:
                    removed.setdefault(text, []).append(old)
                old += 1
            elif marker == " ":
                old += 1
                new += 1
    return added, removed


def _span_covers(target: ReviewTarget, line: int) -> bool:
    """Is new-side *line* inside one of the target's changed spans (a file or dependency part with none covers all)?"""

    if target.kind in ("file", "dependency") and not target.spans:
        return True
    return any(span.side == "new" and span.start_line <= line <= span.end_line for span in target.spans)


def _old_covers(target: ReviewTarget, line: int) -> bool:
    return any(span.side == "old" and span.start_line <= line <= span.end_line for span in target.spans)


def _block_covered(options: tuple[_Block, ...], targets: Sequence[ReviewTarget]) -> bool:
    """Is a change covered under at least one of its equally valid alignments?"""

    for block in options:
        if not block.deletion:
            if all(any(_span_covers(target, n) for target in targets) for n in block.lines):
                return True
            continue
        shown = block.removed_from and all(
            any(_old_covers(target, old) for target in targets for old in homes) for homes in block.removed_from
        )
        if block.old_only:
            # The new line beside a replaced one is another part's, so it says nothing about the old text.
            if shown:
                return True
        elif block.removed_from:
            # A deleted line is shown with the target that owns its ``-`` line; failing that, a target whose spans hold
            # the place it was removed from.
            if shown or any(_span_covers(target, n) for target in targets for n in block.lines):
                return True
        elif any(_span_covers(target, n) for target in targets for n in block.lines) or any(
            # No ``-`` line owns it (the agent added it earlier): the place it left is covered by a part holding the
            # join in its spans, or by a symbol or section whose whole body holds it.
            target.kind in ("symbol", "document_section")
            and any(target.start_line <= n <= target.end_line for n in block.lines)
            for target in targets
        ):
            return True
    return False


_RELOCATION_RUN = 3
"""Lines in a row that must match text removed from another file for a run to count as moved there."""


def _without_relocated(block: _Block, elsewhere: set[tuple[str, ...]]) -> _Block:
    """*block* minus the lines that sit in a run of :data:`_RELOCATION_RUN` or more that left another file verbatim."""

    if block.deletion or len(block.text) < _RELOCATION_RUN:
        return block
    moved: set[int] = set()
    for at in range(len(block.text) - _RELOCATION_RUN + 1):
        if block.text[at : at + _RELOCATION_RUN] in elsewhere:
            moved.update(range(at, at + _RELOCATION_RUN))
    if not moved:
        return block
    keep = [at for at in range(len(block.text)) if at not in moved]
    return _Block(tuple(block.lines[at] for at in keep), tuple(block.text[at] for at in keep))


def _uncovered_text(block: _Block, targets: Sequence[ReviewTarget]) -> tuple[str, ...]:
    """The lines of an uncovered change, as ``+ text`` (an added line no part spans) or ``- text`` (a deleted line)."""

    if block.deletion:
        return tuple(f"- {text}" for text in block.text)
    return tuple(
        f"+ {text}"
        for line, text in zip(block.lines, block.text, strict=True)
        if not any(_span_covers(target, line) for target in targets)
    )


def oracle_lines(
    previous: Capture, current: Capture, outstanding: Sequence[ReviewTarget]
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """:func:`oracle`, with each uncovered file's uncovered lines (``+ added`` / ``- deleted``, sorted) instead of just
    its path, so a known fail-open can be recorded by what it misses, not by the file that holds it."""

    required, removed = _changes_since_seen(previous, current)
    by_path: dict[str, list[ReviewTarget]] = {}
    for target in outstanding:
        by_path.setdefault(target.path, []).append(target)
    runs_elsewhere: dict[str, set[tuple[str, ...]]] = {}
    for other, runs in removed.items():
        for run in runs:
            for at in range(len(run) - _RELOCATION_RUN + 1):
                runs_elsewhere.setdefault(other, set()).add(run[at : at + _RELOCATION_RUN])
    changed: list[str] = []
    uncovered: dict[str, tuple[str, ...]] = {}
    for path, blocks in sorted(required.items()):
        elsewhere = {gram for other, grams in runs_elsewhere.items() if other != path for gram in grams}
        kept: list[tuple[_Block, ...]] = []
        for block in blocks:
            options = [_without_relocated(option, elsewhere) for option in (block, *block.alternatives)]
            if all(option.lines for option in options):
                kept.append(tuple(options))
        if not kept:
            continue
        changed.append(path)
        targets = by_path.get(path, [])
        missing = sorted(
            {
                line
                for options in kept
                if not _block_covered(options, targets)
                for line in _uncovered_text(options[0], targets)
            }
        )
        if missing:
            uncovered[path] = tuple(missing)
    return tuple(changed), uncovered


def batched_only_lines(
    previous: Capture, current: Capture, reading: Sequence[ReviewTarget], batched: Sequence[ReviewTarget]
) -> dict[str, tuple[str, ...]]:
    """The changed lines only the routine batch covers: ``path`` to its ``+ added`` / ``- deleted`` lines.

    A part confirmed in one step is one the prover called routine, not one anyone read. The oracle therefore counts a
    line as covered only by a part *reading* holds; what a part in *batched* alone covers is the risk the batch adds
    (a routine rule that proves too much), and is reported apart from :func:`oracle_lines`' engine fail-opens, which
    no part at all covers.
    """

    if not batched:
        return {}
    _, reached = oracle_lines(previous, current, (*reading, *batched))
    _, unread = oracle_lines(previous, current, tuple(reading))
    only = {path: tuple(line for line in lines if line not in reached.get(path, ())) for path, lines in unread.items()}
    return {path: lines for path, lines in only.items() if lines}


def oracle(
    previous: Capture, current: Capture, outstanding: Sequence[ReviewTarget]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(files changed since the reviewer saw them, files with a changed line no outstanding part covers)``.

    The check is per part, not per file, and independent of the engine's units. Every ``+`` line of the current diff
    that is new to the reviewer (see :func:`_changes_since_seen`) must lie inside a changed span of an outstanding
    target; a deleted line needs the target that owns its ``-`` line (an old-side span) or either line it sits between,
    and a line the agent added in an earlier revision and deleted now needs a part holding the place it left. A run of
    three or more lines that left another file verbatim is a relocation (the moved-symbol carry exists to prove those)
    and is cancelled; within one file a move is not, so reordering statements is a change. An insertion that a repeated
    line lets be aligned several ways is covered when any alignment is. What is left uncovered is a change the reviewer
    would never be asked about: the engine failed open there.
    """

    changed, uncovered = oracle_lines(previous, current, outstanding)
    return changed, tuple(uncovered)


# --------------------------------------------------------------------------- #
# a whole pull request
# --------------------------------------------------------------------------- #


def target_window(target: ReviewTarget) -> int:
    """The lines a reader may open for *target*: a symbol or section is read whole, anything else as its changed lines."""

    changed = target.additions + target.deletions
    if target.kind in ("symbol", "document_section") and target.end_line >= target.start_line > 0:
        return max(changed, target.end_line - target.start_line + 1)
    return changed


@dataclass(frozen=True)
class Transition:
    pr_id: str
    from_revision: int
    to_revision: int
    files_changed: int
    file_level: int
    """Parts to read at file level; every part is a file, so this is also its distinct-file count."""
    hunk_level: int
    to_read: int
    to_read_symbols: int
    to_read_hunks: int
    to_read_files: int
    """Distinct files holding a part to read: the figure comparable to the file-level count."""
    to_confirm_in_one_step: int
    accepted_by_rule: int
    unchanged_since_review: int
    total_parts: int
    behavioural_files: tuple[str, ...]
    fail_open_files: tuple[str, ...]
    """Files with a change since the reviewer saw them that no outstanding part covers: engine findings."""
    file_level_lines: int
    hunk_level_lines: int
    to_read_lines: int
    """Changed (``+``/``-``) lines of the parts to read, under each rule set."""
    to_confirm_lines: int
    """Changed lines of the parts to confirm in one step. The baselines have no such step and still count these lines
    in what they read, so :attr:`to_read_lines` plus this is the like-for-like figure against them."""
    hunk_level_files: int
    """Distinct files holding a hunk to read."""
    file_level_window: int
    hunk_level_window: int
    to_read_window: int
    """Whole-window upper bound: the parts' bodies with context (hunks), or the symbol's whole span (LemonCrow)."""
    to_confirm_window: int
    """The same bound over the parts to confirm in one step."""
    base_moved: bool
    to_read_in_unchanged_files: int
    """Parts to read in files whose head text is exactly the previous revision's: nothing the author wrote changed."""
    reopen_reasons: tuple[tuple[str, int], ...]
    """Parts to read by the reason the engine recorded for opening them (:class:`Reopened`), sorted by reason."""
    reopen_identical: tuple[tuple[str, int], ...]
    """Of those, the parts whose content is exactly content the reviewer was shown (``Reopened.identical``)."""
    fail_open_lines: tuple[tuple[str, str], ...] = ()
    """``(path, line)`` for each uncovered line behind :attr:`fail_open_files`: ``+ added`` or ``- deleted`` text."""
    batched_behavioural_lines: tuple[tuple[str, str], ...] = ()
    """``(path, line)`` for each changed line that only a part in the routine batch covers: no part the person reads
    holds it, so the batch is the one place the reviewer would meet it (see :func:`batched_only_lines`)."""

    @property
    def read_and_confirm_lines(self) -> int:
        """Changed lines LemonCrow puts in front of the person, read or confirmed: what the baselines are compared with."""

        return self.to_read_lines + self.to_confirm_lines

    @property
    def read_and_confirm_window(self) -> int:
        return self.to_read_window + self.to_confirm_window

    @property
    def batched_behavioural_files(self) -> tuple[str, ...]:
        return tuple(sorted({path for path, _ in self.batched_behavioural_lines}))

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.from_revision,
            "to": self.to_revision,
            "files_changed": self.files_changed,
            "file_level": self.file_level,
            "hunk_level": self.hunk_level,
            "lemoncrow_to_read": self.to_read,
            "lemoncrow_to_read_symbols": self.to_read_symbols,
            "lemoncrow_to_read_hunks": self.to_read_hunks,
            "lemoncrow_to_read_files": self.to_read_files,
            "lemoncrow_to_confirm_in_one_step": self.to_confirm_in_one_step,
            "lemoncrow_accepted_by_rule": self.accepted_by_rule,
            "lemoncrow_unchanged_since_review": self.unchanged_since_review,
            "lemoncrow_total_parts": self.total_parts,
            "behavioural_files": list(self.behavioural_files),
            "fail_open_files": list(self.fail_open_files),
            "fail_open_lines": [list(pair) for pair in self.fail_open_lines],
            "batched_behavioural_files": list(self.batched_behavioural_files),
            "batched_behavioural_lines": [list(pair) for pair in self.batched_behavioural_lines],
            "file_level_lines": self.file_level_lines,
            "hunk_level_lines": self.hunk_level_lines,
            "lemoncrow_to_read_lines": self.to_read_lines,
            "lemoncrow_to_confirm_lines": self.to_confirm_lines,
            "hunk_level_files": self.hunk_level_files,
            "file_level_window": self.file_level_window,
            "hunk_level_window": self.hunk_level_window,
            "lemoncrow_to_read_window": self.to_read_window,
            "lemoncrow_to_confirm_window": self.to_confirm_window,
            "base_moved": self.base_moved,
            "lemoncrow_to_read_in_unchanged_files": self.to_read_in_unchanged_files,
            "lemoncrow_reopen_reasons": dict(self.reopen_reasons),
            "lemoncrow_reopen_identical": dict(self.reopen_identical),
        }


@dataclass(frozen=True)
class CaptureCheck:
    """Whether one captured revision's targets partition its changed lines: every ``+``/``-`` line in exactly one."""

    pr_id: str
    revision: int
    packet_lines: int
    target_lines: int
    mismatched: tuple[tuple[str, int, int, str], ...]
    """``(path, changed lines in the packet, changed lines the targets hold, the kinds of target in the file)`` for each
    file that does not add up."""

    @property
    def partitions(self) -> bool:
        return not self.mismatched


def check_partition(capture: Capture) -> CaptureCheck:
    packet: Counter[str] = Counter()
    for item in capture.packet.files:
        packet[item.path] += sum(len(change_lines(hunk.patch)) for hunk in item.hunks)
    held: Counter[str] = Counter()
    kinds: dict[str, set[str]] = {}
    for target in capture.targets:
        held[target.path] += target.additions + target.deletions
        kinds.setdefault(target.path, set()).add(target.kind)
    mismatched = tuple(
        (path, packet[path], held[path], " + ".join(sorted(kinds.get(path, ()))) or "none")
        for path in sorted({*packet, *held})
        if packet[path] != held[path]
    )
    return CaptureCheck(
        pr_id=capture.pr_id,
        revision=capture.revision.index,
        packet_lines=sum(packet.values()),
        target_lines=sum(held.values()),
        mismatched=mismatched,
    )


@dataclass(frozen=True)
class Measurement:
    transitions: tuple[Transition, ...]
    captures: tuple[CaptureCheck, ...]


def measure_pull_request_full(pr: PullRequest, workdir: Path | None = None) -> Measurement:
    """Every revision transition of *pr*, measured under the three rule sets, and every capture's partition check."""

    with tempfile.TemporaryDirectory(prefix="lemoncrow-read-burden-") as scratch:
        root = workdir or Path(scratch)
        previous: Capture | None = None
        out: list[Transition] = []
        checks: list[CaptureCheck] = []
        for revision in pr.revisions:
            current = capture_revision(pr, revision, root / f"r{revision.index}")
            checks.append(check_partition(current))
            if previous is not None:
                count = lemoncrow_parts(previous, current)
                behavioural, fail_open = oracle_lines(
                    previous, current, (*count.to_read, *count.to_confirm_in_one_step, *count.accepted_by_rule)
                )
                batched_only = batched_only_lines(
                    previous, current, (*count.to_read, *count.accepted_by_rule), count.to_confirm_in_one_step
                )
                file_read = file_level_read(previous, current)
                hunk_read = hunk_level_read(previous, current)
                why = [count.reopened[t.unit_key] for t in count.to_read]
                out.append(
                    Transition(
                        pr_id=pr.id,
                        from_revision=previous.revision.index,
                        to_revision=revision.index,
                        files_changed=len(current.packet.files),
                        file_level=file_read.parts,
                        hunk_level=hunk_read.parts,
                        to_read=len(count.to_read),
                        to_read_symbols=sum(1 for t in count.to_read if t.kind == "symbol"),
                        to_read_hunks=sum(1 for t in count.to_read if t.kind == "hunk"),
                        to_read_files=len({t.path for t in count.to_read}),
                        to_confirm_in_one_step=len(count.to_confirm_in_one_step),
                        accepted_by_rule=len(count.accepted_by_rule),
                        unchanged_since_review=count.unchanged_since_review,
                        total_parts=count.total_parts,
                        behavioural_files=behavioural,
                        fail_open_files=tuple(fail_open),
                        fail_open_lines=tuple((path, line) for path, lines in fail_open.items() for line in lines),
                        batched_behavioural_lines=tuple(
                            (path, line) for path, lines in batched_only.items() for line in lines
                        ),
                        file_level_lines=file_read.lines,
                        hunk_level_lines=hunk_read.lines,
                        to_read_lines=sum(t.additions + t.deletions for t in count.to_read),
                        to_confirm_lines=sum(t.additions + t.deletions for t in count.to_confirm_in_one_step),
                        hunk_level_files=len(hunk_read.files),
                        file_level_window=file_read.window,
                        hunk_level_window=hunk_read.window,
                        to_read_window=sum(target_window(t) for t in count.to_read),
                        to_confirm_window=sum(target_window(t) for t in count.to_confirm_in_one_step),
                        base_moved=count.base_moved,
                        to_read_in_unchanged_files=sum(
                            1
                            for t in count.to_read
                            if t.path in previous.head_text
                            and previous.head_text[t.path] == current.head_text.get(t.path)
                        ),
                        reopen_reasons=tuple(sorted(Counter(r.category for r in why).items())),
                        reopen_identical=tuple(sorted(Counter(r.category for r in why if r.identical).items())),
                    )
                )
            previous = current
    return Measurement(tuple(out), tuple(checks))


def measure_pull_request(pr: PullRequest, workdir: Path | None = None) -> tuple[Transition, ...]:
    return measure_pull_request_full(pr, workdir).transitions


def measure_corpus_full(prs: Iterable[PullRequest]) -> Measurement:
    parts = [measure_pull_request_full(pr) for pr in prs]
    return Measurement(
        tuple(row for part in parts for row in part.transitions), tuple(row for part in parts for row in part.captures)
    )


def measure_corpus(prs: Iterable[PullRequest]) -> tuple[Transition, ...]:
    return measure_corpus_full(prs).transitions


# --------------------------------------------------------------------------- #
# expected results and the regression gate
# --------------------------------------------------------------------------- #


def expected_results(transitions: Sequence[Transition]) -> dict[str, Any]:
    """The checked-in ``expected.json``: LemonCrow's counts per transition, plus what the baselines asked."""

    by_pr: dict[str, list[dict[str, Any]]] = {}
    for row in transitions:
        by_pr.setdefault(row.pr_id, []).append(row.to_json())
    return {
        "format": CORPUS_FORMAT,
        "direction": (
            "lemoncrow_to_read (parts), lemoncrow_to_read_lines (changed lines) and lemoncrow_to_read_files (distinct "
            "files) measure what a person must read: MORE of any is a regression and fails the gate; FEWER passes "
            "only once the new number is recorded here with the reason in the commit that changes it"
        ),
        "known_fail_opens": [
            {
                "pr": k.pr_id,
                "from": k.from_revision,
                "to": k.to_revision,
                "path": k.path,
                "kind": k.kind,
                "tracked": k.tracked,
                "lines": list(k.lines),
            }
            for k in KNOWN_FAIL_OPENS
        ],
        "known_batched_behavioural": [
            {
                "pr": k.pr_id,
                "from": k.from_revision,
                "to": k.to_revision,
                "path": k.path,
                "rule": k.rule,
                "kind": k.kind,
                "lines": list(k.lines),
            }
            for k in KNOWN_BATCHED_BEHAVIOURAL
        ],
        "pull_requests": {key: by_pr[key] for key in sorted(by_pr)},
    }


@dataclass(frozen=True)
class GateFinding:
    kind: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.where}: {self.detail}"


def compare_with_expected(measured: Sequence[Transition], expected: Mapping[str, Any]) -> tuple[GateFinding, ...]:
    """Findings, empty when the measured counts hold.

    ``regression`` is a transition that asks a person to read *more* than recorded, in parts, changed lines or distinct
    files. ``improved`` is *fewer*: it fails too, so a better number is recorded on purpose, with its explanation, not
    drifted into.
    ``fail_open`` is a change since the reviewer saw the file that no outstanding part covers, and that expected.json
    does not already list as a known finding, by file and by uncovered line (a listed one that is fixed is
    ``improved``). ``batched_behavioural`` is a changed line only a part in the routine batch covers, listed the same
    way. ``corpus`` is a pull
    request or transition that exists on one side only.
    """

    findings: list[GateFinding] = []
    recorded = expected["pull_requests"]
    measured_by_pr: dict[str, list[Transition]] = {}
    for row in measured:
        measured_by_pr.setdefault(row.pr_id, []).append(row)
    for missing in sorted(set(recorded) - set(measured_by_pr)):
        findings.append(GateFinding("corpus", missing, "recorded in expected.json but not in the corpus"))
    for extra in sorted(set(measured_by_pr) - set(recorded)):
        findings.append(GateFinding("corpus", extra, "in the corpus but not recorded in expected.json"))
    for pr_id in sorted(set(recorded) & set(measured_by_pr)):
        old = {(row["from"], row["to"]): row for row in recorded[pr_id]}
        new = {(row.from_revision, row.to_revision): row for row in measured_by_pr[pr_id]}
        if set(old) != set(new):
            findings.append(
                GateFinding("corpus", pr_id, f"transitions {sorted(new)} differ from the recorded {sorted(old)}")
            )
            continue
        for key in sorted(new):
            row, was = new[key], old[key]
            where = f"{pr_id} r{key[0]}->r{key[1]}"
            for name, label, now in (
                ("lemoncrow_to_read", "parts to read", row.to_read),
                ("lemoncrow_to_read_lines", "changed lines to read", row.to_read_lines),
                ("lemoncrow_to_read_files", "files to read", row.to_read_files),
            ):
                if now > was[name]:
                    findings.append(GateFinding("regression", where, f"{was[name]} -> {now} {label}"))
                elif now < was[name]:
                    findings.append(
                        GateFinding(
                            "improved",
                            where,
                            f"{was[name]} -> {now} {label}; record it in expected.json and say why",
                        )
                    )
            for name, got in (
                ("files_changed", row.files_changed),
                ("file_level", row.file_level),
                ("lemoncrow_to_read_symbols", row.to_read_symbols),
                ("lemoncrow_to_read_hunks", row.to_read_hunks),
                ("hunk_level", row.hunk_level),
                ("lemoncrow_to_confirm_in_one_step", row.to_confirm_in_one_step),
                ("lemoncrow_to_confirm_lines", row.to_confirm_lines),
                ("lemoncrow_to_confirm_window", row.to_confirm_window),
                ("lemoncrow_accepted_by_rule", row.accepted_by_rule),
                ("lemoncrow_unchanged_since_review", row.unchanged_since_review),
                ("lemoncrow_total_parts", row.total_parts),
                ("file_level_lines", row.file_level_lines),
                ("hunk_level_lines", row.hunk_level_lines),
                ("hunk_level_files", row.hunk_level_files),
                ("file_level_window", row.file_level_window),
                ("hunk_level_window", row.hunk_level_window),
                ("lemoncrow_to_read_window", row.to_read_window),
                ("base_moved", row.base_moved),
                ("lemoncrow_to_read_in_unchanged_files", row.to_read_in_unchanged_files),
                ("lemoncrow_reopen_reasons", dict(row.reopen_reasons)),
                ("lemoncrow_reopen_identical", dict(row.reopen_identical)),
            ):
                if got != was[name]:
                    findings.append(GateFinding("changed", where, f"{name}: {was[name]} -> {got}"))
            if list(row.behavioural_files) != was["behavioural_files"]:
                findings.append(
                    GateFinding(
                        "changed",
                        where,
                        f"behavioural_files: {was['behavioural_files']} -> {list(row.behavioural_files)}",
                    )
                )
            known = set(was["fail_open_files"])
            for path in sorted(set(row.fail_open_files) - known):
                findings.append(GateFinding("fail_open", where, f"no outstanding part covers a change in {path}"))
            for path in sorted(known - set(row.fail_open_files)):
                findings.append(
                    GateFinding("improved", where, f"{path} is no longer fail-open; remove it from expected.json")
                )
            # A file that already fails open can fail on a further line: that is new, not the known finding.
            known_lines = {(path, line) for path, line in was.get("fail_open_lines", [])}
            shared = known & set(row.fail_open_files)
            for path, line in sorted(set(row.fail_open_lines) - known_lines):
                if path in shared:
                    findings.append(GateFinding("fail_open", where, f"no outstanding part covers {line!r} in {path}"))
            for path, line in sorted(known_lines - set(row.fail_open_lines)):
                if path in shared:
                    findings.append(
                        GateFinding(
                            "improved", where, f"{line!r} in {path} is covered now; remove it from expected.json"
                        )
                    )
            was_batched = {(path, line) for path, line in was.get("batched_behavioural_lines", [])}
            now_batched = set(row.batched_behavioural_lines)
            for path, line in sorted(now_batched - was_batched):
                findings.append(
                    GateFinding("batched_behavioural", where, f"only the routine batch covers {line!r} in {path}")
                )
            for path, line in sorted(was_batched - now_batched):
                findings.append(
                    GateFinding("improved", where, f"{line!r} in {path} is read now; remove it from expected.json")
                )
    return tuple(findings)


@dataclass(frozen=True)
class KnownFailOpen:
    """A transition whose uncovered change is understood and accepted; everything else the oracle finds is a failure."""

    pr_id: str
    from_revision: int
    to_revision: int
    path: str
    kind: str
    """``defect``: the engine hides a change it should show, and a fix removes this entry. ``policy``: the engine does
    what it was designed to do, and the entry stays until the design changes."""
    tracked: str
    lines: tuple[str, ...]
    """The uncovered lines (``+ added`` / ``- deleted``, sorted), so a new uncovered line in the same file is new."""
    why: str


#: Hand-maintained. ``--write-expected`` refuses to run unless the oracle finds exactly these, so a new fail-open is
#: never recorded by regenerating: it is a finding someone has read and classified here, in review.
#: Empty since P12-1: the two policy entries it once held (a definition moved within a shell or Make file) reopen with
#: ``part_order_changed`` (``revisions._order_moved``).
KNOWN_FAIL_OPENS: tuple[KnownFailOpen, ...] = ()


@dataclass(frozen=True)
class KnownBatchedBehavioural:
    """A changed line only the routine batch covers, read and classified by a person."""

    pr_id: str
    from_revision: int
    to_revision: int
    path: str
    rule: str
    """The ``since_review`` rule that batched the part holding the line."""
    kind: str
    """``prover``: the proof batched a change that changes behaviour; a fix to ``since_review.py`` or ``routine.py``
    removes the entry. ``artefact``: the oracle compares lines of text, and the line is a comment or the same code
    laid out differently, so nothing is hidden."""
    lines: tuple[str, ...]
    why: str


#: Hand-maintained like :data:`KNOWN_FAIL_OPENS`: ``--write-expected`` refuses to run unless the oracle finds exactly
#: these, so a prover that starts batching a behavioural change is a finding read and classified here, in review.
KNOWN_BATCHED_BEHAVIOURAL: tuple[KnownBatchedBehavioural, ...] = (
    KnownBatchedBehavioural(
        "MesilimLLC/Trackdub#353",
        3,
        4,
        "tools/separation-eval/test_gate.py",
        "comment_only",
        "artefact",
        (
            "+         # the threshold across that bracket, is caught by the policy assertion below, not by these two "
            "cases.",
        ),
        "The one changed line is a rewritten `#` comment inside a test method; the proof found the code byte-identical "
        "and the diff of the file shows nothing else. The oracle counts a comment as a line.",
    ),
    KnownBatchedBehavioural(
        "liminal-hq/threshold#31",
        6,
        7,
        "apps/threshold/src/contexts/ThemeContext.tsx",
        "formatting_only",
        "artefact",
        ("+ setForceDark: () => {},", "+ setTheme: () => {},", "+ setUseMaterialYou: () => {},"),
        "A formatter rewrote `() => { }` as `() => {}` in the `createContext` default (and re-indented the file); the "
        "parsed structure is the same. The same file's real change, `getUseMaterialYou() ?? false`, is in a part the "
        "person reads and is not listed.",
    ),
    KnownBatchedBehavioural(
        "mattermost/mattermost-plugin-agents#876",
        4,
        5,
        "search/availability_test.go",
        "comment_only",
        "artefact",
        (
            "+ // An incompatible model blocks queries while preserving indexing for reindexing.",
            "+ // The predicate reflects stored model compatibility without resetting search.",
        ),
        "Two rewritten `//` comments in a test; the proof found the code byte-identical.",
    ),
    KnownBatchedBehavioural(
        "mattermost/mattermost-plugin-agents#876",
        4,
        5,
        "server/main.go",
        "comment_only",
        "artefact",
        (
            "+ // Create indexer service. IndexSearch stays available whenever search is",
            "+ // Store the initialized search; QuerySearch handles compatibility gating.",
            "+ // Store the reinitialized search. QuerySearch blocks incompatible model",
            "+ // compatible with the existing index.",
            "+ // initialized, so a full reindex can run during model incompatibility.",
            "+ // queries while IndexSearch allows reindexing.",
        ),
        "Six lines of rewritten `//` comments in `OnActivate`; the proof found the code byte-identical.",
    ),
    KnownBatchedBehavioural(
        "sghill/java-flight-recorder-plugin#5",
        13,
        14,
        "src/test/java/io/jenkins/plugins/jfr/JavaFlightRecorderActionTest.java",
        "formatting_only",
        "artefact",
        (
            "+ @Mock",
            "+ JfrSession session = new JfrSession(",
            "+ com.fasterxml.jackson.databind.SerializationFeature.WRITE_DATE_TIMESTAMPS_AS_NANOSECONDS, false);",
            "+ objectMapper.configure(",
            "+ private JfrService jfrService;",
        ),
        "A formatter changed the indent and wrapped lines: `@Mock private JfrService` became two lines and two calls "
        "were re-wrapped. Same statements; the oracle compares whole lines.",
    ),
)


def batched_behavioural_drift(expected: Mapping[str, Any]) -> list[str]:
    """How the batched-only lines in *expected* differ from :data:`KNOWN_BATCHED_BEHAVIOURAL`."""

    found: dict[tuple[str, int, int, str], tuple[str, ...]] = {}
    for pr_id, rows in expected["pull_requests"].items():
        for row in rows:
            for path in row["batched_behavioural_files"]:
                found[(pr_id, row["from"], row["to"], path)] = tuple(
                    sorted(line for where, line in row["batched_behavioural_lines"] if where == path)
                )
    known = {
        (k.pr_id, k.from_revision, k.to_revision, k.path): tuple(sorted(k.lines)) for k in KNOWN_BATCHED_BEHAVIOURAL
    }
    out = [f"new, not in KNOWN_BATCHED_BEHAVIOURAL: {key} {found[key]}" for key in sorted(found.keys() - known.keys())]
    out += [f"in KNOWN_BATCHED_BEHAVIOURAL but no longer found: {key}" for key in sorted(known.keys() - found.keys())]
    out += [
        f"batched-only lines differ for {key}: found {found[key]}, known {known[key]}"
        for key in sorted(found.keys() & known.keys())
        if found[key] != known[key]
    ]
    return out


def fail_open_drift(expected: Mapping[str, Any]) -> list[str]:
    """How the fail-opens in *expected* (the computed ``expected.json``) differ from :data:`KNOWN_FAIL_OPENS`."""

    found: dict[tuple[str, int, int, str], tuple[str, ...]] = {}
    for pr_id, rows in expected["pull_requests"].items():
        for row in rows:
            for path in row["fail_open_files"]:
                found[(pr_id, row["from"], row["to"], path)] = tuple(
                    sorted(line for where, line in row["fail_open_lines"] if where == path)
                )
    known = {(k.pr_id, k.from_revision, k.to_revision, k.path): tuple(sorted(k.lines)) for k in KNOWN_FAIL_OPENS}
    out = [f"new, not in KNOWN_FAIL_OPENS: {key} {found[key]}" for key in sorted(found.keys() - known.keys())]
    out += [f"in KNOWN_FAIL_OPENS but no longer found: {key}" for key in sorted(known.keys() - found.keys())]
    out += [
        f"uncovered lines differ for {key}: found {found[key]}, known {known[key]}"
        for key in sorted(found.keys() & known.keys())
        if found[key] != known[key]
    ]
    return out


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Summary:
    transitions: int
    file_level: int
    hunk_level: int
    to_read: int
    to_read_symbols: int
    to_read_hunks: int
    to_read_files: int
    to_confirm_in_one_step: int
    accepted_by_rule: int
    median_file_level: float
    median_hunk_level: float
    median_to_read: float
    fewer_than_file: int
    fewer_than_hunk: int
    equal_to_hunk: int
    more_than_hunk: int
    more_than_file: int


def summarize(rows: Sequence[Transition]) -> Summary:
    def median(values: Sequence[int]) -> float:
        return float(statistics.median(values)) if values else 0.0

    return Summary(
        transitions=len(rows),
        file_level=sum(row.file_level for row in rows),
        hunk_level=sum(row.hunk_level for row in rows),
        to_read=sum(row.to_read for row in rows),
        to_read_symbols=sum(row.to_read_symbols for row in rows),
        to_read_hunks=sum(row.to_read_hunks for row in rows),
        to_read_files=sum(row.to_read_files for row in rows),
        to_confirm_in_one_step=sum(row.to_confirm_in_one_step for row in rows),
        accepted_by_rule=sum(row.accepted_by_rule for row in rows),
        median_file_level=median([row.file_level for row in rows]),
        median_hunk_level=median([row.hunk_level for row in rows]),
        median_to_read=median([row.to_read for row in rows]),
        fewer_than_file=sum(row.to_read < row.file_level for row in rows),
        fewer_than_hunk=sum(row.to_read < row.hunk_level for row in rows),
        equal_to_hunk=sum(row.to_read == row.hunk_level for row in rows),
        more_than_hunk=sum(row.to_read > row.hunk_level for row in rows),
        more_than_file=sum(row.to_read > row.file_level for row in rows),
    )


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _language_of(path: str) -> str:
    return {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".mjs": "JavaScript",
        ".cjs": "JavaScript",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".cs": "C#",
        ".rb": "Ruby",
        ".php": "PHP",
        ".c": "C",
        ".h": "C",
        ".cc": "C++",
        ".cpp": "C++",
        ".hpp": "C++",
        ".kt": "Kotlin",
        ".swift": "Swift",
        ".sh": "Shell",
    }.get(Path(path).suffix, "")


def corpus_languages(prs: Sequence[PullRequest]) -> dict[str, int]:
    """Pull requests per language of the source files they change (a pull request counts once per language)."""

    counts: Counter[str] = Counter()
    for pr in prs:
        languages = {_language_of(change.path) for revision in pr.revisions for change in revision.files}
        counts.update(language for language in languages if language)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


@dataclass(frozen=True)
class ReportInput:
    prs: Sequence[PullRequest]
    transitions: Sequence[Transition]
    corpus_size: int
    manifest: Mapping[str, Any] = field(default_factory=dict)
    captures: Sequence[CaptureCheck] = ()


#: True once P11-2 (``since_review``) is wired into :func:`split_routine_batch`. While False the report labels the
#: LemonCrow column "without routine batch" and shows no "to confirm in one step" figure at all.
ROUTINE_BATCH_WIRED = True

LEMONCROW_COLUMN = "LemonCrow to read" if ROUTINE_BATCH_WIRED else "LemonCrow to read (without routine batch)"

_METHOD = """\
For every pull request the collector stored one *revision* per observed head commit (see "Revisions" in the corpus
README). The benchmark takes each consecutive pair of revisions as a transition and asks: a reviewer who read every part
of revision *n-1* and judged it done, what must they read when revision *n* arrives? Three rule sets answer, each
counted three ways (changed lines, distinct files, parts).

1. **File level (GitHub "Viewed").** A file is one part. It is read again when its diff differs from the diff it had in
   the previous revision, or when it is new. Its changed lines are all the `+` and `-` lines of its diff.
2. **Hunk level (Tidewave, tuicr).** A hunk is one part. It is read again when its content is not among the previous
   revision's hunks of that file. Only the `+` and `-` lines decide that, and only they are counted as changed lines:
   context and `@@` line numbers are ignored, which is the most favourable reading for both baselines (a baseline that
   re-read a hunk because lines above it shifted would be easier to beat).
3. **LemonCrow.** Each revision is written into a real git repository and passed through the product's own code: the
   review packet, `derive_units` (symbol units for the languages that have them, hunk units elsewhere), the dependency
   units and `stamp_answers`. The reviewer is modelled as having marked every part of revision *n-1* `reviewed`. Those
   marks are carried onto revision *n* by `revisions.reconcile`, with the base-only carry from
   `rebase_classification.classify_movement`, the moved-symbol carry from `detect_symbol_moves`, and renames. The parts
   that remain outstanding are put through `derive_review_targets`, and the queue's answer category decides what is
   counted: parts accepted by a rule (generated files, lockfiles) are listed separately, everything else is a part to
   read. Its changed lines are the `+` and `-` lines of those parts. Nothing in this module re-implements those rules.

**Changed lines** are the like-for-like measure because the parts differ in size. They are only comparable if the
parts of a rule set hold each changed line once, which the partition check below verifies for LemonCrow. **Distinct
files** is the like-for-like granularity. **Parts** is the count a person sees in a queue, and the one the part sizes
distort most.

**The whole-window upper bound** counts what a reader may open rather than what changed: a hunk's whole body with its
context, and for LemonCrow the whole span of a symbol part (`end_line - start_line + 1`) or the changed lines of a hunk
part. It exists so the changed-lines table cannot be read as the whole story: a symbol is read in its context, and a
reader who opens whole symbols reads more than its changed lines.

**Why a part was reopened** comes from `reconcile`'s own recorded reason for each part that stayed outstanding. A part
with no recorded reason is `new_unit` when no such unit existed in revision *n-1*, and `never_a_target` when the unit
existed but was not a part to judge there. Nothing is classified by this module's own guess.

**The fail-open oracle** guards the other direction: a count can be lowered by a part wrongly carried as read. For
every file, the oracle aligns the text the reviewer last saw (the previous head, or the base for a file new to the
packet) against the current head with `difflib`, normalising whitespace outside Python and keeping Python indentation.
Each inserted or replaced line that is a `+` line of the current diff is a line the reviewer has not seen, and so is
each line that is deleted outright (a `delete` alignment) and is a `-` line of it. The old side of a replacement is
required when it is newly deleted (a `-` line at a base position the diff the reviewer saw did not remove; after a base move, more `-` lines of that text than it had), and only
the part that owns the `-` line covers it. A line the agent added in an earlier revision and deletes now is a `-` line of no
diff, so it is counted by occurrences (the reviewer saw it more often than the head or the base has it) and required
when the current diff has a changed line where it was or the place lies inside a definition the diff changes (otherwise
the change went back to the base and nothing is left to read); a symbol or section part holding that place covers it. A run of three or more lines that left another file verbatim is a
relocation and cancelled. Every remaining changed line must lie in a changed span of an outstanding part (a deletion in
the span that owns its `-` line, or beside a covered line; one with no `-` line, beside a covered line or inside a
covering symbol or section). A line no outstanding part covers is a change the reviewer
would never be asked about. The oracle uses spans only and shares no unit with the engine's carry rules.

A part confirmed in one step is not a part read. The oracle is therefore run twice: with the batch counted as covering
(a fail-open is a line no part at all covers) and without it (a line only a batched part covers is *batched
behavioural*, a risk of the routine prover, not of the engine). Each such case is classified by hand as a prover defect
or an oracle artefact, and the gate fails on a new one."""

_LIMITATIONS = """\
- **Selection.** The search ranks by comment count and asks for more than three comments, so pull requests a person
  discussed are over-represented. Pull requests with more than {max_revisions} observed revisions, {max_files}
  changed files or {max_kb} of changed text, and those under a non-permissive or missing licence, are excluded. The
  corpus is a sample of public agent pull requests that have been reviewed, not of all agent pull requests.
- **Merged pull requests only.** The search asks for `is:merged`, so a pull request that was closed unmerged, or is
  still open, is not here, so their push histories are not represented.
- **Agent identification is by the opener.** A pull request counts as agent-written because its opening account is a
  known agent bot. A revision a person pushed onto an agent's branch is not told apart from the agent's own, so some
  transitions may be a human's follow-up rather than an agent's push.
- **Revisions are under-counted.** GitHub does not publish push boundaries. A head commit is a revision only if it was
  observed (it ran checks, a human reviewed it, or it is a force-push boundary). Pushes nobody observed are not
  revisions here, so these transitions are larger steps than a push-by-push reviewer would have seen.
- **The reviewer is idealised.** Everything in the previous revision is assumed read and judged. A real reviewer who
  skipped parts reads more under every rule set, and the gap between them is not measured here.
- **Baselines are generous.** Hunks are compared on their changed lines only. A tool that also re-reads a hunk when its
  context or position moves would count more.
- **The routine batch is the product's proof under stated assumptions.** A reopened quick-check part, or an unanswered
  hunk of a re-hunked file, is "to confirm in one step" when `since_review.since_review_proofs` (the function behind
  `/targets`, with its 300-part bound) proves it routine, under a rule the queue accepts. The corpus has no hosted
  server (hosted never sends a proof and never batches), no unusable frozen patch and no AI hint, so none holds a part
  back; and `batch_guard`, which clears quick checks never reopened, is not run, so such a part is read. Each
  assumption can only raise the read count. The batch is a step a person still confirms, and a part in it is not a
  part nobody looks at.
- **Base movement.** When the base branch moved between two revisions, a part is carried only if the authored patch of
  its file is byte-identical apart from whitespace, context lines included. A base change inside the diff's context
  makes that file authored, so LemonCrow can ask for a part that neither baseline asks for. The base-movement table
  counts those transitions and, of the parts read in them, those in files whose head text did not change at all.
- **Whole parts are re-read.** A symbol or hunk that changed is read again as a whole, including changed lines the
  reviewer had already seen in it. The hunk baseline reads only the hunks that changed, so inside a transition the
  difference can go either way; the excess table says how often and why.
- **No code index.** Symbols are derived without the code index, as the engine does when none is built; impact and
  provenance are not computed. This affects which parts are ranked first, not which parts are outstanding.
- **Parts are not equal-sized.** LemonCrow splits a changed file into the symbols it changes and the hunks outside any
  symbol, so a count of parts favours the coarser rule sets: one git hunk that touches an import and a function is one
  hunk-level part and two LemonCrow parts. The changed-lines and distinct-files measures are the like-for-like views.
  A symbol is also usually smaller to read than a file, which a count of parts does not show.
- **A definition moved within a file keeps its mark, except in shell and Make files.** A symbol whose body is unchanged
  is the same part wherever it sits (`units.py`), so a move inside a Python or Go file is not shown. Where order
  decides what runs it is: a shell function moved below its first call fails with `command not found` (exit 127 under
  `set -e`), and a Makefile's immediate expansion (`:=`, a prerequisite list, `ifeq`) reads only what is defined above
  it. In a shell script (`.sh`, `.bash`, `.zsh`, `.ksh`, or a file whose first line is a shell `#!` line) or Make file
  (`Makefile`, `GNUmakefile`, `Makefile.*`, `*.mk`, `*.mak`) a part whose place changed reopens as `part_order_changed`.
  Two comparisons decide it: the order of the parts among themselves (`revisions._order_moved`) and an anchor each part
  carries in its fingerprint, the base lines it sits under (`units._PartAnchors`), which also catches a part that
  crossed lines of the base file and a hunk that moved across identical-looking context. The anchor is text, not a line
  number, so a part that only shifted because lines came or went elsewhere keeps its mark. Left, fail-safe: a part
  whose base line directly above was deleted or edited, or whose identical neighbouring rows git aligns differently,
  reopens as `part_order_changed`, which says its position among the unchanged base lines changed and not that it moved.
  A part whose place cannot be checked reopens as `part_place_unverifiable`: the file is too large (over
  `MAX_BLOB_BYTES`) or unreadable and it changed since the verdict, which an untouched file does not do because its
  fallback anchor is a digest of the file; or the verdict was recorded before places were kept, which is read once and
  then re-marked, never carried. The two transitions that were
  recorded as policy fail-opens, `MaxMind-DB-Reader-php#265` r7 to r8 (`fail()` moved earlier, harmless) and
  `kubernetes-nmstate#1547` r3 to r4 (`HELM_VERSION` and `HELM_RELEASE_NAME` moved above their uses, load-bearing), now
  reopen the moved parts. An extensionless `#!` script has no definition parts, only hunks, and those are anchored.
- **No engine defect is open.** `mattermost-plugin-agents#759` r5 to r6 deleted base lines the engine never showed;
  P11-6 fixed it and it is no longer found. A defect the oracle finds later is listed in `KNOWN_FAIL_OPENS` as an
  engine defect until it is fixed.
- **P11-6 reopens two symbols that are safe to reopen.** On `kanjika_gem#3` r3 to r4 (`Masu.suffix`) and
  `arbiter#241` r2 to r3 (`ReplSession`), P11-6 reopens one part each: the base moved, the text the part owns that
  was deleted changed, and the oracle finds no change in either that the reviewer had not already seen. They are
  fail-safe reopens, one part each, and not defects; they are counted in the parts to read.
- **Time to decision is out of scope.** These are counts of lines, files and parts, not minutes.
- **The oracle is a line heuristic, not a proof of equivalence.** It reads text, not syntax, and treats a run of three
  or more lines that left another file verbatim as a relocation, so a copy-paste that changes behaviour in the way it is
  used would not be flagged. It has been run against deliberately wrong carries (a mutation test in the gate) and flags
  the transitions they drop unless every part dropped held nothing the reviewer had not seen.
- **Base-relative review has a blind spot for a change that went back to the base.** A line the agent added in one
  revision and removes in the next is required only where the diff still has a changed line or a changed definition
  around it. If `AUDIT = True` is added between two unchanged functions while `g` changes, and the next revision removes
  it and keeps `g`'s change, the region is the base again: neither the engine nor the oracle shows it, so a guard the
  reviewer approved can vanish without notice. Showing it takes a review that is relative to what the reviewer saw,
  not to the base (the since-review work, P11-2)."""


def _limitations(manifest: Mapping[str, Any]) -> str:
    """The limitations text, with the collector's selection caps as the manifest recorded them."""

    limits = manifest.get("limits", {})
    size = limits.get("max_text_bytes")
    return _LIMITATIONS.format(
        max_revisions=limits.get("max_revisions", "an unrecorded number of"),
        max_files=limits.get("max_files", "an unrecorded number of"),
        max_kb=f"{size // 1000} KB" if isinstance(size, int) else "an unrecorded size",
    )


def _row(values: Iterable[Any]) -> str:
    return "| " + " | ".join(str(value) for value in values) + " |"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]], text: Sequence[int] = ()) -> list[str]:
    """A Markdown table: the first column and those in *text* are left-aligned, the rest are numbers."""

    lines = [_row(header), _row(["---" if at == 0 or at in text else "---:" for at in range(len(header))])]
    lines.extend(_row(row) for row in rows)
    return lines


def _total(rows: Sequence[Transition], name: str) -> int:
    return sum(int(getattr(row, name)) for row in rows)


def _median_of(rows: Sequence[Transition], name: str) -> str:
    return _num(float(statistics.median([int(getattr(row, name)) for row in rows])) if rows else 0.0)


def _versus(rows: Sequence[Transition], mine: str, theirs: str) -> tuple[int, int, int]:
    """``(fewer, equal, more)`` transitions in which LemonCrow's *mine* is below, at or above the baseline's *theirs*."""

    fewer = sum(getattr(row, mine) < getattr(row, theirs) for row in rows)
    more = sum(getattr(row, mine) > getattr(row, theirs) for row in rows)
    return fewer, len(rows) - fewer - more, more


#: The order the reopen-reason table lists the reasons it names; any other recorded reason follows alphabetically.
_REASON_ORDER = (
    "content_changed",
    "new_unit",
    "hunks_renumbered",
    NEVER_A_TARGET,
    "symbol_renamed",
    "part_order_changed",
    "part_place_unverifiable",
)

_REASON_MEANING = {
    "content_changed": "the part's content differs from what the reviewer saw",
    "new_unit": "no such part in the previous revision",
    "hunks_renumbered": "the file's hunk count changed, so ordinal-derived hunk keys were reset",
    NEVER_A_TARGET: "the unit existed before but was not a part to judge there",
    "symbol_renamed": "a renamed symbol never keeps a verdict",
    "part_order_changed": "a shell or Make part kept its content but its position among the file's unchanged base lines changed",
    "part_place_unverifiable": "a shell or Make part kept its content but where it sits cannot be checked (a verdict from before places were kept, or a file too large or unreadable that changed)",
}


def _reason_counts(rows: Sequence[Transition]) -> tuple[Counter[str], Counter[str]]:
    reasons: Counter[str] = Counter()
    identical: Counter[str] = Counter()
    for row in rows:
        reasons.update(dict(row.reopen_reasons))
        identical.update(dict(row.reopen_identical))
    return reasons, identical


def render_report(data: ReportInput) -> str:
    """The Markdown report. Pure: the same corpus and transitions give the same bytes."""

    prs = sorted(data.prs, key=lambda pr: pr.id)
    rows = list(data.transitions)
    total = summarize(rows)
    by_pr: dict[str, list[Transition]] = {}
    for row in rows:
        by_pr.setdefault(row.pr_id, []).append(row)
    agents = Counter(pr.agent for pr in prs)
    licences = Counter(pr.license for pr in prs)
    languages = corpus_languages(prs)
    lemoncrow = "LemonCrow" if ROUTINE_BATCH_WIRED else "LemonCrow (without routine batch)"
    count = len(rows)
    out: list[str] = [
        "# Read burden: what a person must read when a coding agent pushes again",
        "",
        "This report is generated by `make review-read-burden-report` from the stored corpus in "
        "`evals/review/read_burden/`; do not edit it. Every number below is counted from real public pull requests.",
        "",
        "## Result",
        "",
        f"{len(prs)} agent-written pull requests, {count} revision transitions. For each transition, a reviewer who read "
        "all of the previous revision is asked to read again whatever the rule set cannot carry over. Three rule sets are "
        "measured three ways, because parts are not the same size across them: changed (`+`/`-`) lines, distinct files "
        "and parts. Totals over all transitions:",
        "",
    ]
    out += _table(
        [
            "Rule set",
            "Changed lines to read",
            "Changed lines to confirm in one step",
            "Distinct files with something to read",
            "Parts to read",
            "Parts to confirm in one step",
        ],
        [
            [
                "File level (GitHub Viewed)",
                _total(rows, "file_level_lines"),
                "-",
                total.file_level,
                total.file_level,
                "-",
            ],
            [
                "Hunk level (Tidewave, tuicr)",
                _total(rows, "hunk_level_lines"),
                "-",
                _total(rows, "hunk_level_files"),
                total.hunk_level,
                "-",
            ],
            [
                lemoncrow,
                _total(rows, "to_read_lines"),
                _total(rows, "to_confirm_lines"),
                total.to_read_files,
                total.to_read,
                total.to_confirm_in_one_step,
            ],
        ],
    )
    out += [
        "",
        "A baseline has no confirm step: it asks the person to read every part it reopens, including the routine ones "
        "LemonCrow lets them confirm together. **Changed lines to read plus changed lines to confirm is therefore the "
        f"like-for-like figure against the baselines**: {_total(rows, 'read_and_confirm_lines')} changed lines for "
        f"LemonCrow ({_total(rows, 'to_read_lines')} to read, {_total(rows, 'to_confirm_lines')} to confirm in one step), "
        f"against {_total(rows, 'hunk_level_lines')} for hunk level and {_total(rows, 'file_level_lines')} for file "
        "level. Lines to read alone are what LemonCrow asks a person to read closely, not what it puts in front of them.",
    ]
    out += ["", "Median per transition:", ""]
    out += _table(
        ["Rule set", "Changed lines to read", "Changed lines to read or confirm", "Distinct files", "Parts"],
        [
            [
                "File level",
                _median_of(rows, "file_level_lines"),
                _median_of(rows, "file_level_lines"),
                _median_of(rows, "file_level"),
                _num(total.median_file_level),
            ],
            [
                "Hunk level",
                _median_of(rows, "hunk_level_lines"),
                _median_of(rows, "hunk_level_lines"),
                _median_of(rows, "hunk_level_files"),
                _num(total.median_hunk_level),
            ],
            [
                lemoncrow,
                _median_of(rows, "to_read_lines"),
                _median_of(rows, "read_and_confirm_lines"),
                _median_of(rows, "to_read_files"),
                _num(total.median_to_read),
            ],
        ],
    )
    out += ["", "Per transition, whether LemonCrow reads fewer, the same or more than each baseline:", ""]
    comparison = []
    for label, mine, file_name, hunk_name in (
        ("changed lines to read", "to_read_lines", "file_level_lines", "hunk_level_lines"),
        ("changed lines to read or confirm", "read_and_confirm_lines", "file_level_lines", "hunk_level_lines"),
        ("distinct files", "to_read_files", "file_level", "hunk_level_files"),
        ("parts", "to_read", "file_level", "hunk_level"),
    ):
        for baseline, theirs in (("file level", file_name), ("hunk level", hunk_name)):
            fewer, equal, more = _versus(rows, mine, theirs)
            comparison.append([f"{label} versus {baseline}", fewer, equal, more])
    out += _table(["Measure", "Fewer", "Equal", "More"], comparison)
    out += [
        "",
        "LemonCrow's parts are the outstanding parts after the carry; parts accepted by a rule (generated files, "
        f"lockfiles) are listed apart and not counted: {total.accepted_by_rule} in all"
        + (
            f"; parts to confirm in one step: {total.to_confirm_in_one_step}."
            if ROUTINE_BATCH_WIRED
            else "; parts to confirm in one step are not measured until P11-2."
        ),
        "",
        "What the LemonCrow parts are made of. A changed file is split into the symbols it changes and the hunks outside "
        "any symbol (imports, top-level statements), so one changed file can be more than one part:",
        "",
    ]
    out += _table(
        ["LemonCrow parts to read", "Total"],
        [
            ["symbols", total.to_read_symbols],
            ["hunks outside a symbol", total.to_read_hunks],
            [
                "other (document sections, dependencies, whole files)",
                total.to_read - total.to_read_symbols - total.to_read_hunks,
            ],
        ],
    )
    out += [
        "",
        "### Whole-window upper bound",
        "",
        "The changed-lines figures count what changed. A reader opens more: a hunk with its context, a symbol whole. "
        "Counted that way (hunk bodies with context for the baselines; a symbol part's whole span, a hunk part's "
        "changed lines for LemonCrow) the lines a reader may open are:",
        "",
    ]
    out += _table(
        ["Rule set", "Whole-window lines to read", "Whole-window lines to read or confirm", "Median per transition"],
        [
            [
                "File level",
                _total(rows, "file_level_window"),
                _total(rows, "file_level_window"),
                _median_of(rows, "file_level_window"),
            ],
            [
                "Hunk level",
                _total(rows, "hunk_level_window"),
                _total(rows, "hunk_level_window"),
                _median_of(rows, "hunk_level_window"),
            ],
            [
                lemoncrow,
                _total(rows, "to_read_window"),
                _total(rows, "read_and_confirm_window"),
                _median_of(rows, "read_and_confirm_window"),
            ],
        ],
    )
    fewer, equal, more = _versus(rows, "read_and_confirm_window", "hunk_level_window")
    out += [
        "",
        "The median is of the lines to read or confirm. Read plus confirm is again the like-for-like figure.",
        "",
        f"Per transition against hunk level, LemonCrow's window (read plus confirm) is smaller in {fewer}, equal in {equal} and larger in "
        f"{more} of {count}. The two views point different ways on purpose: neither alone is the reading burden.",
        "",
        "### Do the parts account for every changed line?",
        "",
    ]
    captures = list(data.captures)
    exceptions = [check for check in captures if not check.partitions]
    out += [
        f"The measures above are only comparable if LemonCrow's parts hold each changed line of the packet exactly once. "
        f"For each of the {len(captures)} captured revisions the harness sums the changed lines of the parts per file "
        f"and compares them with the packet's changed lines: {len(captures) - len(exceptions)} of {len(captures)} "
        "captures partition exactly.",
    ]
    if exceptions:
        out += ["", "Exceptions:", ""]
        out += _table(
            [
                "Pull request",
                "Revision",
                "File",
                "Changed lines in the packet",
                "Changed lines in the parts",
                "Parts there",
            ],
            [
                [check.pr_id, check.revision, path, packet_lines, held, kinds]
                for check in exceptions
                for path, packet_lines, held, kinds in check.mismatched
            ],
            text=(2, 5),
        )
        out += [
            "",
            "A part's lines are the `+` and `-` lines its own additions and deletions say it holds. Where a file's parts "
            "hold fewer than the packet's (a dependency part carries no line count), LemonCrow's changed-lines figure "
            "leaves those lines out; where they hold more, it counts some twice.",
        ]
    reasons, identical = _reason_counts(rows)
    names = [name for name in _REASON_ORDER if name in reasons]
    names += sorted(name for name in reasons if name not in _REASON_ORDER)
    out += [
        "",
        "## Why LemonCrow reads a part again",
        "",
        "The reason `reconcile` recorded for each part a reviewer is asked to read. A part whose content is exactly "
        "content the reviewer was shown is asked again for a reason other than a change:",
        "",
    ]
    out += _table(
        ["Reason", "Parts", "Of which content already seen", "Meaning"],
        [
            [
                name,
                reasons[name],
                identical[name] if name in ("hunks_renumbered", NEVER_A_TARGET) else "",
                _REASON_MEANING.get(name, "other `reconcile` reason"),
            ]
            for name in names
        ]
        + [["total", sum(reasons.values()), sum(identical.values()), ""]],
        text=(3,),
    )
    moved = [row for row in rows if row.base_moved]
    moved_parts = _total(moved, "to_read")
    out += [
        "",
        f"The base branch moved in {len(moved)} of {count} transitions. LemonCrow asks for {moved_parts} parts in them "
        f"({_total(moved, 'to_read_in_unchanged_files')} of those in a file whose head text is exactly the previous "
        "revision's: nothing the author wrote changed, so those parts are attributable to the base move) against "
        f"{_total(moved, 'to_read_lines')} changed lines to read and {_total(moved, 'to_confirm_lines')} to confirm for "
        f"LemonCrow ({_total(moved, 'read_and_confirm_lines')} in all), {_total(moved, 'hunk_level_lines')} for hunk level "
        f"and {_total(moved, 'file_level_lines')} for file level.",
        "",
        "### Where LemonCrow shows more changed lines than hunk level",
        "",
    ]
    excess = [row for row in rows if row.read_and_confirm_lines > row.hunk_level_lines]

    def renumbered(row: Transition) -> bool:
        return dict(row.reopen_reasons).get("hunks_renumbered", 0) > 0

    cases: tuple[tuple[str, Callable[[Transition], bool]], ...] = (
        ("the base moved", lambda row: row.base_moved and not renumbered(row)),
        ("hunks were renumbered (the rehunk reset)", lambda row: renumbered(row) and not row.base_moved),
        ("both", lambda row: renumbered(row) and row.base_moved),
        ("neither", lambda row: not renumbered(row) and not row.base_moved),
    )
    out += [
        f"LemonCrow puts more changed lines (to read or to confirm) in front of the person than hunk level in "
        f"{len(excess)} of {count} transitions, {sum(row.read_and_confirm_lines - row.hunk_level_lines for row in excess)} "
        "lines more in all. By what the harness can observe about those transitions:",
        "",
    ]
    out += _table(
        ["In those transitions", "Transitions", "Lines more than hunk level"],
        [
            [
                label,
                sum(1 for row in excess if test(row)),
                sum(row.read_and_confirm_lines - row.hunk_level_lines for row in excess if test(row)),
            ]
            for label, test in cases
        ],
    )
    out += [
        "",
        "The drivers of that excess, in the order the engine meets them:",
        "",
        "- **Granularity.** A symbol or hunk whose content changed is read again whole, so changed lines the reviewer had "
        "already seen inside it are read again. The hunk baseline compares hunks, which are smaller than most symbols.",
        "- **The rehunk reset.** Hunk keys are derived from their ordinal in the file, so when a file's hunk count changes "
        "`reconcile` resets every hunk mark of that file (`revisions._rehunked` and the `hunks_renumbered` branch of "
        "`revisions._next_state`). Hunks whose content did not change are asked again; the reason table counts how many.",
        "- **Base move.** A part is carried across a base move only when its file's authored patch is unchanged "
        "(`classify_movement`); otherwise it is asked again although the author changed nothing in it.",
        "- **Target boundary shifts.** An edit that moves a line across a symbol boundary, or turns a hunk outside any "
        "symbol into part of a symbol, changes the content of every part it touches. The harness does not isolate this "
        "driver: it is the remainder in the neither row.",
        "",
        "### Fail-open check",
        "",
    ]
    behavioural = [row for row in rows if row.behavioural_files]
    fail_open = [row for row in rows if row.fail_open_files]
    known = {(k.pr_id, k.from_revision, k.to_revision, k.path): k for k in KNOWN_FAIL_OPENS}
    out += [
        "A count that falls because a part was wrongly carried as read would look like a better result. The oracle (see "
        f"Method) found a change the reviewer had not seen in {len(behavioural)} of {count} transitions, and a changed "
        f"line that no outstanding part covers in {len(fail_open)} of them. Those are listed here, each classified in "
        "code (`KNOWN_FAIL_OPENS`) by a person who read it: an **engine defect** is a change the engine hides that it "
        "should show, and a fix removes it; a **policy** is the engine doing what it was designed to do, with a hazard "
        "the Limitations name. The gate fails on any new one, and on a new uncovered line in a file already listed.",
    ]
    for kind, heading in (("defect", "Engine defects"), ("policy", "Policy")):
        entries = [
            (row, path, known.get((row.pr_id, row.from_revision, row.to_revision, path)))
            for row in fail_open
            for path in row.fail_open_files
        ]
        listed = [(row, path, k) for row, path, k in entries if k is not None and k.kind == kind]
        if not listed:
            continue
        out += ["", f"#### {heading}", ""]
        out += _table(
            ["Pull request", "Transition", "File", "Lines no part covers", "Tracked"],
            [
                [
                    row.pr_id,
                    f"r{row.from_revision} to r{row.to_revision}",
                    path,
                    "<br>".join("`" + line.replace("|", "\\|") + "`" for line in k.lines),
                    k.tracked,
                ]
                for row, path, k in listed
            ],
            text=(1, 2, 3, 4),
        )
        out += [""] + [f"- `{k.pr_id}` r{k.from_revision} to r{k.to_revision}: {k.why}" for _, _, k in listed]
    if not fail_open:
        out += [
            "",
            "None is listed: no transition has a changed line that no outstanding part covers. The two policy cases this "
            "check once recorded, a definition moved within a shell or Make file, reopen as `part_order_changed`.",
        ]
    batched = [row for row in rows if row.batched_behavioural_files]
    found_by_reading = sum(1 for row in behavioural if not row.fail_open_files and not row.batched_behavioural_files)
    known_batched = {(k.pr_id, k.from_revision, k.to_revision, k.path): k for k in KNOWN_BATCHED_BEHAVIOURAL}
    out += [
        "",
        "#### Batched behavioural",
        "",
        "A part confirmed in one step is one the prover called routine, not one anyone read, so a changed line counts as "
        "reached only when a part the person reads holds it. "
        f"**Batched behavioural: {len(batched)} of {count} transitions** have a changed line that only a batched part "
        f"covers. Of the {len(behavioural)} transitions with a change the reviewer had not seen, {found_by_reading} are "
        "still found by a part the person reads, with no engine fail-open and no batched-only line. Each case is "
        "classified in code (`KNOWN_BATCHED_BEHAVIOURAL`): a **prover** case is a proof that batched a change that "
        "changes behaviour and a fix removes it; an **artefact** is a line the oracle compares as text that is only a "
        "comment or the same code laid out differently. The gate fails on any new one.",
    ]
    if batched:
        out += [""]
        out += _table(
            ["Pull request", "Transition", "File", "Rule", "Classified as", "Lines only the batch covers"],
            [
                [
                    row.pr_id,
                    f"r{row.from_revision} to r{row.to_revision}",
                    path,
                    k.rule if k else "?",
                    k.kind if k else "not classified",
                    "<br>".join(
                        "`" + line.replace("|", "\\|") + "`"
                        for where, line in row.batched_behavioural_lines
                        if where == path
                    ),
                ]
                for row in batched
                for path in row.batched_behavioural_files
                for k in [known_batched.get((row.pr_id, row.from_revision, row.to_revision, path))]
            ],
            text=(1, 2, 3, 4, 5),
        )
        out += [""] + [
            f"- `{k.pr_id}` r{k.from_revision} to r{k.to_revision} ({k.rule}): {k.why}"
            for k in KNOWN_BATCHED_BEHAVIOURAL
        ]
    unclassified = [
        (row, path)
        for row in fail_open
        for path in row.fail_open_files
        if (row.pr_id, row.from_revision, row.to_revision, path) not in known
    ]
    if unclassified:
        out += ["", "#### Not classified", ""]
        out += _table(
            ["Pull request", "Transition", "File"],
            [[row.pr_id, f"r{row.from_revision} to r{row.to_revision}", path] for row, path in unclassified],
            text=(1, 2),
        )
    out += [
        "",
        "## Per pull request",
        "",
        "Totals over the pull request's transitions.",
        "",
    ]
    table_rows = []
    for pr in prs:
        own = by_pr.get(pr.id, [])
        table_rows.append(
            [
                f"[{pr.id}]({pr.url})",
                pr.agent,
                len(pr.revisions),
                len(own),
                _total(own, "file_level_lines"),
                _total(own, "hunk_level_lines"),
                _total(own, "to_read_lines"),
                _total(own, "to_confirm_lines"),
                _total(own, "file_level"),
                _total(own, "hunk_level"),
                _total(own, "to_read"),
                _total(own, "accepted_by_rule"),
            ]
        )
    out += _table(
        [
            "Pull request",
            "Agent",
            "Revisions",
            "Transitions",
            "File lines",
            "Hunk lines",
            "LemonCrow lines to read",
            "LemonCrow lines to confirm",
            "File parts",
            "Hunk parts",
            LEMONCROW_COLUMN,
            "Accepted by rule",
        ],
        table_rows,
        text=(1,),
    )
    out += ["", "## Corpus", ""]
    manifest = data.manifest
    out += [
        f"- Pull requests: {len(prs)}; transitions: {total.transitions}; stored size: {data.corpus_size} bytes "
        f"(cap {CORPUS_MAX_BYTES}).",
        f"- Collected on: {manifest.get('collected_on', 'unrecorded')} by `{manifest.get('collector', 'the corpus collector')}`; "
        f"{manifest.get('candidates_examined', 'unrecorded')} candidates examined.",
        "- Agents: " + ", ".join(f"{name} ({count})" for name, count in sorted(agents.items())) + ".",
        "- Licences: " + ", ".join(f"{name} ({count})" for name, count in sorted(licences.items())) + ".",
        "- Languages (pull requests that change a file of the language): "
        + ", ".join(f"{name} ({count})" for name, count in languages.items())
        + ".",
        "",
        "Why candidates were rejected:",
        "",
    ]
    rejected = manifest.get("rejected", {})
    out += (
        _table(["Reason", "Candidates"], [[reason, rejected[reason]] for reason in sorted(rejected)])
        if rejected
        else ["None recorded."]
    )
    out += ["", "Attribution and how each pull request was identified as agent-written:", ""]
    out += _table(
        ["Pull request", "Licence", "Identified as agent by", "Attribution"],
        [[pr.id, pr.license, pr.identified_by, pr.attribution] for pr in prs],
        text=(1, 2, 3),
    )
    out += ["", "## Method", "", _METHOD, "", "## Limitations", "", _limitations(data.manifest), ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #


DEFAULT_CORPUS = Path("evals/review/read_burden")


def load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / "manifest.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}


def build_sources(directory: Path) -> dict[str, Any]:
    """The fetch list of the corpus in *directory* (``sources.json``): commits and blob ids, never file text.

    Deterministic: the pull requests are in id order and every mapping is written with sorted keys.
    """

    load_corpus(directory)  # validates every record, blob id and licence first
    records = [
        {key: value for key, value in _read_gzip_json(path, RECORD_MAX_BYTES).items() if key != "blobs"}
        for path in sorted((directory / "prs").glob("*.json.gz"))
    ]
    stored = _read_gzip_json(directory / LICENCES_FILE, RECORD_MAX_BYTES)["repositories"]
    used = {str(record["repository"]) for record in records}
    licences = {
        repository: {
            "license": row["license"],
            "sha": row["sha"],
            "files": {path: item["blob"] for path, item in sorted(row["files"].items())},
        }
        for repository, row in sorted(stored.items())
        if repository in used
    }
    return {
        "format": SOURCES_FORMAT,
        "licences": licences,
        "pull_requests": sorted(records, key=lambda record: str(record["id"])),
    }


def sources_text(sources: Mapping[str, Any]) -> str:
    return json.dumps(sources, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def build_report(directory: Path) -> tuple[str, dict[str, Any]]:
    """``(report markdown, expected-results mapping)`` for the corpus stored in *directory*."""

    prs = load_corpus(directory)
    measurement = measure_corpus_full(prs)
    report = render_report(
        ReportInput(
            prs, measurement.transitions, corpus_bytes(directory), load_manifest(directory), measurement.captures
        )
    )
    return report, expected_results(measurement.transitions)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Regenerate the read-burden report from the stored corpus (offline).")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--report", type=Path, default=DEFAULT_CORPUS / "report.md")
    parser.add_argument("--expected", type=Path, default=DEFAULT_CORPUS / "expected.json")
    parser.add_argument(
        "--write-expected", action="store_true", help="also rewrite expected.json (record a deliberate change)"
    )
    parser.add_argument(
        "--write-sources",
        action="store_true",
        help=f"also rewrite {SOURCES_FILE}, the fetch list the public bundle ships (derived, no file text)",
    )
    args = parser.parse_args(argv)
    report, expected = build_report(args.corpus)
    if args.write_expected and (drift := fail_open_drift(expected)):
        print(
            "refusing to write expected.json: the oracle's fail-opens differ from KNOWN_FAIL_OPENS in read_burden.py. "
            "Read each one, classify it there (defect or policy) and run again:\n  " + "\n  ".join(drift),
            file=sys.stderr,
        )
        return 2
    if args.write_expected and (drift := batched_behavioural_drift(expected)):
        print(
            "refusing to write expected.json: the lines only the routine batch covers differ from "
            "KNOWN_BATCHED_BEHAVIOURAL in read_burden.py. Read each one, classify it there (prover or artefact) and "
            "run again:\n  " + "\n  ".join(drift),
            file=sys.stderr,
        )
        return 2
    args.report.write_text(report, encoding="utf-8")
    if args.write_sources:
        (args.corpus / SOURCES_FILE).write_text(sources_text(build_sources(args.corpus)), encoding="utf-8")
    if args.write_expected:
        args.expected.write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
