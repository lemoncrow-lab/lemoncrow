"""Route a coding host's own code review into lc review, and name the Review URL.

Claude's ``/code-review`` ends with one ``ReportFindings`` call, or, run in the
background as a forked subagent, with the same findings as a JSON block (see
``forked_review_payload``). Its hook is fire and forget: it names the branch
Review's URL (``branch_review_url``, two quick reads) and hands the report to a
detached worker (``spawn_reported_findings``). The worker captures the change as
a Snapshot exactly as a bare ``lc review`` would (reusing the build the edit
hooks already drafted), retains the report in the session ledger bound to that
Snapshot, and has the local server import it, so the findings appear at that URL.
Local server only: a hosted server cannot read this ledger.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lemoncrow_client.config import ClientConfig

_WORKER = "lemoncrow.pro.capabilities.review.reported_findings"


@dataclass(frozen=True, slots=True)
class ReportedReview:
    url: str
    review_id: str
    revision_id: str
    findings: int


def report_message(host: str, findings: int, url: str) -> str:
    """The line the hook prints; ``url`` is empty when the capture opens a new Review."""
    count = f"{findings} finding{'' if findings == 1 else 's'}"
    where = url or "capturing a new Review; `lc review status` shows its link"
    return f"lc review: {count} from {host.capitalize()} code review → {where}"


_JSON_FENCE = re.compile(r"```json[ \t]*\n")


def _findings_block(text: str) -> list[dict[str, Any]] | None:
    """The last findings array in ``text``, fenced as ```json or bare; ``None`` when there is none."""
    decoder = json.JSONDecoder()
    starts = [match.end() for match in _JSON_FENCE.finditer(text)] or [0]
    found: list[dict[str, Any]] | None = None
    for start in starts:
        try:
            # raw_decode stops at the array's end: backticks or prose after it never matter.
            value, _ = decoder.raw_decode(text[start:].lstrip())
        except ValueError:
            continue
        if isinstance(value, list) and all(isinstance(item, dict) and "summary" in item for item in value):
            found = value
    return found


def forked_review_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """A forked ``/code-review``'s SubagentStop as the ``ReportFindings`` call it was told not to make.

    In the background, ``/code-review`` runs as a forked subagent whose output contract
    is a JSON findings block. Claude Code records the forked skill beside the subagent's
    transcript; only a ``code-review`` fork whose final message carries findings counts.
    """

    transcript = str(payload.get("agent_transcript_path") or "")
    if not transcript.endswith(".jsonl"):
        return None
    sidecar = Path(transcript.removesuffix(".jsonl") + ".forked-skill.json").expanduser()
    try:
        skill = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(skill, dict) or skill.get("skillName") != "code-review":
        return None
    findings = _findings_block(str(payload.get("last_assistant_message") or ""))
    if findings is None:
        return None
    agent = str(payload.get("agent_id") or sidecar.name.removesuffix(".forked-skill.json"))
    return {
        **payload,
        "tool_name": "ReportFindings",
        "tool_use_id": f"subagent:{agent}",
        "tool_input": {"findings": findings},
    }


def branch_review_url(payload: dict[str, Any], *, store_root: Path, config: ClientConfig | None = None) -> str | None:
    """The Review a report will land on, without capturing anything.

    Its URL when this branch has an open Review (capture re-bases onto it), ``""``
    when the capture will open a new one, ``None`` when the report cannot be routed
    at all: no checkout, or a hosted configuration.
    """

    cwd = str(payload.get("cwd") or "").strip()
    if not cwd:
        return None
    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root
    from lemoncrow.pro.capabilities.review.hosted import active_local_review_base, server_review_request

    try:
        repo_root = detect_repo_root(Path(cwd))
    except ValueError:
        return None
    config = config or load_config(cwd=repo_root)
    if config.hosted:
        return None
    review_id, _ = active_local_review_base(
        config, repo_root=repo_root, store_root=store_root, range_mode="working_tree"
    )
    if not review_id:
        return ""
    try:
        overview = server_review_request(config, "GET", f"/api/reviews/{quote(review_id, safe='')}")
    except ClientError:
        return ""
    raw = overview.get("session") if isinstance(overview, dict) else None
    ref = str(raw.get("ref") or "") if isinstance(raw, dict) else ""
    return config.endpoint(f"/{ref}") if ref.startswith("r/") else ""


def spawn_reported_findings(payload: dict[str, Any], *, host: str, store_root: Path) -> None:
    """Hand one report to a detached worker and return at once; the hook never waits for a capture."""
    import subprocess
    import sys

    worker = subprocess.Popen(
        [sys.executable, "-m", _WORKER, "--host", host, "--store-root", str(store_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    assert worker.stdin is not None
    # The worker reads stdin before importing anything heavy, so this write never waits long.
    with worker.stdin:
        worker.stdin.write(json.dumps(payload).encode())


def main(argv: Sequence[str] | None = None) -> int:
    """The detached worker: one report on stdin, captured, recorded and imported."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog=_WORKER)
    parser.add_argument("--host", required=True)
    parser.add_argument("--store-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 1
    if not isinstance(payload, dict):
        return 1
    return 0 if record_reported_findings(payload, host=args.host, store_root=args.store_root) else 1


def record_reported_findings(
    payload: dict[str, Any], *, host: str, store_root: Path, config: ClientConfig | None = None
) -> ReportedReview | None:
    """Capture, publish and record one reported review; ``None`` when there is nothing to bind it to.

    ``config`` defaults to the checkout's own client configuration.
    """

    tool_input = payload.get("tool_input")
    findings = tool_input.get("findings") if isinstance(tool_input, dict) else None
    cwd = str(payload.get("cwd") or "").strip()
    if not isinstance(findings, list) or not cwd:
        return None

    from lemoncrow_client.config import load_config

    from lemoncrow.core.capabilities.session_comment_capture import capture_reported_findings
    from lemoncrow.pro.capabilities.review.gitdiff import (
        default_review_base,
        detect_repo_root,
        resolve_rev_range,
        with_review_base,
    )
    from lemoncrow.pro.capabilities.review.hosted import (
        active_local_review_base,
        capture_server_review,
        server_review_request,
    )

    repo_root = detect_repo_root(Path(cwd))
    config = config or load_config(cwd=repo_root)
    if config.hosted:
        return None
    # The range a bare `lc review` captures: this branch Review's frozen base,
    # else where the branch leaves its remote copy, else the working tree (a
    # clean tree: the last commit). Capture itself re-bases a working-tree range
    # onto the branch Review, so only a clean tree with no remote copy asks here.
    base = default_review_base(repo_root)
    rng = resolve_rev_range(repo_root, working_tree=bool(base))
    if rng.mode != "working_tree":
        _, base = active_local_review_base(
            config, repo_root=repo_root, store_root=store_root, range_mode="working_tree"
        )
        if base:
            rng = resolve_rev_range(repo_root, working_tree=True)
    if base:
        rng = with_review_base(repo_root, base, rng)
    session_id = str(payload.get("session_id") or "").strip()
    # lc-debt: a branch far ahead of its remote copy captures slowly before its findings show (956 files:
    # 48 s, changed symbols 20 s); upgrade path: reuse the draft the edit hooks prewarm for this range.
    captured = capture_server_review(
        config,
        repo_root,
        rng,
        store_root=store_root,
        session_id=session_id or None,
        session_id_is_hint=True,
        limit=40,
        with_impact=True,
        with_provenance=True,
        # Impact and provenance finish in the background; the URL never waits for them.
        defer_post_ready=rng.mode in {"working_tree", "commit_range"},
    )
    raw_revision = captured.response.get("revision")
    revision = raw_revision if isinstance(raw_revision, dict) else {}
    revision_id = str(revision.get("id") or "")
    number = revision.get("revision_number")
    if not revision_id or not isinstance(number, int):
        return None
    review_path = f"/api/reviews/{quote(captured.review_id, safe='')}"
    server_review_request(config, "POST", f"{review_path}/revisions/{number}/publish", body={})
    capture_reported_findings(store_root, payload, host=host, review_id=captured.review_id, revision_id=revision_id)
    # Reading the annotations imports the ledger now, not on the Reader's first poll.
    server_review_request(config, "GET", f"{review_path}/annotations")
    return ReportedReview(
        url=captured.canonical_url,
        review_id=captured.review_id,
        revision_id=revision_id,
        findings=len(findings),
    )


if __name__ == "__main__":
    raise SystemExit(main())
