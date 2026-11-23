"""Hosted part proof: what the repository's CI reported about a changed test, as P11-1 part-proof records.

A hosted review runs no tests and builds no import graph. The only per-test result it ever receives is the
``LemonCrow Proof`` check run that the GitHub Action posts to the pull request's head commit: a table of the pull
request's added or changed test files, each run on the head and on the base (``proof.run.red_verdict``). This module
reads that table (data, never instructions) and turns each row into the same per-part outcome a local run writes, in
the same record, pinned to the Snapshot's exact head and base. LemonCrow adds no compute: only ingest and projection.

What a row can say, and nothing stronger:

``Fails on base``   head passed, base failed. ``proven`` only when the report's base is this Snapshot's base.
``Passes on base``  head passed, base passed too. ``exercised``, never ``proven``.
``Could not run``   by its reason: the test fails on the head (``fails``); the head did not run it (``couldnt_run``);
                    the head passed and the base was undecided (``exercised``: a head-only pass); the sandbox was
                    unusable, so nothing ran (``couldnt_run`` with ``proof_sandbox_missing``); the test fails on the
                    head while the pull request changes a dependency file the run did not install (``couldnt_run``
                    with ``proof_deps_changed``, never ``fails``: the head ran against the base's dependencies).

A part is a changed file, and the file a row names is the part it speaks for. A source file has no row of its own
and hosted has no import graph, so reach to it is *unknown* here and it reads ``couldnt_run`` with the reason
``no_ci_result``, never ``not_tested`` (which would claim reach is known to be empty). When the report says its
sandbox was unusable (``wire.SANDBOX_UNUSABLE_MARK`` ends the ``Sandbox:`` line, or any row says so) every part with
no row reads ``proof_sandbox_missing`` instead: nothing ran, so "CI reported no result" would blame the wrong thing.

A report is believed only when it is for this Snapshot: its head must be the Snapshot's head commit (else it is
dropped whole), and its base must be the Snapshot's merge base or, only when none was recorded, its base commit (else its base-side
claims are dropped and only what the head showed stays; the Action plans on the merge base, so that is the one a
real report names). A report must also say the run was sandboxed (``Sandbox: `bubblewrap```): an unsandboxed run let
the pull request's code write its own results, and a missing or unreadable line is read the same way. Then no row of
it is believed: every part it names reads ``couldnt_run`` with ``proof_unsandboxed``, whichever CLI version wrote it
(the Action itself never runs plainly under GitHub Actions, and its report job does not attest such a run, but an
older version did and a custom workflow can). The check run must be the Action's (``github-actions``) completed ``LemonCrow Proof`` check, as the
provider's signed webhook reported it; the caller (``reconcile.project_check_evidence``) already admits only the
Review's current head. The newest *completed* run for a head decides: a report that cannot be read (a cancelled
rerun, another head, no table) replaces the earlier one with an empty CI record, so parts read ``no_ci_result``
and never keep a stale "proven". The record carries the run's order, and an older run (a webhook delivered late)
never overwrites a newer one.

Which workflow run the report comes from (P12-2). Any workflow job with ``checks: write`` can post a check named
``LemonCrow Proof`` as ``github-actions``, including one in a workflow file a same-repository pull request adds or
edits, and GitHub files a check created with ``GITHUB_TOKEN`` under an existing ``github-actions`` check suite of the
commit, not under the suite of the run that posted it. So nothing on the check (name, app, suite, ``details_url``,
summary) says who posted it, and the check is only a trigger: it names a workflow run (``external_id`` is
``<run id>:<attempt>``) and a commit. The provider side takes everything else from GitHub's own records of *that run*:
the run must be a ``pull_request`` run of this repository for the check's commit under ``.github/workflows/``, and
the report is the ``lemoncrow-proof-report`` artifact the run uploaded, never the check's text (:class:`WorkflowBinding`
holds what the run record says).

The run record cannot say *which pull request* the run was for: two pull requests of one head commit (one into
``main``, one into a branch an attacker pushed with its own workflow file) run the same workflow file at the same head,
and GitHub's ``pull_requests`` field does not tell them apart. So the report file carries a GitHub Actions identity
token (OIDC) the ``proof-report`` job minted, whose audience is the SHA-256 of the head commit and the exact report
text. GitHub signs it, and fills ``ref`` (``refs/pull/<number>/merge``), ``base_ref``, ``repository_id``, ``run_id``,
``run_attempt`` and the workflow files from the run that asked, none of which the pull request's code can choose (the
``proof-run`` job, which executes it, is given no ``id-token`` permission). The provider side verifies the signature
against GitHub's keys and the claims (``github_oidc``); this module judges the part that depends on the Review: the
token's pull request number and base branch must be this Review's (:func:`workflow_trust`).

The report is trusted only when the Snapshot's head is the run's commit, the token names this pull request and base
branch, and neither the workflow file (the token's ``workflow_ref`` and ``job_workflow_ref``), a reusable workflow of
this repository it calls, nor anything under ``.github/actions/`` is among the pull request's changed paths: a pull
request's workflows run from its merge commit, so a file it leaves unchanged is the base's own. A check naming a
trusted run therefore yields only that run's own report, and a check that cannot be bound to one, whose token cannot
be believed, or whose run is not trusted, marks every part it reported ``couldnt_run`` with the reason it could not be
believed (``untrusted_workflow``, ``workflow_unreadable``, ``proof_report_missing``, ``proof_report_invalid``,
``proof_report_unattested``); it never proves anything. The newest check decides, as for any check: a newer rerun that
is unbound, untrusted or has no report leaves the parts ``couldnt_run`` rather than keeping an older ``proven``.

lc-debt (insider ceiling): only a collaborator with push access to the repository can post a check on a head, and such
a collaborator can post a newer ``LemonCrow Proof`` check there that cannot be bound, which makes the parts read
``couldnt_run``. That fails safe (nothing is ever proven by it) and is accepted: it needs the access that already lets
the same person merge a failing change or push to the branch. Upgrade: list the head's real runs and order checks by
the run's own record, ignoring a check that names none.

lc-debt (sandbox ceiling): the identity token is minted only in ``proof-report``, but ``proof-run`` executes the pull
request's code and uploads the artifact the token is bound to, and the artifact-upload credential (``ACTIONS_RUNTIME_TOKEN``)
is in the environment of the job's processes. Under GitHub Actions the run never executes the pull request's code
plainly (P13-2: ``--sandbox auto`` and ``none`` need bubblewrap there too, and without it nothing runs, reading
``proof_sandbox_missing``), so the test child cannot read that credential, and the CLI looks ``bwrap``, ``git``,
``env`` and ``systemd-run`` up on the system path, never on ``PATH``, which a wheel's console script could shadow.
What stays outside the sandbox is whatever the workflow runs before the proof, in the same job. The documented
workflow installs the test dependencies from the *base* commit's requirements file, outside the head checkout and
wheels only, so no pull request code builds or runs there; a workflow that installs the head's own dependency list
instead runs a package's build script as the pull request's code, and a leftover process of it can read the credential
from ``/proc/<pid>/environ`` and upload another report: it can then forge what the report says about the *base* side
(``proven``), though never mint the identity token, so it still cannot make a report GitHub did not run pass for this
pull request. The same holds on a self-hosted runner whose jobs share a machine: a job's leftovers can outlive it, and
the server cannot tell the two apart. The base's dependencies are also what the head's tests run against, so a test
that imports a dependency the pull request adds cannot load and reads ``couldnt_run``, never ``proven``. Upgrade: a
prebuilt image, or an ephemeral runner per job; have ``proof-report`` re-run the base side itself, or sign the report
with a key only the trusted job holds.

lc-debt: source files get no CI-reported outcome (no import graph on hosted). A report is read for its
``Red-on-base`` section only, from at most the first ``MAX_SUMMARY`` characters of the summary and at most
``MAX_SECTION`` characters of that section; a row past either is not read. Upgrade: build the base import graph in a
metered sandboxed hosted job and credit parts through ``test_impact.changed_reach``; carry the rows as structured
check output instead of a Markdown table.

lc-debt: a write that fails after the check's evidence row exists (a database error) is not retried, because a
redelivery of the same check is deduplicated before this module runs; parts read ``no_ci_result`` until the next
run of the check. Upgrade: retry the record on the dedupe hit, now that the record is idempotent for one run order.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review import part_proof as pp
from lemoncrow.pro.capabilities.review.proof import wire
from lemoncrow.pro.capabilities.review.proof.report import RED_LABELS

#: Only the GitHub Action's own check run speaks for the proof job (the Action posts with the workflow's token).
CI_INTEGRATIONS: Final[frozenset[str]] = frozenset({"github-actions"})
#: The summary the provider fact keeps for this check (``github_facts.PROOF_DETAIL_LIMIT``): GitHub's own cap.
MAX_SUMMARY: Final[int] = 65_536
#: How much of the ``Red-on-base`` section is read, counted from its header (findings above it do not eat into it).
MAX_SECTION: Final[int] = 32_768
#: The longest run order kept in a record (``<started>|<20-digit id>:<attempt>`` is about 55).
MAX_ORDER: Final[int] = 100
_ZWSP: Final[str] = "\u200b"
#: A run with no start time sorts below every dated one (the provider side writes it as ``"<this>|<id>"``; a value
#: stored as ``"|<id>"`` before that was fixed is read the same way). Same value as ``facts.UNDATED_RUN``.
UNDATED_ORDER: Final[str] = "0000-00-00T00:00:00.000000Z"

_BY_LABEL: Final[Mapping[str, str]] = {label: outcome for outcome, label in RED_LABELS.items()}
_HEADER: Final[re.Pattern[str]] = re.compile(r"\*\*[^\n]{1,260}?\*\* on `([0-9a-f]{12})` against base `([0-9a-f]{12})`")
_SANDBOX: Final[re.Pattern[str]] = re.compile(r"\r?\n\r?\nSandbox: `([^`\n]{0,40})`([^\n]{0,600})")
_SECTION: Final[re.Pattern[str]] = re.compile(r"^\*\*Red-on-base\*\*:", re.MULTILINE)
_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\| `([^`|\n]{1,200})` \| (" + "|".join(re.escape(label) for label in _BY_LABEL) + r") \| ([^|\n]{0,300}) \|$"
)
_HEAD_NOT_PASSED: Final[re.Pattern[str]] = re.compile(
    "^" + re.escape(wire.RED_HEAD_NOT_PASSED).replace(r"\{\}", "[a-z]{1,16}") + "$"
)
_BASE_UNDECIDED: Final[re.Pattern[str]] = re.compile(
    "^" + re.escape(wire.RED_BASE_UNDECIDED).replace(r"\{\}", "[a-z]{1,16}") + "$"
)


@dataclass(frozen=True, slots=True)
class CiReport:
    """One ``LemonCrow Proof`` check run's claims: the commits it names and one ``(path, outcome, reason)`` per row."""

    head12: str
    base12: str
    #: The report says the run was sandboxed (``bubblewrap``). Only then can a base-side claim be believed.
    sandboxed: bool
    rows: tuple[tuple[str, str, str], ...]
    #: The ``Sandbox:`` line ends with ``wire.SANDBOX_UNUSABLE_MARK``: the job had no working sandbox and ran nothing.
    sandbox_missing: bool = False


#: The only place a trusted workflow file can live (GitHub runs no other path as a repository workflow).
WORKFLOW_DIR: Final[str] = ".github/workflows/"
#: Local composite actions: a pull request that changes any of them can change what a trusted workflow runs.
ACTIONS_DIR: Final[str] = ".github/actions/"
#: Why a check was not trusted (log text; the wire reason is the one the check was marked with).
UNBOUND: Final[str] = "no workflow run could be bound to the check"
#: The wire reason of a check whose run is not one a pull request leaves as the base has it.
UNTRUSTED: Final[str] = "untrusted_workflow"
#: The wire reason of a part whose report says the run was not in a sandbox (or does not say it was).
UNSANDBOXED: Final[str] = "proof_unsandboxed"
#: The wire reason of a part whose changed test the proof job did not run for want of a working sandbox.
SANDBOX_MISSING: Final[str] = "proof_sandbox_missing"
#: The wire reason of a part whose changed test failed on head while the pull request changes a dependency file the
#: proof run did not install, so the failure is not shown to be the test's own.
DEPS_CHANGED: Final[str] = "proof_deps_changed"


@dataclass(frozen=True, slots=True)
class WorkflowBinding:
    """What GitHub's Actions records say about the workflow run a proof check names (and whose artifact is the report).

    ``bound`` is false when the run could not be read or the check does not belong to it. ``path`` is the
    repository-relative workflow file the run executed, ``head_sha`` the commit the run is for, and ``refs`` the
    repository-relative paths of the reusable workflows of this same repository that it calls. ``pull`` and
    ``base_ref`` are the pull request number and base branch the run's identity token names.
    """

    bound: bool = False
    path: str = ""
    head_sha: str = ""
    refs: tuple[str, ...] = ()
    pull: str = ""
    base_ref: str = ""


def changed_paths(packet: object) -> frozenset[str] | None:
    """Every path (and rename source) the Snapshot's packet lists as changed; ``None`` when it cannot be read."""

    files = packet.get("files") if isinstance(packet, dict) else None
    if not isinstance(files, list):
        return None
    out: set[str] = set()
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            return None
        out.add(row["path"])
        if isinstance(row.get("old_path"), str):
            out.add(row["old_path"])
    return frozenset(out)


def _norm(path: str) -> str:
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.casefold()


def workflow_trust(
    binding: WorkflowBinding | None, revision: Any, changed: frozenset[str] | None, *, pull: str, base_ref: str
) -> str:
    """``""`` when the check came from a workflow file this pull request leaves as the base has it, else why not.

    *pull* and *base_ref* are the Review's own pull request number and base branch; the run's token must name them.
    Fails closed: no binding, a run for another commit, another pull request or base branch (or none known), a file
    outside ``.github/workflows/``, or a changed-path list that could not be read are all untrusted. Paths compare
    case-insensitively (stricter than GitHub's own).
    """

    if binding is None or not binding.bound:
        return UNBOUND
    path = _norm(binding.path)
    if not path.startswith(WORKFLOW_DIR) or ".." in path.split("/"):
        return "the check's run did not execute a repository workflow file"  # also any ``dynamic/...`` run
    head = _sha(getattr(revision, "head_sha", "")).lower()
    if not head or binding.head_sha.strip().lower() != head:
        return "the check's workflow run is for another commit"
    if not pull or not base_ref or binding.pull != pull or binding.base_ref != base_ref:
        return "the check's workflow run was for another pull request or base branch"
    if changed is None:
        return "the pull request's changed files are not known"
    touched = {_norm(item) for item in changed}
    if path in touched or any(_norm(ref) in touched for ref in binding.refs):
        return "the pull request adds or changes the workflow that posted the check"
    if any(item.startswith(ACTIONS_DIR) for item in touched):
        return "the pull request changes a local action under .github/actions"
    return ""


def reports_proof(*, integration: str, name: str, state: str, actor_kind: str) -> bool:
    """The check run is the Action's completed ``LemonCrow Proof`` check (an exact match, never a prefix)."""

    return integration in CI_INTEGRATIONS and name == wire.CHECK_NAME and state == "completed" and actor_kind == "app"


def parse_report(summary: object) -> CiReport | None:
    """The report in a check run's summary, or ``None`` when it is not one (never raises on hostile text).

    Rows are read from the ``Red-on-base`` section only, bounded (``wire.MAX_ITEMS`` rows, ``MAX_SECTION``
    characters), a path must be a safe repository-relative path, and a line that is not exactly the table row the
    Action writes is ignored. Nothing here is kept as markup: only a path that a changed file of the Snapshot also
    names, and an outcome from the fixed vocabulary, ever leave this module.
    """

    if not isinstance(summary, str):
        return None
    text = summary[:MAX_SUMMARY]
    header = _HEADER.match(text)
    if header is None:
        return None
    mode = _SANDBOX.match(text, header.end())
    sandboxed = mode is not None and mode.group(1) == "bubblewrap"
    rows: list[tuple[str, str, str]] = []
    section = _SECTION.search(text, header.end())
    if section is not None:
        for line in text[section.start() : section.start() + MAX_SECTION].splitlines():
            match = _ROW.match(line)
            if match is None:
                continue
            # ``render.clean`` writes a zero-width space after an ``@`` so a path cannot mention anyone.
            path = match.group(1).replace("@" + _ZWSP, "@").split("::", 1)[0]
            if not wire.SAFE_PATH.match(path):
                continue
            rows.append((path, _BY_LABEL[match.group(2)], match.group(3).strip()))
            if len(rows) >= wire.MAX_ITEMS:
                break
    missing = sandboxed and mode is not None and mode.group(2).rstrip().endswith(wire.SANDBOX_UNUSABLE_MARK)
    return CiReport(header.group(1), header.group(2), sandboxed, tuple(rows), missing)


def _sha(value: object) -> str:
    return value if isinstance(value, str) and wire.SHA.match(value) else ""


def _row_outcome(outcome: str, reason: str, *, base_is_ours: bool) -> pp.PartOutcome | None:
    """What one row shows about the head and (when the base is this Snapshot's) the base; ``None``: no claim."""

    if outcome == "fails_on_base":
        return pp.PartOutcome(pp.PROVEN if base_is_ours else pp.EXERCISED)
    if outcome == "passes_on_base":
        return pp.PartOutcome(pp.EXERCISED)
    if reason == wire.RED_HEAD_FAILED:
        return pp.PartOutcome(pp.FAILS)
    if reason == wire.RED_DEPS_CHANGED:
        return pp.PartOutcome(pp.COULDNT_RUN, DEPS_CHANGED)
    if reason == wire.RED_SANDBOX_MISSING:
        return pp.PartOutcome(pp.COULDNT_RUN, SANDBOX_MISSING)
    if _HEAD_NOT_PASSED.match(reason):
        return pp.PartOutcome(pp.COULDNT_RUN, "ci_not_run")
    if _BASE_UNDECIDED.match(reason):
        # The head passed and the base could not say: a head-only pass, never a differential.
        return pp.PartOutcome(pp.EXERCISED)
    return None


def outcomes_for(report: CiReport, revision: Any, parts: frozenset[str]) -> dict[str, pp.PartOutcome] | None:
    """Per changed file, what the report shows for this Snapshot; ``None`` when it is for another head."""

    head = _sha(getattr(revision, "head_sha", ""))
    if getattr(revision, "range_mode", "") != "commit_range" or not head or head[:12] != report.head12:
        return None
    if not report.sandboxed:
        # Whatever CLI version wrote it: a run that was not in a sandbox let the pull request's code write its own
        # results, head side and base side alike, so no row of it is believed and every part it names says why.
        return {
            path: pp.PartOutcome(pp.COULDNT_RUN, UNSANDBOXED)
            for path in {row[0] for row in report.rows}
            if path in parts
        }
    # The Action plans on the merge base, else the base tip (``merge_base_sha or base_sha``): that one commit is
    # this Snapshot's base. A recorded merge base never also admits the tip: a commit only the base branch has can
    # make a test fail there for a reason this change is not responsible for. A base that is not it leaves only
    # what the head showed.
    base = _sha(getattr(revision, "merge_base_sha", "")) or _sha(getattr(revision, "base_sha", ""))
    ours = {base[:12]} if base else set()
    base_is_ours = report.base12 in ours
    files: dict[str, pp.PartOutcome] = {}
    for path, outcome, reason in report.rows:
        if path not in parts:
            continue
        row = _row_outcome(outcome, reason, base_is_ours=base_is_ours)
        if row is not None:
            files[path] = pp.stronger(files.get(path), row) or row
    return files


def _order(raw: object) -> str:
    """A run order as compared: bounded, and an undated ``"|<id>"`` read as lowest."""

    text = str(raw or "")[:MAX_ORDER]
    return UNDATED_ORDER + text if text.startswith("|") else text


def record_report(
    store: Any, revision: Any, summary: object, *, run_order: str = "", untrusted: str = ""
) -> pp.ProofRecord:
    """Store what one completed ``LemonCrow Proof`` run shows for *revision*, as its part-proof record.

    The record is the newest run's report: it replaces an earlier report for the same head (a rerun corrects it,
    and a rerun that cannot be read leaves an empty CI record, never the earlier "proven") and is pinned to the
    Snapshot's exact head and base like every record, so it is deleted with the Snapshot and never served for
    another. *run_order* (the check run's order key) is stored with it; a run older than the stored one changes
    nothing, so webhook deliveries in any order converge on the newest run.

    An *untrusted* check (*untrusted* is the reason code it could not be believed for, one of
    ``pp.UNTRUSTED_CI_REASONS``) proves nothing: every part its report names, for this head, reads ``couldnt_run``
    with that reason whatever the row said, and its other claims are dropped. It is ordered like any other run: the
    newest check decides, so one nobody can verify replaces an older trusted report (insider ceiling in the module
    docstring).
    """

    report = parse_report(summary)
    parts = frozenset(str(unit.path) for unit in store.list_units(revision.id) if unit.kind == "file")
    outcomes = outcomes_for(report, revision, parts) if report is not None else None
    files = outcomes or {}
    order = _order(run_order)
    # A part with no row (a source file, a pull request that changes no test) reads the reason nothing ran when the run
    # had no working sandbox, instead of "CI reported no result": that is a believed report's own word, so never for an
    # untrusted check or a report for another head.
    sandbox_missing = (
        not untrusted
        and report is not None
        and outcomes is not None
        and (report.sandbox_missing or any(row.reason == SANDBOX_MISSING for row in outcomes.values()))
    )
    if untrusted:
        reason = untrusted if untrusted in pp.UNTRUSTED_CI_REASONS else UNTRUSTED
        # A report for another commit (``outcomes`` is ``None``) says nothing about this one, trusted or not.
        named = (
            {row[0] for row in report.rows if row[0] in parts} if report is not None and outcomes is not None else ()
        )
        files = {path: pp.PartOutcome(pp.COULDNT_RUN, reason) for path in named}
    head, base = pp.identities(revision)

    def change(old: pp.ProofRecord | None) -> pp.ProofRecord:
        if old is not None and old.source == pp.CI_SOURCE and _order(old.ci_order) > order:
            return old
        return pp.ProofRecord(
            revision_id=str(revision.id),
            head_identity=head,
            base_identity=base,
            files=files,
            default=pp.COULDNT_RUN,
            default_reason=SANDBOX_MISSING if sandbox_missing else "no_ci_result",
            source=pp.CI_SOURCE,
            complete=True,
            ci_order=order,
        )

    return pp.update_record(store, revision, change)
