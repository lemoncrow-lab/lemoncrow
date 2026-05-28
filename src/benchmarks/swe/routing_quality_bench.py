"""Routing QUALITY benchmark - real export replay.

Answers the question: when Atelier recommends downtiering a turn to haiku,
is that recommendation actually safe?

⚠️  Measurement limitations
----------------------------
We never actually ran haiku on these sessions. All signals are PROXIES
for how likely downtiering would have hurt - not a direct counterfactual.
The benchmark explicitly separates what it can and cannot measure:

  MEASURED directly:
    - Tool type used in the downtiered turn (edit = harder)
    - Output token count (more tokens = more reasoning needed)
    - Whether the tool call errored with sonnet (upper bound: haiku can only
      do same or worse, but env errors are NOT model-caused)
    - Whether the model retried the same tool next turn (genuine regression signal)
    - prior_errors context (debugging sessions need smarter models)

  NOT measured (requires haiku replay):
    - Whether haiku would have produced a different/worse output
    - Whether haiku's edit would have been wrong vs sonnet's correct edit

Error attribution
-----------------
A critical distinction: not all errors in downtiered turns are model-caused.

  env_error  - file not found, permission denied, path errors, exit-code from
               bad path. These fail with ANY model tier and should NOT penalise
               the routing recommendation.

  model_error - wrong old_string in Edit (model selected wrong content),
                logical/reasoning error in output, retry of same tool after
                non-env failure. These ARE model-capability-dependent.

Only model_errors count toward the risk score.

Bash risk stratification
------------------------
Bash covers a wide range of complexity:
  bash(ls/cat/grep/find/head/tail/wc) -> retrieval, risk 0.0
  bash(python/pytest/npm/yarn/cargo)  -> test runner, risk 0.5
  bash(git commit/push/sed/awk/tee)   -> write-adjacent, risk 0.6
  bash(unknown or long command)       -> default, risk 0.4

Retry signal
------------
If the turn IMMEDIATELY AFTER a failed downtiered turn calls the same tool
again, that's a genuine quality regression - the model had to retry because
its first attempt was wrong. This has higher signal than immediate_error alone.

Risk formula (revised)
----------------------
  risk = 0.35 x tool_risk
       + 0.20 x output_complexity
       + 0.25 x model_error          (env errors excluded)
       + 0.20 x retry_signal         (same tool next turn after failure)

Classification
--------------
  safe     risk < 0.25   -> clearly fine to downtier
  moderate 0.25-0.55     -> uncertain; haiku probably OK
   risky    >= 0.55       -> haiku likely would have struggled

Quality score = (safe x 1.0 + moderate x 0.6 + risky x 0.0) / total_downtiered
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.capabilities.model_routing.router import ModelRouter, ModelTier

# ---------------------------------------------------------------------------
# Tool risk - base scores
# ---------------------------------------------------------------------------

_TOOL_RISK_BASE: dict[str, float] = {
    # Write/edit - precision critical; wrong old_string = hard failure
    "edit": 1.0,
    "write": 1.0,
    "multiedit": 1.0,
    "notebookedit": 1.0,
    "todowrite": 0.6,
    # Sub-agent / orchestration - needs full reasoning
    "agent": 0.9,
    "skill": 0.7,
    # Bash - stratified below by command content
    "bash": 0.4,  # default; refined by _bash_risk()
    "shell": 0.4,
    # Pure retrieval - haiku handles these fine
    "read": 0.0,
    "grep": 0.0,
    "glob": 0.0,
    "webfetch": 0.1,
    "websearch": 0.1,
    "toolsearch": 0.1,
    "askuserquestion": 0.1,
}

# Bash command patterns for risk stratification
_BASH_RETRIEVAL = re.compile(
    r"^\s*(ls|cat|head|tail|wc|find|echo|pwd|which|type|file|stat|du|df"
    r"|grep|egrep|fgrep|rg|ag|ripgrep|sort|uniq|cut|tr|column)\b",
    re.IGNORECASE,
)
_BASH_TEST_RUNNER = re.compile(
    r"\b(python|python3|pytest|py\.test|npm\s+test|yarn\s+test|jest|"
    r"cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|make\s+test)\b",
    re.IGNORECASE,
)
_BASH_WRITE = re.compile(
    r"\b(git\s+commit|git\s+push|git\s+add|sed\s+-i|awk\s+.*>\s*|tee\b|"
    r"mv\b|cp\b|rm\b|mkdir\b|touch\b|chmod\b|chown\b|install\b)\b",
    re.IGNORECASE,
)

# Environment error patterns - these fail with ANY model, not model-caused
_ENV_ERROR_PATTERNS = re.compile(
    r"\b(no\s+such\s+file|file\s+does\s+not\s+exist|permission\s+denied|"
    r"not\s+a\s+directory|is\s+a\s+directory|cannot\s+open|"
    r"command\s+not\s+found|not\s+found\s+in\s+PATH|"
    r"address\s+already\s+in\s+use|connection\s+refused|"
    r"network\s+unreachable|ENOENT|EACCES|EPERM)\b",
    re.IGNORECASE,
)

# Model-caused error patterns - these are capability-dependent
_MODEL_ERROR_PATTERNS = re.compile(
    r"\b(old_string|string\s+not\s+found|no\s+match|"
    r"syntaxerror|typeerror|nameerror|attributeerror|"
    r"assertion\s+error|failed\s+assertion|"
    r"validation\s+error|schema\s+error|"
    r"test\s+failed|tests?\s+failed|FAILED\b)\b",
    re.IGNORECASE,
)


def _bash_risk(command_text: str) -> float:
    """Stratify bash risk by inspecting the command string."""
    if not command_text:
        return 0.4
    # Pure retrieval commands
    if _BASH_RETRIEVAL.match(command_text):
        return 0.0
    # Test runners - significant reasoning in interpreting failures
    if _BASH_TEST_RUNNER.search(command_text):
        return 0.5
    # Write-adjacent operations
    if _BASH_WRITE.search(command_text):
        return 0.6
    return 0.4  # unknown bash = default medium


def _tool_risk(tool_name: str, tool_input: dict[str, Any] | None = None) -> float:
    """Return risk score for a tool_use block."""
    name = tool_name.lower().strip()
    base = _TOOL_RISK_BASE.get(name, 0.3)
    if name in ("bash", "shell") and tool_input:
        cmd = str(tool_input.get("command", tool_input.get("cmd", "")))
        base = _bash_risk(cmd)
    return base


def _output_complexity(output_tokens: int) -> float:
    if output_tokens > 1_000:
        return 1.0
    if output_tokens > 400:
        return 0.6
    if output_tokens > 100:
        return 0.3
    return 0.0


def _classify_error(content: Any) -> str:
    """Classify error content as 'env', 'model', or 'none'."""
    if isinstance(content, list):
        text = " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    else:
        text = str(content or "")
    if not text:
        return "none"
    if _ENV_ERROR_PATTERNS.search(text):
        return "env"
    if _MODEL_ERROR_PATTERNS.search(text):
        return "model"
    # Fallback: if is_error is set but no pattern matched, classify as model
    # (conservative - unknown errors count as potentially model-caused)
    return "model"


def _classify_risk(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    output_tokens: int,
    model_error: bool,
    retry_signal: bool,
) -> tuple[str, float]:
    """Classify a downtiered turn as safe / moderate / risky.

    Returns (label, risk_score 0.0-1.0).
    """
    tr = _tool_risk(tool_name, tool_input)
    oc = _output_complexity(output_tokens)
    me = 1.0 if model_error else 0.0
    rs = 1.0 if retry_signal else 0.0

    risk = 0.35 * tr + 0.20 * oc + 0.25 * me + 0.20 * rs

    if risk < 0.25:
        return "safe", risk
    if risk < 0.55:
        return "moderate", risk
    return "risky", risk


# ---------------------------------------------------------------------------
# Model / tier mapping
# ---------------------------------------------------------------------------

_MODEL_TO_TIER: dict[str, ModelTier] = {
    "claude-haiku-4-5": "cheap",
    "claude-haiku-4-6": "cheap",
    "claude-sonnet-4-5": "medium",
    "claude-sonnet-4-6": "medium",
    "claude-sonnet-4.6": "medium",
    "claude-opus-4-5": "expensive",
    "claude-opus-4-7": "expensive",
    "claude-opus-4.7": "expensive",
}
_TIER_RANK: dict[ModelTier, int] = {"cheap": 0, "medium": 1, "expensive": 2}
_TIER_MODELS: dict[ModelTier, str] = {
    "cheap": "claude-haiku-4-5",
    "medium": "claude-sonnet-4-6",
    "expensive": "claude-opus-4-7",
}


def _model_tier(model: str) -> ModelTier:
    m = (model or "").lower().strip()
    for key, tier in _MODEL_TO_TIER.items():
        if key in m:
            return tier
    return "medium"


# ---------------------------------------------------------------------------
# Parsed event
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    ev_type: str  # "assistant" | "user" | "other"
    # assistant fields
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    synthetic: bool = False
    tool_uses: list[dict[str, Any]] = field(default_factory=list)  # [{id, name, input}]
    # user fields
    tool_results: list[dict[str, Any]] = field(default_factory=list)  # [{id, is_error, content, error_class}]


def _parse_events(path: Path) -> list[_Event]:
    """Parse all assistant and user events from a JSONL export."""
    events: list[_Event] = []
    last_a_fingerprint: tuple[int, int, int, int] | None = None

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue

                ev_type = ev.get("type", "")

                if ev_type == "assistant":
                    msg = ev.get("message") or {}
                    usage = msg.get("usage") or {}
                    inp = int(usage.get("input_tokens", 0))
                    cache_c = int(usage.get("cache_creation_input_tokens", 0))
                    cache_r = int(usage.get("cache_read_input_tokens", 0))
                    out = int(usage.get("output_tokens", 0))
                    fp = (inp, cache_c, cache_r, out)
                    if fp == last_a_fingerprint:
                        events.append(_Event(ev_type="other"))
                        continue
                    last_a_fingerprint = fp
                    model = str(msg.get("model") or "")
                    synthetic = model == "<synthetic>" or not model
                    tool_uses = [
                        {
                            "id": str(b.get("id", "")),
                            "name": str(b.get("name", "")),
                            "input": b.get("input") or {},
                        }
                        for b in (msg.get("content") or [])
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    ]
                    events.append(
                        _Event(
                            ev_type="assistant",
                            input_tokens=inp + cache_c,
                            output_tokens=out,
                            model=model,
                            synthetic=synthetic,
                            tool_uses=tool_uses,
                        )
                    )

                elif ev_type == "user":
                    last_a_fingerprint = None
                    msg = ev.get("message") or {}
                    content = msg.get("content") or []
                    tool_results: list[dict[str, Any]] = []
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                is_err = bool(b.get("is_error", False))
                                result_content = b.get("content", "")
                                err_class = "none"
                                if is_err:
                                    err_class = _classify_error(result_content)
                                elif result_content and _MODEL_ERROR_PATTERNS.search(str(result_content)[:500]):
                                    # Content-based error detection even without is_error flag.
                                    err_class = "model"
                                tool_results.append(
                                    {
                                        "id": str(b.get("tool_use_id", "")),
                                        "is_error": is_err,
                                        "content": result_content,
                                        "error_class": err_class,
                                    }
                                )
                    events.append(_Event(ev_type="user", tool_results=tool_results))

                else:
                    events.append(_Event(ev_type="other"))

    except Exception:
        pass

    return events


# ---------------------------------------------------------------------------
# Per-turn quality analysis
# ---------------------------------------------------------------------------


@dataclass
class _TurnQuality:
    turn_index: int
    tool_name: str
    output_tokens: int
    actual_tier: ModelTier
    recommended_tier: ModelTier
    had_env_error: bool  # environment failure - not model-caused
    had_model_error: bool  # capability-dependent failure
    had_retry: bool  # same tool called again next turn (genuine regression)
    risk_label: str
    risk_score: float

    @property
    def quality_contribution(self) -> float:
        return {"safe": 1.0, "moderate": 0.6, "risky": 0.0}.get(self.risk_label, 0.0)


def _analyze_session(path: Path, router: ModelRouter) -> list[_TurnQuality]:
    """Return quality analysis for every downtiered turn in a session."""
    events = _parse_events(path)
    results: list[_TurnQuality] = []

    # Build tool_use_id -> error_class map
    error_class_by_id: dict[str, str] = {}
    for ev in events:
        if ev.ev_type == "user":
            for tr in ev.tool_results:
                uid = tr["id"]
                cls = tr["error_class"]
                if cls != "none":
                    error_class_by_id[uid] = cls

    # Build assistant event list with their index in events[] so we can
    # look up the next real assistant event for the retry signal.
    asst_events: list[tuple[int, _Event]] = [
        (i, ev)
        for i, ev in enumerate(events)
        if ev.ev_type == "assistant" and not ev.synthetic and (ev.input_tokens > 0 or ev.output_tokens > 0)
    ]

    prior_errors = 0

    for asst_seq, (_ev_idx, ev) in enumerate(asst_events):
        actual_tier = _model_tier(ev.model)
        tool_name = ev.tool_uses[0]["name"] if ev.tool_uses else ""
        tool_input = ev.tool_uses[0]["input"] if ev.tool_uses else {}

        # Build session-phase signals from the preceding turns' tool calls.
        recent_tool_calls = [
            (asst_events[j][1].tool_uses[0]["name"] if asst_events[j][1].tool_uses else "")
            for j in range(max(0, asst_seq - 10), asst_seq)
        ]
        rec = router.score(
            tool_name,
            "",
            {
                "prior_errors": prior_errors,
                "turn_number": asst_seq,
                "recent_tool_calls": recent_tool_calls,
            },
        )

        # Check error classes for this turn's tool uses
        had_env = any(error_class_by_id.get(tu["id"], "none") == "env" for tu in ev.tool_uses)
        had_model = any(error_class_by_id.get(tu["id"], "none") == "model" for tu in ev.tool_uses)

        # Retry signal: does the NEXT real assistant event call the same tool
        # AND did this turn have a model error?
        had_retry = False
        if had_model and asst_seq + 1 < len(asst_events):
            next_ev = asst_events[asst_seq + 1][1]
            next_tool = next_ev.tool_uses[0]["name"] if next_ev.tool_uses else ""
            had_retry = next_tool.lower() == tool_name.lower()

        # Update prior_errors count (affects routing for subsequent turns)
        if had_env or had_model:
            prior_errors += 1

        # Only analyse downtiered turns
        if rec is None:
            continue
        if _TIER_RANK[rec.tier] < _TIER_RANK[actual_tier]:
            label, risk = _classify_risk(tool_name, tool_input, ev.output_tokens, had_model, had_retry)
            results.append(
                _TurnQuality(
                    turn_index=asst_seq,
                    tool_name=tool_name or "(none)",
                    output_tokens=ev.output_tokens,
                    actual_tier=actual_tier,
                    recommended_tier=rec.tier,
                    had_env_error=had_env,
                    had_model_error=had_model,
                    had_retry=had_retry,
                    risk_label=label,
                    risk_score=risk,
                )
            )

    return results


# ---------------------------------------------------------------------------
# Session + global results
# ---------------------------------------------------------------------------


@dataclass
class SessionQualityResult:
    session_id: str
    actual_model: str
    downtiered_turns: int
    safe_turns: int
    moderate_turns: int
    risky_turns: int
    env_errors: int  # excluded from risk - not model-caused
    model_errors: int  # counted in risk - capability-dependent
    retry_signals: int  # strongest quality-degradation signal
    quality_score: float

    def to_dict(self) -> dict[str, Any]:
        n = max(self.downtiered_turns, 1)
        return {
            "session_id": self.session_id,
            "actual_model": self.actual_model,
            "downtiered_turns": self.downtiered_turns,
            "safe_turns": self.safe_turns,
            "safe_pct": round(self.safe_turns / n * 100, 1),
            "moderate_turns": self.moderate_turns,
            "moderate_pct": round(self.moderate_turns / n * 100, 1),
            "risky_turns": self.risky_turns,
            "risky_pct": round(self.risky_turns / n * 100, 1),
            "env_errors": self.env_errors,
            "model_errors": self.model_errors,
            "retry_signals": self.retry_signals,
            "quality_score": round(self.quality_score, 3),
        }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_routing_quality_bench(
    corpus_dir: Path,
    *,
    max_sessions: int | None = None,
) -> dict[str, Any]:
    search_dir = corpus_dir / "claude" if (corpus_dir / "claude").is_dir() else corpus_dir

    candidates = sorted(search_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_size)

    router = ModelRouter(
        cheap_model=_TIER_MODELS["cheap"],
        medium_model=_TIER_MODELS["medium"],
        expensive_model=_TIER_MODELS["expensive"],
    )

    session_results: list[SessionQualityResult] = []
    sessions_skipped = 0

    for path in candidates:
        if max_sessions is not None and len(session_results) >= max_sessions:
            break

        tqs = _analyze_session(path, router)
        if not tqs:
            sessions_skipped += 1
            continue

        safe = sum(1 for t in tqs if t.risk_label == "safe")
        moderate = sum(1 for t in tqs if t.risk_label == "moderate")
        risky = sum(1 for t in tqs if t.risk_label == "risky")
        env_errors = sum(1 for t in tqs if t.had_env_error)
        model_errors = sum(1 for t in tqs if t.had_model_error)
        retries = sum(1 for t in tqs if t.had_retry)
        quality = sum(t.quality_contribution for t in tqs) / len(tqs)

        dom_model = next(
            (ev.model for ev in _parse_events(path) if ev.ev_type == "assistant" and not ev.synthetic and ev.model),
            "claude-sonnet-4-6",
        )

        session_results.append(
            SessionQualityResult(
                session_id=path.stem,
                actual_model=dom_model,
                downtiered_turns=len(tqs),
                safe_turns=safe,
                moderate_turns=moderate,
                risky_turns=risky,
                env_errors=env_errors,
                model_errors=model_errors,
                retry_signals=retries,
                quality_score=quality,
            )
        )

    if not session_results:
        return {
            "benchmark": "quality-routing",
            "methodology_note": (
                "Proxies only - haiku was never run. risk = 0.35 x tool_risk "
                "+ 0.20 x output_complexity + 0.25 x model_error + 0.20 x retry_signal. "
                "env_errors excluded (file-not-found etc fail with any model). "
                "model_errors = wrong edit string / logical failure / schema error. "
                "retry_signal = same tool called again after model_error."
            ),
            "sessions_benchmarked": 0,
            "sessions_skipped": sessions_skipped,
            "total_downtiered_turns": 0,
            "safe_turns": 0,
            "safe_pct": 0.0,
            "moderate_turns": 0,
            "risky_turns": 0,
            "risky_pct": 0.0,
            "env_errors_excluded": 0,
            "model_errors_counted": 0,
            "retry_signals": 0,
            "avg_quality_score": 0.0,
            "sessions": [],
            "generated_at": datetime.now(UTC).isoformat(),
        }

    n_sess = len(session_results)
    total_down = sum(r.downtiered_turns for r in session_results)
    total_safe = sum(r.safe_turns for r in session_results)
    total_mod = sum(r.moderate_turns for r in session_results)
    total_risky = sum(r.risky_turns for r in session_results)
    total_env = sum(r.env_errors for r in session_results)
    total_model = sum(r.model_errors for r in session_results)
    total_retry = sum(r.retry_signals for r in session_results)
    avg_quality = sum(r.quality_score for r in session_results) / n_sess

    return {
        "benchmark": "quality-routing",
        "methodology_note": (
            "Proxies only - haiku was never run. "
            "risk = 0.35 x tool_risk + 0.20 x output_complexity "
            "+ 0.25 x model_error + 0.20 x retry_signal. "
            "env_errors (file-not-found, permission-denied etc) excluded - "
            "these fail with any model tier. "
            "model_errors = wrong edit string / logical failure / schema error. "
            "retry_signal = same tool called immediately after a model_error (strongest signal). "
            "A true counterfactual requires replaying sessions with haiku."
        ),
        "sessions_benchmarked": n_sess,
        "sessions_skipped": sessions_skipped,
        "total_downtiered_turns": total_down,
        "safe_turns": total_safe,
        "safe_pct": round(total_safe / max(total_down, 1) * 100, 1),
        "moderate_turns": total_mod,
        "moderate_pct": round(total_mod / max(total_down, 1) * 100, 1),
        "risky_turns": total_risky,
        "risky_pct": round(total_risky / max(total_down, 1) * 100, 1),
        "env_errors_excluded": total_env,
        "env_error_pct_on_downtiered": round(total_env / max(total_down, 1) * 100, 1),
        "model_errors_counted": total_model,
        "model_error_pct_on_downtiered": round(total_model / max(total_down, 1) * 100, 1),
        "retry_signals": total_retry,
        "retry_pct_on_downtiered": round(total_retry / max(total_down, 1) * 100, 1),
        "avg_quality_score": round(avg_quality, 3),
        "sessions": [r.to_dict() for r in session_results],
        "generated_at": datetime.now(UTC).isoformat(),
    }
