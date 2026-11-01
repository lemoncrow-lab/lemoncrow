"""Post what the proof run showed: a ``LemonCrow Proof`` check run and a reply on each finding's thread.

This step may write to the pull request and runs no pull request code. The plan file is the plan job's (no
pull request code ran there), so its finding ids, thread ids and generated tests are trusted. The results file
came from the job that ran pull request code, so it is checked before anything in it is shown: it must name the
plan's head and the plan's digest, every outcome must be in vocabulary, and a pass or a reproduced bug is
believed only beside a recorded run that agrees with it. Every text from it is bounded and made inert.

Conclusion: ``failure`` when a finding is reproduced (``supported``); ``success`` only when runs were recorded
and every finding was contradicted and every changed test fails on base; ``neutral`` otherwise, including when
nothing could run. ``could_not_run`` is never ``success``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from lemoncrow.pro.capabilities.review.proof import wire

_ZWSP: Final[str] = "​"
PROOF_MARKER: Final[str] = "<!-- lemoncrow:proof {finding_id} {head_sha} -->"
REPLAY: Final[str] = "lc review proof run --local --plan lemoncrow-proof-plan.json"

REPRO_LABELS: Final[Mapping[str, str]] = {
    "supported": "Supported",
    "contradicted": "Contradicted",
    "inconclusive": "Inconclusive",
    "could_not_run": "Could not run",
}
RED_LABELS: Final[Mapping[str, str]] = {
    "fails_on_base": "Fails on base",
    "passes_on_base": "Passes on base",
    "could_not_run": "Could not run",
}
_MEANINGS: Final[str] = (
    "Supported: the reproduction fails on head as claimed and passes on base (or the code is new). "
    "Contradicted: it passes on head. Inconclusive: it fails for another reason, or on base too. "
    "Fails on base: the added test can catch the change being undone. "
    "Passes on base: the added test may not test this change."
)


def _inert(value: object, limit: int = wire.MAX_TEXT) -> str:
    """Untrusted or model-written text, one line, bounded, with nothing it could forge in a comment."""

    from lemoncrow.pro.capabilities.review.pr_bot.render import clean

    return clean(" ".join(wire.text(value, limit * 2).split())[:limit]).replace("|", "/")


_FENCE_LANGUAGE: Final[Mapping[str, str]] = {"python": "python", "js": "js", "go": "go", "rust": "rust"}


def _fenced(text: str, language: str = "") -> str:
    """*text* inside a code fence that it cannot close, with no hidden marker of ours in it."""

    from lemoncrow.pro.capabilities.review.pr_bot.render import fence_for

    body = text.replace("<!--", f"<{_ZWSP}!--").replace("-->", f"-{_ZWSP}->")
    fence = fence_for(body)
    return f"{fence}{_FENCE_LANGUAGE.get(language, '')}\n{body.rstrip()}\n{fence}"


@dataclass(slots=True)
class Finding:
    plan: dict[str, Any]
    outcome: str = "could_not_run"
    reason: str = ""
    runs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Summary:
    head_sha: str
    base_sha: str
    sandbox: str
    sandbox_reason: str
    findings: list[Finding]
    red: list[dict[str, Any]]
    red_overflow: int
    recorded: bool
    notes: list[str]
    #: The plan's digest and what its model calls cost (the plan job's own ``usage``), counted once on the summary.
    plan_digest: str = ""
    plan_usage: dict[str, Any] = field(default_factory=dict)

    def record(self, check_id: int = 0) -> str:
        """This proof as the summary state keeps it (``pr_bot.state.PROOF_STATE``)."""

        counts = {name: min(sum(1 for item in self.findings if item.outcome == name), 9999) for name in REPRO_LABELS}
        failing = min(sum(1 for row in self.red if row["outcome"] == "fails_on_base"), 9999)
        record = (
            f"{self.head_sha[:12]}:s{counts['supported']}:c{counts['contradicted']}:i{counts['inconclusive']}"
            f":n{counts['could_not_run']}:rb{failing}/{min(len(self.red), 9999)}:p{self.plan_digest[:8] or '0' * 8}"
        )
        return record + (f":k{check_id}" if check_id > 0 else "")

    @property
    def conclusion(self) -> str:
        outcomes = [item.outcome for item in self.findings]
        red = [row["outcome"] for row in self.red]
        if "supported" in outcomes:
            return "failure"
        if (
            self.recorded
            and (outcomes or red)
            and all(outcome == "contradicted" for outcome in outcomes)
            and all(outcome == "fails_on_base" for outcome in red)
            and not self.red_overflow
        ):
            return "success"
        return "neutral"

    def title(self) -> str:
        counts = {name: sum(1 for item in self.findings if item.outcome == name) for name in REPRO_LABELS}
        parts = [f"{counts[name]} {REPRO_LABELS[name].lower()}" for name in REPRO_LABELS if counts[name]]
        failing = sum(1 for row in self.red if row["outcome"] == "fails_on_base")
        if self.red:
            parts.append(f"added tests: {failing} of {len(self.red)} fail on base")
        return (", ".join(parts) or "nothing to prove")[:250]


def _agrees(outcome: str, runs: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a reproduction's claimed *outcome* is what its recorded head run shows."""

    head = next((run for run in runs if run["side"] == "head"), None)
    if outcome == "contradicted":
        return head is not None and head["status"] == "passed" and head["exit_code"] == 0
    if outcome == "supported":
        base = next((run for run in runs if run["side"] == "base"), None)
        return (
            head is not None
            and head["status"] == "failed"
            and base is not None
            and base["status"] in {"passed", "failed"}
            and base["exit_code"] is not None
        )
    return True


def summarize(plan: Mapping[str, Any], plan_raw: bytes, results: Mapping[str, Any] | None) -> Summary:
    """Merge the trusted plan with the checked results."""

    head = str(plan.get("head_sha") or "")
    notes = [wire.text(note, 300) for note in plan.get("notes") or () if isinstance(note, str)][:20]
    usable = results is not None
    if results is not None and (results.get("head_sha") != head or results.get("plan_digest") != wire.digest(plan_raw)):
        notes.append("the results file does not belong to this plan (another head or plan); it was not used")
        usable = False
    checked: Mapping[str, Any] = results if usable and results is not None else {}
    by_id = {wire.text(row.get("finding_id"), 80): row for row in wire.rows(checked.get("repros"))}
    findings: list[Finding] = []
    for repro in wire.rows(plan.get("repros")):
        item = Finding(dict(repro))
        findings.append(item)
        if repro.get("status") != "planned":
            item.reason = wire.text(repro.get("reason")) or "no reproduction was planned"
            continue
        row = by_id.get(str(repro.get("finding_id") or ""))
        if row is None:
            item.reason = "no result was recorded for this reproduction"
            continue
        runs = [run for run in (wire.clean_run(raw) for raw in wire.rows(row.get("runs"), 4)) if run is not None]
        outcome = wire.one_of(row.get("outcome"), wire.REPRO_OUTCOMES, "could_not_run")
        reason = wire.text(row.get("reason"))
        if outcome in {"supported", "contradicted"} and not wire.recorded(runs):
            outcome, reason = "could_not_run", "no recorded run backs this outcome"
        elif not _agrees(outcome, runs):
            outcome, reason = "inconclusive", "the recorded runs do not show this outcome"
        item.outcome, item.reason, item.runs = outcome, reason, runs
    red_runs = [run for run in (wire.clean_run(raw) for raw in wire.rows(checked.get("red_runs"))) if run is not None]
    red: list[dict[str, Any]] = []
    for row in wire.rows(checked.get("red_on_base")):
        path = wire.text(row.get("path"), 300)
        if not wire.SAFE_PATH.match(path):
            continue
        outcome = wire.one_of(row.get("outcome"), wire.RED_OUTCOMES, "could_not_run")
        reason = wire.text(row.get("reason"))
        if outcome != "could_not_run" and not wire.recorded([run for run in red_runs if run["side"] == "base"]):
            outcome, reason = "could_not_run", "no recorded base run backs this outcome"
        red.append({"path": path, "test": wire.text(row.get("test"), 200), "outcome": outcome, "reason": reason})
    raw_sandbox = checked.get("sandbox")
    sandbox: Mapping[str, Any] = raw_sandbox if isinstance(raw_sandbox, dict) else {}
    raw_notes = checked.get("notes")
    notes.extend(wire.text(note, 300) for note in (raw_notes if isinstance(raw_notes, list) else [])[:20] if note)
    every = [run for item in findings for run in item.runs] + red_runs
    return Summary(
        head_sha=head,
        base_sha=str(plan.get("base_sha") or ""),
        sandbox=wire.one_of(sandbox.get("mode"), wire.SANDBOXES, "none") if usable else "none",
        sandbox_reason=wire.text(sandbox.get("reason"), 300) if usable else "no usable results",
        findings=findings,
        red=red,
        red_overflow=wire.count(checked.get("red_overflow"), ceiling=100_000),
        recorded=wire.recorded(every),
        notes=notes,
        plan_digest=wire.digest(plan_raw).removeprefix("sha256:"),
        plan_usage=dict(plan["usage"]) if isinstance(plan.get("usage"), dict) else {},
    )


def check_summary(summary: Summary) -> tuple[str, str]:
    """``(summary, text)`` Markdown for the check run's output."""

    lines = [
        f"**{summary.title()}** on `{summary.head_sha[:12]}` against base `{summary.base_sha[:12]}`",
        "",
        f"Sandbox: `{summary.sandbox}` ({_inert(summary.sandbox_reason, 300)})."
        + (
            " Without a sandbox the pull request's code ran with this job's (empty) permissions and could have "
            "written its own results."
            if summary.sandbox == "none"
            else ""
        ),
        "",
    ]
    if summary.findings:
        lines += ["| Finding | Severity | Outcome | Why |", "| --- | --- | --- | --- |"]
        for item in summary.findings:
            plan = item.plan
            where = f"{_inert(plan.get('path'), 200)}:{wire.count(plan.get('line'))}"
            lines.append(
                f"| {_inert(plan.get('title'), 120)} (`{where}`) | {str(plan.get('severity') or '').upper()} "
                f"| {REPRO_LABELS[item.outcome]} | {_inert(item.reason, 300)} |"
            )
        lines.append("")
    else:
        lines += ["No open P0/P1 finding to reproduce.", ""]
    if summary.red:
        lines += ["**Red-on-base**: the pull request's added or changed tests, run on the base commit.", ""]
        lines += ["| Test | Outcome | Why |", "| --- | --- | --- |"]
        for row in summary.red:
            name = f"{row['path']}::{row['test']}" if row["test"] else row["path"]
            lines.append(f"| `{_inert(name, 200)}` | {RED_LABELS[row['outcome']]} | {_inert(row['reason'], 300)} |")
        lines.append("")
    if summary.red_overflow:
        lines += [f"{summary.red_overflow} more changed test file(s) were not run.", ""]
    lines += [_MEANINGS]
    text: list[str] = []
    for item in summary.findings:
        for run in item.runs:
            text += [
                f"### {_inert(item.plan.get('finding_id'), 80)} on {run['side']}: {run['status']} "
                f"(exit {run['exit_code']}, {run['duration_ms']} ms, sandbox {run['sandbox']})",
                "",
                _fenced(" ".join(run["command"])),
                "",
                _fenced(run["output_tail"][-3000:] or "(no output)"),
                "",
            ]
    for note in summary.notes:
        text.append(f"- {_inert(note, 300)}")
    return "\n".join(lines)[:65_000], "\n".join(text)[:65_000]


def check_payload(summary: Summary, *, details_url: str = "") -> dict[str, Any]:
    """``POST /repos/{owner}/{repo}/check-runs`` body for the ``LemonCrow Proof`` check."""

    body, text = check_summary(summary)
    payload: dict[str, Any] = {
        "name": wire.CHECK_NAME,
        "head_sha": summary.head_sha,
        "status": "completed",
        "conclusion": summary.conclusion,
        "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output": {"title": summary.title(), "summary": body, "text": text},
    }
    if details_url:
        payload["details_url"] = details_url
    return payload


def thread_reply(item: Finding, summary: Summary) -> str:
    """The reply on one finding's inline thread."""

    plan = item.plan
    finding_id = str(plan.get("finding_id") or "")
    lines = [
        PROOF_MARKER.format(finding_id=finding_id, head_sha=summary.head_sha),
        f"**LemonCrow Proof: {REPRO_LABELS[item.outcome]}**. {_inert(item.reason, 400)}",
        "",
    ]
    if item.runs:
        sides = ", ".join(
            f"{run['side']} `{(summary.head_sha if run['side'] == 'head' else summary.base_sha)[:7]}`: {run['status']}"
            for run in item.runs
        )
        lines += [
            f"Ran `{_inert(plan.get('test_path'), 200)}` ({_inert(plan.get('framework'), 20)}, sandbox "
            f"`{item.runs[0]['sandbox']}`). {sides}.",
            "",
        ]
    if plan.get("status") == "planned" and isinstance(plan.get("test_code"), str):
        lines += [
            "<details><summary>Reproduction</summary>",
            "",
            _fenced(plan["test_code"], str(plan.get("language") or "")),
            "",
            "</details>",
            "",
        ]
        head = next((run for run in item.runs if run["side"] == "head"), None)
        if head is not None and head["output_tail"]:
            lines += [
                "<details><summary>Head output (tail)</summary>",
                "",
                _fenced(head["output_tail"][-2000:]),
                "",
                "</details>",
                "",
            ]
        lines.append(f"Replay: download the plan artifact and run `{REPLAY}`.")
    return "\n".join(lines)


def record_on_summary(github: Any, summary: Summary, *, check_id: int = 0) -> str:
    """Write this proof into the review summary: its record and the plan's spend in the state, and the ``Proof`` line.

    As a chat answer records itself (``pr_bot.chat._record``): the summary is read right before the write and only
    ``proof`` and ``proof_usage`` change. The plan's spend is counted once per plan (its digest is in the record), so a
    re-run report does not count it twice. The visible line is written only while the summary is for the proof's head.
    """

    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.pr_bot.render import MAX_COMMENT, proof_result_line, with_proof_result
    from lemoncrow.pro.capabilities.review.pr_bot.state import PROOF_STATE, Usage, with_state

    existing, state = github.summary()
    if existing is None or state is None:
        return "no LemonCrow summary with state on this pull request: the proof is not recorded there"
    record = summary.record(check_id)
    before = PROOF_STATE.match(state.proof) if state.proof else None
    counted = before is not None and summary.plan_digest and before["plan"] == summary.plan_digest[:8]
    usage = state.proof_usage if counted else state.proof_usage.plus(Usage.from_wire(summary.plan_usage))
    fresh = replace(state, proof=record, proof_usage=usage)
    body = with_state(existing.body, fresh)
    repo_url = f"https://{github.pr.host}/{github.pr.slug}"
    line = proof_result_line(record, state.head_sha, repo_url=repo_url)
    if line:
        body = with_proof_result(body, line)
    if len(body) > MAX_COMMENT:
        return "the summary is at its size limit: the proof is not recorded there"
    if body != existing.body:
        github.write_summary(body, existing)
    if not line:
        return f"the summary is for {state.head_sha[:12]}, not {summary.head_sha[:12]}: recorded in its state only"
    return f"recorded on the summary for {state.head_sha[:12]}"


def post(
    summary: Summary,
    *,
    repository: str,
    number: int,
    host: str,
    runner: Any,
    details_url: str = "",
    replies: bool = True,
) -> dict[str, Any]:
    """Post the check run and the thread replies with *runner* (a ``gh`` runner). Returns what was posted."""

    from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub
    from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, PullRequestRef, gh_json

    owner, _, name = repository.partition("/")
    payload = check_payload(summary, details_url=details_url)
    created = gh_json(
        runner,
        ["api", "--method", "POST", f"repos/{owner}/{name}/check-runs", "--input", "-"],
        json.dumps(payload, ensure_ascii=False),
    )
    posted: dict[str, Any] = {
        "check_url": str(created.get("html_url") or "") if isinstance(created, dict) else "",
        "conclusion": payload["conclusion"],
        "replies": [],
        "errors": [],
        "summary": "",
    }
    if not number:
        return posted
    github = BotGitHub(PullRequestRef(owner, name, number, host), runner)
    check_id = wire.count(created.get("id"), ceiling=10**19) if isinstance(created, dict) else 0
    try:
        posted["summary"] = record_on_summary(github, summary, check_id=check_id)
    except GitHubCliError as exc:
        posted["errors"].append(f"could not record the proof on the summary: {exc}"[:300])
    targets = [item for item in summary.findings if wire.count(item.plan.get("thread_id"))]
    if not replies or not targets:
        return posted
    try:
        existing = [comment.body for comment in github.review_comments() if comment.by_bot]
    except GitHubCliError as exc:
        posted["errors"].append(f"could not read the review threads: {exc}"[:300])
        return posted
    for item in targets:
        marker = PROOF_MARKER.format(finding_id=item.plan.get("finding_id"), head_sha=summary.head_sha)
        if any(marker in body for body in existing):
            continue
        try:
            reply = github.reply_in_thread(int(item.plan["thread_id"]), thread_reply(item, summary))
        except GitHubCliError as exc:
            posted["errors"].append(f"thread {item.plan['thread_id']}: {exc}"[:300])
            continue
        posted["replies"].append({"finding_id": item.plan.get("finding_id"), "url": reply.url})
    return posted
