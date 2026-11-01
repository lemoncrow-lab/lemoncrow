"""Proof of work: what CI reported on the exact commit the review read, and whether the description's claims are backed.

LemonCrow runs no tests. What it can do is read what CI said about the reviewed head and put it next to the verdict,
labelled as CI's word, not LemonCrow's. Three rules keep that honest:

* **Only the reviewed head.** The check runs and the combined commit status are read for that one SHA, and a check run
  whose own ``head_sha`` is another commit is dropped, so a result from an older push is never shown as current.
* **Only a pass is a pass.** A check that was skipped, cancelled, neutral or stale *did not pass* and is listed as such;
  one still queued or running is "still running at review time". LemonCrow's own ``LemonCrow Review`` status and the
  check runs of the workflow run doing this review are left out: they are this review, not proof of anything.
* **A refusal is said.** A token without ``checks: read`` gets a 403 for the check runs; the summary then says the CI
  results are not readable and why, instead of dropping the block.

:func:`claims_tests_pass` finds a description that says the tests pass or CI is green, so the summary can say when
nothing completed and passing on the head backs it. It is a fixed pattern over the author's text, never a model call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: The commit status context LemonCrow writes (``render.STATUS_CONTEXT``); repeated here to keep this module import-free.
OWN_CONTEXT: Final[str] = "LemonCrow Review"
#: The human-review tier status LemonCrow writes (``render.HUMAN_REVIEW_CONTEXT``): no more CI than its own status is.
HUMAN_CONTEXT: Final[str] = "LemonCrow / human review"
OWN_CONTEXTS: Final[frozenset[str]] = frozenset({OWN_CONTEXT, HUMAN_CONTEXT})
#: What the summary says when GitHub refuses the check runs (a 403 or 404 for a token without the permission).
NOT_READABLE: Final[str] = "CI results not readable: the workflow needs `checks: read`"
#: Check runs read in one call. GitHub returns the latest run per name (``filter=latest``), so a page is 100 distinct checks.
PAGE: Final[int] = 100
#: Pages of check runs read on one commit, so at most 500 checks.
# lc-debt: check runs are read up to MAX_CHECK_PAGES pages (500) and statuses one page (100); the summary counts what is
# past the cap as "N more checks not read"; upgrade: summarise by conclusion through the GraphQL rollup when a commit has more.
MAX_CHECK_PAGES: Final[int] = 5

_PASSED: Final[frozenset[str]] = frozenset({"success"})
_FAILED: Final[frozenset[str]] = frozenset({"failure", "timed_out", "startup_failure", "error"})
#: Completed, and not a pass and not a failure: these never count as passing.
_DID_NOT_PASS: Final[frozenset[str]] = frozenset({"neutral", "cancelled", "skipped", "stale", "action_required"})

_CLAIM: Final = re.compile(
    r"(?<![\w-])(?:all\s+|the\s+|unit\s+|integration\s+)*tests?\s+(?:are\s+|now\s+|all\s+)*(?:pass(?:es|ing|ed)?|green)\b"
    r"|(?<![\w-])(?:ci|checks?|builds?|pipeline)\s+(?:is\s+|are\s+|now\s+|all\s+)*(?:green|pass(?:es|ing|ed)?)\b"
    r"|(?<![\w-])passing\s+(?:all\s+)?(?:the\s+)?(?:tests|ci|checks)\b"
    r"|[✅✔]️?\s*(?:all\s+)?(?:tests?|ci|checks?)\b",
    re.IGNORECASE,
)
_HTML_COMMENT: Final = re.compile(r"<!--.*?-->", re.DOTALL)
#: A negation just before a claim ("not all tests pass") makes it no claim.
_NEGATED: Final = re.compile(r"(?:\bnot|n't|\bno|\bnever)\s+(?:all\s+|the\s+|yet\s+)*$", re.IGNORECASE)
#: An unticked checklist item is a template's question, not the author's claim.
_UNTICKED: Final = re.compile(r"^\s*[-*]\s+\[\s\]", re.MULTILINE)


def claims_tests_pass(body: str) -> bool:
    """Whether the pull request's description says its tests pass or its CI is green (``tests pass``, ``CI is green``).

    HTML comments (a template's hints) and unticked checklist items are not the author's words and are skipped.
    """

    text = _HTML_COMMENT.sub(" ", body or "")
    kept = "\n".join(line for line in text.splitlines() if not _UNTICKED.match(line))
    return any(not _NEGATED.search(kept[max(0, found.start() - 24) : found.start()]) for found in _CLAIM.finditer(kept))


@dataclass(frozen=True, slots=True)
class CiCheck:
    """One check run or commit status on the head: its name and ``passed``, ``failed``, ``running`` or ``did_not_pass``."""

    name: str
    outcome: str
    #: GitHub's own word for a ``did_not_pass`` (``skipped``, ``cancelled``, ``neutral`` ...), shown beside the name.
    detail: str = ""


OUTCOMES: Final[tuple[str, ...]] = ("failed", "running", "did_not_pass", "passed")


@dataclass(frozen=True, slots=True)
class CiReport:
    """What CI reported on ``sha`` when the review ran; ``readable`` is false when GitHub refused (``reason`` says why)."""

    sha: str
    checks: tuple[CiCheck, ...] = ()
    readable: bool = True
    reason: str = ""
    #: Checks GitHub has on the head beyond the page that was read.
    unread: int = 0
    #: The combined commit status could not be read (the check runs could): said, not hidden.
    statuses_unreadable: bool = False

    def of(self, outcome: str) -> tuple[CiCheck, ...]:
        return tuple(check for check in self.checks if check.outcome == outcome)

    @property
    def has_pass(self) -> bool:
        """A completed, passing check run or status exists on the head."""

        return any(check.outcome == "passed" for check in self.checks)

    def to_wire(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "readable": self.readable,
            **({"reason": self.reason} if self.reason else {}),
            **{outcome: [check.name for check in self.of(outcome)] for outcome in OUTCOMES},
            **({"unread": self.unread} if self.unread else {}),
        }


def refused(message: str) -> bool:
    """Whether a ``gh api`` error is GitHub refusing the read (a missing permission), not a transient failure."""

    lowered = message.lower()
    return "resource not accessible" in lowered or "http 403" in lowered or "http 404" in lowered


def unreadable(sha: str, message: str) -> CiReport:
    reason = NOT_READABLE if refused(message) else "CI results could not be read: GitHub did not answer"
    return CiReport(sha=sha, readable=False, reason=reason)


def _check_outcome(status: str, conclusion: str) -> tuple[str, str]:
    if status != "completed":
        return "running", ""
    if conclusion in _PASSED:
        return "passed", ""
    if conclusion in _FAILED:
        return "failed", ""
    return "did_not_pass", conclusion if conclusion in _DID_NOT_PASS else (conclusion or "no conclusion")


def _status_outcome(state: str) -> str:
    return {"success": "passed", "pending": "running"}.get(state, "failed")


def from_api(
    sha: str,
    check_runs: object,
    combined: object | None,
    *,
    exclude_run: str = "",
) -> CiReport:
    """The report from ``GET commits/{sha}/check-runs`` and ``GET commits/{sha}/status`` as GitHub returned them.

    *combined* is ``None`` when the status could not be read. *exclude_run* is the id of the workflow run doing this
    review (``GITHUB_RUN_ID``): its own jobs are check runs on the head too, and are not proof of anything.
    """

    runs = check_runs.get("check_runs") if isinstance(check_runs, Mapping) else None
    rows = [row for row in runs if isinstance(row, Mapping)] if isinstance(runs, list) else []
    total = check_runs.get("total_count") if isinstance(check_runs, Mapping) else None
    own = f"/actions/runs/{exclude_run}/" if exclude_run else ""
    checks: list[CiCheck] = []
    for row in rows:
        if str(row.get("head_sha") or sha) != sha:
            continue  # another commit's run: never shown as this one's
        if own and own in f"{row.get('details_url') or ''}{row.get('html_url') or ''}":
            continue
        outcome, detail = _check_outcome(str(row.get("status") or ""), str(row.get("conclusion") or ""))
        checks.append(CiCheck(str(row.get("name") or "unnamed check"), outcome, detail))
    unread = max(total - len(rows), 0) if isinstance(total, int) and not isinstance(total, bool) else 0
    statuses = combined.get("statuses") if isinstance(combined, Mapping) else None
    if isinstance(combined, Mapping) and str(combined.get("sha") or sha) != sha:
        statuses = None
    for row in statuses if isinstance(statuses, list) else ():
        if not isinstance(row, Mapping):
            continue
        context = str(row.get("context") or "")
        if context in OWN_CONTEXTS:
            continue
        checks.append(CiCheck(context or "unnamed status", _status_outcome(str(row.get("state") or ""))))
    ordered = sorted(checks, key=lambda check: (OUTCOMES.index(check.outcome), check.name.lower()))
    return CiReport(sha=sha, checks=tuple(ordered), unread=unread, statuses_unreadable=combined is None)


def names(checks: Sequence[CiCheck], limit: int = 5) -> tuple[list[CiCheck], int]:
    """The first *limit* checks and how many more there are."""

    return list(checks[:limit]), max(len(checks) - limit, 0)


__all__ = [
    "NOT_READABLE",
    "OUTCOMES",
    "OWN_CONTEXT",
    "CiCheck",
    "CiReport",
    "claims_tests_pass",
    "from_api",
    "names",
    "refused",
    "unreadable",
]
