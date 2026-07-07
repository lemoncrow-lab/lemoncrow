"""Claude plugin runtime helpers for Atelier.

The functions in this module are intentionally small and deterministic. Hook
scripts and tests call these helpers so lifecycle behavior stays consistent
across the Claude plugin, MCP gateway, and validation fixtures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RECALL_DIM = 256
RECALL_TOP_K = 10
RECALL_MAX_SESSIONS = 200
RECALL_MAX_CHUNK_CHARS = 3000
RECALL_MIN_SCORE_THRESHOLD = 0.15
RECALL_RESCAN_DEBOUNCE_MS = 30_000

FUZZY_ACCEPT_THRESHOLD = 0.95
FUZZY_AMBIGUITY_MARGIN = 0.05
COLUMN_REPAIR_THRESHOLD = 0.85

PLUGIN_DEFAULT_SETTINGS: dict[str, bool] = {
    "attribution": True,
    "statusLine": True,
    "statusLineSession": True,
    "statusLineLifetime": True,
    "statusLineTips": True,
    "statusLineShare": True,
    "spinnerVerbs": True,
    "alwaysLoadTools": True,
}
SPINNER_VERBS = [
    "Reasoning",
    "Searching",
    "Editing",
    "Validating",
    "Recalling",
    "Routing",
    "Compacting",
    "Forging",
]
# Commit/PR co-author identity for the opt-in attribution trailer, installed
# into a repo via scripts/install_attribution_hook.sh.
ATTRIBUTION_NAME = "atelier"
ATTRIBUTION_EMAIL = "293447754+atelier@users.noreply.github.com"
ATTRIBUTION_TRAILER = f"Co-Authored-By: {ATTRIBUTION_NAME} <{ATTRIBUTION_EMAIL}>"
AUTH_REFRESH_GRACE_SECONDS = 300
UPDATE_CHECK_THROTTLE_SECONDS = 30 * 60
# Billing meter: trailing window (days) used to sum realized spend/savings, and
# the fraction of the monthly limit at which the soft warning turns on. The
# meter is non-blocking — nothing enforces the cap; it only surfaces spend and a
# warning on the statusline (see refresh_subscription_meter / _resolve_status_text).
BILLING_WINDOW_DAYS = 30
SUBSCRIPTION_WARN_FRACTION = 0.8


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Failed to read JSON from %s", path, exc_info=True)
    return default


def _write_json(path: Path, data: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    if mode is not None:
        with suppress(OSError):
            os.chmod(path, mode)


def plugin_settings_path(root: str | Path) -> Path:
    return Path(root) / "plugin_settings.json"


def auth_state_path(root: str | Path) -> Path:
    return Path(root) / "auth.json"


def update_flag_path(root: str | Path) -> Path:
    return Path(root) / "update.json"


def subscription_state_path(root: str | Path) -> Path:
    return Path(root) / "subscription.json"


def _summarize_ab_calibration(root: str | Path) -> dict[str, Any]:
    """Summarise rolling A/B measurements from ``savings_calibration.jsonl``.

    Returns ``{}`` when no benchmarks have been run yet. Otherwise returns::

        {
            "samples": int,            # total rows across all tools
            "by_tool": {
                "<tool>": {
                    "n": int,
                    "median_ratio": float,      # atelier_chars / native_chars
                    "median_chars_saved": int,  # native_chars - atelier_chars
                    "median_saved_pct": float,  # 100 * (1 - ratio)
                },
                ...
            },
        }

    Measured-by-A/B view of per-tool savings.
    """
    path = Path(root) / "savings_calibration.jsonl"
    if not path.is_file():
        return {}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("tool"):
            rows.append(row)
    if not rows:
        return {}
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_tool.setdefault(str(row["tool"]), []).append(row)

    def _median(values: list[float]) -> float:
        s = sorted(values)
        n = len(s)
        if not n:
            return 0.0
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    summary: dict[str, dict[str, Any]] = {}
    for tool, tool_rows in by_tool.items():
        ratios = [float(r.get("ratio", 1.0) or 1.0) for r in tool_rows]
        token_ratios = [float(r.get("token_ratio", r.get("ratio", 1.0)) or 1.0) for r in tool_rows]
        saved_chars = [int(r.get("chars_saved", 0) or 0) for r in tool_rows]
        median_ratio = round(_median(ratios), 4)
        median_token_ratio = round(_median(token_ratios), 4)

        # Per-language breakdown — essential because outline behavior varies
        # massively (Python AST ~86% saved vs generic Rust ~52%). A single
        # tool-wide median would hide that variance and mislead the dashboard.
        by_lang_rows: dict[str, list[dict[str, Any]]] = {}
        for row in tool_rows:
            lang = str(row.get("language") or "unknown")
            by_lang_rows.setdefault(lang, []).append(row)
        by_language: dict[str, dict[str, Any]] = {}
        for lang, lrows in by_lang_rows.items():
            lr = [float(r.get("ratio", 1.0) or 1.0) for r in lrows]
            lt = [float(r.get("token_ratio", r.get("ratio", 1.0)) or 1.0) for r in lrows]
            ls = [int(r.get("chars_saved", 0) or 0) for r in lrows]
            mlr = round(_median(lr), 4)
            mlt = round(_median(lt), 4)
            by_language[lang] = {
                "n": len(lrows),
                "median_ratio": mlr,
                "median_token_ratio": mlt,
                "median_chars_saved": int(_median([float(s) for s in ls])),
                "median_saved_pct": round(100.0 * (1.0 - mlr), 1),
                "median_token_saved_pct": round(100.0 * (1.0 - mlt), 1),
            }

        summary[tool] = {
            "n": len(tool_rows),
            "median_ratio": median_ratio,
            "median_token_ratio": median_token_ratio,
            "median_chars_saved": int(_median([float(s) for s in saved_chars])),
            "median_saved_pct": round(100.0 * (1.0 - median_ratio), 1),
            "median_token_saved_pct": round(100.0 * (1.0 - median_token_ratio), 1),
            "by_language": by_language,
        }
    return {"samples": len(rows), "by_tool": summary}


def lifetime_savings_path(root: str | Path) -> Path:
    return Path(root) / "lifetime_savings.json"


def baseline_estimate_path(root: str | Path) -> Path:
    return Path(root) / "baseline_estimate.json"


def load_plugin_settings(root: str | Path) -> dict[str, bool]:
    data = _read_json(plugin_settings_path(root), {})
    if not isinstance(data, dict):
        data = {}
    nested = data.get("atelier") if isinstance(data.get("atelier"), dict) else None
    raw = nested or data
    settings = dict(PLUGIN_DEFAULT_SETTINGS)
    for key in settings:
        if key in raw:
            settings[key] = bool(raw[key])
    return settings


def write_plugin_setting(root: str | Path, key: str, value: bool) -> dict[str, bool]:
    if key not in PLUGIN_DEFAULT_SETTINGS:
        raise ValueError(f"unknown plugin setting: {key}")
    settings = load_plugin_settings(root)
    settings[key] = bool(value)
    _write_json(plugin_settings_path(root), settings)
    return settings


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _fingerprint(seed: str | None = None) -> str:
    from atelier.core.foundation.identity import get_anon_id

    raw = seed or os.environ.get("ATELIER_MACHINE_ID") or get_anon_id()
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def normalize_auth_credentials(raw: dict[str, Any], *, anonymous: bool = False) -> dict[str, Any]:
    user_id = str(raw.get("userId") or raw.get("user_id") or raw.get("sub") or "")
    email = str(raw.get("email") or raw.get("user_email") or "")
    refresh_token = str(raw.get("refreshToken") or raw.get("refresh_token") or raw.get("token") or "")
    access_token = str(raw.get("accessToken") or raw.get("access_token") or "")
    if not user_id:
        user_id = f"user-{_fingerprint(refresh_token or access_token or email or 'local')}"
    if anonymous and not email:
        email = "anonymous@local"
    auth = {
        "authenticated": True,
        "isAnonymous": bool(raw.get("isAnonymous") or raw.get("is_anonymous") or anonymous),
        "is_anonymous": bool(raw.get("isAnonymous") or raw.get("is_anonymous") or anonymous),
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": str(raw.get("expiresAt") or raw.get("expires_at") or ""),
        "userId": user_id,
        "email": email,
        "organizationId": raw.get("organizationId") or raw.get("organization_id"),
        "referralCode": raw.get("referralCode") or raw.get("referral_code"),
        "subscriptionStatus": raw.get("subscriptionStatus") or raw.get("subscription_status") or {},
    }
    if not auth["expiresAt"]:
        auth["expiresAt"] = "local"
    if not auth["referralCode"]:
        auth["referralCode"] = f"ATELIER-{_fingerprint(user_id)[:6].upper()}"
    return auth


def parse_login_token(token: str) -> dict[str, Any]:
    text = token.strip()
    candidates = [text]
    try:
        padded = text + "=" * (-len(text) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        candidates.append(decoded)
    except (ValueError, UnicodeDecodeError):
        logger.warning("Failed to base64-decode login token", exc_info=True)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            continue
        if isinstance(payload, dict):
            if isinstance(payload.get("credentials"), dict):
                payload = payload["credentials"]
            return normalize_auth_credentials(payload)
    return normalize_auth_credentials({"refreshToken": text})


def write_auth_state(root: str | Path, auth: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_auth_credentials(auth, anonymous=bool(auth.get("isAnonymous") or auth.get("is_anonymous")))
    _write_json(auth_state_path(root), normalized, mode=0o600)
    return normalized


def claim_anonymous_trial(root: str | Path, *, monthly_limit_usd: float = 0.0) -> dict[str, Any]:
    """Seed local free-mode auth state (auth.json) when none exists.

    Default is no local limit (``monthly_limit_usd=0.0``): the open-source
    free core never stamps a spending cap. A positive limit is only set by
    hosted/licensed flows, and the meter (:func:`compute_usage_meter`) treats
    ``<= 0`` as report-only — it surfaces spend/savings but never warns.
    """
    existing = _read_json(auth_state_path(root), None)
    if isinstance(existing, dict) and existing.get("authenticated"):
        return normalize_auth_credentials(existing, anonymous=bool(existing.get("isAnonymous")))
    fp = _fingerprint()
    subscription = {
        "isValid": True,
        "status": "FREE",
        "plan": "LOCAL",
        "monthlySavingsInUsd": 0.0,
        "monthlyLimitInUsd": monthly_limit_usd,
        "message": "Local free mode active.",
    }
    auth = normalize_auth_credentials(
        {
            "accessToken": f"local-anonymous-{fp}",
            "refreshToken": "",
            "userId": f"anon-{fp}",
            "email": "anonymous@local",
            "isAnonymous": True,
            "subscriptionStatus": subscription,
            "referralCode": f"ATELIER-{fp[:6].upper()}",
        },
        anonymous=True,
    )
    _write_json(auth_state_path(root), auth, mode=0o600)
    return auth


def logout_local(root: str | Path, *, claim_trial: bool = True) -> dict[str, Any]:
    path = auth_state_path(root)
    if path.exists():
        path.unlink()
    if claim_trial:
        return {"logged_out": True, "anonymous": claim_anonymous_trial(root)}
    return {"logged_out": True, "anonymous": None}


def auth_status(root: str | Path) -> dict[str, Any]:
    auth = _read_json(auth_state_path(root), None)
    if not isinstance(auth, dict):
        return {"authenticated": False, "isAnonymous": False, "root": str(Path(root))}
    normalized = normalize_auth_credentials(auth, anonymous=bool(auth.get("isAnonymous") or auth.get("is_anonymous")))
    subscription = normalized.get("subscriptionStatus") or _read_json(subscription_state_path(root), {})
    subscription = compute_usage_meter(root, subscription=subscription)
    return {
        "authenticated": bool(normalized.get("authenticated")),
        "isAnonymous": bool(normalized.get("isAnonymous")),
        "email": normalized.get("email"),
        "userId": normalized.get("userId"),
        "expiresAt": normalized.get("expiresAt"),
        "subscription": subscription,
        "referralCode": normalized.get("referralCode"),
        "root": str(Path(root)),
    }


def compute_usage_meter(root: str | Path, *, subscription: dict[str, Any] | None = None) -> dict[str, Any]:
    """Price trailing-window usage against the plan's monthly limit.

    Realized spend and savings come from :func:`aggregate_window_savings` (the
    same per-session ledger that drives the statusline and ``atelier savings``),
    so the meter always reconciles with those surfaces. Returns the subscription
    dict enriched with live billing fields (``monthlySpendInUsd``,
    ``monthlySavingsInUsd``, ``remainingUsd``, ``usageFraction``, ``warning``,
    ``overLimit``). Purely additive and non-blocking — callers decide what, if
    anything, to do with ``warning``/``overLimit``; nothing here enforces a cap.

    ``monthlyLimitInUsd <= 0`` (or absent) means "no local limit": spend and
    savings are still reported, but ``warning``/``overLimit`` stay False.
    """
    root_path = Path(root)
    if subscription is None:
        # The canonical plan blob lives in auth.json under subscriptionStatus;
        # fall back to a standalone subscription.json when auth has none.
        auth = _read_json(auth_state_path(root_path), {})
        raw_sub = auth.get("subscriptionStatus") if isinstance(auth, dict) else None
        subscription = raw_sub or _read_json(subscription_state_path(root_path), {})
    subscription = dict(subscription) if isinstance(subscription, dict) else {}
    # Legacy builds stamped the LOCAL trial with a $5 monthlyLimitInUsd; the
    # free core is uncapped, and positive limits come only from licensed/hosted
    # plans, so force the local plan onto the report-only (no-limit) path.
    if subscription.get("plan") == "LOCAL":
        subscription["monthlyLimitInUsd"] = 0.0

    spend_usd = 0.0
    savings_usd = 0.0
    try:
        from atelier.core.capabilities.savings_summary import aggregate_window_savings

        window = aggregate_window_savings(root_path, days=BILLING_WINDOW_DAYS)
        spend_usd = round(max(0.0, float(window.spend_usd)), 4)
        savings_usd = round(max(0.0, float(window.saved_usd)), 4)
    except Exception:
        logging.exception("Recovered from broad exception handler")

    limit_usd = float(subscription.get("monthlyLimitInUsd") or 0.0)
    has_limit = limit_usd > 0.0
    fraction = round(spend_usd / limit_usd, 4) if has_limit else 0.0
    warning = bool(has_limit and spend_usd >= SUBSCRIPTION_WARN_FRACTION * limit_usd)
    over_limit = bool(has_limit and spend_usd >= limit_usd)

    subscription["monthlySpendInUsd"] = spend_usd
    subscription["monthlySavingsInUsd"] = savings_usd
    subscription["remainingUsd"] = round(max(0.0, limit_usd - spend_usd), 4) if has_limit else None
    subscription["usageFraction"] = fraction
    subscription["windowDays"] = BILLING_WINDOW_DAYS
    subscription["warning"] = warning
    subscription["overLimit"] = over_limit
    if over_limit:
        subscription["message"] = f"Monthly limit reached — ${spend_usd:.2f} of ${limit_usd:.2f} used"
    elif warning:
        subscription["message"] = f"Approaching monthly limit — ${spend_usd:.2f} of ${limit_usd:.2f} used"
    return subscription


def refresh_subscription_meter(root: str | Path) -> dict[str, Any]:
    """Recompute the usage meter and persist it to ``subscription.json``.

    ``subscription.json`` is the file the statusline (:func:`_resolve_status_text`
    in ``savings_summary``) reads to surface the plan warning, so persisting the
    metered blob here is what lights up that surface. Called from session
    lifecycle seams (SessionStart bootstrap, Stop). Best-effort — never raises
    into a hook.
    """
    root_path = Path(root)
    metered = compute_usage_meter(root_path)
    with suppress(OSError, ValueError):
        _write_json(subscription_state_path(root_path), metered)
    return metered


def begin_browser_login(
    root: str | Path,
    *,
    app_url: str | None = None,
    state: str | None = None,
    callback_port: int | None = None,
) -> dict[str, Any]:
    fp = _fingerprint()
    chosen_state = state or _fingerprint(f"state:{fp}:{_iso_now()}")
    port = callback_port or 49152 + (int(fp[:4], 16) % (65535 - 49152))
    base = (app_url or os.environ.get("ATELIER_APP_URL") or "https://127.0.0.1:8787").rstrip("/")
    url = f"{base}/auth?callback_port={port}&state={chosen_state}&fp={fp}"
    pending = {
        "url": url,
        "state": chosen_state,
        "callbackPort": port,
        "fingerprint": fp,
        "createdAt": _iso_now(),
    }
    _write_json(Path(root) / "login_pending.json", pending, mode=0o600)
    return pending


def share_referral(root: str | Path, *, app_url: str | None = None) -> dict[str, Any]:
    status = auth_status(root)
    if not status.get("authenticated"):
        return {"is_error": True, "message": "Log in or start a local trial before sharing."}
    code = str(status.get("referralCode") or f"ATELIER-{_fingerprint(str(status.get('userId')))[:6].upper()}")
    base = (app_url or os.environ.get("ATELIER_APP_URL") or "https://127.0.0.1:8787").rstrip("/")
    text = f"Use code {code} for Atelier: {base}?ref={code}"
    return {"code": code, "url": f"{base}?ref={code}", "text": text}


def compare_versions(left: str, right: str) -> int:
    def parts(value: str) -> list[int]:
        nums = [int(match.group(0)) for match in re.finditer(r"\d+", value or "0")]
        return nums or [0]

    a = parts(left)
    b = parts(right)
    width = max(len(a), len(b))
    a.extend([0] * (width - len(a)))
    b.extend([0] * (width - len(b)))
    return (a > b) - (a < b)


def validate_search_input(input_data: dict[str, Any]) -> dict[str, Any]:
    selectors = (
        input_data.get("content_regex"),
        input_data.get("file_glob_patterns"),
        input_data.get("type"),
    )
    if not any(selectors):
        return {
            "is_error": True,
            "message": "Provide content_regex, file_glob_patterns, or type",
        }
    return {"is_error": False}


def parse_line_suffix(pattern: str) -> dict[str, Any]:
    match = re.search(r"#(\d+)(?:-(\d+))?$", pattern)
    if not match:
        return {"clean_pattern": pattern, "start_line": None, "end_line": None}
    start_line = int(match.group(1))
    end_line = int(match.group(2) or match.group(1))
    return {
        "clean_pattern": pattern[: match.start()],
        "start_line": start_line,
        "end_line": end_line,
    }


def should_summarize(
    *,
    file_glob_patterns: list[str] | None,
    summary: bool | str | None,
    ast_truncation: bool,
    aggressive_truncation: bool,
) -> dict[str, Any]:
    if summary is not None:
        return {"summary_mode": bool(summary), "reason": "explicit summary setting"}
    patterns = file_glob_patterns or []
    has_single_exact_path = len(patterns) == 1 and not re.search(r"[*?\[\]{}]", patterns[0])
    if has_single_exact_path and ast_truncation and aggressive_truncation:
        return {
            "summary_mode": False,
            "reason": "exact non-glob path should return full content unless summary is explicitly requested",
        }
    return {"summary_mode": bool(ast_truncation or aggressive_truncation), "reason": "default"}


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(normalized)


def apply_if_modified_since(
    *,
    output_mode: str,
    if_modified_since: str | None,
    file_mtime: str,
    path: str,
) -> dict[str, Any]:
    if not if_modified_since or output_mode != "file_paths_with_content":
        return {"include_content": True, "render": path}
    include_content = _parse_timestamp(file_mtime) > _parse_timestamp(if_modified_since)
    render = path if include_content else f"{path} (unchanged)"
    return {"include_content": include_content, "render": render}


def apply_text_file_edits(initial: str, edits: list[dict[str, str]]) -> dict[str, Any]:
    content = initial
    applied_count = 0
    for edit in edits:
        old_string = edit.get("old_string", "")
        new_string = edit.get("new_string", "")
        index = content.find(old_string)
        if index == -1:
            existed_before = old_string and old_string in initial
            message = "old_string not found"
            if existed_before:
                message = "old_string existed in the pre-batch file but no longer matches current batch state"
            return {"is_error": True, "message": message, "applied_count": applied_count}
        content = content[:index] + new_string + content[index + len(old_string) :]
        applied_count += 1
    return {"final": content, "writes": 1 if applied_count else 0, "applied_count": applied_count}


def fuzzy_acceptance_policy(*, best_score: float, second_best_score: float, snippet_line_count: int) -> dict[str, Any]:
    if best_score < FUZZY_ACCEPT_THRESHOLD:
        return {"accepted": False, "reason": "public accepted fuzzy threshold is 0.95"}
    if second_best_score and (best_score - second_best_score) < FUZZY_AMBIGUITY_MARGIN:
        return {"accepted": False, "reason": "second best is within ambiguity margin 0.05"}
    return {"accepted": True, "reason": f"accepted {snippet_line_count} line snippet"}


def apply_notebook_source_edit(cell: dict[str, Any], old_string: str, new_string: str) -> dict[str, Any]:
    source = cell.get("source", "")
    if old_string not in source:
        return {"is_error": True, "message": "old_string not found in cell"}
    updated = dict(cell)
    updated["source"] = source.replace(old_string, new_string, 1)
    if updated.get("cell_type") == "code":
        updated["outputs"] = []
        updated["execution_count"] = None
    return updated


def find_notebook_match(
    *, cell_target: int | str | None, cells: list[dict[str, Any]], old_string: str
) -> dict[str, Any]:
    matches = [idx for idx, cell in enumerate(cells) if old_string in str(cell.get("source", ""))]
    if cell_target is not None:
        target = int(cell_target)
        if target < 0 or target >= len(cells):
            return {"is_error": True, "message": "cell target out of range"}
        return {"cell_index": target, "matched": old_string in str(cells[target].get("source", ""))}
    if len(matches) > 1:
        return {"is_error": True, "message": "old_string matched more than one cell"}
    if not matches:
        return {"is_error": True, "message": "old_string not found in notebook"}
    return {"cell_index": matches[0], "matched": True}


_AUTO_LIMIT_WRITE_VERBS = frozenset({"insert", "update", "delete", "replace"})


def _cte_trailing_verb_is_write(sql: str) -> bool:
    """True if a leading `WITH ...` resolves to a top-level write verb.

    Mirrors the trailing-verb scan used by the SQL tool's path-confinement
    layer: skip the parenthesized CTE bodies and string literals, then read the
    first depth-0 verb after the CTE list. INSERT/UPDATE/DELETE/REPLACE there
    mean the statement modifies data and must not be wrapped/auto-limited.
    """
    depth = 0
    in_single = in_double = False
    for match in re.finditer(r"[()'\"]|[A-Za-z_][A-Za-z_]*", sql):
        token = match.group(0)
        if in_single:
            if token == "'":
                in_single = False
            continue
        if in_double:
            if token == '"':
                in_double = False
            continue
        if token == "'":
            in_single = True
        elif token == '"':
            in_double = True
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            lowered_token = token.lower()
            if lowered_token in _AUTO_LIMIT_WRITE_VERBS:
                return True
            if lowered_token == "select":
                return False
    return False


def sql_auto_limit(sql: str, max_rows: int, auto_limit: bool = True) -> dict[str, Any]:
    if not auto_limit:
        return {"sql": sql, "changed": False}
    stripped = sql.strip().rstrip(";")
    lowered = stripped.lower()
    is_select = lowered.startswith("select")
    is_cte = lowered.startswith("with")
    if not (is_select or is_cte):
        return {"sql": sql, "changed": False, "reason": "only select statements are auto-limited"}
    # A `WITH ...` prefix is not necessarily a read: a write-CTE
    # (`WITH x AS (...) DELETE FROM t ...`) starts with WITH but its effective
    # top-level verb is a write. Wrapping such a statement as
    # `SELECT * FROM (... DELETE ...)` produces invalid SQL, so detect the
    # trailing top-level verb and skip auto-limit when it modifies data.
    if is_cte and _cte_trailing_verb_is_write(stripped):
        return {"sql": sql, "changed": False, "reason": "write CTEs are not auto-limited"}
    if re.search(r"\blimit\b", lowered):
        return {"sql": sql, "changed": False}
    has_set_op = bool(re.search(r"\b(union|intersect|except)\b", lowered))
    # Plain selects can take a trailing LIMIT directly. Set-operations and
    # WITH-CTE selects must be wrapped so the bound applies to the whole result
    # rather than only the final SELECT branch (or being a syntax error).
    if is_cte or has_set_op:
        return {"sql": f"SELECT * FROM ({stripped}) LIMIT {max_rows}", "changed": True}
    return {"sql": f"{stripped} LIMIT {max_rows}", "changed": True}


def discover_connection(
    env: dict[str, str] | None = None,
    dotenv_files: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    env = env or dict(os.environ)
    dotenv_files = dotenv_files or {}
    keys = ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "MYSQL_URL", "SQLITE_URL")
    for key in keys:
        if env.get(key):
            return {"connection_string": env[key], "source": f"env:{key}"}
    for filename in (".env", ".env.local", ".env.development", ".env.production"):
        values = dotenv_files.get(filename) or {}
        for key in keys:
            if values.get(key):
                return {"connection_string": values[key], "source": f"{filename}:{key}"}
    return {"connection_string": None, "source": None}


def column_typo_repair_policy(column_score: float, second_best_score: float) -> dict[str, Any]:
    if column_score < COLUMN_REPAIR_THRESHOLD:
        return {"repair": False, "reason": "column fuzzy threshold is 0.85"}
    if second_best_score and (column_score - second_best_score) < FUZZY_AMBIGUITY_MARGIN:
        return {"repair": False, "reason": "column match is ambiguous"}
    return {"repair": True, "reason": "single confident column match"}


def postgres_try_auto_fix(sql: str, error_signature: str) -> dict[str, Any]:
    if "column" in error_signature.lower() and "date_trunc" in sql.lower():
        fixed = re.sub(r'date_trunc\("([a-zA-Z_]+)",', r"date_trunc('\1',", sql)
        if fixed != sql:
            return {"fixed_sql": fixed, "retry": True}
    return {"fixed_sql": sql, "retry": False}


def recall_constants() -> dict[str, Any]:
    return {
        "dim": RECALL_DIM,
        "top_k": RECALL_TOP_K,
        "max_sessions": RECALL_MAX_SESSIONS,
        "max_chunk_chars": RECALL_MAX_CHUNK_CHARS,
        "min_score_threshold": RECALL_MIN_SCORE_THRESHOLD,
        "rescan_debounce_ms": RECALL_RESCAN_DEBOUNCE_MS,
    }


def chunk_transcript(messages: list[dict[str, Any]]) -> dict[str, Any]:
    kept: list[str] = []
    for message in messages:
        content = str(message.get("content", ""))
        if not content or "task-notification:" in content:
            continue
        kept.append(content)
    if not kept:
        return {"chunks": []}
    content = "\n".join(kept)
    return {"chunks": [{"content": content[:RECALL_MAX_CHUNK_CHARS]}]}


def status_line_choose_message(
    *,
    auth_present: bool = True,
    update_flag: dict[str, Any] | None = None,
    session_id: str | None = None,
    total_tool_calls: int = 0,
    turn_count: int = 0,
    enabled_families: list[str] | None = None,
    subscription_warning: bool = False,
) -> dict[str, Any]:
    if not auth_present:
        return {"message_family": "login", "rotation_skipped": True}
    if update_flag and update_flag.get("toVersion") != update_flag.get("fromVersion"):
        return {"message_family": "update", "rotation_skipped": True}
    if subscription_warning:
        return {"message_family": "subscription", "rotation_skipped": True}
    if not session_id:
        return {"message_family": "default", "rotation_skipped": True}
    families = enabled_families or ["savings", "tip", "lifetime"]
    if total_tool_calls <= 0 or not families:
        return {"message_family": "savings", "rotation_skipped": False}
    weights = {"savings": 6, "baseline": 1, "tip": 1, "lifetime": 1, "trial": 1, "share": 1}
    expanded = [family for family in families for _ in range(weights.get(family, 1))]
    return {"message_family": expanded[turn_count % len(expanded)], "rotation_skipped": False}


def session_start_install_status_line(plugin_root: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    updated = dict(settings or {})
    existing = updated.get("subagentStatusLine")
    if not isinstance(existing, dict):
        existing = updated.get("statusLine")

    command = f"{plugin_root}/scripts/statusline.sh"
    padding: Any | None = None
    if isinstance(existing, dict):
        existing_command = str(existing.get("command", ""))
        if "statusline.sh" in existing_command:
            command = existing_command
        padding = existing.get("padding")

    status_config: dict[str, Any] = {"type": "command", "command": command}
    if padding is not None:
        status_config["padding"] = padding

    updated["statusLine"] = dict(status_config)
    updated["subagentStatusLine"] = dict(status_config)
    return {"settings": updated}


def apply_status_line_setting(host_settings: dict[str, Any], plugin_root: str, enabled: bool) -> dict[str, Any]:
    updated = dict(host_settings or {})
    if enabled:
        installed = session_start_install_status_line(plugin_root, updated).get("settings")
        return installed if isinstance(installed, dict) else updated
    for key in ("statusLine", "subagentStatusLine"):
        current = updated.get(key)
        if isinstance(current, dict) and "statusline.sh" in str(current.get("command", "")):
            updated.pop(key, None)
    return updated


def apply_spinner_setting(host_settings: dict[str, Any], enabled: bool) -> dict[str, Any]:
    # Claude Code consumes a top-level ``spinnerVerbs`` object
    # ({"mode": "replace"|"append", "verbs": [...]}). A namespaced
    # ``atelier.spinnerVerbs`` array is ignored by the host, so write the
    # documented top-level key.
    updated = dict(host_settings or {})
    if enabled:
        updated["spinnerVerbs"] = {"mode": "replace", "verbs": list(SPINNER_VERBS)}
    else:
        updated.pop("spinnerVerbs", None)
    return updated


def apply_attribution_setting(host_settings: dict[str, Any], enabled: bool) -> dict[str, Any]:
    updated = dict(host_settings or {})
    namespace = dict(updated.get("atelier") or {})
    if enabled:
        namespace["attribution"] = {"enabled": True, "source": "Atelier"}
        # Suppress Claude Code's default Co-Authored-By trailer so the Atelier
        # trailer (installed by scripts/install_attribution_hook.sh) is the only
        # co-author line — but never override a value the user set themselves.
        if "includeCoAuthoredBy" not in updated:
            updated["includeCoAuthoredBy"] = False
    else:
        namespace.pop("attribution", None)
        # Leave includeCoAuthoredBy untouched on disable (respect prior state).
    if namespace:
        updated["atelier"] = namespace
    else:
        updated.pop("atelier", None)
    return updated


def apply_recall_settings(
    host_settings: dict[str, Any],
    *,
    auto_index: bool | None = None,
    embedder: str | None = None,
    embed_model: str | None = None,
) -> dict[str, Any]:
    """Merge all-sessions Recall settings into a plugin_settings dict.

    Only provided fields are changed (None = leave as-is). Top-level keys:
    ``recallAutoIndex`` (background SessionStart indexer), ``recallEmbedder``
    (local|openai|ollama), ``recallEmbedModel`` (e.g. an Ollama model name).
    """
    updated = dict(host_settings or {})
    if auto_index is not None:
        updated["recallAutoIndex"] = bool(auto_index)
    if embedder is not None:
        updated["recallEmbedder"] = str(embedder)
    if embed_model is not None:
        updated["recallEmbedModel"] = str(embed_model)
    return updated


def set_recall_settings(
    root: str | Path,
    *,
    auto_index: bool | None = None,
    embedder: str | None = None,
    embed_model: str | None = None,
) -> dict[str, Any]:
    """Read-merge-write Recall settings into plugin_settings.json."""
    path = plugin_settings_path(root)
    settings = _read_json(path, {})
    if not isinstance(settings, dict):
        settings = {}
    updated = apply_recall_settings(settings, auto_index=auto_index, embedder=embedder, embed_model=embed_model)
    _write_json(path, updated)
    return updated


def rewrite_mcp_always_load(
    mcp_json: dict[str, Any] | None,
    enabled: bool,
    *,
    server_name: str | None = None,
) -> dict[str, Any]:
    updated = json.loads(json.dumps(mcp_json or {"mcpServers": {}}))
    servers = updated.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        updated["mcpServers"] = {}
        servers = updated["mcpServers"]
    names = [server_name] if server_name else list(servers.keys())
    changed = False
    for name in names:
        server = servers.get(name)
        if isinstance(server, dict) and server.get("alwaysLoad") != bool(enabled):
            server["alwaysLoad"] = bool(enabled)
            changed = True
    return {"mcp_json": updated, "changed": changed}


# SQL commands list — used by detect_bash_sql for analytics counting.


def session_start(settings: dict[str, Any], plugin_root: str) -> dict[str, Any]:
    return {
        "settings_write_contains": session_start_install_status_line(plugin_root, settings)["settings"],
        "stdout": "",
    }


def session_start_bootstrap(
    root: str | Path,
    plugin_root: str,
    *,
    host_settings: dict[str, Any] | None = None,
    mcp_json: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    current_version: str = "0.0.0",
) -> dict[str, Any]:
    settings = load_plugin_settings(root)
    updated_host = dict(host_settings or {})
    actions: list[str] = []
    updated_host = apply_status_line_setting(updated_host, plugin_root, settings["statusLine"])
    actions.append("status_line_installed" if settings["statusLine"] else "status_line_removed")
    updated_host = apply_spinner_setting(updated_host, settings["spinnerVerbs"])
    actions.append("spinner_verbs_installed" if settings["spinnerVerbs"] else "spinner_verbs_removed")
    updated_host = apply_attribution_setting(updated_host, settings["attribution"])
    actions.append("attribution_installed" if settings["attribution"] else "attribution_removed")
    mcp_result = rewrite_mcp_always_load(mcp_json, settings["alwaysLoadTools"])
    if mcp_result["changed"]:
        actions.append("always_load_updated")
    auth = claim_anonymous_trial(root)
    refresh_subscription_meter(root)
    update = update_notification(current_version, _read_json(update_flag_path(root), None))
    if payload:
        update_session_stats(root, {"hook_event_name": "SessionStart", **payload})
    stdout = _merge_session_start_stdout(update.get("stdout"), _session_optimizer_start_notice(root, host="claude"))
    return {
        "settings": settings,
        "host_settings": updated_host,
        "mcp_json": mcp_result["mcp_json"],
        "auth": auth,
        "actions": actions,
        "stdout": stdout,
        "update": update,
    }


def apply_session_start_files(
    root: str | Path,
    plugin_root: str | Path,
    *,
    config_dir: str | Path | None = None,
    payload: dict[str, Any] | None = None,
    current_version: str = "0.0.0",
) -> dict[str, Any]:
    plugin_root_path = Path(plugin_root)
    config_path = (
        Path(config_dir)
        if config_dir is not None
        else Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    )
    settings_path = config_path / "settings.json"
    host_settings = _read_json(settings_path, {})
    if not isinstance(host_settings, dict):
        host_settings = {}
    mcp_path = plugin_root_path / ".mcp.json"
    mcp_json = _read_json(mcp_path, {"mcpServers": {}})
    if not isinstance(mcp_json, dict):
        mcp_json = {"mcpServers": {}}
    result = session_start_bootstrap(
        root,
        str(plugin_root_path),
        host_settings=host_settings,
        mcp_json=mcp_json,
        payload=payload,
        current_version=current_version,
    )
    _write_json(settings_path, result["host_settings"])
    if mcp_path.exists():
        _write_json(mcp_path, result["mcp_json"])
    return result


def update_notification(current_version: str, flag: dict[str, Any] | None) -> dict[str, Any]:
    """Check for available update; return hook metadata for the plugin system.

    Returns the version info for the plugin system to record/stash — does NOT
    inject any text into the LLM's context.
    """
    if not flag:
        return {"no_output": True}
    to_version = str(flag.get("toVersion") or "")
    if not to_version:
        return {"no_output": True}
    if compare_versions(to_version, current_version) <= 0:
        return {"delete_flag": True, "no_output": True}
    return {
        "stdout": {
            "hookSpecificOutput": {"hookEventName": "SessionStart"},
        }
    }


def _session_optimizer_start_notice(root: str | Path, *, host: str) -> dict[str, Any]:
    from atelier.core.capabilities.session_optimizer import build_session_start_notice

    return build_session_start_notice(str(root), host=host)


def _merge_session_start_stdout(*items: Any) -> dict[str, Any] | str:
    contexts: list[str] = []
    messages: list[str] = []
    hook_output: dict[str, Any] = {"hookEventName": "SessionStart"}
    for item in items:
        if not item:
            continue
        if isinstance(item, str):
            if item.strip():
                contexts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        hook = item.get("hookSpecificOutput")
        if isinstance(hook, dict):
            hook_output.update(hook)
        context = item.get("additionalContext")
        if isinstance(context, str) and context.strip():
            contexts.append(context.strip())
        message = item.get("message")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    if not contexts and not messages:
        return ""
    output: dict[str, Any] = {"hookSpecificOutput": hook_output}
    if contexts:
        output["additionalContext"] = "\n\n".join(contexts)
    if messages:
        output["message"] = " | ".join(messages)
    return output


def _codex_session_start_tool_policy() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {"hookEventName": "SessionStart"},
        "message": "Atelier policy: use Atelier tools first and keep responses delivery-focused.",
        "additionalContext": "\n".join(
            [
                "Codex Atelier tool policy:",
                "- Call `context` before exploratory reads or edits on coding tasks. Use the host-displayed handle if it adds an `mcp__atelier__` prefix.",
                "- Prefer Atelier read/search/edit/code-intel tools; use native Codex tools only when the Atelier equivalent is hidden, unavailable, or returned noop.",
                "- Keep replies concise and delivery-focused unless the user explicitly asks for a walkthrough.",
            ]
        ),
    }


def codex_update_notification(root: str | Path, *, current_version: str) -> dict[str, Any]:
    # Session-optimizer guidance is intentionally NOT injected here: its rules
    # duplicate the agent persona (core-discipline / change-discipline). The
    # offline analysis (build_trace_optimization_report) and rule data are kept.
    result = update_notification(current_version, _read_json(update_flag_path(root), None))
    if result.get("delete_flag"):
        update_flag_path(root).unlink(missing_ok=True)
    stdout = _merge_session_start_stdout(
        result.get("stdout"),
        _codex_session_start_tool_policy(),
    )
    return {**result, "stdout": stdout, "optimizer": {"host": "codex"}}


_ATELIER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "context",
        "route",
        "rescue",
        "trace",
        "verify",
        "memory",
        "read",
        "edit",
        "sql",
        "code",
        "grep",
        "search",
        "compact",
        "bash",
    }
)


def _is_atelier_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    if "atelier" in lowered:
        return True
    # Bare tool name from hosts that strip the MCP server prefix
    return lowered in _ATELIER_TOOL_NAMES


def _codex_native_tool_replacement(payload: dict[str, Any]) -> tuple[str, str] | None:
    tool_name = str(payload.get("tool_name") or "")
    lowered = tool_name.lower().strip()
    raw_tool_input = payload.get("tool_input")
    tool_input: dict[str, Any] = raw_tool_input if isinstance(raw_tool_input, dict) else {}
    command = str(tool_input.get("command") or "")
    normalized = " ".join(command.strip().split()).lower()

    if lowered == "read":
        return ("mcp__atelier__read", "Use Atelier read for file reads and ranges.")
    if lowered in {"edit", "write", "multiedit"}:
        return (
            "mcp__atelier__edit",
            "Use Atelier edit for deterministic grouped writes and rollback.",
        )
    if lowered in {"grep", "glob"}:
        return ("mcp__atelier__grep", "Use Atelier grep/search for text and path discovery.")
    if lowered in {"bash", "shell", "exec_command", "run_command"}:
        if (
            normalized.startswith(("rg ", "grep ", "find "))
            or " rg " in f" {normalized} "
            or " grep " in f" {normalized} "
        ):
            return (
                "mcp__atelier__grep",
                "Use Atelier grep/search instead of shell rg/grep/find loops.",
            )
        if normalized.startswith(("cat ", "sed ", "head ", "tail ")):
            return ("mcp__atelier__read", "Use Atelier read instead of shell file-print commands.")
        return (
            "mcp__atelier__bash",
            "Use Atelier bash so command execution stays compact and supervised.",
        )
    return None


def _codex_native_tool_nudge(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    replacement = _codex_native_tool_replacement(payload)
    if replacement is None:
        return {"no_output": True}
    session_id = str(payload.get("session_id") or "default")
    path = session_stats_path(root, session_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        logging.exception("Recovered from broad exception handler")
        state = {}
    nudged = state.setdefault("native_tool_nudges", {}) if isinstance(state, dict) else {}
    tool_name = str(payload.get("tool_name") or "unknown")
    command = ""
    if isinstance(payload.get("tool_input"), dict):
        command = str((payload.get("tool_input") or {}).get("command") or "")
    nudge_key = f"{tool_name.lower()}::{command.strip().lower()[:120]}"
    if bool(nudged.get(nudge_key)):
        return {"no_output": True}
    nudged[nudge_key] = True
    if isinstance(state, dict):
        state["native_tool_nudges"] = nudged
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    replacement_tool, rationale = replacement
    return {
        "message": f"Atelier policy: native tool '{tool_name}' was used where {replacement_tool} should be preferred.",
        "additionalContext": "\n".join(
            [
                rationale,
                "For coding tasks, call mcp__atelier__context first if you have not already.",
                "Keep native Codex tools as fallback only when the Atelier equivalent is hidden, unavailable, or returned noop.",
            ]
        ),
    }


_CTX_NUDGE_DEFAULT_TOKENS = 160_000


def _maybe_emit_ctx_notice(
    stats: dict[str, Any], payload: dict[str, Any], *, host: str = "claude"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One-shot compact nudge when live context crosses the cost-aware threshold.

    Context size is per-turn ground truth from the transcript. The message is
    priced with the live rate card: per-turn cache-read carry cost plus the
    >200k long-context premium boundary (input-side rates double past it), so
    the agent can weigh compaction against real dollars instead of a bare
    percentage.
    """
    from atelier.core.capabilities.session_optimizer import mark_session_optimizer_notice

    if bool((stats.get("optimizer_notices") or {}).get("ctx_high")):
        return stats, {"no_output": True}
    try:
        session_id = str(payload.get("session_id") or "")
        if host == "claude":
            from atelier.core.capabilities import savings_summary as ss

            ctx, model = ss.transcript_context_state(session_id)
        else:
            from atelier.gateway.hosts.context_state import host_context_state

            ctx, model = host_context_state(host, session_id)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return stats, {"no_output": True}
    if ctx <= 0:
        return stats, {"no_output": True}
    try:
        threshold = int(os.environ.get("ATELIER_CTX_NUDGE_TOKENS", "") or _CTX_NUDGE_DEFAULT_TOKENS)
    except ValueError:
        threshold = _CTX_NUDGE_DEFAULT_TOKENS
    if threshold <= 0 or ctx < threshold:  # <=0 disables the nudge
        return stats, {"no_output": True}

    ctx_k = ctx // 1000
    detail = [f"Atelier context guard: high context — ~{ctx_k}k tokens in the live window."]
    try:
        from atelier.core.capabilities.pricing import get_model_pricing

        pricing = get_model_pricing(model) if model else None
        if pricing is not None and pricing.known and pricing.cache_read > 0:
            lc_threshold = pricing.long_context_threshold()
            over_premium = bool(lc_threshold and ctx > lc_threshold)
            rate_cr = (
                pricing.cache_read_tiers[0].rate if over_premium and pricing.cache_read_tiers else pricing.cache_read
            )
            per_turn = ctx * rate_cr / 1_000_000
            detail.append(f"Every further turn re-reads it (~${per_turn:.2f}/turn cache-read).")
            if over_premium:
                detail.append(
                    f"The window is past the {lc_threshold // 1000}k long-context boundary, so "
                    "input-side rates are doubled until it shrinks — compact now to drop back to base rates."
                )
            elif lc_threshold:
                headroom = lc_threshold - ctx
                detail.append(
                    f"~{headroom // 1000}k tokens of headroom before the {lc_threshold // 1000}k "
                    "long-context premium doubles input-side rates — compact at the next natural boundary."
                )
    except Exception:
        logging.exception("Recovered from broad exception handler")
    if len(detail) == 1:
        detail.append("Compact at the next natural boundary to cut the per-turn re-read tax.")

    updated = mark_session_optimizer_notice(stats, "ctx_high")
    return updated, {
        "message": f"Atelier context guard: high context (~{ctx_k}k) — consider compacting",
        "additionalContext": " ".join(detail),
    }


def build_codex_user_prompt_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a display-only Codex compaction notice when needed."""
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return {"no_output": True}
    with suppress(Exception):
        update_session_stats(root, payload)
    session_id = str(payload.get("session_id") or "default")
    path = session_stats_path(root, session_id)
    try:
        stats = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError, TypeError):
        stats = {}

    with suppress(Exception):
        _codex_enrich_user_prompt(root, payload)
    updated, ctx_output = _maybe_emit_ctx_notice(stats, payload, host="codex")
    output: dict[str, Any] = {}
    compact_message = ctx_output.get("message")
    if isinstance(compact_message, str) and compact_message.strip():
        output["uiMessage"] = compact_message
    if updated != stats:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    return output or {"no_output": True}


def build_opencode_user_prompt_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a display-only OpenCode compaction notice when needed."""
    normalized = dict(payload)
    normalized["hook_event_name"] = "UserPromptSubmit"
    session_id = str(normalized.get("session_id") or "default")
    path = session_stats_path(root, session_id)
    try:
        stats = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError, TypeError):
        stats = {}

    updated, ctx_output = _maybe_emit_ctx_notice(stats, normalized, host="opencode")
    output: dict[str, Any] = {}
    compact_message = ctx_output.get("message")
    if isinstance(compact_message, str) and compact_message.strip():
        output["uiMessage"] = compact_message
    if updated != stats:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    return output or {"no_output": True}


def build_codex_post_tool_use_savings_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("hook_event_name") != "PostToolUse":
        return {"no_output": True}
    tool_name = str(payload.get("tool_name") or "")
    stats = update_session_stats(root, payload)
    if not _is_atelier_tool(tool_name):
        return _codex_native_tool_nudge(root, payload)
    return {"stats": stats, "no_output": True}


def build_codex_subagent_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if event not in {"SubagentStart", "SubagentStop"}:
        return {"no_output": True}
    update_session_stats(root, payload)
    return {"no_output": True}


def _codex_fmt_tokens(value: int) -> str:
    value = int(value or 0)
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    """Return *value* when it is a dict, else an empty dict (mypy-narrowing helper)."""
    return value if isinstance(value, dict) else {}


def _codex_payload_model(payload: dict[str, Any]) -> str:
    candidates: list[Any] = [payload.get("model"), payload.get("model_id")]
    context_window = _as_dict(payload.get("context_window"))
    candidates.append(context_window.get("model"))
    message = _as_dict(payload.get("message"))
    candidates.append(message.get("model"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("id", "name", "display_name", "model"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _codex_payload_cost_usd(payload: dict[str, Any]) -> float:
    candidates: list[Any] = [payload.get("total_cost_usd"), payload.get("total_cost")]
    cost = _as_dict(payload.get("cost"))
    candidates.extend([cost.get("total_cost_usd"), cost.get("total_usd"), cost.get("total_cost")])
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return max(0.0, float(candidate))
        if isinstance(candidate, str) and candidate.strip():
            try:
                return max(0.0, float(candidate))
            except ValueError:
                continue
    return 0.0


def _codex_estimated_cost_usd(model: str, usage: dict[str, Any]) -> float:
    if not model:
        return 0.0
    try:
        from atelier.core.capabilities.pricing import get_model_pricing

        pricing = get_model_pricing(model)
        return pricing.cost_usd(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=int(usage.get("cache_write_tokens", 0) or 0),
        )
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return 0.0


def _atelier_tool_segment(name: Any) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    lowered_text = text.lower()
    if lowered_text.startswith("atelier_"):
        return text[len("atelier_") :]
    parts = [part for part in re.split(r"__|::|\.", text) if part]
    if any("atelier" in part.lower() for part in parts) and len(parts) > 1:
        return parts[-1]
    return text


def _merge_raw_tool_count(target: dict[str, int], name: Any, count: Any) -> None:
    text = str(name or "").strip()
    if not text:
        return
    target[text] = int(target.get(text, 0) or 0) + int(count or 0)


def _raw_tool_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for name, count in raw.items():
        _merge_raw_tool_count(result, name, count)
    return result


def _is_atelier_qualified_tool_name(name: Any) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if text.lower().startswith("atelier_"):
        return True
    parts = [part for part in re.split(r"__|::|\.", text) if part]
    return any("atelier" in part.lower() for part in parts) and len(parts) > 1


def _display_tool_counts(raw: Any) -> dict[str, int]:
    counts = _raw_tool_counts(raw)
    qualified_by_base: dict[str, int] = {}
    bare_by_base: dict[str, str] = {}
    for name, count in counts.items():
        base = _atelier_tool_segment(name).lower()
        if _is_atelier_qualified_tool_name(name):
            qualified_by_base[base] = int(qualified_by_base.get(base, 0) or 0) + int(count or 0)
        elif name.lower() == base:
            bare_by_base[base] = name

    for base, bare_name in bare_by_base.items():
        qualified_count = int(qualified_by_base.get(base, 0) or 0)
        if qualified_count <= 0:
            continue
        remaining = int(counts.get(bare_name, 0) or 0) - qualified_count
        if remaining > 0:
            counts[bare_name] = remaining
        else:
            counts.pop(bare_name, None)
    return counts


def _codex_tools_text(raw: Any, *, limit: int | None = None) -> str:
    counts = list(_display_tool_counts(raw).items())
    top = sorted(counts, key=lambda item: (-item[1], item[0]))
    if limit is not None:
        top = top[:limit]
    return " · ".join(f"{name}×{count}" for name, count in top) if top else "none"  # noqa: RUF001


def build_codex_stop_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if event != "Stop":
        return {"no_output": True}

    session_id = str(payload.get("session_id") or "default")
    state = update_session_stats(root, payload)
    statusline_cost = 0.0
    if not _usage_has_tokens(_as_dict(state.get("usage"))):
        statusline_cost = _apply_codex_statusline_snapshot(root, session_id, payload)
        with suppress(Exception):
            state = json.loads(session_stats_path(root, session_id).read_text(encoding="utf-8"))
    _apply_codex_transcript_snapshot(root, session_id, payload)
    report = build_savings_report(root, session_id=session_id)
    session = report.get("session") or {}
    cost = report.get("cost") or {}
    usage = _as_dict(session.get("usage"))

    llm_turns = int(session.get("llm_turns", 0) or 0)
    prompt_turns = int(session.get("turns", 0) or 0)
    total_tool_calls = int(session.get("total_tool_calls", 0) or 0)
    calls_avoided = int(report.get("calls_avoided", 0) or 0)
    tokens_saved = int(report.get("tokens_saved", 0) or 0)
    saved_usd = float(cost.get("saved_usd", 0.0) or 0.0)
    routing_saved_usd = float(cost.get("routing_saved_usd", 0.0) or 0.0)
    carry_usd = float(cost.get("carry_usd", 0.0) or 0.0)
    carry_tokens = int(cost.get("carry_tokens", 0) or 0)
    compactions = int(session.get("compactions", 0) or 0)
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_tokens", 0) or 0)
    cache_write = int(usage.get("cache_write_tokens", 0) or 0)
    reasoning_out = int(usage.get("reasoning_output_tokens", 0) or 0)
    thinking = int(usage.get("thinking_tokens", 0) or 0)
    visible_out = max(0, out - reasoning_out)
    total_tokens = inp + out + cache_read + cache_write

    if (
        total_tool_calls <= 0
        and llm_turns <= 0
        and prompt_turns <= 0
        and total_tokens <= 0
        and calls_avoided <= 0
        and tokens_saved <= 0
        and saved_usd <= 0
    ):
        return {"no_output": True}

    with suppress(Exception):
        from atelier.core.service.telemetry.public_rollup import publish_public_savings_rollup

        publish_public_savings_rollup(
            session_id=session_id,
            saved_usd=saved_usd,
            tokens_saved=tokens_saved,
            calls_avoided=calls_avoided,
            turn_count=llm_turns,
            source="codex",
        )

    model = str(session.get("last_model") or session.get("model") or _codex_payload_model(payload) or "")
    real_cost = _codex_payload_cost_usd(payload) or statusline_cost
    estimated_cost = real_cost if real_cost > 0 else _codex_estimated_cost_usd(model, usage)
    fresh_in = inp + cache_write
    tool_source = str(session.get("tool_call_source") or "hooks")
    turn_part = f"{llm_turns} LLM turn{'s' if llm_turns != 1 else ''}"
    if llm_turns <= 0 and prompt_turns > 0:
        turn_part += f" · {prompt_turns} prompt turn{'s' if prompt_turns != 1 else ''}"
    activity = f"{turn_part} · {total_tool_calls} tool call{'s' if total_tool_calls != 1 else ''} ({tool_source})"

    lines = [
        "Atelier session complete.",
        activity,
    ]
    if total_tokens > 0:
        lines.append(
            "tokens: "
            f"{_codex_fmt_tokens(fresh_in)} input ({_codex_fmt_tokens(inp)} new + {_codex_fmt_tokens(cache_write)} cW) / "
            f"{_codex_fmt_tokens(cache_read)} cR / {_codex_fmt_tokens(out)} out  ({_codex_fmt_tokens(total_tokens)} total)"
        )
        output_parts = [
            f"{_codex_fmt_tokens(reasoning_out)} reasoning",
            f"{_codex_fmt_tokens(visible_out)} visible",
        ]
        if thinking > 0:
            output_parts.append(f"{_codex_fmt_tokens(thinking)} thinking")
        lines.append(
            "token breakdown: "
            f"new input {_codex_fmt_tokens(inp)} · cache read {_codex_fmt_tokens(cache_read)} · "
            f"cache write {_codex_fmt_tokens(cache_write)} · output {_codex_fmt_tokens(out)} "
            f"({', '.join(output_parts)})"
        )
    cost_prefix = "cost: " if real_cost > 0 else "est. cost: ~"
    lines.append(f"{cost_prefix}${estimated_cost:.4f}")
    lines.append(
        f"savings: ${saved_usd:.4f} · {tokens_saved:,} tokens saved · {calls_avoided} calls avoided · "
        f"routing ${routing_saved_usd:.4f} · carry ${carry_usd:.4f} / {carry_tokens:,} tokens"
    )
    if compactions > 0:
        lines.append(f"compactions: {compactions}")
    lines.append(f"tools: {_codex_tools_text(session.get('tools_used'))}")
    return {"systemMessage": "\n".join(lines), "report": report}


# ===========================================================================
# Codex lifecycle-hook parity
# ---------------------------------------------------------------------------
# These helpers give the Codex plugin the depth the Claude plugin has:
# PostToolUse run-ledger capture + tool supervision + repeat-failure rescue,
# and compaction lifecycle bookkeeping. They emit Codex-native output schemas
# (systemMessage for surfaced guidance) and mirror the Claude hook bodies, but
# always fail open -- a raised exception must never block the Codex agent.
# Run-ledger writes reuse the proven raw-append pattern from the Claude hooks
# rather than round-tripping the RunLedger model, so unknown fields written by
# the MCP server are preserved verbatim.
# ===========================================================================

_CODEX_MAX_LEDGER_BYTES = 4096
_CODEX_MAX_DIFF_BYTES = 20_000
_CODEX_FAILURE_THRESHOLD = 2


def _codex_workspace_root(payload: dict[str, Any]) -> str:
    cwd = str(payload.get("cwd") or "").strip()
    if cwd:
        return cwd
    return os.environ.get("CODEX_WORKSPACE_ROOT") or os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()


def _codex_session_state_path(root: str | Path, payload: dict[str, Any]) -> Path:
    workspace = _codex_workspace_root(payload)
    from atelier.core.foundation.paths import workspace_key

    digest = workspace_key(Path(workspace).resolve())
    return Path(root) / "workspaces" / digest / "session_state.json"


def _read_codex_session_state(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = _codex_session_state_path(root, payload)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_codex_session_state(root: str | Path, payload: dict[str, Any], state: dict[str, Any]) -> None:
    path = _codex_session_state_path(root, payload)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def _codex_run_file(root: str | Path, session_id: str) -> Path:
    return Path(root) / "runs" / f"{session_id}.json"


def _codex_ledger_session_id(root: str | Path, payload: dict[str, Any]) -> str:
    """Resolve the run-ledger id whose ``runs/<id>.json`` exists.

    Priority: session_state ``active_session_id`` / ``session_id`` (the internal
    Atelier id the MCP server keys the ledger to), then the host ``session_id``
    on the payload. Returns "" when no run file exists yet -- callers then skip
    rather than create a spurious ledger from a hook.
    """
    state = _read_codex_session_state(root, payload)
    for candidate in (state.get("active_session_id"), state.get("session_id"), payload.get("session_id")):
        sid = str(candidate or "").strip()
        if sid and _codex_run_file(root, sid).exists():
            return sid
    return ""


def _codex_atomic_write_json(path: Path, data: dict[str, Any]) -> bool:
    import tempfile

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(path)
        return True
    except (OSError, TypeError, ValueError):
        if tmp_path:
            with suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
        return False


def _codex_append_ledger_events(root: str | Path, session_id: str, events_to_add: list[dict[str, Any]]) -> bool:
    if not session_id or not events_to_add:
        return False
    run_file = _codex_run_file(root, session_id)
    if not run_file.exists():
        return False
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    events = data.setdefault("events", [])
    if not isinstance(events, list):
        return False
    events.extend(events_to_add)
    touched = data.setdefault("files_touched", [])
    if isinstance(touched, list):
        for event in events_to_add:
            if event.get("kind") == "file_edit":
                path_value = (event.get("payload") or {}).get("path")
                if isinstance(path_value, str) and path_value and path_value not in touched:
                    touched.append(path_value)
    return _codex_atomic_write_json(run_file, data)


def _normalize_codex_tool(tool_name: str) -> str:
    base = _atelier_tool_segment(tool_name).lower().rsplit("__", 1)[-1].strip()
    if base in {"edit", "write", "multiedit", "apply_patch", "applypatch", "str_replace_editor"}:
        return "edit"
    if base in {"bash", "shell", "exec_command", "run_command", "unified_exec", "local_shell"}:
        return "bash"
    if base == "read":
        return "read"
    if base in {"grep", "glob", "search"}:
        return "search"
    return "other"


def _codex_edit_targets(tool_input: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                value = edit.get("file_path") or edit.get("path") or edit.get("filename")
                if isinstance(value, str) and value:
                    targets.append(value)
    single = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename")
    if isinstance(single, str) and single:
        targets.append(single)
    seen: set[str] = set()
    ordered: list[str] = []
    for target in targets:
        if target not in seen:
            seen.add(target)
            ordered.append(target)
    return ordered


def _codex_path_keys(path: str) -> set[str]:
    clean = path.split("#", 1)[0]
    clean = re.sub(r":(?:full|outline|summary|head=\d+|tail=\d+)$", "", clean)
    clean = re.sub(r":L\d+(?:-L?\d+)?$", "", clean)
    p = Path(clean)
    keys = {clean, p.name}
    with suppress(OSError):
        keys.add(str(p.expanduser().resolve()))
    return {key for key in keys if key}


def _codex_edited_path_keys(root: str | Path, payload: dict[str, Any]) -> set[str]:
    session_id = _codex_ledger_session_id(root, payload)
    if not session_id:
        return set()
    run_file = _codex_run_file(root, session_id)
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    edited: set[str] = set()
    touched = data.get("files_touched")
    if isinstance(touched, list):
        for path in touched:
            if isinstance(path, str):
                edited.update(_codex_path_keys(path))
    events = data.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict) or event.get("kind") != "file_edit":
                continue
            payload_data = event.get("payload")
            if isinstance(payload_data, dict):
                path = payload_data.get("path")
                if isinstance(path, str):
                    edited.update(_codex_path_keys(path))
    return edited


def _codex_read_path_is_full(path: str, tool_input: dict[str, Any]) -> bool:
    has_range = bool(tool_input.get("range")) or bool(re.search(r":L\d+(?:-L?\d+)?$", path)) or "#" in path
    has_full = bool(tool_input.get("full")) or path.endswith(":full")
    return has_full and not has_range


def _codex_full_read_paths(tool_input: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    raw_path = tool_input.get("path") or tool_input.get("file_path") or tool_input.get("filename")
    if isinstance(raw_path, str) and _codex_read_path_is_full(raw_path, tool_input):
        paths.append(raw_path)
    files = tool_input.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, str):
                if _codex_read_path_is_full(item, {}):
                    paths.append(item)
            elif isinstance(item, dict):
                item_path = item.get("path") or item.get("file_path") or item.get("filename")
                if isinstance(item_path, str) and _codex_read_path_is_full(item_path, item):
                    paths.append(item_path)
    return paths


def build_codex_pre_tool_use_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Block wasteful full rereads of files already edited in this Codex run.

    This intentionally ports only the narrow Claude read-after-edit guard. The
    older Codex grounding gate was removed because it blocked from-scratch file
    creation too broadly; this guard only fires on explicit full-file reads of
    paths already observed in the run ledger as edited.
    """
    if os.environ.get("ATELIER_READ_AFTER_EDIT_GUARD", "1") == "0":
        return {"no_output": True}
    if str(payload.get("hook_event_name") or "") != "PreToolUse":
        return {"no_output": True}
    if _normalize_codex_tool(str(payload.get("tool_name") or "")) != "read":
        return {"no_output": True}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return {"no_output": True}
    full_reads = _codex_full_read_paths(tool_input)
    if not full_reads:
        return {"no_output": True}
    edited = _codex_edited_path_keys(root, payload)
    for path in full_reads:
        keys = _codex_path_keys(path)
        if keys.isdisjoint(edited):
            continue
        base = Path(next(iter(keys))).name or path
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f'Edited {base} already -- read an exact range (for example, range="L1-L120") '
                    "instead of expanding the whole file again."
                ),
            }
        }
    return {"no_output": True}


def _codex_unified_diff(old: str, new: str, path: str) -> str:
    import difflib

    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return "\n".join(diff)


def _codex_git_diff(path: str, workspace: str) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", path],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=workspace or None,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout.strip()


def _codex_edit_diff(tool_input: dict[str, Any], path: str, workspace: str) -> str:
    parts: list[str] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            edit_path = edit.get("file_path") or edit.get("path") or edit.get("filename")
            if isinstance(edit_path, str) and edit_path and edit_path != path:
                continue
            old = str(edit.get("old_string") or "")
            new = str(edit.get("new_string") or "")
            if old or new:
                parts.append(_codex_unified_diff(old, new, path))
    old = str(tool_input.get("old_string") or "")
    new = str(tool_input.get("new_string") or "")
    if old or new:
        parts.append(_codex_unified_diff(old, new, path))
    diff = "\n".join(part for part in parts if part)
    if not diff:
        diff = _codex_git_diff(path, workspace)
    return diff[:_CODEX_MAX_DIFF_BYTES]


def _codex_command_text(tool_input: dict[str, Any]) -> str:
    raw = tool_input.get("command")
    if isinstance(raw, list):
        return " ".join(str(item) for item in raw).strip()
    return str(raw or "").strip()


def _codex_return_code(tool_response: dict[str, Any]) -> int | None:
    for key in ("exit_code", "exitCode", "returnCode", "return_code", "code"):
        value = tool_response.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _codex_error_signature(command: str, error: str) -> str:
    norm = re.sub(r"0x[0-9a-fA-F]+", "0xX", error)
    norm = re.sub(r"\b\d+\b", "N", norm)
    norm = re.sub(r"/[^\s:]+", "<path>", norm)
    key = f"{command.strip()[:80]}::{norm.strip()[:200]}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _codex_cache_bash(root: str | Path, command: str, stdout: str, stderr: str, rc: int | None) -> None:
    if os.environ.get("ATELIER_CACHE_DISABLED") == "1":
        return
    try:
        from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

        cap = ToolSupervisionCapability(Path(root))
        key = f"Bash:{json.dumps({'command': command}, sort_keys=True)[:100]}"
        cap.observe(
            key,
            {
                "command": command,
                "stdout": stdout[:_CODEX_MAX_LEDGER_BYTES],
                "stderr": stderr[:_CODEX_MAX_LEDGER_BYTES],
                "return_code": rc,
            },
            cache_hit=False,
        )
    except (OSError, ImportError, ValueError, AttributeError, TypeError):
        pass


def _codex_record_file_edits(
    root: str | Path, payload: dict[str, Any], session_id: str, tool_input: dict[str, Any]
) -> None:
    if not session_id:
        return
    targets = _codex_edit_targets(tool_input)
    if not targets:
        return
    workspace = _codex_workspace_root(payload)
    events: list[dict[str, Any]] = []
    for path in targets:
        diff = _codex_edit_diff(tool_input, path, workspace)
        events.append(
            {
                "kind": "file_edit",
                "at": _iso_now(),
                "summary": f"edited {Path(path).name}",
                "payload": {"path": path, "diff": diff, "event": "PostToolUse"},
            }
        )
    _codex_append_ledger_events(root, session_id, events)


def _codex_record_command(
    root: str | Path,
    payload: dict[str, Any],
    session_id: str,
    tool_input: dict[str, Any],
    tool_response: dict[str, Any],
) -> dict[str, Any]:
    command = _codex_command_text(tool_input)
    if not command:
        return {"no_output": True}
    stdout = str(tool_response.get("stdout") or tool_response.get("output") or "")
    stderr = str(tool_response.get("stderr") or "")
    rc = _codex_return_code(tool_response)
    ok = (rc == 0) if rc is not None else not stderr
    error = stderr or str(tool_response.get("error") or "")
    signature = _codex_error_signature(command, error)

    _codex_cache_bash(root, command, stdout, stderr, rc)

    if session_id:
        short = command[:80] + ("…" if len(command) > 80 else "")
        _codex_append_ledger_events(
            root,
            session_id,
            [
                {
                    "kind": "command_result",
                    "at": _iso_now(),
                    "summary": f"{'✓' if ok else '✗'} {short}",
                    "payload": {
                        "command": command,
                        "stdout": stdout[:_CODEX_MAX_LEDGER_BYTES],
                        "stderr": stderr[:_CODEX_MAX_LEDGER_BYTES],
                        "return_code": rc,
                        "ok": ok,
                        "event": "PostToolUse",
                    },
                }
            ],
        )

    if ok:
        return {"no_output": True}
    return _codex_track_failure(root, payload, command, signature)


def _codex_track_failure(root: str | Path, payload: dict[str, Any], command: str, signature: str) -> dict[str, Any]:
    state = _read_codex_session_state(root, payload)
    failures = state.get("failures")
    if not isinstance(failures, dict):
        failures = {}
    failures[signature] = int(failures.get(signature, 0) or 0) + 1
    state["failures"] = failures
    _write_codex_session_state(root, payload, state)
    if failures[signature] < _CODEX_FAILURE_THRESHOLD:
        return {"no_output": True}
    return {
        "systemMessage": (
            "Atelier: this command has failed repeatedly with the same error. "
            "Call `rescue` before retrying and change the approach instead of repeating the fix."
        )
    }


def build_codex_post_tool_use_ledger_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Capture PostToolUse edits/commands into the run ledger + supervision cache.

    Stays silent (records state only) except on the second identical command
    failure, where it returns a ``systemMessage`` telling the agent to call
    ``rescue`` -- Codex has no separate PostToolUseFailure event, so the rescue
    nudge is folded into PostToolUse.
    """
    if str(payload.get("hook_event_name") or "") != "PostToolUse":
        return {"no_output": True}
    tool_input = payload.get("tool_input")
    tool_response = payload.get("tool_response")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not isinstance(tool_response, dict):
        tool_response = {}
    norm = _normalize_codex_tool(str(payload.get("tool_name") or ""))
    session_id = _codex_ledger_session_id(root, payload)
    if norm == "edit":
        _codex_record_file_edits(root, payload, session_id, tool_input)
        return {"no_output": True}
    if norm == "bash":
        return _codex_record_command(root, payload, session_id, tool_input, tool_response)
    return {"no_output": True}


def _codex_append_note(root: str | Path, session_id: str, summary: str, payload: dict[str, Any]) -> None:
    if not session_id:
        return
    _codex_append_ledger_events(
        root,
        session_id,
        [{"kind": "note", "at": _iso_now(), "summary": summary, "payload": payload}],
    )


def _codex_context_occupancy(payload: dict[str, Any]) -> tuple[int, str]:
    try:
        from atelier.gateway.hosts.context_state import host_context_state

        occ, model = host_context_state("codex", str(payload.get("session_id") or ""))
        return int(occ or 0), str(model or "")
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return 0, ""


def _codex_append_compaction_savings_row(root: str | Path, session_id: str, model: str | None) -> None:
    """Append a compaction boundary-marker row to sessions/<id>/savings.jsonl.

    Not a savings credit: every reader zeroes ``kind=="compaction"`` rows
    (``_price_savings_row``) — the marker exists so carry/cliff attribution
    can segment the context window at the compaction point.

    Keyed by the host session_id -- the same id ``build_savings_report`` /
    ``build_codex_stop_output`` read per-session savings from.
    """
    if not session_id:
        return
    try:
        from atelier.core.foundation.paths import session_dir

        # Hardcoded, not detect_host(): this helper is Codex-specific (its
        # callers only ever run in a Codex context), and the calling process
        # (e.g. a test, or a future non-Codex-env invocation) may not have the
        # CODEX_* env vars set that detect_host() would otherwise need.
        path = session_dir(root, "codex", session_id) / "savings.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "kind": "compaction",
            "model": model or "",
            "ts": _iso_now(),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _codex_clear_precompact(state: dict[str, Any]) -> None:
    for key in (
        "precompact_pending",
        "precompact_occupancy",
        "precompact_model",
        "precompact_attempts",
    ):
        state.pop(key, None)


def _codex_credit_pending_compaction(
    root: str | Path, session_id: str, state: dict[str, Any], occupancy: int, model: str | None
) -> None:
    """Write the compaction boundary marker after a recent /compact (mirrors Claude).

    PreCompact stored the pre-compaction occupancy; once a turn has run on the
    compacted window we append the ``kind:"compaction"`` marker row. Not a
    savings credit — every reader zeroes compaction rows — the marker segments
    carry/cliff attribution at the compaction point. Conservative: skips while
    the delta isn't visible yet, gives up after three prompts, one-shot per
    compaction. Mutates *state* in place; the caller persists it.
    """
    if not state.get("precompact_pending"):
        return
    attempts = int(state.get("precompact_attempts", 0) or 0) + 1
    state["precompact_attempts"] = attempts
    pre = int(state.get("precompact_occupancy", 0) or 0)
    delta = pre - occupancy
    if occupancy > 0 and 0 < delta <= pre:
        price_model = model or str(state.get("precompact_model") or "")
        _codex_append_compaction_savings_row(root, session_id, price_model)
        _codex_clear_precompact(state)
    elif attempts >= 3:
        _codex_clear_precompact(state)  # post-compact size never resolved; stop trying


def build_codex_pre_compact_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("hook_event_name") or "") != "PreCompact":
        return {"no_output": True}
    trigger = str(payload.get("trigger") or payload.get("matcher") or "auto")
    occ, model = _codex_context_occupancy(payload)
    state = _read_codex_session_state(root, payload)
    if occ > 0:
        state["precompact_occupancy"] = occ
        state["precompact_model"] = model
        state["precompact_pending"] = True
        state["precompact_attempts"] = 0
    # Carry optimizer_notices across the session boundary so that one-shot
    # nudges (ctx_high) survive compaction. Save before opencode replaces
    # the transcript / session_id.
    old_session_id = str(payload.get("session_id") or "default")
    old_stats_path = session_stats_path(root, old_session_id)
    try:
        old_stats = json.loads(old_stats_path.read_text("utf-8")) if old_stats_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        old_stats = {}
    notices = old_stats.get("optimizer_notices")
    if isinstance(notices, dict) and notices:
        state["precompact_optimizer_notices"] = notices
    _write_codex_session_state(root, payload, state)
    _codex_append_note(
        root,
        _codex_ledger_session_id(root, payload),
        f"context compaction starting ({trigger})",
        {"event": "PreCompact", "trigger": trigger},
    )
    return {"no_output": True}


def build_codex_post_compact_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("hook_event_name") or "") != "PostCompact":
        return {"no_output": True}
    trigger = str(payload.get("trigger") or payload.get("matcher") or "auto")
    _codex_append_note(
        root,
        _codex_ledger_session_id(root, payload),
        f"context compaction completed ({trigger})",
        {"event": "PostCompact", "trigger": trigger},
    )
    state = _read_codex_session_state(root, payload)
    state["compaction_epoch"] = int(state.get("compaction_epoch", 0) or 0) + 1
    # Restore optimizer_notices from the pre-compact session so the one-shot
    # nudge guard survives the session_id change opencode performs on /compact.
    notices = state.pop("precompact_optimizer_notices", None)
    if isinstance(notices, dict) and notices:
        new_session_id = str(payload.get("session_id") or "default")
        new_stats_path = session_stats_path(root, new_session_id)
        try:
            new_stats = json.loads(new_stats_path.read_text("utf-8")) if new_stats_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            new_stats = {}
        existing = new_stats.get("optimizer_notices") or {}
        merged = {**notices, **(existing if isinstance(existing, dict) else {})}
        new_stats["optimizer_notices"] = merged
        new_stats_path.parent.mkdir(parents=True, exist_ok=True)
        new_stats_path.write_text(json.dumps(new_stats, indent=2), encoding="utf-8")
    _write_codex_session_state(root, payload, state)
    return {"no_output": True}


def _codex_enrich_user_prompt(root: str | Path, payload: dict[str, Any]) -> None:
    """Record the user prompt into the run ledger + session state (fail-open)."""
    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        return
    stored = prompt[:8192]
    state = _read_codex_session_state(root, payload)
    state["last_user_prompt"] = stored
    # Bank the one-time cache-read saving from a recent /compact (parity with the
    # Claude UserPromptSubmit hook). PreCompact recorded the pre-compaction
    # occupancy; now that a turn has run on the compacted window we read the new
    # occupancy, credit the realized delta, and clear the precompact_* keys --
    # all folded into this single read-modify-write of session state.
    occupancy, occ_model = _codex_context_occupancy(payload)
    _codex_credit_pending_compaction(root, str(payload.get("session_id") or "default"), state, occupancy, occ_model)
    _write_codex_session_state(root, payload, state)
    session_id = _codex_ledger_session_id(root, payload)
    if not session_id:
        return
    short = stored[:100].replace("\n", " ")
    _codex_append_ledger_events(
        root,
        session_id,
        [
            {
                "kind": "agent_message",
                "at": _iso_now(),
                "summary": f"user: {short}{'…' if len(stored) > 100 else ''}",
                "payload": {
                    "role": "user",
                    "prompt": stored,
                    "truncated": len(prompt) > 8192,
                    "event": "UserPromptSubmit",
                },
            }
        ],
    )


def build_codex_savings_line(root: str | Path, session_id: str) -> str:
    """Pipe-delimited savings line for the Codex command-backed statusline.

    Uses the statusline pipe-field order (saved|tok|calls|status|routing|cost|
    total|in|cache|out|carry$|carry_tok|carry%|saved%) so a shared statusline
    parser works, but sources the figures from ``build_savings_report`` (the
    Codex savings data model) instead of the Claude sidecar. Carry and
    display-token fields are not tracked for Codex and report 0. Fail-open: any
    error yields an all-zero line rather than raising into the statusline.
    """
    saved_usd = 0.0
    routing_usd = 0.0
    tokens_saved = 0
    calls_saved = 0
    total_calls = 0
    # A fresh session has no stats sidecar yet; skip the report build entirely
    # so the statusline never triggers the missing-file path on every refresh.
    if session_id and session_stats_path(root, session_id).exists():
        try:
            report = build_savings_report(root, session_id=session_id)
            session = report.get("session") or {}
            cost = report.get("cost") or {}
            saved_usd = float(cost.get("saved_usd", 0.0) or 0.0)
            routing_usd = float(cost.get("routing_saved_usd", 0.0) or 0.0)
            tokens_saved = int(report.get("tokens_saved", 0) or 0)
            calls_saved = int(report.get("calls_avoided", 0) or 0)
            total_calls = int(session.get("total_tool_calls", 0) or 0)
        except (OSError, KeyError, ValueError, TypeError):
            pass
    status_text = f"{total_calls} tool calls" if total_calls else "ready"
    return (
        f"${saved_usd:.3f}|{tokens_saved}|{calls_saved}|{status_text}|${routing_usd:.3f}|0.000|0|0|0|0|$0.000|0|0%|0%"
    )


# ---------------------------------------------------------------------------
# Codex-exclusive surfaces (no Claude analog)
# ---------------------------------------------------------------------------
# PermissionRequest fires before Codex shows an approval prompt for an escalated
# tool; a hook may return ``behavior: "deny"`` to block it outright. We use it
# as defense-in-depth: auto-deny a small set of catastrophic command patterns
# even when the approval policy would otherwise allow them. We deliberately do
# NOT auto-*allow* here (trusted-tool approval is config.toml's job) so the hook
# can only ever make the session safer, never looser.

_CODEX_DANGEROUS_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+/\s*(\*\s*)?$",  # rm -rf /   or  rm -rf /*
        r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+/\*",  # rm -rf /* (mid-line)
        # rm -rf /<critical-root> (bare root only; /usr/local/... is left to the prompt)
        r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+/(usr|etc|bin|sbin|lib|lib64|boot|sys|proc|root)/?(\s|$|[;&|])",
        r"\brm\b[^\n]*--force\b[^\n]*\s/\s*($|[;&|])",  # rm --recursive --force /  (long-form, bare root)
        r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+~",  # rm -rf ~  /  ~/...
        r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+\$HOME\b",
        r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+\*",  # rm -rf *
        r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+\.\.?\s*$",  # rm -rf .  /  ..
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork bomb
        r"\bmkfs\.[a-z0-9]+\b",
        r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|disk)",
        r">\s*/dev/(sd|nvme|disk)",
        r"\bgit\s+push\b[^\n]*--force(?!-with-lease)",  # git push --force
        r"\bgit\s+push\b[^\n]*\s-[a-zA-Z]*f[a-zA-Z]*(\s|$)",  # git push -f / -fv (short force)
        r"\bchmod\s+-R\s+0?777\s+/(\s|$)",
    )
)


def build_codex_permission_request_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Auto-deny catastrophic shell commands at Codex's approval prompt.

    Codex-only: there is no Claude analog for PermissionRequest. Returns a
    ``behavior: "deny"`` decision for a small denylist of irreversible command
    patterns; otherwise stays silent so the normal approval flow runs. Never
    auto-approves.
    """
    del root  # reserved for symmetry with other build_codex_* entry points
    if str(payload.get("hook_event_name") or "") != "PermissionRequest":
        return {"no_output": True}
    if _normalize_codex_tool(str(payload.get("tool_name") or "")) != "bash":
        return {"no_output": True}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return {"no_output": True}
    command = _codex_command_text(tool_input)
    if not command:
        return {"no_output": True}
    if not any(pattern.search(command) for pattern in _CODEX_DANGEROUS_COMMAND_PATTERNS):
        return {"no_output": True}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "behavior": "deny",
            "message": f"Atelier blocked an irreversible command pattern: {command[:120]}",
        }
    }


def ingest_codex_exec_events(root: str | Path, session_id: str, lines: list[str]) -> int:
    """Record ``codex exec --json`` item events into the run ledger.

    Backfills telemetry for headless/CI runs and for tools that do not fire
    interactive PostToolUse hooks. Parses the JSON-Lines event stream and
    appends ``command_result`` / ``file_edit`` ledger events. Returns the number
    of ledger events written. Fail-open and schema-defensive: unrecognized lines
    are skipped. The exec item schema is Codex-version-dependent.
    """
    if not session_id:
        return 0
    events_to_add: list[dict[str, Any]] = []
    for raw in lines:
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "item.completed":
            continue
        item = obj.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "command_execution":
            command = str(item.get("command") or "")
            if not command:
                continue
            rc = item.get("exit_code")
            rc_int = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
            ok = rc_int == 0 if rc_int is not None else True
            output = str(item.get("aggregated_output") or item.get("output") or "")
            short = command[:80] + ("…" if len(command) > 80 else "")
            events_to_add.append(
                {
                    "kind": "command_result",
                    "at": _iso_now(),
                    "summary": f"{'✓' if ok else '✗'} {short}",
                    "payload": {
                        "command": command,
                        "stdout": output[:_CODEX_MAX_LEDGER_BYTES],
                        "return_code": rc_int,
                        "ok": ok,
                        "event": "codex_exec",
                    },
                }
            )
        elif item_type == "file_change":
            changes = item.get("changes")
            paths: list[str] = []
            if isinstance(changes, list):
                for change in changes:
                    candidate = change.get("path") if isinstance(change, dict) else None
                    if isinstance(candidate, str) and candidate:
                        paths.append(candidate)
            single = item.get("path")
            if isinstance(single, str) and single:
                paths.append(single)
            for path in paths:
                events_to_add.append(
                    {
                        "kind": "file_edit",
                        "at": _iso_now(),
                        "summary": f"edited {Path(path).name}",
                        "payload": {"path": path, "event": "codex_exec"},
                    }
                )
    if not events_to_add:
        return 0
    _codex_append_ledger_events(root, session_id, events_to_add)
    return len(events_to_add)


def baseline_is_available(vanillaSessions: int, totalVanillaCostInUsd: float) -> dict[str, Any]:
    available = vanillaSessions >= 5 and totalVanillaCostInUsd > 0
    if not available:
        return {"available": False, "reason": "requires at least 5 vanilla sessions"}
    return {"available": True}


def session_stats_path(root: str | Path, session_id: str) -> Path:
    from atelier.core.foundation.paths import detect_host, session_dir

    return session_dir(root, detect_host(), session_id) / "stats.json"


def _session_event_path(root: str | Path, session_id: str) -> Path:
    from atelier.core.foundation.paths import detect_host, session_dir

    return session_dir(root, detect_host(), session_id) / "events.jsonl"


def _now_ms(payload: dict[str, Any] | None = None) -> int:
    payload = payload or {}
    raw = payload.get("now_ms") or payload.get("timestamp_ms") or payload.get("now") or payload.get("timestamp")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        text = raw.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except ValueError:
            try:
                return int(float(raw))
            except ValueError:
                logger.warning("Failed to parse timestamp %r", raw, exc_info=True)
    return int(datetime.now().timestamp() * 1000)


def _token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if not isinstance(value, str):
        return 0
    text = value.strip().replace(",", "")
    if not text:
        return 0
    multiplier = 1
    suffix = text[-1].lower()
    if suffix == "k":
        multiplier = 1_000
        text = text[:-1]
    elif suffix == "m":
        multiplier = 1_000_000
        text = text[:-1]
    elif suffix == "b":
        multiplier = 1_000_000_000
        text = text[:-1]
    try:
        return max(0, int(float(text) * multiplier))
    except ValueError:
        return 0


def _nested_token_count(raw: dict[str, Any], path: tuple[str, ...]) -> int:
    current: Any = raw
    for key in path:
        if not isinstance(current, dict):
            return 0
        current = current.get(key)
    return _token_count(current)


def _usage_numbers(raw: dict[str, Any]) -> dict[str, int]:
    aliases = {
        "input_tokens": (
            ("input_tokens",),
            ("prompt_tokens",),
            ("inputTokens",),
            ("tokens_in",),
            ("input",),
            ("in",),
        ),
        "output_tokens": (
            ("output_tokens",),
            ("completion_tokens",),
            ("outputTokens",),
            ("tokens_out",),
            ("output",),
            ("out",),
        ),
        "reasoning_output_tokens": (
            ("reasoning_output_tokens",),
            ("reasoningOutputTokens",),
            ("reasoning_tokens",),
            ("reasoningTokens",),
            ("output_tokens_details", "reasoning_tokens"),
            ("outputTokensDetails", "reasoningTokens"),
        ),
        "thinking_tokens": (
            ("thinking_tokens",),
            ("thinkingTokens",),
            ("thoughts_tokens",),
            ("thoughtsTokens",),
        ),
        "cache_read_tokens": (
            ("cache_read_input_tokens",),
            ("cache_read_tokens",),
            ("cached_input_tokens",),
            ("cacheReadTokens",),
            ("cachedInputTokens",),
            ("cache_read",),
            ("cacheRead",),
            ("cache", "read"),
        ),
        "cache_write_tokens": (
            ("cache_creation_input_tokens",),
            ("cache_write_tokens",),
            ("cacheCreationInputTokens",),
            ("cacheWriteTokens",),
            ("cache_write",),
            ("cacheWrite",),
            ("cache", "write"),
        ),
    }
    result: dict[str, int] = {key: 0 for key in aliases}
    for target, paths in aliases.items():
        for path in paths:
            value = _nested_token_count(raw, path)
            if value > 0:
                result[target] = value
                break
    return result


def _extract_usage(payload: dict[str, Any]) -> dict[str, int]:
    # Only accumulate per-turn deltas — NOT context_window.current_usage (cumulative session
    # total) and NOT transcript data (handled separately in stop.py).  Both are overwrite/
    # snapshot sources and must not be summed across calls.
    usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_output_tokens": 0,
        "thinking_tokens": 0,
    }
    message_usage = (payload.get("message") or {}).get("usage") if isinstance(payload.get("message"), dict) else None
    candidates = (
        payload.get("usage"),
        payload.get("token_usage"),
        payload.get("tokenUsage"),
        payload.get("tokens"),
        message_usage,
    )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        found = _usage_numbers(candidate)
        for key, value in found.items():
            usage[key] += value
    return usage


def _usage_from_transcript(path: Path) -> list[dict[str, int]]:
    rows: list[dict[str, int]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            continue
        if not isinstance(payload, dict):
            continue
        for candidate in (
            payload.get("usage"),
            (payload.get("message") or {}).get("usage") if isinstance(payload.get("message"), dict) else None,
        ):
            if isinstance(candidate, dict):
                rows.append(_usage_numbers(candidate))
    return rows


def _merge_usage(state: dict[str, Any], usage: dict[str, int]) -> None:
    totals = state.setdefault(
        "usage",
        {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
    )
    for key, value in usage.items():
        totals[key] = int(totals.get(key, 0) or 0) + max(0, int(value))


def _usage_has_tokens(usage: dict[str, Any]) -> bool:
    return any(
        int(usage.get(key, 0) or 0) > 0
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    )


def _context_usage_snapshot(payload: dict[str, Any]) -> dict[str, int]:
    context_cw = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else None
    context_usage = payload.get("context") if isinstance(payload.get("context"), dict) else None
    context_cw_usage = context_cw.get("current_usage") if context_cw else None
    context_usage_snapshot = None
    if context_usage:
        context_usage_snapshot = context_usage.get("current_usage") or context_usage.get("usage")
    for snapshot_source in (context_cw_usage, context_usage_snapshot):
        if isinstance(snapshot_source, dict):
            snapshot = _usage_numbers(snapshot_source)
            if _usage_has_tokens(snapshot):
                return snapshot
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}


def record_codex_statusline_snapshot(root: str | Path, payload: dict[str, Any]) -> None:
    """Persist Codex statusline usage even when the native footer lacks a session id."""
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        update_session_stats(root, payload)
        return
    usage = _context_usage_snapshot(payload)
    if not _usage_has_tokens(usage):
        usage = _extract_usage(payload)
    if not _usage_has_tokens(usage):
        return
    state = _read_codex_session_state(root, payload)
    snapshot: dict[str, Any] = {"at_ms": _now_ms(payload), "usage": usage}
    model = _codex_payload_model(payload)
    if model:
        snapshot["model"] = model
    cost_usd = _codex_payload_cost_usd(payload)
    if cost_usd > 0:
        snapshot["cost_usd"] = cost_usd
    state["last_statusline_usage"] = snapshot
    _write_codex_session_state(root, payload, state)


def _apply_codex_statusline_snapshot(root: str | Path, session_id: str, payload: dict[str, Any]) -> float:
    state = _read_codex_session_state(root, payload)
    snapshot = _as_dict(state.get("last_statusline_usage"))
    usage = _as_dict(snapshot.get("usage"))
    if not session_id or not _usage_has_tokens(usage):
        return 0.0
    at_ms = int(snapshot.get("at_ms", 0) or 0)
    if at_ms > 0 and _now_ms(payload) - at_ms > 6 * 60 * 60 * 1000:
        return 0.0
    status_payload: dict[str, Any] = {
        "hook_event_name": "StatuslineUpdate",
        "session_id": session_id,
        "context_window": {
            "current_usage": {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_tokens", 0) or 0),
                "cache_creation_input_tokens": int(usage.get("cache_write_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "reasoning_output_tokens": int(usage.get("reasoning_output_tokens", 0) or 0),
                "thinking_tokens": int(usage.get("thinking_tokens", 0) or 0),
            }
        },
    }
    model = str(snapshot.get("model") or "").strip()
    if model:
        status_payload["model"] = model
    update_session_stats(root, status_payload)
    return float(snapshot.get("cost_usd", 0.0) or 0.0)


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_transcript_paths(payload: dict[str, Any], session_id: str) -> list[Path]:
    explicit = payload.get("transcript_path") or payload.get("session_path")
    if isinstance(explicit, str) and explicit.strip():
        path = Path(explicit).expanduser()
        if path.is_file():
            return [path]

    sessions_root = _codex_home() / "sessions"
    if not sessions_root.is_dir():
        return []

    def _mtime(path: Path) -> float:
        with suppress(OSError):
            return path.stat().st_mtime
        return 0.0

    paths = sorted(sessions_root.glob("**/*.jsonl"), key=_mtime, reverse=True)
    if session_id:
        matched = [path for path in paths if session_id in path.name]
        if matched:
            return matched
    return paths[:100]


def _codex_transcript_snapshot(path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "session_id": "",
        "cwd": "",
        "model": "",
        "usage": {},
        "llm_turns": 0,
        "tools_used": {},
        "total_tool_calls": 0,
    }
    latest_usage: dict[str, Any] = {}
    llm_turns = 0
    previous_total_signature: tuple[int, int, int, int] | None = None
    previous_unidentified_event = ""
    seen_event_ids: set[str] = set()
    tools_used: dict[str, int] = {}
    fallback_tools_used: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return snapshot
    for line in lines:
        raw_line = line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_id = ""
        if isinstance(event, dict):
            raw_payload = event.get("payload")
            payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
            for candidate in (event, payload):
                for key in ("id", "event_id", "eventId", "uuid", "message_id", "messageId"):
                    value = str(candidate.get(key) or "").strip()
                    if value:
                        event_id = value
                        break
                if event_id:
                    break
        if event_id:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            previous_unidentified_event = ""
        elif raw_line == previous_unidentified_event:
            continue
        else:
            previous_unidentified_event = raw_line
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "session_meta":
            if payload.get("id"):
                snapshot["session_id"] = str(payload.get("id"))
            if payload.get("cwd"):
                snapshot["cwd"] = str(payload.get("cwd"))
            continue
        if event.get("type") == "turn_context":
            if payload.get("model"):
                snapshot["model"] = str(payload.get("model"))
            continue
        if event.get("type") == "response_item":
            item_type = str(payload.get("type") or "")
            if item_type in {"function_call", "custom_tool_call"}:
                name = str(payload.get("name") or "custom_tool").strip()
                if name:
                    tools_used[name] = int(tools_used.get(name, 0) or 0) + 1
            continue
        if event.get("type") != "event_msg":
            continue
        payload_type = str(payload.get("type") or "")
        if payload_type == "exec_command_end":
            fallback_tools_used["exec_command"] = int(fallback_tools_used.get("exec_command", 0) or 0) + 1
            continue
        if payload_type == "patch_apply_end":
            fallback_tools_used["apply_patch"] = int(fallback_tools_used.get("apply_patch", 0) or 0) + 1
            continue
        if payload_type == "mcp_tool_call_end":
            invocation = _as_dict(payload.get("invocation"))
            tool_name = str(invocation.get("tool") or "mcp").strip()
            server = str(invocation.get("server") or "").strip()
            name = f"{server}.{tool_name}" if server else tool_name
            if name:
                tools_used[name] = int(tools_used.get(name, 0) or 0) + 1
            continue
        if payload_type != "token_count":
            continue
        info = _as_dict(payload.get("info"))
        last_usage = _as_dict(info.get("last_token_usage"))
        last_numbers = _usage_numbers(last_usage) if last_usage else {}
        if _usage_has_tokens(last_numbers):
            llm_turns += 1
        usage = _as_dict(info.get("total_token_usage"))
        if usage:
            latest_usage = usage
            total_numbers = _usage_numbers(usage)
            total_signature = (
                int(total_numbers.get("input_tokens", 0) or 0),
                int(total_numbers.get("output_tokens", 0) or 0),
                int(total_numbers.get("cache_read_tokens", 0) or 0),
                int(total_numbers.get("cache_write_tokens", 0) or 0),
            )
            if not _usage_has_tokens(last_numbers) and _usage_has_tokens(total_numbers):
                if previous_total_signature is None or total_signature != previous_total_signature:
                    llm_turns += 1
            previous_total_signature = total_signature

    total_numbers = _usage_numbers(latest_usage)
    total_input = int(total_numbers.get("input_tokens", 0) or 0)
    cached_input = int(total_numbers.get("cache_read_tokens", 0) or 0)
    output = int(total_numbers.get("output_tokens", 0) or 0)
    cache_write = int(total_numbers.get("cache_write_tokens", 0) or 0)
    if total_input or cached_input or output:
        snapshot["usage"] = {
            "input_tokens": max(0, total_input - cached_input),
            "output_tokens": output,
            "cache_read_tokens": cached_input,
            "cache_write_tokens": cache_write,
            "reasoning_output_tokens": int(total_numbers.get("reasoning_output_tokens", 0) or 0),
            "thinking_tokens": int(total_numbers.get("thinking_tokens", 0) or 0),
        }
    snapshot["llm_turns"] = llm_turns
    if tools_used:
        for name, count in fallback_tools_used.items():
            if name not in tools_used:
                tools_used[name] = count
    else:
        tools_used = fallback_tools_used
    snapshot["tools_used"] = tools_used
    snapshot["total_tool_calls"] = sum(int(count or 0) for count in tools_used.values())
    return snapshot


def _apply_codex_transcript_snapshot(root: str | Path, session_id: str, payload: dict[str, Any]) -> None:
    if not session_id:
        return
    wanted_cwd = str(payload.get("cwd") or os.environ.get("CODEX_WORKSPACE_ROOT") or "").strip()
    best: dict[str, Any] = {}
    for path in _codex_transcript_paths(payload, session_id):
        snapshot = _codex_transcript_snapshot(path)
        usage = _as_dict(snapshot.get("usage"))
        if not _usage_has_tokens(usage):
            continue
        transcript_session_id = str(snapshot.get("session_id") or "")
        transcript_cwd = str(snapshot.get("cwd") or "")
        if session_id and transcript_session_id == session_id:
            best = snapshot
            break
        if wanted_cwd and transcript_cwd == wanted_cwd:
            best = snapshot
            break
    usage = _as_dict(best.get("usage"))
    if not _usage_has_tokens(usage):
        return
    status_payload: dict[str, Any] = {
        "hook_event_name": "StatuslineUpdate",
        "session_id": session_id,
        "context_window": {
            "current_usage": {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_tokens", 0) or 0),
                "cache_creation_input_tokens": int(usage.get("cache_write_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "reasoning_output_tokens": int(usage.get("reasoning_output_tokens", 0) or 0),
                "thinking_tokens": int(usage.get("thinking_tokens", 0) or 0),
            }
        },
    }
    model = str(best.get("model") or "").strip()
    if model:
        status_payload["model"] = model
    llm_turns = int(best.get("llm_turns", 0) or 0)
    if llm_turns > 0:
        status_payload["llm_turns"] = llm_turns
    tools_used = _as_dict(best.get("tools_used"))
    if tools_used:
        status_payload["tools_used"] = tools_used
        status_payload["total_tool_calls"] = int(best.get("total_tool_calls", 0) or 0)
        status_payload["tool_call_source"] = "transcript"
    update_session_stats(root, status_payload)


def _append_session_event(root: str | Path, session_id: str, payload: dict[str, Any]) -> None:
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if not event:
        return
    path = _session_event_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at_ms": _now_ms(payload),
        "event": event,
        "tool_name": payload.get("tool_name"),
        "subagent_type": (
            (payload.get("tool_input") or {}).get("subagent_type")
            if isinstance(payload.get("tool_input"), dict)
            else None
        ),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _normalize_workflow_state_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    workflow_step = str(raw.get("workflow_step") or raw.get("current_step") or "").strip()
    session_phase = str(raw.get("session_phase") or "").strip()
    result: dict[str, Any] = {}
    if workflow_step:
        result["workflow_step"] = workflow_step
    if session_phase:
        result["session_phase"] = session_phase
    return result


def _normalize_plan_review_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    review_decision = str(raw.get("review_decision") or raw.get("decision") or "").strip()
    plan_id = str(raw.get("plan_id") or "").strip()
    workflow_step = str(raw.get("workflow_step") or "").strip()
    result: dict[str, Any] = {}
    if review_decision:
        result["review_decision"] = review_decision
    if plan_id:
        result["plan_id"] = plan_id
    if workflow_step:
        result["workflow_step"] = workflow_step
    return result


def _normalize_task_progress_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    task_id = str(raw.get("task_id") or "").strip()
    workflow_step = str(raw.get("workflow_step") or "").strip()
    result: dict[str, Any] = {}
    if task_id:
        result["task_id"] = task_id
    if workflow_step:
        result["workflow_step"] = workflow_step
    for key in ("completed_tasks", "remaining_tasks"):
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        try:
            result[key] = max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    return result


def _normalize_spawn_telemetry_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    dropped = raw.get("host_dropped_fields")
    return {
        "eligible_for_reuse": bool(raw.get("eligible_for_reuse", False)),
        "reuse_observed": bool(raw.get("reuse_observed", False)),
        "spawn_latency_ms": max(0, int(raw.get("spawn_latency_ms", 0) or 0)),
        "cache_capability": str(raw.get("cache_capability") or "").strip(),
        "host_dropped_fields": (
            [str(item).strip() for item in dropped if str(item).strip()] if isinstance(dropped, list | tuple) else []
        ),
    }


def _normalize_spawn_summary_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result = {
        "step_count": max(0, int(raw.get("step_count", 0) or 0)),
        "eligible_for_reuse": max(0, int(raw.get("eligible_for_reuse", 0) or 0)),
        "reuse_observed": max(0, int(raw.get("reuse_observed", 0) or 0)),
        "spawn_latency_ms": max(0, int(raw.get("spawn_latency_ms", 0) or 0)),
        "cache_capability_counts": {},
        "host_dropped_fields": {},
    }
    cache_capability_counts = raw.get("cache_capability_counts")
    if isinstance(cache_capability_counts, dict):
        result["cache_capability_counts"] = {
            str(key): max(0, int(value or 0)) for key, value in cache_capability_counts.items() if str(key).strip()
        }
    host_dropped_fields = raw.get("host_dropped_fields")
    if isinstance(host_dropped_fields, dict):
        result["host_dropped_fields"] = {
            str(key): max(0, int(value or 0)) for key, value in host_dropped_fields.items() if str(key).strip()
        }
    return result


def optional_usage_log_path(root: str | Path) -> Path:
    """Append-only log of optional (non-default) agent/skill invocations.

    One JSON line per use: ``{"kind": "agent"|"skill", "name": ..., "at_ms": ...}``.
    Backs the staleness nudge (Claude statusline tip, OpenCode prompt-time
    plugin, `atelier stale-nudge`): the only durable record of "when was this
    last used" -- session stats are per-session and don't survive across
    sessions, and nothing else in the codebase tracks this granularity.
    """
    return Path(root) / "optional_usage.jsonl"


def record_optional_use(root: str | Path, kind: str, name: str, at_ms: int) -> None:
    """Append one usage event. Fail-open: never let logging break the caller."""
    path = optional_usage_log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "name": name, "at_ms": at_ms}) + "\n")
    except OSError:
        pass


def last_optional_use_ms(root: str | Path, kind: str, name: str) -> int | None:
    """Most recent ``at_ms`` recorded for (kind, name), or None if never used."""
    path = optional_usage_log_path(root)
    if not path.exists():
        return None
    last: int | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("kind") == kind and row.get("name") == name:
                    at_ms = row.get("at_ms")
                    if isinstance(at_ms, int) and (last is None or at_ms > last):
                        last = at_ms
    except OSError:
        return None
    return last


def _optional_usage_event(event: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return ("agent"|"skill", name) if this hook event is use of an optional
    (non-default) atelier agent role or skill; else None.

    Two agent-dispatch shapes are handled: a native ``SubagentStart`` event
    (``agent_type`` on the payload) and a ``PostToolUse`` event for the
    ``Agent`` tool (``subagent_type`` in ``tool_input`` -- how subagent
    dispatch is reported by hosts without a distinct SubagentStart event; see
    test_session_telemetry_tracks_usage_compaction_and_subagents). The
    default role (``code``) and default skill (``atelier``) never match --
    callers never nudge about either.
    """
    from atelier.core.capabilities.default_definitions import DEFAULT_ROLE_IDS, SURFACED_ROLE_IDS
    from atelier.core.environment import DEFAULT_SKILLS

    agent_type = ""
    if event == "SubagentStart":
        agent_type = str(payload.get("agent_type") or "")
    elif event == "PostToolUse" and str(payload.get("tool_name") or "") == "Agent":
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict):
            agent_type = str(tool_input.get("subagent_type") or "")
    if agent_type:
        role_id = agent_type.split(":", 1)[1] if ":" in agent_type else agent_type
        if role_id in SURFACED_ROLE_IDS and role_id not in DEFAULT_ROLE_IDS:
            return "agent", role_id
        return None

    if event == "PostToolUse" and str(payload.get("tool_name") or "") == "Skill":
        tool_input = payload.get("tool_input")
        raw = str(tool_input.get("skill") or "") if isinstance(tool_input, dict) else ""
        name = raw.split(":", 1)[1] if ":" in raw else raw
        if name and name not in DEFAULT_SKILLS:
            return "skill", name
    return None


def update_session_stats(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "default")
    path = session_stats_path(root, session_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        logging.exception("Recovered from broad exception handler")
        state = {}
    state.setdefault("session_id", session_id)
    state.setdefault("started_at_ms", _now_ms(payload))
    state.setdefault("total_tool_calls", 0)
    state.setdefault("edit_tool_calls", 0)
    state.setdefault("turns", 0)
    state.setdefault("event_counts", {})
    if not isinstance(state.get("event_counts"), dict):
        state["event_counts"] = {}
    state.setdefault("tools_used", {})
    if not isinstance(state.get("tools_used"), dict):
        state["tools_used"] = {}
    state.setdefault(
        "usage",
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_output_tokens": 0,
            "thinking_tokens": 0,
        },
    )
    state["last_event_at_ms"] = _now_ms(payload)
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if event:
        state["event_counts"][event] = int(state["event_counts"].get(event, 0) or 0) + 1
    model = _codex_payload_model(payload)
    if model:
        state.setdefault("model", model)
        state["last_model"] = model
    workflow_state = _normalize_workflow_state_payload(payload.get("workflow_state"))
    if workflow_state:
        state["workflow_state"] = workflow_state
    plan_review = _normalize_plan_review_payload(payload.get("plan_review"))
    if plan_review:
        state["plan_review"] = plan_review
    task_progress = _normalize_task_progress_payload(payload.get("task_progress"))
    if task_progress:
        state["task_progress"] = task_progress
    spawn_summary = _normalize_spawn_summary_payload(payload.get("spawn_summary"))
    if spawn_summary:
        state["spawn_summary"] = spawn_summary
    state.setdefault(
        "spawn_telemetry",
        {
            "eligible_for_reuse": 0,
            "reuse_observed": 0,
            "spawn_latency_ms": 0,
            "cache_capability_counts": {},
            "host_dropped_fields": {},
        },
    )
    _merge_usage(state, _extract_usage(payload))
    # context_window.current_usage is a cumulative snapshot of the entire session so far.
    # Overwrite state["usage"] with it each time — never accumulate it additively.
    snapshot = _context_usage_snapshot(payload)
    if _usage_has_tokens(snapshot):
        state["usage"].update(snapshot)
    llm_turns = int(payload.get("llm_turns", 0) or 0)
    if llm_turns > 0:
        state["llm_turns"] = llm_turns
    transcript_tools = _as_dict(payload.get("tools_used"))
    if transcript_tools and payload.get("tool_call_source") == "transcript":
        if "hook_total_tool_calls" not in state:
            state["hook_total_tool_calls"] = int(state.get("total_tool_calls", 0) or 0)
        if "hook_tools_used" not in state:
            state["hook_tools_used"] = _as_dict(state.get("tools_used"))
        clean_tools = _raw_tool_counts(transcript_tools)
        state["tools_used"] = clean_tools
        state["total_tool_calls"] = int(payload.get("total_tool_calls", 0) or 0) or sum(clean_tools.values())
        state["tool_call_source"] = "transcript"
    if event == "UserPromptSubmit":
        turn_id = str(payload.get("turn_id") or payload.get("prompt_id") or "")
        if turn_id:
            seen_turn_ids = state.get("seen_turn_ids")
            if not isinstance(seen_turn_ids, dict):
                seen_turn_ids = {}
                state["seen_turn_ids"] = seen_turn_ids
            if not seen_turn_ids.get(turn_id):
                state["turns"] = int(state.get("turns", 0) or 0) + 1
                seen_turn_ids[turn_id] = True
        else:
            state["turns"] = int(state.get("turns", 0) or 0) + 1
    elif event == "SubagentStart":
        agent_id = str(payload.get("agent_id") or "")
        active_subagents = state.get("active_subagents")
        if not isinstance(active_subagents, dict):
            active_subagents = {}
            state["active_subagents"] = active_subagents
        should_count = not agent_id or agent_id not in active_subagents
        if should_count:
            state["subagents_started"] = int(state.get("subagents_started", 0) or 0) + 1
            state["pending_subagents"] = max(0, int(state.get("pending_subagents", 0) or 0) + 1)
        if agent_id:
            active_subagents[agent_id] = {
                "agent_type": str(payload.get("agent_type") or ""),
                "started_at_ms": _now_ms(payload),
            }
    elif event == "PostToolUse":
        tool_name = str(payload.get("tool_name") or "")
        if tool_name:
            tools_used = state.get("tools_used")
            if not isinstance(tools_used, dict):
                tools_used = {}
                state["tools_used"] = tools_used
            _merge_raw_tool_count(tools_used, tool_name, 1)
        state["total_tool_calls"] = int(state.get("total_tool_calls", 0)) + 1
        from atelier.core.capabilities.session_optimizer import tool_is_edit

        if tool_is_edit(tool_name):
            state["edit_tool_calls"] = int(state.get("edit_tool_calls", 0) or 0) + 1
            state.setdefault("first_edit_at_ms", _now_ms(payload))
        if tool_name == "Agent":
            state["subagents_started"] = int(state.get("subagents_started", 0) or 0) + 1
            state["pending_subagents"] = max(0, int(state.get("pending_subagents", 0) or 0) + 1)
        spawn_telemetry = _normalize_spawn_telemetry_payload(payload.get("spawn_telemetry"))
        if spawn_telemetry:
            state["spawn_telemetry"]["eligible_for_reuse"] += int(spawn_telemetry["eligible_for_reuse"])
            state["spawn_telemetry"]["reuse_observed"] += int(spawn_telemetry["reuse_observed"])
            state["spawn_telemetry"]["spawn_latency_ms"] += int(spawn_telemetry["spawn_latency_ms"])
            cache_capability = str(spawn_telemetry.get("cache_capability") or "")
            if cache_capability:
                counts = state["spawn_telemetry"]["cache_capability_counts"]
                counts[cache_capability] = int(counts.get(cache_capability, 0) or 0) + 1
            for field in spawn_telemetry.get("host_dropped_fields", []):
                dropped_fields = state["spawn_telemetry"]["host_dropped_fields"]
                dropped_fields[field] = int(dropped_fields.get(field, 0) or 0) + 1
    elif event == "PreCompact":
        state["compaction_started_at_ms"] = _now_ms(payload)
    elif event == "PostCompact":
        state["compactions"] = int(state.get("compactions", 0)) + 1
        started_at = int(state.pop("compaction_started_at_ms", _now_ms(payload)) or _now_ms(payload))
        state["compaction_duration_ms"] = int(state.get("compaction_duration_ms", 0) or 0) + max(
            0, _now_ms(payload) - started_at
        )
    elif event == "SubagentStop":
        agent_id = str(payload.get("agent_id") or "")
        active_subagents = _as_dict(state.get("active_subagents"))
        if agent_id:
            active_subagents.pop(agent_id, None)
            state["active_subagents"] = active_subagents
        state["subagents_completed"] = int(state.get("subagents_completed", 0) or 0) + 1
        state["pending_subagents"] = max(0, int(state.get("pending_subagents", 0) or 0) - 1)
        state["completed"] = True
    elif event == "Stop":
        state["completed"] = True
        # Session ended: refresh the month-to-date usage meter so the plan
        # spend/savings and the statusline warning reflect this session's cost.
        refresh_subscription_meter(root)
    usage_event = _optional_usage_event(event, payload)
    if usage_event is not None:
        record_optional_use(root, usage_event[0], usage_event[1], int(state["last_event_at_ms"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _append_session_event(root, session_id, payload)
    return state


def get_session_stats_from_trace(trace: Any) -> dict[str, Any]:
    """Reconstruct a session stats dictionary from a Trace object.

    NOTE: Savings (tokens_saved / cost_saved_usd) come from the Claude
    transcript JSONL (tool_result.content[].saved). This function only
    reports tool-call counts that can be derived deterministically from
    the trace.
    """
    tools_called = {tc.name: tc.count for tc in trace.tools_called}
    total_tool_calls = sum(tools_called.values())

    return {
        "id": trace.id,
        "session_id": trace.session_id,
        "agent": trace.agent,
        "task": trace.task,
        "total_tool_calls": total_tool_calls,
        "usage": {
            "input_tokens": trace.input_tokens,
            "output_tokens": trace.output_tokens,
            "cache_read_tokens": trace.cached_input_tokens,
            "cache_write_tokens": trace.cache_creation_input_tokens,
            "thinking_tokens": getattr(trace, "thinking_tokens", 0),
        },
        "model": trace.model,
        "completed": True,
        "last_event_at_ms": int(trace.created_at.timestamp() * 1000),
    }


def list_session_stats(root: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    sessions_dir = Path(root) / "sessions"
    if not sessions_dir.exists():
        return []

    # Get the newest sessions first
    files = sorted(sessions_dir.glob("*/stats.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    results: list[dict[str, Any]] = []
    for file_path in files[:limit]:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                results.append(data)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            continue
    return results


def aggregate_session_stats(root: str | Path, session_id: str | None = None) -> dict[str, Any]:
    sessions_dir = Path(root) / "sessions"
    files = (
        [session_stats_path(root, session_id)]
        if session_id
        else sorted(sessions_dir.glob("*/*/*/*/*/stats.json")) if sessions_dir.exists() else []
    )
    aggregate: dict[str, Any] = {
        "session_count": 0,
        "total_tool_calls": 0,
        "hook_total_tool_calls": 0,
        "edit_tool_calls": 0,
        "turns": 0,
        "llm_turns": 0,
        "tools_used": {},
        "hook_tools_used": {},
        "tool_call_source": "",
        "model": "",
        "last_model": "",
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_output_tokens": 0,
            "thinking_tokens": 0,
        },
        "pre_compact_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "est_cost_usd": 0.0,
        },
        "compactions": 0,
        "compaction_duration_ms": 0,
        "pending_subagents": 0,
        "subagents_started": 0,
        "subagents_completed": 0,
        "spawn_telemetry": {
            "eligible_for_reuse": 0,
            "reuse_observed": 0,
            "spawn_latency_ms": 0,
            "cache_capability_counts": {},
            "host_dropped_fields": {},
        },
    }
    for file_path in files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - best-effort skip of unreadable session-stats file
            logging.getLogger(__name__).debug("skipping unreadable session-stats file: %s", file_path, exc_info=True)
            continue
        aggregate["session_count"] += 1
        aggregate["total_tool_calls"] += int(data.get("total_tool_calls", 0) or 0)
        aggregate["hook_total_tool_calls"] += int(data.get("hook_total_tool_calls", 0) or 0)
        aggregate["edit_tool_calls"] += int(data.get("edit_tool_calls", 0) or 0)
        aggregate["turns"] += int(data.get("turns", 0) or 0)
        aggregate["llm_turns"] += int(data.get("llm_turns", 0) or 0)
        tool_call_source = str(data.get("tool_call_source") or "").strip()
        if tool_call_source == "transcript":
            aggregate["tool_call_source"] = "transcript"
        model = str(data.get("model") or "").strip()
        if model and not aggregate["model"]:
            aggregate["model"] = model
        last_model = str(data.get("last_model") or model).strip()
        if last_model:
            aggregate["last_model"] = last_model
        tools_used = _as_dict(data.get("tools_used"))
        for name, count in tools_used.items():
            _merge_raw_tool_count(aggregate["tools_used"], name, count)
        hook_tools_used = _as_dict(data.get("hook_tools_used"))
        for name, count in hook_tools_used.items():
            _merge_raw_tool_count(aggregate["hook_tools_used"], name, count)
        for key in aggregate["usage"]:
            aggregate["usage"][key] += int((data.get("usage") or {}).get(key, 0) or 0)
        pre_compact_raw = data.get("pre_compact_usage")
        if isinstance(pre_compact_raw, dict):
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                aggregate["pre_compact_usage"][key] += int(pre_compact_raw.get(key, 0) or 0)
            aggregate["pre_compact_usage"]["est_cost_usd"] += float(pre_compact_raw.get("est_cost_usd", 0.0) or 0.0)
        for key in (
            "compactions",
            "compaction_duration_ms",
            "pending_subagents",
            "subagents_started",
            "subagents_completed",
        ):
            aggregate[key] += int(data.get(key, 0) or 0)
        spawn_telemetry_raw = data.get("spawn_telemetry")
        spawn_telemetry = spawn_telemetry_raw if isinstance(spawn_telemetry_raw, dict) else {}
        aggregate["spawn_telemetry"]["eligible_for_reuse"] += int(spawn_telemetry.get("eligible_for_reuse", 0) or 0)
        aggregate["spawn_telemetry"]["reuse_observed"] += int(spawn_telemetry.get("reuse_observed", 0) or 0)
        aggregate["spawn_telemetry"]["spawn_latency_ms"] += int(spawn_telemetry.get("spawn_latency_ms", 0) or 0)
        cache_capability_counts: dict[str, Any] = {}
        raw_cache_capability_counts = spawn_telemetry.get("cache_capability_counts")
        if isinstance(raw_cache_capability_counts, dict):
            cache_capability_counts = raw_cache_capability_counts
        for key, value in cache_capability_counts.items():
            aggregate["spawn_telemetry"]["cache_capability_counts"][str(key)] = int(
                aggregate["spawn_telemetry"]["cache_capability_counts"].get(str(key), 0) or 0
            ) + int(value or 0)
        host_dropped_fields: dict[str, Any] = {}
        raw_host_dropped_fields = spawn_telemetry.get("host_dropped_fields")
        if isinstance(raw_host_dropped_fields, dict):
            host_dropped_fields = raw_host_dropped_fields
        for key, value in host_dropped_fields.items():
            aggregate["spawn_telemetry"]["host_dropped_fields"][str(key)] = int(
                aggregate["spawn_telemetry"]["host_dropped_fields"].get(str(key), 0) or 0
            ) + int(value or 0)
    return aggregate


def _cost_history_summary(root: str | Path) -> dict[str, Any]:
    path = Path(root) / "cost_history.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"operations": {}}
    except Exception:
        logging.exception("Recovered from broad exception handler")
        data = {"operations": {}}
    operations = data.get("operations") if isinstance(data, dict) else {}
    if not isinstance(operations, dict):
        operations = {}
    total_baseline = 0.0
    total_current = 0.0
    total_calls = 0
    for entry in operations.values():
        if not isinstance(entry, dict):
            continue
        calls = entry.get("calls") or []
        if not calls:
            continue
        baseline = float(calls[0].get("cost_usd", 0.0) or 0.0)
        current = float(calls[-1].get("cost_usd", 0.0) or 0.0)
        total_baseline += baseline * len(calls)
        total_current += current * len(calls)
        total_calls += len(calls)
    saved = max(0.0, total_baseline - total_current)
    pct = round(100.0 * saved / total_baseline, 2) if total_baseline > 0 else 0.0
    return {
        "operations_tracked": len(operations),
        "total_calls": total_calls,
        "would_have_cost_usd": round(total_baseline, 6),
        "actually_cost_usd": round(total_current, 6),
        "saved_usd": round(saved, 6),
        "live_saved_usd": 0.0,
        "routing_saved_usd": 0.0,
        "saved_pct": pct,
    }


def live_savings_events_path(root: str | Path) -> Path:
    """Routing/compaction analytics log. Not used for display savings."""
    return Path(root) / "live_savings_events.jsonl"


def load_live_savings_summary(
    root: str | Path,
    *,
    session_id: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Aggregate routing/compaction events from the analytics log.

    NOTE: This no longer drives statusline / stop-hook savings display — those
    come from the Claude transcript JSONL (tool_result.content[].saved).
    Kept for cross_vendor_routing.advisor and audit_export consumers.
    """
    path = live_savings_events_path(root)
    if not path.is_file():
        return {"calls_saved": 0, "tokens_saved": 0, "saved_usd": 0.0, "routing_saved_usd": 0.0}

    cutoff = None
    if days is not None:
        cutoff = datetime.now(UTC).timestamp() - (days * 86400)

    calls_saved = 0
    tokens_saved = 0
    saved_usd = 0.0
    routing_saved_usd = 0.0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        # Use 'ts' (unix) or 'at' (iso string) for filtering
        ts = event.get("ts")
        if ts is None and "at" in event:
            try:
                ts = datetime.fromisoformat(str(event["at"])).timestamp()
            except (ValueError, TypeError):
                pass

        if cutoff is not None and ts is not None and ts < cutoff:
            continue

        if session_id and str(event.get("session_id") or "") != session_id:
            continue
        calls_saved += max(0, int(event.get("calls_saved", 0) or 0))
        tokens_saved += max(0, int(event.get("tokens_saved", 0) or 0))
        cost_saved_usd = max(0.0, float(event.get("cost_saved_usd", 0.0) or 0.0))
        saved_usd += cost_saved_usd
        lever = str(event.get("lever") or event.get("kind") or "").strip().lower()
        if lever in {"model_routing", "model_recommendation"}:
            routing_saved_usd += cost_saved_usd
    return {
        "calls_saved": calls_saved,
        "tokens_saved": tokens_saved,
        "saved_usd": round(saved_usd, 6),
        "routing_saved_usd": round(routing_saved_usd, 6),
    }


def build_savings_report(
    root: str | Path,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Compose the savings/cost report.

    - With ``session_id``: per-session live display, sourced from the Claude
      transcript JSONL (tool_result.content[].saved entries).
    - Without ``session_id``: all-session analytics aggregate from the
      routing/compaction event log.
    """
    root_path = Path(root)
    session = aggregate_session_stats(root_path, session_id=session_id)
    from atelier.core.capabilities.savings_summary import aggregate_window_savings

    if session_id:
        from atelier.core.capabilities.savings_summary import compute_savings_summary

        summary = compute_savings_summary(session_id, atelier_root=root_path)
        tokens_saved = int(summary.ctx_saved)
        calls_avoided = int(summary.smart_calls)
        saved_usd = float(summary.saved_usd)
        # SavingsSummary.routing_saved_usd is never populated by
        # compute_savings_summary, so source the per-session routing savings
        # from the live analytics log scoped to this session (same helper the
        # all-sessions branch uses, with the session_id filter applied).
        routing_saved_usd = float(
            load_live_savings_summary(root_path, session_id=session_id).get("routing_saved_usd", 0.0) or 0.0
        )
        carry_usd = float(summary.carry_usd)
        carry_tokens = int(summary.carry_tokens)
        live = {
            "calls_saved": calls_avoided,
            "tokens_saved": tokens_saved,
            "saved_usd": round(saved_usd, 6),
            "routing_saved_usd": round(routing_saved_usd, 6),
            "carry_usd": round(carry_usd, 6),
            "carry_tokens": carry_tokens,
        }
    else:
        # All-sessions view: realized savings come from the per-session ledger
        # (sessions/*/savings.jsonl) so the CLI agrees with the statusline and
        # web Savings page. Routing credit stays sourced from the analytics log.
        analytics = load_live_savings_summary(root_path)
        lifetime_w = aggregate_window_savings(root_path, days=36500)
        tokens_saved = lifetime_w.tokens_saved
        calls_avoided = lifetime_w.calls_saved
        saved_usd = lifetime_w.saved_usd
        routing_saved_usd = float(analytics.get("routing_saved_usd", 0.0) or 0.0)
        carry_usd = 0.0
        carry_tokens = 0
        live = {
            "calls_saved": calls_avoided,
            "tokens_saved": tokens_saved,
            "saved_usd": round(saved_usd, 6),
            "routing_saved_usd": round(routing_saved_usd, 6),
            "carry_usd": round(carry_usd, 6),
            "carry_tokens": carry_tokens,
        }

    if session_id:
        cost = {
            "saved_usd": round(saved_usd, 6),
            "live_saved_usd": round(saved_usd, 6),
            "routing_saved_usd": round(routing_saved_usd, 6),
            "carry_usd": round(carry_usd, 6),
            "carry_tokens": carry_tokens,
            "total_calls": int(session.get("total_tool_calls", 0) or 0),
        }
    else:
        cost = {
            "saved_usd": round(saved_usd, 6),
            "live_saved_usd": round(saved_usd, 6),
            "routing_saved_usd": round(routing_saved_usd, 6),
            "carry_usd": round(carry_usd, 6),
            "carry_tokens": carry_tokens,
            "total_calls": int(session.get("total_tool_calls", 0) or 0),
        }

    baseline = _read_json(baseline_estimate_path(root_path), {})
    if not isinstance(baseline, dict):
        baseline = {}
    vanilla_sessions = int(baseline.get("vanillaSessions") or baseline.get("vanilla_sessions") or 0)
    vanilla_cost = float(baseline.get("totalVanillaCostInUsd") or baseline.get("total_vanilla_cost_usd") or 0.0)
    baseline_gate = baseline_is_available(vanilla_sessions, vanilla_cost)
    lifetime = _read_json(lifetime_savings_path(root_path), {})
    if not isinstance(lifetime, dict):
        lifetime = {}
    lifetime.setdefault("calls_saved", calls_avoided)
    lifetime.setdefault("tokens_saved", tokens_saved)
    lifetime.setdefault("saved_usd", saved_usd)
    auth = auth_status(root_path)
    subscription = _read_json(subscription_state_path(root_path), auth.get("subscription") or {})
    if not isinstance(subscription, dict):
        subscription = {}
    subscription = compute_usage_meter(root_path, subscription=subscription)
    ab_calibration = _summarize_ab_calibration(root_path)

    # --- Summary breakdown (1D, 7D, 30D) ---
    # Realized savings from the per-session ledger (sessions/*/savings.jsonl) —
    # the same source as the statusline and stop hook.
    def _window(d: int) -> dict[str, Any]:
        w = aggregate_window_savings(root_path, days=d)
        return {
            "calls": w.calls_saved,
            "usd": round(w.saved_usd, 2),
            "tokens": w.tokens_saved,
            "spend": round(w.spend_usd, 2),
            "carry": round(w.carry_usd, 2),
            "routing": round(w.routing_usd, 2),
        }

    summary_breakdown = {
        "1D": _window(1),
        "7D": _window(7),
        "30D": _window(30),
    }

    return {
        "calls_avoided": calls_avoided,
        "tokens_saved": tokens_saved,
        "saved_usd": saved_usd,
        "summary_breakdown": summary_breakdown,
        "live": live,
        "session": session,
        "lifetime": lifetime,
        "baseline": {
            "available": baseline_gate.get("available", False),
            "estimate": baseline,
            **baseline_gate,
        },
        "subscription": subscription,
        "ab_calibration": ab_calibration,
        "cost": cost,
        "local_note": "Savings reflect tokens Atelier actually kept out of LLM input, priced per-turn at the model in use.",
    }
