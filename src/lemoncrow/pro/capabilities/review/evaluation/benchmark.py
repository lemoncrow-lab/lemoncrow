"""The real-PR review benchmark (G3): run the PR reviewer on pinned pull requests at each depth and record what it cost.

What it answers, per pull request and in aggregate: how many tokens, dollars and model calls a review takes at each
depth; how long it takes; how many P0, P1 and P2 findings it posts; how many of the bugs a later commit fixed it
catches (and which rule said so); how many of its findings match no known bug; what a follow-up push costs with
carry-forward and without it; and, where the pull request carries another reviewer's comments, where the two agree.

It runs the product, not a model of it: :func:`engine.capture_pull_request` and :func:`engine.run_ai_stage` are what
``lc review --pr N --ai`` runs in a customer's CI, with the same run plan, hard call cap, content-access policy and
repository-context pack. A depth is chosen the way a customer chooses it: ``quick`` is ``review.effort: quick`` in the
trusted base's ``.lemoncrow/review.yaml`` (which this module commits into a throwaway copy of the range, so the
diff under review is unchanged), ``auto`` is the default, ``thorough`` and ``deep`` are the Action's ``depth`` input.

Three drivers, one code path:

* ``live``: the model route in the environment (``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY``). It spends money. It
  needs a ceiling (``--max-usd`` and/or ``--max-calls``), refuses to run in CI or under pytest, and stops at the first
  call that would pass the ceiling.
* ``replay``: answers only from fixtures an earlier run recorded (``--record-dir``), by the exact call identity, so a
  changed prompt is a reported miss, never a stale answer.
* ``fake``: a scripted stand-in that confirms a finding wherever the changed code carries a ``BUG:`` marker. It exists
  to check the harness end to end. Its numbers describe the harness and are labelled so in every report.

Nothing here decides that a number may be published; :mod:`benchmark_report` lists what is missing before one may.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.evaluation.baseline_reviews import (
    BaselineComment,
    BaselineError,
    fetch_pull_comments,
    load_comments,
)
from lemoncrow.pro.capabilities.review.evaluation.fixtures import (
    CallKey,
    CaseFixtures,
    FixtureCall,
    FixtureUnavailable,
    LiveRecordingRefused,
    ensure_live_recording_allowed,
    load_case_fixtures,
    stage_for,
    write_case_fixtures,
)
from lemoncrow.pro.capabilities.review.evaluation.real_prs import PushSpec, RealPr, RealPrCorpus
from lemoncrow.pro.capabilities.review.evaluation.scoring import (
    DEFAULT_LINE_WINDOW,
    DEFAULT_SIMILARITY,
    Candidate,
    compare_reviewers,
    match_labels,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution

SCHEMA: Final[int] = 1
DEPTHS: Final[tuple[str, ...]] = ("quick", "auto", "thorough", "deep")
Mode = Literal["live", "replay", "fake"]
#: ``review.effort`` in the trusted base's policy, for the one depth the Action has no input for.
_QUICK_POLICY: Final[str] = "review:\n  effort: quick\n"
_GIT_IDENTITY: Final[dict[str, str]] = {
    "GIT_AUTHOR_NAME": "LemonCrow benchmark",
    "GIT_AUTHOR_EMAIL": "benchmark@lemoncrow.invalid",
    "GIT_COMMITTER_NAME": "LemonCrow benchmark",
    "GIT_COMMITTER_EMAIL": "benchmark@lemoncrow.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
}
#: The most calls a review at each depth may make: the investigator ladder plus its synthesis, and the hard cap of 20.
#: Auto is one synthesis and two investigations, or, for a change with three to six risk signals, up to six
#: investigations of their own and no synthesis (``ai_review.AUTO_FULL_COVERAGE_SIGNALS``).
CALLS_AT_MOST: Final[Mapping[str, int]] = {"quick": 1, "auto": 6, "thorough": 6, "deep": 10}
FAKE_USD_PER_MTOK: Final[tuple[float, float]] = (1.0, 5.0)


class BenchmarkError(RuntimeError):
    """The benchmark cannot run as asked, for a reason the person running it can act on."""


class BudgetStop(RuntimeError):
    """A model call was refused because the run's spending ceiling was reached."""


class PushUnavailable(RuntimeError):
    """A simulated push could not be built; the reason goes in the report."""


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BenchmarkOptions:
    depths: tuple[str, ...] = ("quick", "auto", "deep")
    mode: Mode = "live"
    max_usd: float | None = None
    max_calls: int | None = None
    pushes: bool = True
    with_impact: bool = True
    workdir: Path = Path(".benchmark-work")
    record_dir: Path | None = None
    fixtures_dir: Path | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    line_window: int = DEFAULT_LINE_WINDOW
    similarity: float = DEFAULT_SIMILARITY
    reviewer_logins: tuple[str, ...] = ()
    fetch_baseline: bool = False
    provider: str = "auto"

    def __post_init__(self) -> None:
        unknown = [depth for depth in self.depths if depth not in DEPTHS]
        if unknown or not self.depths:
            raise BenchmarkError(f"depth must be one or more of {', '.join(DEPTHS)}")
        if len(set(self.depths)) != len(self.depths):
            raise BenchmarkError("a depth is listed twice")
        if self.max_usd is not None and self.max_usd <= 0:
            raise BenchmarkError("--max-usd must be positive")
        if self.max_calls is not None and self.max_calls < 1:
            raise BenchmarkError("--max-calls must be at least 1")
        if self.mode == "live" and self.max_usd is None and self.max_calls is None:
            raise BenchmarkError("a live run needs a spending ceiling: pass --max-usd and/or --max-calls")
        if self.mode == "replay" and self.fixtures_dir is None:
            raise BenchmarkError("a replay run needs --fixtures-dir")

    def to_wire(self) -> dict[str, Any]:
        return {
            "depths": list(self.depths),
            "mode": self.mode,
            "max_usd": self.max_usd,
            "max_calls": self.max_calls,
            "pushes": self.pushes,
            "with_impact": self.with_impact,
            "line_window": self.line_window,
            "similarity": self.similarity,
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "recording": self.record_dir is not None,
        }


# --------------------------------------------------------------------------- #
# spend: the ceiling, the price of a call, the record of it
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CallRecord:
    stage: str
    ok: bool
    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    usd: float | None = None
    usd_source: str = "unknown"  # provider | price_table | override | unknown
    context_tokens: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "ok": self.ok,
            "seconds": round(self.seconds, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usd": self.usd,
            "usd_source": self.usd_source,
        }


class Pricer:
    """The dollars one call cost: what the provider reported, else the local price table, else the person's override.

    Never a guess: a call with none of the three has no price (``None``), and a run with a dollar ceiling stops.
    """

    def __init__(self, input_usd_per_mtok: float | None = None, output_usd_per_mtok: float | None = None) -> None:
        self.input_rate = input_usd_per_mtok
        self.output_rate = output_usd_per_mtok

    def can_price(self, model: str) -> bool:
        if self.input_rate is not None and self.output_rate is not None:
            return True
        return self._table(model) is not None

    @staticmethod
    def _table(model: str) -> Any | None:
        from lemoncrow.core.capabilities.pricing import get_model_pricing

        if not model:
            return None
        pricing = get_model_pricing(model)
        return pricing if pricing.known and not pricing.approximate else None

    def price(self, execution: StructuredExecution) -> tuple[float | None, str]:
        usage = execution.usage
        if usage.provider_cost is not None and usage.currency in {None, "USD"}:
            return float(usage.provider_cost), "provider"
        if usage.input_tokens is None or usage.output_tokens is None:
            return None, "unknown"
        table = self._table(execution.model)
        if table is not None:
            return (
                float(
                    table.cost_usd(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_input_tokens or 0,
                        cache_write_tokens=usage.cache_write_input_tokens or 0,
                    )
                ),
                "price_table",
            )
        if self.input_rate is not None and self.output_rate is not None:
            return (
                usage.input_tokens * self.input_rate + usage.output_tokens * self.output_rate
            ) / 1_000_000, "override"
        return None, "unknown"


class SpendLedger:
    """The run's ceiling, shared by every review. Checked before a call, settled after it, safe across threads.

    A call already in flight when the ceiling is reached is not cancelled, so a run can pass ``max_usd`` by what a
    handful of parallel calls cost (at most the deepest depth's investigators). That is stated in the report.
    """

    def __init__(self, max_calls: int | None, max_usd: float | None) -> None:
        self.max_calls = max_calls
        self.max_usd = max_usd
        self.calls = 0
        self.spent_usd = 0.0
        self.unpriced_calls = 0
        self.stopped = ""
        self._lock = threading.Lock()

    def reserve(self) -> None:
        with self._lock:
            if self.stopped:
                raise BudgetStop(self.stopped)
            if self.max_calls is not None and self.calls >= self.max_calls:
                self.stopped = f"the call ceiling of {self.max_calls} was reached"
                raise BudgetStop(self.stopped)
            if self.max_usd is not None and self.spent_usd >= self.max_usd:
                self.stopped = f"the spending ceiling of ${self.max_usd:.2f} was reached"
                raise BudgetStop(self.stopped)
            self.calls += 1

    def settle(self, usd: float | None, model: str) -> None:
        with self._lock:
            if usd is not None:
                self.spent_usd += usd
                return
            self.unpriced_calls += 1
            if self.max_usd is not None and not self.stopped:
                self.stopped = (
                    f"a call to {model or 'the model'} reported no cost and the local price table has none, so "
                    "--max-usd cannot be enforced; pass --input-usd-per-mtok and --output-usd-per-mtok, or use --max-calls"
                )

    def to_wire(self) -> dict[str, Any]:
        return {
            "max_usd": self.max_usd,
            "max_calls": self.max_calls,
            "calls": self.calls,
            "spent_usd": round(self.spent_usd, 6),
            "unpriced_calls": self.unpriced_calls,
            "stopped": self.stopped,
        }


Executor = Callable[..., StructuredExecution]


class RunMeter:
    """A ``structured_executor`` that records each call of one review and charges it to the shared ledger."""

    def __init__(self, inner: Executor, ledger: SpendLedger, pricer: Pricer) -> None:
        self._inner = inner
        self._ledger = ledger
        self._pricer = pricer
        self._lock = threading.Lock()
        self.calls: list[CallRecord] = []
        self.refused = ""

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        stage = stage_for(kwargs.get("output_schema") or {})
        try:
            self._ledger.reserve()
        except BudgetStop as exc:
            self.refused = str(exc)
            raise
        started = time.monotonic()
        try:
            execution = self._inner(**kwargs)
        except Exception:
            with self._lock:
                self.calls.append(CallRecord(stage, False, time.monotonic() - started))
            self._ledger.settle(None, str(kwargs.get("model") or ""))
            raise
        usd, source = self._pricer.price(execution)
        self._ledger.settle(usd, execution.model)
        usage = execution.usage
        with self._lock:
            self.calls.append(
                CallRecord(
                    stage,
                    True,
                    time.monotonic() - started,
                    usage.input_tokens,
                    usage.output_tokens,
                    usd,
                    source,
                )
            )
        return execution


# --------------------------------------------------------------------------- #
# the drivers: scripted, recording, replaying
# --------------------------------------------------------------------------- #

_BUG_MARKER = re.compile(r"BUG:\s*(.+)")


class ScriptedExecutor:
    """A deterministic stand-in for a model. It confirms a finding wherever the changed code carries ``BUG: <title>``.

    It answers the stages the investigator pipeline asks for (hypotheses, investigation, dedupe) from the prompt alone,
    reports tokens in proportion to the prompt, and prices them at a made-up rate. Its only job is to let the harness be
    tested end to end without a key; a report made with it says so.

    It speaks the pull request fields a real model is asked for: a confirmed finding cites the line that carries the
    marker, and a re-check (a ``prior_findings`` block) is answered per finding id as a model reading the code would:
    ``still_present`` while the code it was shown still carries that finding's marker, ``resolved`` once the marker is
    gone from code it was shown, ``unclear`` when it was not shown the finding's code at all.
    """

    def __init__(self, *, report_cost: bool = True, model: str = "scripted") -> None:
        self.report_cost = report_cost
        self.model = model
        self._lock = threading.Lock()
        self.prompts: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        stage = stage_for(kwargs.get("output_schema") or {})
        prompt_text = str(kwargs.get("prompt") or "")
        try:
            prompt = json.loads(prompt_text)
        except ValueError:
            prompt = {}
        with self._lock:
            self.prompts.append(prompt if isinstance(prompt, dict) else {})
        if stage == "hypothesis_generation":
            result: dict[str, Any] = self._hypotheses(prompt)
        elif stage == "finding_investigation":
            result = self._investigation(prompt)
        elif stage == "finding_dedupe":
            result = {"decisions": []}
        else:
            raise BenchmarkError(f"the scripted driver does not script the {stage!r} stage")
        size_in = (len(str(kwargs.get("system") or "")) + len(prompt_text)) // 4
        size_out = max(20, len(json.dumps(result)) // 4)
        usd = (size_in * FAKE_USD_PER_MTOK[0] + size_out * FAKE_USD_PER_MTOK[1]) / 1_000_000
        usage = ModelCallUsage(
            calls=1,
            input_tokens=size_in,
            output_tokens=size_out,
            provider_cost=usd if self.report_cost else None,
            currency="USD" if self.report_cost else None,
        )
        # The pipeline checks that an executor answers as the route it was asked for.
        return StructuredExecution(
            result=result,
            driver=kwargs.get("driver") or "openai",
            model=str(kwargs.get("model") or self.model),
            usage=usage,
        )

    @staticmethod
    def _hypotheses(prompt: Mapping[str, Any]) -> dict[str, Any]:
        wrapper = prompt.get("candidates")
        rows = wrapper.get("value", []) if isinstance(wrapper, Mapping) else []
        return {
            "hypotheses": [
                {
                    "category": row["category"],
                    "target_ids": row["target_ids"],
                    "failure_mode": f"The changed {row['category']} path may be unsound.",
                    "investigation_rationale": "The deterministic triage flagged it.",
                    "priority_rationale": "It is on a consequential path.",
                    "evidence_needed": "Trace the changed lines.",
                    "rule_refs": row.get("rule_refs", []),
                }
                for row in rows
                if isinstance(row, Mapping)
            ]
        }

    @staticmethod
    def _investigation(prompt: Mapping[str, Any]) -> dict[str, Any]:
        hypothesis = prompt["hypothesis"]["value"]
        targets: list[str] = hypothesis["target_ids"]
        source = prompt["snapshot_source"]["value"]
        text = "\n".join(str(source[t].get("text", "")) for t in targets if t in source)
        marker = _BUG_MARKER.search(text)
        result: dict[str, Any] = {
            "status": "not_confirmed",
            "category": hypothesis["category"],
            "target_ids": targets,
            "title": "",
            "explanation": "",
            "consequence": "",
            "evidence_refs": [],
            "rule_refs": [],
        }
        if marker is not None:
            title = marker.group(1).strip()
            result.update(
                status="confirmed",
                title=title,
                explanation=f"The changed code does this: {title}.",
                consequence="Callers of the changed code are affected.",
                evidence_refs=[f"target:{target}" for target in targets],
                lines=ScriptedExecutor._marker_lines(source.get(targets[0]) if targets else None),
            )
        prior = prompt.get("prior_findings")
        asked = prior.get("value", []) if isinstance(prior, Mapping) else []
        result["prior_findings"] = ScriptedExecutor._prior_answers(asked, source)
        return result

    @staticmethod
    def _marker_lines(row: Mapping[str, Any] | None) -> dict[str, int] | None:
        """The file line that carries the marker in the excerpt *row*, as the model's ``lines`` claim; else ``None``."""

        if row is None:
            return None
        for offset, line in enumerate(str(row.get("text", "")).splitlines()):
            if _BUG_MARKER.search(line):
                number = int(row.get("start_line") or 1) + offset
                return {"start_line": number, "end_line": number}
        return None

    @staticmethod
    def _prior_answers(asked: Any, source: Mapping[str, Any]) -> list[dict[str, str]]:
        """One answer per earlier finding, from the code this investigation was shown."""

        answers: list[dict[str, str]] = []
        for row in asked if isinstance(asked, list) else ():
            if not isinstance(row, Mapping):
                continue
            shown = [
                str(source[target].get("text", ""))
                for target in row.get("target_ids") or ()
                if isinstance(source.get(target), Mapping)
            ]
            title = str(row.get("title", "")).strip()
            if not any(shown):
                status = "unclear"  # not shown the finding's code: never "resolved" on that
            elif any(found.group(1).strip() == title for text in shown for found in _BUG_MARKER.finditer(text)):
                status = "still_present"
            else:
                status = "resolved"
            answers.append({"id": str(row.get("id", "")), "status": status})
        return answers


class RecordingExecutor:
    """Wraps a responder and keeps every exact call it answered, with the usage and latency, for later replay."""

    def __init__(self, responder: Executor) -> None:
        self._responder = responder
        self._lock = threading.Lock()
        self.calls: dict[str, FixtureCall] = {}

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        key = CallKey.of(kwargs)
        with self._lock:
            known = self.calls.get(key.key)
        if known is not None:
            return StructuredExecution(
                result=dict(known.response),
                driver=key.driver,  # type: ignore[arg-type]
                model=key.model,
                usage=ModelCallUsage.from_wire(known.usage),
            )
        started = time.monotonic()
        execution = self._responder(**kwargs)
        usage = {**execution.usage.to_wire(), "calls": 1, "seconds": round(time.monotonic() - started, 3)}
        with self._lock:
            self.calls[key.key] = FixtureCall(key=key, response=dict(execution.result), usage=usage)
        return execution


class ReplayExecutor:
    """Answers only from recorded fixtures, by the exact call identity; anything else is a reported miss."""

    def __init__(self, fixtures: CaseFixtures | None, pr: RealPr) -> None:
        self._fixtures = fixtures
        self._pr = pr
        self._by_key = {call.key.key: call for call in fixtures.calls} if fixtures is not None else {}
        self._lock = threading.Lock()
        self.misses: list[str] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        key = CallKey.of(kwargs)
        call = self._by_key.get(key.key)
        if call is None:
            if self._fixtures is None:
                reason = "no fixtures were recorded for this pull request"
            elif self._fixtures.case_input_digest != self._pr.input_digest():
                reason = "the pull request's pinned inputs changed since recording"
            else:
                reason = f"no recorded {key.stage} call has this exact prompt (code, depth or prompt changed)"
            with self._lock:
                self.misses.append(reason)
            raise FixtureUnavailable(reason)
        usage = ModelCallUsage.from_wire(call.usage)
        return StructuredExecution(
            result=json.loads(json.dumps(dict(call.response))),
            driver=key.driver,  # type: ignore[arg-type]
            model=key.model,
            usage=replace(usage, calls=1),
        )

    def recorded_seconds(self) -> float:
        return sum(float(call.usage.get("seconds") or 0) for call in self._by_key.values())


# --------------------------------------------------------------------------- #
# git: a throwaway copy of the range, a policy, a simulated push
# --------------------------------------------------------------------------- #


def git(
    repo: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    raw: bool = False,
) -> str:
    """Run git in *repo* and return its output, stripped unless *raw* (a patch must keep its trailing context line)."""

    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        capture_output=True,
        env={**os.environ, **_GIT_IDENTITY, **(env or {})},
        timeout=900,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise BenchmarkError(f"git {' '.join(args[:3])} failed in {repo.name}: {detail[:400]}")
    text = completed.stdout.decode("utf-8", errors="replace")
    return text if raw else text.strip()


class Workspace:
    """One pull request's repository: pinned commits fetched shallowly, everything else built from plumbing."""

    def __init__(self, pr: RealPr, workdir: Path) -> None:
        self.pr = pr
        self.repo = workdir / pr.id / "repo"
        self._policy: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._pushes: dict[str, str | PushUnavailable] = {}
        self.push_methods: dict[str, str] = {}

    def prepare(self) -> None:
        if not (self.repo / ".git").exists():
            self.repo.mkdir(parents=True, exist_ok=True)
            git(self.repo, "init", "-q")
        remotes = git(self.repo, "remote").split()
        if "origin" not in remotes:
            git(self.repo, "remote", "add", "origin", self.pr.clone_url)
        else:
            git(self.repo, "remote", "set-url", "origin", self.pr.clone_url)
        self.fetch(self.pr.base_sha, self.pr.head_sha)

    def has(self, sha: str) -> bool:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.repo, capture_output=True, check=False
        )
        return completed.returncode == 0

    def fetch(self, *shas: str, depth: int = 1) -> None:
        missing = [sha for sha in dict.fromkeys(shas) if not self.has(sha)]
        if missing:
            git(self.repo, "-c", "protocol.version=2", "fetch", "-q", f"--depth={depth}", "origin", *missing)

    def with_policy(self, base: str, head: str, text: str) -> tuple[str, str]:
        """``(base', head')``: the same range with *text* as the trusted base's ``.lemoncrow/review.yaml``.

        Both commits get the file, so the diff between them, which is what is reviewed, is unchanged.
        """

        key = (base, head, text)
        if key in self._policy:
            return self._policy[key]
        with tempfile.TemporaryDirectory(prefix="lemoncrow-bench-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            blob = git(self.repo, "hash-object", "-w", "--stdin", input_bytes=text.encode("utf-8"))

            def commit(source: str, parent: str | None) -> str:
                git(self.repo, "read-tree", source, env=env)
                git(self.repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},.lemoncrow/review.yaml", env=env)
                tree = git(self.repo, "write-tree", env=env)
                args = ["commit-tree", tree, "-m", "benchmark: review policy"]
                if parent:
                    args += ["-p", parent]
                return git(self.repo, *args, env=env)

            new_base = commit(base, base)
            new_head = commit(head, new_base)
        self._policy[key] = (new_base, new_head)
        return new_base, new_head

    def push_head(self, push: PushSpec, onto: str) -> str:
        """The head after a simulated push, built once per pull request. Raises :class:`PushUnavailable`."""

        cached = self._pushes.get(push.id)
        if isinstance(cached, PushUnavailable):
            raise cached
        if cached is not None:
            return cached
        try:
            head = push.head_sha or self._apply(push, onto)
            if push.head_sha:
                self.fetch(head)
        except (PushUnavailable, BenchmarkError) as exc:
            failure = exc if isinstance(exc, PushUnavailable) else PushUnavailable(str(exc))
            self._pushes[push.id] = failure
            raise failure from exc
        self._pushes[push.id] = head
        return head

    def _apply(self, push: PushSpec, onto: str) -> str:
        """Apply a commit's edits on *onto* in a scratch index and commit the result as a child of *onto*.

        Tried in order: the patch as written; with a three-way merge (the code around it moved); and with no context
        (the code around it changed, but the edited line is still there exactly). The one that worked is recorded.
        """

        self.fetch(push.apply_commit, depth=2)
        try:
            parent = git(self.repo, "rev-parse", f"{push.apply_commit}^")
        except BenchmarkError as exc:
            raise PushUnavailable(f"{push.apply_commit[:10]} has no parent to diff against") from exc
        paths = ["--", *push.apply_paths] if push.apply_paths else []
        # lc-debt: a fix that does not apply on the reviewed head even without context makes that push unavailable (the
        # report says so) instead of being replayed some other way; a corpus of larger pull requests will hit it.
        attempts = (
            ("as written", ["-U3"], ["--cached"]),
            ("three-way merge", ["-U3"], ["--cached", "--3way"]),
            ("without context", ["-U0"], ["--cached", "--unidiff-zero"]),
        )
        with tempfile.TemporaryDirectory(prefix="lemoncrow-bench-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            for method, diff_flags, apply_flags in attempts:
                patch = git(self.repo, "diff", "--binary", *diff_flags, parent, push.apply_commit, *paths, raw=True)
                if not patch.strip():
                    raise PushUnavailable("the commit changes none of apply_paths")
                git(self.repo, "read-tree", onto, env=env)
                try:
                    git(self.repo, "apply", *apply_flags, env=env, input_bytes=patch.encode("utf-8"))
                except BenchmarkError:
                    continue
                self.push_methods[push.id] = method
                tree = git(self.repo, "write-tree", env=env)
                return git(
                    self.repo, "commit-tree", tree, "-p", onto, "-m", f"benchmark push: apply {push.apply_commit[:10]}"
                )
        raise PushUnavailable(f"{push.apply_commit[:10]} does not apply on the reviewed head, even without context")


# --------------------------------------------------------------------------- #
# one review
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FindingRow:
    id: str
    severity: str
    category: str
    path: str
    start_line: int
    end_line: int
    anchor_line: int
    title: str
    explanation: str
    consequence: str
    targets: tuple[str, ...]

    def candidate(self) -> Candidate:
        return Candidate(
            id=self.id,
            path=self.path,
            start_line=self.start_line,
            end_line=self.end_line,
            text=f"{self.explanation}\n{self.consequence}",
            severity=self.severity,
            title=self.title,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "category": self.category,
            "path": self.path,
            "lines": [self.start_line, self.end_line],
            "anchor_line": self.anchor_line,
            "title": self.title,
            "explanation": self.explanation,
            "consequence": self.consequence,
        }


@dataclass(slots=True)
class RunRecord:
    """One execution of the AI stage and everything measured about it."""

    kind: str  # review | push_carry_forward | push_full
    pr_id: str
    depth: str
    head_sha: str
    push_id: str = ""
    status: str = "ran"
    complete: bool = False
    reason: str = ""
    stopped_by_budget: bool = False
    wall_seconds: float = 0.0
    capture_seconds: float = 0.0
    calls: list[CallRecord] = field(default_factory=list)
    findings: list[FindingRow] = field(default_factory=list)
    pr_paths: tuple[str, ...] = ()
    #: Files a model call returned for: what the model was asked about, as against what was only triaged.
    examined_paths: tuple[str, ...] = ()
    targets: int = 0
    rereviewed: int = 0
    unchanged: int = 0
    candidates: int = 0
    investigated: int = 0
    changed_lines: int = 0
    context_tokens: int = 0
    states: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    model: str = ""
    #: Not serialised: what the next push is compared against.
    ai: Any = None

    def usage(self) -> dict[str, Any]:
        calls = self.calls
        tokens_known = all(call.input_tokens is not None and call.output_tokens is not None for call in calls)
        usd_known = all(call.usd is not None for call in calls)
        sources = sorted({call.usd_source for call in calls})
        return {
            "model_calls": len(calls),
            "failed_calls": sum(1 for call in calls if not call.ok),
            "input_tokens": sum(call.input_tokens or 0 for call in calls) if tokens_known else None,
            "output_tokens": sum(call.output_tokens or 0 for call in calls) if tokens_known else None,
            "provider_usd": round(sum(call.usd or 0.0 for call in calls), 6) if usd_known else None,
            "usd_sources": sources,
            "model_seconds": round(sum(call.seconds for call in calls), 3),
            "context_tokens": self.context_tokens,
        }

    def severity_counts(self) -> dict[str, int]:
        counts = {"p0": 0, "p1": 0, "p2": 0}
        for finding in self.findings:
            if finding.severity in counts:
                counts[finding.severity] += 1
        return {**counts, "total": len(self.findings)}

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "pr": self.pr_id,
            "depth": self.depth,
            "push": self.push_id,
            "head_sha": self.head_sha,
            "status": self.status,
            "complete": self.complete,
            "reason": self.reason,
            "stopped_by_budget": self.stopped_by_budget,
            "model": self.model,
            "wall_seconds": round(self.wall_seconds, 3),
            "capture_seconds": round(self.capture_seconds, 3),
            **self.usage(),
            "examined_files": list(self.examined_paths),
            "findings_by_severity": self.severity_counts(),
            "findings": [finding.to_wire() for finding in self.findings],
            "targets": self.targets,
            "rereviewed_targets": self.rereviewed,
            "unchanged_targets": self.unchanged,
            "risk_candidates": self.candidates,
            "investigated": self.investigated,
            "changed_lines": self.changed_lines,
            "states": dict(self.states),
            "errors": list(self.errors),
        }


def _owner_and_name(repository: str) -> tuple[str, str]:
    owner, _, name = repository.partition("/")
    return owner, name


def _finding_rows(ai: Any, head: str) -> list[FindingRow]:
    from lemoncrow.pro.capabilities.review.pr_bot.reconcile import finding_from_confirmed

    rows: list[FindingRow] = []
    for confirmed in ai.findings:
        first = next((t for t in confirmed.target_ids if t in ai.locations), None)
        located = ai.locations.get(first) if first else None
        bot = finding_from_confirmed(confirmed, located, head)
        spans = [
            (start, end)
            for target in confirmed.target_ids
            for side, start, end in ai.spans.get(target, ())
            if side == "new" and start > 0
        ]
        start, end = (min(s for s, _ in spans), max(e for _, e in spans)) if spans else (bot.line, bot.end_line)
        rows.append(
            FindingRow(
                id=bot.id,
                severity=bot.severity,
                category=bot.category,
                path=bot.path,
                start_line=start,
                end_line=max(start, end),
                anchor_line=bot.line,
                title=bot.title,
                explanation=bot.explanation,
                consequence=bot.consequence,
                targets=tuple(confirmed.target_ids),
            )
        )
    return sorted(rows, key=lambda row: (row.path, row.start_line, row.id))


def run_review(
    pr: RealPr,
    workspace: Workspace,
    *,
    kind: str,
    depth: str,
    base: str,
    head: str,
    executor: Executor,
    meter: RunMeter,
    previous: Any = None,
    push_id: str = "",
    with_impact: bool = True,
) -> RunRecord:
    """Capture ``base..head`` into a fresh store and run the AI stage once; measure it. Never raises on a model failure."""

    from lemoncrow.pro.capabilities.review.pr_bot import engine
    from lemoncrow.pro.capabilities.review.sources.github import PullRequestRef, PullRequestSource

    record = RunRecord(kind=kind, pr_id=pr.id, depth=depth, head_sha=head, push_id=push_id)
    policy_depth = "auto" if depth == "quick" else depth
    if depth == "quick":
        base, head = workspace.with_policy(base, head, _QUICK_POLICY)
    owner, name = _owner_and_name(pr.repository)
    source = PullRequestSource(
        ref=PullRequestRef(owner=owner, repo=name, number=pr.pull or 1),
        title=pr.title or pr.id,
        author="benchmark",
        state="open",
        head_sha=head,
        base_ref="main",
        base_sha=base,
        merge_base_sha=base,
        remote="origin",
    )
    with tempfile.TemporaryDirectory(prefix="lemoncrow-bench-store-") as scratch:
        started = time.monotonic()
        try:
            captured = engine.capture_pull_request(workspace.repo, source, Path(scratch), with_impact=with_impact)
        except Exception as exc:
            record.status, record.reason = "error", f"capture failed: {type(exc).__name__}: {exc}"[:400]
            record.errors.append(record.reason)
            return record
        record.capture_seconds = time.monotonic() - started
        started = time.monotonic()
        try:
            ai = engine.run_ai_stage(captured, depth=policy_depth, previous=previous, structured_executor=meter)
        except Exception as exc:
            record.wall_seconds = time.monotonic() - started
            record.status, record.reason = "error", f"{type(exc).__name__}: {exc}"[:400]
            record.errors.append(record.reason)
            record.calls = list(meter.calls)
            return record
        record.wall_seconds = time.monotonic() - started
    record.calls = list(meter.calls)
    record.status = ai.status
    record.complete = bool(ai.complete and ai.ran)
    record.reason = ai.reason
    record.model = ai.model
    record.errors = list(ai.errors)
    record.pr_paths = tuple(ai.files)
    record.examined_paths = tuple(
        sorted({ai.locations[target].path for target in ai.examined if target in ai.locations})
    )
    record.targets = len(ai.target_ids)
    record.rereviewed = len(ai.rereviewed)
    record.unchanged = ai.unchanged
    record.candidates = ai.candidates
    record.investigated = ai.investigated
    record.changed_lines = ai.changed_lines
    record.context_tokens = int(ai.usage.context_tokens)
    record.stopped_by_budget = bool(meter.refused)
    if ai.ran:
        record.findings = _finding_rows(ai, head)
    record.ai = ai
    return record


def carry_state(pr: RealPr, record: RunRecord, depth: str) -> Any:
    """The review state a push is compared against, built the way the runner builds it from the first review."""

    from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
    from lemoncrow.pro.capabilities.review.pr_bot.reconcile import reconcile
    from lemoncrow.pro.capabilities.review.pr_bot.render import short
    from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState

    ai = record.ai
    merged = reconcile(
        None,
        ai.findings,
        locations=ai.locations,
        current_targets=ai.target_ids,
        rereviewed=ai.rereviewed,
        covered=ai.covered,
        head=short(record.head_sha),
    )
    findings: tuple[BotFinding, ...] = tuple(merged.findings)
    return ReviewState(
        head_sha=record.head_sha,
        pull_request=f"{pr.repository}#{pr.pull}",
        depth="auto" if depth == "quick" else depth,
        model=ai.model,
        policy=ai.policy,
        complete=ai.complete,
        reviewed_units=ai.reviewed_units,
        units_truncated=ai.units_truncated,
        findings=findings,
        usage=ai.usage,
        usage_total=ai.usage,
    )


def push_states(pr: RealPr, previous: Any, record: RunRecord) -> dict[str, int]:
    """New, carried forward, fixed, stale and no-longer-reported findings after a push, by the bot's own rules.

    A finding is ``fixed`` only when the re-check said it is resolved and the lines it cited changed
    (``record.ai.answers`` and ``.intact``, exactly what the runner hands ``reconcile``); one the review did not
    report again without that proof is counted as ``unreported``, never as fixed.
    """

    from lemoncrow.pro.capabilities.review.pr_bot.reconcile import reconcile
    from lemoncrow.pro.capabilities.review.pr_bot.render import short

    ai = record.ai
    if ai is None or not ai.ran:
        return {}
    merged = reconcile(
        previous,
        ai.findings,
        locations=ai.locations,
        current_targets=ai.target_ids,
        rereviewed=ai.rereviewed,
        covered=ai.covered,
        head=short(record.head_sha),
        answers=ai.answers,
        intact=ai.intact,
    )
    counts = {"new": 0, "carried": 0, "fixed": 0, "stale": 0, "unreported": 0}
    for finding in merged.findings:
        if finding.state in counts:
            counts[finding.state] += 1
    return counts


# --------------------------------------------------------------------------- #
# the model route
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def scoped_env(updates: Mapping[str, str | None]) -> Iterator[None]:
    """Set (or unset, for ``None``) environment variables for the duration, then put them back."""

    saved = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def stand_in_route(driver: str = "openai", model: str = "") -> dict[str, str | None]:
    """Environment under which the intelligence layer resolves *driver*/*model* with no real credential.

    Used by ``fake`` and ``replay``: nothing is sent to a provider, but the review pipeline still needs a route to
    resolve, and a recorded call is keyed by the driver and model it was recorded under.
    """

    from lemoncrow.pro.capabilities.review.pr_bot import ci_env

    env: dict[str, str | None] = {ci_env.BACKEND_ENV: driver, "OPENAI_API_KEY": "benchmark-stand-in"}
    variable = ci_env._MODEL_VARIABLE.get(driver)
    if variable and model:
        env[variable] = model
    return env


def live_route(options: BenchmarkOptions) -> dict[str, str]:
    """Resolve the route from the environment as the Action does, and check it can be used."""

    from lemoncrow.pro.capabilities.review.pr_bot import ci_env, engine

    route = ci_env.resolve_model_route(os.environ, options.provider)
    if route is None:
        raise BenchmarkError(
            "no model route: set ANTHROPIC_API_KEY or OPENAI_API_KEY (or LEMONCROW_LLM_BACKEND with its provider "
            "variables), then run again"
        )
    updates: dict[str, str] = {}
    scratch: dict[str, str] = dict(os.environ)
    ci_env.apply_model_route(route, scratch)
    for key, value in scratch.items():
        if os.environ.get(key) != value:
            updates[key] = value
    with scoped_env(updates):
        problem, why = engine.route_problem()
        if problem:
            raise BenchmarkError(f"the model route is not usable ({problem}): {why}")
        from lemoncrow.pro.capabilities.review.intelligence_runtime import resolve_driver_model

        resolved = resolve_driver_model(None, None)
    updates["_driver"] = resolved.driver
    updates["_model"] = resolved.model
    return updates


# --------------------------------------------------------------------------- #
# the whole benchmark
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PrResult:
    pr: RealPr
    reviews: dict[str, RunRecord] = field(default_factory=dict)
    pushes: dict[str, dict[str, dict[str, RunRecord | str]]] = field(default_factory=dict)
    comments: list[BaselineComment] = field(default_factory=list)
    baseline_note: str = ""
    skipped: str = ""


EventSink = Callable[[str], None]


def _load_baseline(pr: RealPr, corpus: RealPrCorpus, options: BenchmarkOptions) -> tuple[list[BaselineComment], str]:
    source = pr.baseline
    fetch = bool(pr.pull) and (options.fetch_baseline or (source is not None and source.fetch))
    if source is None and not fetch:
        return [], ""
    try:
        if source is not None and source.comments_file:
            path = (corpus.root / source.comments_file).resolve()
            return load_comments(path, logins=None), f"read {source.comments_file}"
        if fetch:
            if not options.reviewer_logins:
                raise BaselineError("name the baseline reviewer's bot login (--reviewer-login) to fetch its comments")
            comments = fetch_pull_comments(pr.repository, pr.pull, logins=options.reviewer_logins)
            return comments, f"fetched from GitHub ({pr.repository}#{pr.pull})"
    except BaselineError as exc:
        return [], f"baseline comments unavailable: {exc}"
    return [], "no comments file and fetching is off"


def make_executor(
    options: BenchmarkOptions, pr: RealPr, *, scripted: ScriptedExecutor | None
) -> tuple[Executor, Callable[[], None], ReplayExecutor | None]:
    """The model-facing executor for one pull request, and what to call when its reviews are done."""

    replay: ReplayExecutor | None = None
    recorder: RecordingExecutor | None = None
    if options.mode == "replay":
        assert options.fixtures_dir is not None
        replay = ReplayExecutor(load_case_fixtures(options.fixtures_dir, pr.id), pr)
        inner: Executor = replay
    elif options.mode == "fake":
        inner = scripted or ScriptedExecutor()
    else:
        from lemoncrow.pro.capabilities.review.intelligence_runtime import execute_structured

        inner = execute_structured
    if options.record_dir is not None and options.mode != "replay":
        recorder = RecordingExecutor(inner)
        inner = recorder

    def finish() -> None:
        if recorder is None or options.record_dir is None or not recorder.calls:
            return
        drivers = sorted({(call.key.driver, call.key.model) for call in recorder.calls.values()})
        write_case_fixtures(
            options.record_dir,
            CaseFixtures(
                case_id=pr.id,
                case_input_digest=pr.input_digest(),
                recorded_with={
                    "mode": options.mode,
                    "driver": drivers[0][0],
                    "model": drivers[0][1],
                    "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "note": (
                        "scripted responses; no provider was contacted"
                        if options.mode == "fake"
                        else "outputs of a live provider run"
                    ),
                },
                calls=tuple(recorder.calls.values()),
            ),
        )

    return inner, finish, replay


def run_benchmark(
    corpus: RealPrCorpus,
    options: BenchmarkOptions,
    *,
    scripted: ScriptedExecutor | None = None,
    say: EventSink | None = None,
) -> tuple[list[PrResult], SpendLedger, dict[str, Any]]:
    """Run every pull request at every depth, then every simulated push. Returns the results, the ledger and run facts.

    Reviews run depth by depth across all pull requests, so a ceiling that stops the run still leaves every pull request
    with the cheaper depths. A stopped run is a partial result and says so; it is never completed from a guess.
    """

    emit = say or (lambda _message: None)
    if options.mode == "live":
        try:
            ensure_live_recording_allowed()
        except LiveRecordingRefused as exc:
            raise BenchmarkError(str(exc)) from exc
    pricer = Pricer(options.input_usd_per_mtok, options.output_usd_per_mtok)
    ledger = SpendLedger(options.max_calls, options.max_usd)
    facts: dict[str, Any] = {"mode": options.mode, "driver": "", "model": "", "notes": []}
    env: dict[str, str | None]
    if options.mode == "live":
        route = live_route(options)
        facts["driver"], facts["model"] = route.pop("_driver"), route.pop("_model")
        env = dict(route)
        if options.max_usd is not None and facts["driver"] == "openai" and not pricer.can_price(str(facts["model"])):
            raise BenchmarkError(
                f"--max-usd cannot be enforced: the OpenAI route reports no cost and the local price table has no "
                f"exact price for {facts['model']!r}. Pass --input-usd-per-mtok and --output-usd-per-mtok, or use --max-calls."
            )
    elif options.mode == "replay":
        assert options.fixtures_dir is not None
        recorded = [load_case_fixtures(options.fixtures_dir, pr.id) for pr in corpus.prs]
        identities = sorted({ident for item in recorded if item for ident in item.identities()})
        if not identities:
            raise BenchmarkError(f"no fixtures for these pull requests in {options.fixtures_dir}")
        facts["driver"], facts["model"] = identities[0]
        env = stand_in_route(identities[0][0], identities[0][1])
    else:
        scripted = scripted or ScriptedExecutor()
        env = stand_in_route("openai", "")
        with scoped_env(env):
            from lemoncrow.pro.capabilities.review.intelligence_runtime import resolve_driver_model

            resolved = resolve_driver_model(None, None)
        facts["driver"], facts["model"] = resolved.driver, resolved.model
    results: list[PrResult] = []
    workspaces: dict[str, Workspace] = {}
    executors: dict[str, tuple[Executor, Callable[[], None], ReplayExecutor | None]] = {}
    options.workdir.mkdir(parents=True, exist_ok=True)

    with warnings.catch_warnings(), scoped_env({**env, "LEMONCROW_REVIEW_INTELLIGENCE_MAX_COST": None}):
        # Reviewed code is parsed, not run; its own invalid escapes and the like are not this tool's to print.
        warnings.simplefilter("ignore", SyntaxWarning)
        for pr in corpus.prs:
            result = PrResult(pr)
            results.append(result)
            result.comments, result.baseline_note = _load_baseline(pr, corpus, options)
            workspace = Workspace(pr, options.workdir)
            try:
                workspace.prepare()
            except BenchmarkError as exc:
                result.skipped = f"could not fetch the pinned commits: {exc}"
                emit(f"{pr.id}: {result.skipped}")
                continue
            workspaces[pr.id] = workspace
            executors[pr.id] = make_executor(options, pr, scripted=scripted)

        # Phase 1: a full review of every pull request, depth by depth.
        for depth in options.depths:
            for result in results:
                pr = result.pr
                if result.skipped or pr.id not in workspaces:
                    continue
                if ledger.stopped:
                    result.reviews[depth] = _skipped(pr, depth, "review", pr.head_sha, ledger.stopped)
                    continue
                emit(f"{pr.id}: reviewing at {depth}")
                inner, _finish, _replay = executors[pr.id]
                meter = RunMeter(inner, ledger, pricer)
                result.reviews[depth] = run_review(
                    pr,
                    workspaces[pr.id],
                    kind="review",
                    depth=depth,
                    base=pr.base_sha,
                    head=pr.head_sha,
                    executor=inner,
                    meter=meter,
                    with_impact=options.with_impact,
                )

        # Phase 2: a simulated follow-up push, once with carry-forward and once without.
        if options.pushes:
            for depth in options.depths:
                for result in results:
                    pr = result.pr
                    first = result.reviews.get(depth)
                    if first is None or first.ai is None or not first.complete or not pr.pushes:
                        continue
                    for push in pr.pushes:
                        if ledger.stopped:
                            break
                        cell = result.pushes.setdefault(depth, {}).setdefault(push.id, {})
                        try:
                            head = workspaces[pr.id].push_head(push, pr.head_sha)
                        except PushUnavailable as exc:
                            cell["unavailable"] = str(exc)
                            continue
                        if push.id in workspaces[pr.id].push_methods:
                            cell["applied"] = workspaces[pr.id].push_methods[push.id]
                        previous = carry_state(pr, first, depth)
                        inner, _finish, _replay = executors[pr.id]
                        for label, state in (("carry_forward", previous), ("full", None)):
                            emit(f"{pr.id}: push {push.id} at {depth} ({label})")
                            meter = RunMeter(inner, ledger, pricer)
                            run = run_review(
                                pr,
                                workspaces[pr.id],
                                kind=f"push_{label}",
                                depth=depth,
                                base=pr.base_sha,
                                head=head,
                                executor=inner,
                                meter=meter,
                                previous=state,
                                push_id=push.id,
                                with_impact=options.with_impact,
                            )
                            if label == "carry_forward":
                                run.states = push_states(pr, previous, run)
                            cell[label] = run
        for _inner, finish, _replay in executors.values():
            finish()
        for pr_id, (_inner, _finish, replay) in executors.items():
            if replay is not None and replay.misses:
                facts["notes"].append(f"{pr_id}: replay misses: " + "; ".join(sorted(set(replay.misses))))
    facts["ledger"] = ledger.to_wire()
    return results, ledger, facts


def _skipped(pr: RealPr, depth: str, kind: str, head: str, reason: str) -> RunRecord:
    return RunRecord(
        kind=kind, pr_id=pr.id, depth=depth, head_sha=head, status="skipped", reason=reason, stopped_by_budget=True
    )


def score_run(
    pr: RealPr, run: RunRecord, corpus: RealPrCorpus, comments: Sequence[BaselineComment], options: BenchmarkOptions
) -> dict[str, Any]:
    """Catch, noise and agreement for one review of one pull request."""

    out: dict[str, Any] = {}
    candidates = [finding.candidate() for finding in run.findings]
    if pr.labelled and run.complete:
        score = match_labels(
            pr.labels, candidates, mode=corpus.match, tolerance=corpus.line_tolerance, pr_paths=run.pr_paths
        )
        inline = [c for c in score.unmatched if c.severity in {"p0", "p1"}]
        asked = set(run.examined_paths)
        missed_detail = [
            {
                "label": label.id,
                "cause": (
                    "asked_not_reported"
                    if {region.path for region in label.regions} & asked
                    else ("no_risk_candidate" if run.candidates == 0 else "not_asked")
                ),
            }
            for label in score.missed
        ]
        out["labels"] = {
            "missed_detail": missed_detail,
            "expected": len(pr.labels),
            "caught": score.caught,
            "noise": len(score.unmatched),
            "noise_duplicates": len(score.duplicates),
            "noise_inline": len(inline),
            "findings": len(candidates),
            **score.to_wire(),
        }  # "matches" lists the catches; "caught" is their count
    inline_comments = [comment.candidate() for comment in comments if comment.kind == "inline"]
    if inline_comments and run.complete:
        agreement = compare_reviewers(
            candidates, inline_comments, line_window=options.line_window, threshold=options.similarity
        )
        out["agreement"] = agreement.to_wire()
    return out


__all__ = [
    "CALLS_AT_MOST",
    "DEPTHS",
    "FAKE_USD_PER_MTOK",
    "SCHEMA",
    "BenchmarkError",
    "BenchmarkOptions",
    "BudgetStop",
    "CallRecord",
    "FindingRow",
    "PrResult",
    "Pricer",
    "PushUnavailable",
    "RecordingExecutor",
    "ReplayExecutor",
    "RunMeter",
    "RunRecord",
    "ScriptedExecutor",
    "SpendLedger",
    "Workspace",
    "carry_state",
    "git",
    "live_route",
    "make_executor",
    "push_states",
    "run_benchmark",
    "run_review",
    "scoped_env",
    "score_run",
    "stand_in_route",
]
