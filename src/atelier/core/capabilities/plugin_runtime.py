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
from datetime import datetime
from pathlib import Path
from typing import Any, cast

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

# Used by baseline_time_saved() to estimate wall-clock time saved per call.
# Tokens/cost are NEVER synthesized from constants — those come from real
# tool measurements and per-model pricing at emit time.
BASELINE_TIME_SAVED_PER_CALL_MS = 7_000
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
SPINNER_VERBS = ["reasoning", "searching", "editing", "validating", "recalling"]
AUTH_REFRESH_GRACE_SECONDS = 300
UPDATE_CHECK_THROTTLE_SECONDS = 30 * 60


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning(
            "Suppressed exception at plugin_runtime.py:58",
            exc_info=True,
        )
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

    Measured-by-A/B view of per-tool savings. Produced by
    ``tests/benchmarks/test_*_ab_real.py`` (run via ``make bench-ab``).
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
    except Exception:
        logger.warning(
            "Suppressed exception at plugin_runtime.py:170",
            exc_info=True,
        )
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
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


def claim_anonymous_trial(root: str | Path, *, monthly_limit_usd: float = 5.0) -> dict[str, Any]:
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
        "message": "Local anonymous trial active.",
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
    base = (app_url or os.environ.get("ATELIER_APP_URL") or "https://atelier.local").rstrip("/")
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
    base = (app_url or os.environ.get("ATELIER_APP_URL") or "https://atelier.local").rstrip("/")
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


def sql_auto_limit(sql: str, max_rows: int, auto_limit: bool = True) -> dict[str, Any]:
    if not auto_limit:
        return {"sql": sql, "changed": False}
    stripped = sql.strip().rstrip(";")
    lowered = stripped.lower()
    if not lowered.startswith("select"):
        return {"sql": sql, "changed": False, "reason": "only select statements are auto-limited"}
    if re.search(r"\blimit\b", lowered):
        return {"sql": sql, "changed": False}
    if re.search(r"\b(union|intersect|except)\b", lowered):
        return {"sql": sql, "changed": False, "reason": "set operations are not auto-limited"}
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
    updated = dict(host_settings or {})
    namespace = dict(updated.get("atelier") or {})
    if enabled:
        namespace["spinnerVerbs"] = list(SPINNER_VERBS)
    else:
        namespace.pop("spinnerVerbs", None)
    if namespace:
        updated["atelier"] = namespace
    else:
        updated.pop("atelier", None)
    return updated


def apply_attribution_setting(host_settings: dict[str, Any], enabled: bool) -> dict[str, Any]:
    updated = dict(host_settings or {})
    namespace = dict(updated.get("atelier") or {})
    if enabled:
        namespace["attribution"] = {"enabled": True, "source": "Atelier"}
    else:
        namespace.pop("attribution", None)
    if namespace:
        updated["atelier"] = namespace
    else:
        updated.pop("atelier", None)
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
_SQL_COMMANDS = {"psql", "pg_dump", "pg_restore", "mysql", "sqlite3"}


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
        "message": "Atelier policy: for coding tasks call mcp__atelier__context first and use Atelier MCP tools before native Codex tools.",
        "additionalContext": "\n".join(
            [
                "Codex Atelier tool policy:",
                "- Call mcp__atelier__context before exploratory reads or edits on coding tasks.",
                "- Use mcp__atelier__node / callers / callees / impact / explore for code intelligence before native search loops.",
                "- Use mcp__atelier__grep or mcp__atelier__search instead of native Grep/Glob or shell rg/grep/find.",
                "- Use mcp__atelier__read instead of native Read or shell cat/sed/head for file reads.",
                "- Use mcp__atelier__edit instead of native Edit/Write/MultiEdit for file changes.",
                "- Use mcp__atelier__shell only for commands without a better Atelier equivalent (git, make, uv, npm, pytest).",
                "- Treat native Codex Read/Edit/Write/MultiEdit/Bash/Grep/Glob as disabled-by-policy unless the Atelier equivalent is hidden, unavailable, or returned noop.",
            ]
        ),
    }


def codex_update_notification(root: str | Path, *, current_version: str) -> dict[str, Any]:
    result = update_notification(current_version, _read_json(update_flag_path(root), None))
    if result.get("delete_flag"):
        update_flag_path(root).unlink(missing_ok=True)
    stdout = _merge_session_start_stdout(
        result.get("stdout"),
        _session_optimizer_start_notice(root, host="codex"),
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
        "shell",
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
        return ("mcp__atelier__edit", "Use Atelier edit for deterministic grouped writes and rollback.")
    if lowered in {"grep", "glob"}:
        return ("mcp__atelier__grep", "Use Atelier grep/search for text and path discovery.")
    if lowered in {"bash", "shell", "exec_command", "run_command"}:
        if (
            normalized.startswith(("rg ", "grep ", "find "))
            or " rg " in f" {normalized} "
            or " grep " in f" {normalized} "
        ):
            return ("mcp__atelier__grep", "Use Atelier grep/search instead of shell rg/grep/find loops.")
        if normalized.startswith(("cat ", "sed ", "head ", "tail ")):
            return ("mcp__atelier__read", "Use Atelier read instead of shell file-print commands.")
        return ("mcp__atelier__shell", "Use Atelier shell so command execution stays compact and supervised.")
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


def build_codex_post_tool_use_savings_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("hook_event_name") != "PostToolUse":
        return {"no_output": True}
    tool_name = str(payload.get("tool_name") or "")
    if not _is_atelier_tool(tool_name):
        return _codex_native_tool_nudge(root, payload)
    stats = update_session_stats(root, payload)
    session_id = str(payload.get("session_id") or "default")
    from atelier.core.capabilities.savings_summary import compute_savings_summary

    summary = compute_savings_summary(session_id, atelier_root=root)
    calls = int(summary.smart_calls)
    tokens = int(summary.ctx_saved)
    saved_usd = float(summary.saved_usd)
    output: dict[str, Any] = {
        "systemMessage": (f"Atelier saved ${saved_usd:.4f} · {tokens:,} tokens · {calls} calls in this session."),
        "stats": stats,
    }
    progress = build_session_progress_optimization_output(root, payload)
    if not progress.get("no_output"):
        message = progress.get("message")
        if isinstance(message, str) and message.strip():
            output["message"] = message
        context = progress.get("additionalContext")
        if isinstance(context, str) and context.strip():
            output["additionalContext"] = context
    return output


def build_codex_stop_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if event != "Stop":
        return {"no_output": True}

    session_id = str(payload.get("session_id") or "default")
    update_session_stats(root, payload)
    report = build_savings_report(root, session_id=session_id)
    session = report.get("session") or {}
    cost = report.get("cost") or {}

    total_tool_calls = int(session.get("total_tool_calls", 0) or 0)
    calls_avoided = int(report.get("calls_avoided", 0) or 0)
    tokens_saved = int(report.get("tokens_saved", 0) or 0)
    saved_usd = float(cost.get("saved_usd", 0.0) or 0.0)
    routing_saved_usd = float(cost.get("routing_saved_usd", 0.0) or 0.0)
    compactions = int(session.get("compactions", 0) or 0)

    if total_tool_calls <= 0 and calls_avoided <= 0 and tokens_saved <= 0 and saved_usd <= 0:
        return {"no_output": True}

    parts = [f"${saved_usd:.4f}"]
    if tokens_saved > 0:
        parts.append(f"{tokens_saved:,} tokens saved")
    if calls_avoided > 0:
        parts.append(f"{calls_avoided} calls avoided")
    lines = [
        "Atelier session complete.",
        "savings: " + " · ".join(parts),
        f"Atelier tool calls: {total_tool_calls}",
    ]
    if compactions > 0:
        lines.append(f"compactions: {compactions}")
    if routing_saved_usd > 0:
        lines.append(f"routing savings: ${routing_saved_usd:.4f}")
    return {"systemMessage": "\n".join(lines), "report": report}


def _tool_uses(turns: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    return [(idx, tool) for idx, turn in enumerate(turns) for tool in (turn.get("tool_uses") or [])]


def detect_read_batch(turns: list[dict[str, Any]]) -> dict[str, Any]:
    for _, turn in enumerate(turns):
        reads = [tool for tool in turn.get("tool_uses", []) if tool.get("name") == "Read"]
        if len(reads) >= 2:
            return {
                "workflows": 1,
                "calls_saved": len(reads) - 1,
                "consumed_tool_use_ids": [r.get("id") for r in reads],
            }
    return {"workflows": 0, "calls_saved": 0, "consumed_tool_use_ids": []}


def detect_edit_batch(turns: list[dict[str, Any]]) -> dict[str, Any]:
    for turn in turns:
        edits = [tool for tool in turn.get("tool_uses", []) if tool.get("name") in {"Edit", "Write", "MultiEdit"}]
        if len(edits) >= 2:
            return {"workflows": 1, "calls_saved": len(edits) - 1}
    return {"workflows": 0, "calls_saved": 0}


def detect_grep_read(turns: list[dict[str, Any]], max_gap_turns: int = 3) -> dict[str, Any]:
    for idx, turn in enumerate(turns):
        greps = [tool for tool in turn.get("tool_uses", []) if tool.get("name") in {"Grep", "Glob"}]
        if not greps:
            continue
        reads: list[dict[str, Any]] = []
        for later in turns[idx + 1 : idx + max_gap_turns + 1]:
            reads.extend([tool for tool in later.get("tool_uses", []) if tool.get("name") == "Read"])
        if reads:
            return {"workflows": 1, "calls_saved": len(greps) + len(reads) - 1}
    return {"workflows": 0, "calls_saved": 0}


def detect_failed_edit(turns: list[dict[str, Any]], max_gap_turns: int = 5) -> dict[str, Any]:
    for idx, turn in enumerate(turns):
        failed = [tool for tool in turn.get("tool_uses", []) if tool.get("name") == "Edit" and tool.get("is_error")]
        if not failed:
            continue
        chain = list(failed)
        for later in turns[idx + 1 : idx + max_gap_turns + 1]:
            chain.extend([tool for tool in later.get("tool_uses", []) if tool.get("name") in {"Read", "Edit"}])
        if len(chain) >= 2:
            return {"workflows": 1, "calls_saved": len(chain) - 1}
    return {"workflows": 0, "calls_saved": 0}


def detect_bash_sql(turns: list[dict[str, Any]]) -> dict[str, Any]:
    matches = []
    for _, tool in _tool_uses(turns):
        command = str((tool.get("input") or {}).get("command", ""))
        if tool.get("name") == "Bash" and any(sql_cmd in command for sql_cmd in _SQL_COMMANDS):
            matches.append(tool)
    if len(matches) >= 2:
        return {"workflows": 1, "calls_saved": len(matches) - 1}
    return {"workflows": 0, "calls_saved": 0}


def baseline_is_available(vanillaSessions: int, totalVanillaCostInUsd: float) -> dict[str, Any]:
    available = vanillaSessions >= 5 and totalVanillaCostInUsd > 0
    if not available:
        return {"available": False, "reason": "requires at least 5 vanilla sessions"}
    return {"available": True}


def baseline_time_saved(calls_saved: int) -> dict[str, Any]:
    return {
        "time_saved_ms": calls_saved * BASELINE_TIME_SAVED_PER_CALL_MS,
        "per_call_ms": BASELINE_TIME_SAVED_PER_CALL_MS,
    }


def efficiency_gain(actual_tool_calls: int, equivalent_baseline_calls: int) -> dict[str, Any]:
    if equivalent_baseline_calls <= 0:
        return {"efficiency_gain_percent": 0}
    gain = round(100 * (equivalent_baseline_calls - actual_tool_calls) / equivalent_baseline_calls)
    return {"efficiency_gain_percent": gain}


def session_stats_path(root: str | Path, session_id: str) -> Path:
    return Path(root) / "session_stats" / f"{session_id}.json"


def _session_event_path(root: str | Path, session_id: str) -> Path:
    return Path(root) / "session_events" / f"{session_id}.jsonl"


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
                logger.warning(
                    "Suppressed exception at plugin_runtime.py:1028",
                    exc_info=True,
                )
    return int(datetime.now().timestamp() * 1000)


def _usage_numbers(raw: dict[str, Any]) -> dict[str, int]:
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "cache_read_tokens": ("cache_read_input_tokens", "cache_read_tokens"),
        "cache_write_tokens": ("cache_creation_input_tokens", "cache_write_tokens"),
    }
    result: dict[str, int] = {key: 0 for key in aliases}
    for target, names in aliases.items():
        for name in names:
            value = raw.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[target] += int(value)
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
    }
    message_usage = (payload.get("message") or {}).get("usage") if isinstance(payload.get("message"), dict) else None
    for candidate in (payload.get("usage"), payload.get("token_usage"), message_usage):
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


def update_session_stats(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "default")
    path = session_stats_path(root, session_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        state = {}
    state.setdefault("session_id", session_id)
    state.setdefault("started_at_ms", _now_ms(payload))
    state.setdefault("total_tool_calls", 0)
    state.setdefault("edit_tool_calls", 0)
    state.setdefault("event_counts", {})
    state.setdefault(
        "usage",
        {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
    )
    state["last_event_at_ms"] = _now_ms(payload)
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if event:
        state["event_counts"][event] = int(state["event_counts"].get(event, 0) or 0) + 1
    _merge_usage(state, _extract_usage(payload))
    # context_window.current_usage is a cumulative snapshot of the entire session so far.
    # Overwrite state["usage"] with it each time — never accumulate it additively.
    context_cw = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else None
    context_cw_usage = context_cw.get("current_usage") if context_cw else None
    if isinstance(context_cw_usage, dict):
        snapshot = _usage_numbers(context_cw_usage)
        if any(v > 0 for v in snapshot.values()):
            state["usage"].update(snapshot)
    if event == "PostToolUse":
        tool_name = str(payload.get("tool_name") or "")
        state["total_tool_calls"] = int(state.get("total_tool_calls", 0)) + 1
        from atelier.core.capabilities.session_optimizer import tool_is_edit

        if tool_is_edit(tool_name):
            state["edit_tool_calls"] = int(state.get("edit_tool_calls", 0) or 0) + 1
            state.setdefault("first_edit_at_ms", _now_ms(payload))
        if tool_name == "Agent":
            state["subagents_started"] = int(state.get("subagents_started", 0) or 0) + 1
            state["pending_subagents"] = max(0, int(state.get("pending_subagents", 0) or 0) + 1)
    elif event == "PreCompact":
        state["compaction_started_at_ms"] = _now_ms(payload)
    elif event == "PostCompact":
        state["compactions"] = int(state.get("compactions", 0)) + 1
        started_at = int(state.pop("compaction_started_at_ms", _now_ms(payload)) or _now_ms(payload))
        state["compaction_duration_ms"] = int(state.get("compaction_duration_ms", 0) or 0) + max(
            0, _now_ms(payload) - started_at
        )
    elif event == "SubagentStop":
        state["subagents_completed"] = int(state.get("subagents_completed", 0) or 0) + 1
        state["pending_subagents"] = max(0, int(state.get("pending_subagents", 0) or 0) - 1)
        state["completed"] = True
    elif event in {"Stop", "SubagentStop"}:
        state["completed"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _append_session_event(root, session_id, payload)
    return state


def _merge_progress_outputs(*items: dict[str, Any]) -> dict[str, Any]:
    contexts: list[str] = []
    messages: list[str] = []
    for item in items:
        if not item or item.get("no_output"):
            continue
        context = item.get("additionalContext")
        if isinstance(context, str) and context.strip():
            contexts.append(context.strip())
        message = item.get("message")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    if not contexts and not messages:
        return {"no_output": True}
    output: dict[str, Any] = {}
    if contexts:
        output["additionalContext"] = "\n\n".join(contexts)
    if messages:
        output["message"] = " | ".join(messages)
    return output


def _session_quality_band(score: int) -> str:
    if score >= 85:
        return "healthy"
    if score >= 70:
        return "stable"
    if score >= 55:
        return "degrading"
    return "at-risk"


def _session_quality_snapshot(stats: dict[str, Any]) -> dict[str, Any]:
    raw_usage = stats.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_read_tokens = int(usage.get("cache_read_tokens", 0) or 0)
    total_tool_calls = int(stats.get("total_tool_calls", 0) or 0)
    edit_tool_calls = int(stats.get("edit_tool_calls", 0) or 0)
    compactions = int(stats.get("compactions", 0) or 0)

    score = 100
    reasons: list[str] = []

    if input_tokens >= 300_000:
        score -= 25
        reasons.append("input spend is already above 300k tokens")
    elif input_tokens >= 180_000:
        score -= 15
        reasons.append("input spend is already above 180k tokens")
    elif input_tokens >= 90_000:
        score -= 8
        reasons.append("input spend is climbing without a finished slice yet")

    if total_tool_calls >= 8 and edit_tool_calls == 0:
        score -= 22
        reasons.append("8 or more tool calls landed without a single edit")
    elif total_tool_calls >= 5 and edit_tool_calls == 0:
        score -= 14
        reasons.append("5 or more tool calls landed without a single edit")

    if total_tool_calls >= 6 and edit_tool_calls / max(total_tool_calls, 1) < 0.15:
        score -= 8
        reasons.append("delivery is lagging behind exploration in this session")

    if compactions >= 2:
        score -= 14
        reasons.append("the session already needed multiple compactions")
    elif compactions >= 1:
        score -= 8
        reasons.append("the session already needed a compaction")

    if input_tokens >= 120_000:
        cache_ratio = cache_read_tokens / max(input_tokens, 1)
        if cache_ratio == 0:
            score -= 8
            reasons.append("no cache reuse was observed on a large-input slice")
        elif cache_ratio < 0.05:
            score -= 5
            reasons.append("cache reuse is low for the current input volume")
        elif cache_ratio >= 0.15:
            score += 4

    if edit_tool_calls > 0 and total_tool_calls > 0:
        score += 4

    score = max(0, min(100, score))
    return {
        "score": score,
        "band": _session_quality_band(score),
        "reasons": reasons,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "tool_calls": total_tool_calls,
        "edit_tool_calls": edit_tool_calls,
        "compactions": compactions,
    }


def _loop_notice_text(report: dict[str, Any]) -> str:
    rescue_scores = report.get("rescue_scores") if isinstance(report.get("rescue_scores"), dict) else {}
    if rescue_scores:
        ordered = [name for name, _score in sorted(rescue_scores.items(), key=lambda item: item[1], reverse=True)]
    else:
        ordered = [str(item) for item in (report.get("rescue_strategies") or [])]
    cleaned = [item.replace("_", " ") for item in ordered if item][:2]
    if not cleaned:
        return "narrow the plan, validate the current slice, or call rescue"
    return "; ".join(cleaned)


def _normalized_loop_event(raw_event: Any) -> dict[str, Any]:
    from atelier.core.capabilities.session_optimizer import tool_is_edit

    if isinstance(raw_event, dict):
        kind = str(raw_event.get("kind") or "")
        summary = str(raw_event.get("summary") or "")
        raw_payload = raw_event.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    else:
        kind = str(getattr(raw_event, "kind", "") or "")
        summary = str(getattr(raw_event, "summary", "") or "")
        raw_payload = getattr(raw_event, "payload", {})
        payload = raw_payload if isinstance(raw_payload, dict) else {}

    if kind != "tool_call":
        return {"kind": kind, "summary": summary, "payload": dict(payload)}

    tool_name = str(payload.get("tool") or "")
    raw_args = payload.get("args")
    args = cast(dict[str, Any], raw_args) if isinstance(raw_args, dict) else {}
    normalized_payload = dict(args)
    for key in ("path", "file_path", "file"):
        value = args.get(key)
        if value and "path" not in normalized_payload:
            normalized_payload["path"] = value
            break
    for key in ("query", "content_regex", "pattern", "sql", "command"):
        value = args.get(key)
        if value and "query" not in normalized_payload:
            normalized_payload["query"] = value
            break
    if "key" not in normalized_payload and tool_name:
        normalized_payload["key"] = tool_name

    lowered = tool_name.lower().strip()
    if tool_is_edit(lowered):
        normalized_kind = "file_edit"
    elif lowered.endswith("search") or lowered in {
        "search",
        "grep",
        "glob",
        "file_search",
        "symbol_search",
    }:
        normalized_kind = "search"
    elif lowered in {"read", "smart_read", "read_file"} or lowered.endswith("read"):
        normalized_kind = "read_file"
    else:
        normalized_kind = lowered or kind

    return {
        "kind": normalized_kind,
        "summary": summary or tool_name,
        "payload": normalized_payload,
    }


def _active_run_loop_report(root: str | Path) -> dict[str, Any] | None:
    state_path = Path(root) / "session_state.json"
    state = _read_json(state_path, {})
    if not isinstance(state, dict):
        return None
    session_id = str(state.get("session_id") or state.get("active_session_id") or "").strip()
    if not session_id:
        return None
    atelier_root = Path(str(state.get("atelier_root") or root))
    ledger_path = atelier_root / "runs" / f"{session_id}.json"
    if not ledger_path.exists():
        return None

    from atelier.core.capabilities.loop_detection.capability import LoopDetectionCapability
    from atelier.infra.runtime.run_ledger import RunLedger

    ledger = RunLedger.load(ledger_path)
    normalized = RunLedger(session_id=ledger.session_id, agent=ledger.agent, task=ledger.task, domain=ledger.domain)
    normalized.events = cast(Any, [_normalized_loop_event(event) for event in ledger.events])
    if not normalized.events:
        return None
    report = LoopDetectionCapability().check(normalized)
    return report.to_dict()


def _maybe_emit_quality_notice(stats: dict[str, Any], *, now_ms: int) -> tuple[dict[str, Any], dict[str, Any]]:
    from atelier.core.capabilities.session_optimizer import mark_session_optimizer_notice

    snapshot = _session_quality_snapshot(stats)
    raw_previous = stats.get("optimizer_quality")
    previous: dict[str, Any] = raw_previous if isinstance(raw_previous, dict) else {}
    previous_score = int(previous.get("score", snapshot["score"]) or snapshot["score"])

    updated = dict(stats)
    updated["optimizer_quality"] = {
        **snapshot,
        "previous_score": previous_score,
        "updated_at_ms": now_ms,
    }

    already_sent = bool((updated.get("optimizer_notices") or {}).get("quality_drop"))
    score_drop = previous_score - int(snapshot["score"])
    should_emit = (
        int(snapshot["tool_calls"]) >= 4
        and not already_sent
        and (int(snapshot["score"]) <= 70 or (score_drop >= 10 and int(snapshot["score"]) <= 80))
    )
    if not should_emit:
        return updated, {"no_output": True}

    updated = mark_session_optimizer_notice(updated, "quality_drop")
    reasons = "; ".join(snapshot["reasons"][:2]) or "token spend is growing faster than delivery"
    return updated, {
        "message": "Atelier quality guard: narrow the slice before more token spend",
        "additionalContext": (
            f"Atelier quality guard: session quality is {snapshot['score']}/100 ({snapshot['band']}). "
            f"Signals: {reasons}. Next move: narrow the slice, make an edit or run a validation step, "
            "and compact before broadening context again."
        ),
    }


def _maybe_emit_loop_notice(
    root: str | Path, stats: dict[str, Any], *, now_ms: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    from atelier.core.capabilities.session_optimizer import mark_session_optimizer_notice

    updated = dict(stats)
    report = _active_run_loop_report(root)
    updated["optimizer_loop"] = {
        "updated_at_ms": now_ms,
        "report": report,
    }
    already_sent = bool((updated.get("optimizer_notices") or {}).get("loop_detected"))
    if not report or already_sent:
        return updated, {"no_output": True}
    if not report.get("loop_detected") or report.get("severity") not in {"medium", "high"}:
        return updated, {"no_output": True}

    updated = mark_session_optimizer_notice(updated, "loop_detected")
    loop_types = [str(item).replace("_", " ") for item in (report.get("loop_types") or [])[:2]]
    loop_text = ", ".join(loop_types) if loop_types else "repeated work loop"
    risk_score = float(report.get("risk_score") or 0.0)
    wasted_tokens = int(report.get("wasted_tokens") or 0)
    return updated, {
        "message": "Atelier loop detector: change approach before another retry",
        "additionalContext": (
            f"Atelier loop detector: {report['severity']} loop risk detected ({loop_text}), "
            f"risk {risk_score:.2f}, with about {wasted_tokens} tokens likely wasted already. "
            f"Next move: {_loop_notice_text(report)}."
        ),
    }


def build_session_progress_optimization_output(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Return one-shot hook nudges for no-edit drift, quality drop, and live loop risk."""
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if event not in {"PostToolUse", "PostToolUseFailure"}:
        return {"no_output": True}
    session_id = str(payload.get("session_id") or "default")
    path = session_stats_path(root, session_id)
    try:
        stats = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {"no_output": True}
    from atelier.core.capabilities.session_optimizer import (
        mark_session_optimizer_notice,
        session_stats_need_no_edit_notice,
    )

    now_ms = _now_ms(payload)
    updated = dict(stats)
    outputs: list[dict[str, Any]] = []

    if session_stats_need_no_edit_notice(updated, now_ms=now_ms):
        updated = mark_session_optimizer_notice(updated, "no_edit_10m")
        outputs.append(
            {
                "additionalContext": (
                    "Atelier budget optimizer: more than 10 minutes have passed without an edit in this session. "
                    "Name the deliverable and expected output now, or pause for user review before continuing broad exploration."
                ),
                "message": "Atelier budget optimizer: check delivery before more exploration",
            }
        )

    updated, quality_output = _maybe_emit_quality_notice(updated, now_ms=now_ms)
    outputs.append(quality_output)

    updated, loop_output = _maybe_emit_loop_notice(root, updated, now_ms=now_ms)
    outputs.append(loop_output)

    if updated != stats:
        path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    return _merge_progress_outputs(*outputs)


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
    stats_dir = Path(root) / "session_stats"
    if not stats_dir.exists():
        return []

    # Get the newest sessions first
    files = sorted(stats_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    results: list[dict[str, Any]] = []
    for file_path in files[:limit]:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                results.append(data)
        except Exception:
            continue
    return results


def aggregate_session_stats(root: str | Path, session_id: str | None = None) -> dict[str, Any]:
    stats_dir = Path(root) / "session_stats"
    files = (
        [session_stats_path(root, session_id)]
        if session_id
        else sorted(stats_dir.glob("*.json")) if stats_dir.exists() else []
    )
    aggregate: dict[str, Any] = {
        "session_count": 0,
        "total_tool_calls": 0,
        "edit_tool_calls": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        "compactions": 0,
        "compaction_duration_ms": 0,
        "pending_subagents": 0,
        "subagents_started": 0,
        "subagents_completed": 0,
    }
    for file_path in files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        aggregate["session_count"] += 1
        aggregate["total_tool_calls"] += int(data.get("total_tool_calls", 0) or 0)
        aggregate["edit_tool_calls"] += int(data.get("edit_tool_calls", 0) or 0)
        for key in aggregate["usage"]:
            aggregate["usage"][key] += int((data.get("usage") or {}).get(key, 0) or 0)
        for key in (
            "compactions",
            "compaction_duration_ms",
            "pending_subagents",
            "subagents_started",
            "subagents_completed",
        ):
            aggregate[key] += int(data.get(key, 0) or 0)
    return aggregate


def _cost_history_summary(root: str | Path) -> dict[str, Any]:
    path = Path(root) / "cost_history.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"operations": {}}
    except Exception:
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


def load_live_savings_summary(root: str | Path, *, session_id: str | None = None) -> dict[str, Any]:
    """Aggregate routing/compaction events from the analytics log.

    NOTE: This no longer drives statusline / stop-hook savings display — those
    come from the Claude transcript JSONL (tool_result.content[].saved).
    Kept for cross_vendor_routing.advisor and audit_export consumers.
    """
    path = live_savings_events_path(root)
    if not path.is_file():
        return {"calls_saved": 0, "tokens_saved": 0, "saved_usd": 0.0, "routing_saved_usd": 0.0}

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

    if session_id:
        from atelier.core.capabilities.savings_summary import compute_savings_summary

        summary = compute_savings_summary(session_id, atelier_root=root_path)
        tokens_saved = int(summary.ctx_saved)
        calls_avoided = int(summary.smart_calls)
        saved_usd = float(summary.saved_usd)
        routing_saved_usd = float(summary.routing_saved_usd)
        live = {
            "calls_saved": calls_avoided,
            "tokens_saved": tokens_saved,
            "saved_usd": round(saved_usd, 6),
            "routing_saved_usd": round(routing_saved_usd, 6),
        }
    else:
        live = load_live_savings_summary(root_path)
        tokens_saved = int(live.get("tokens_saved", 0) or 0)
        calls_avoided = int(live.get("calls_saved", 0) or 0)
        saved_usd = float(live.get("saved_usd", 0.0) or 0.0)
        routing_saved_usd = float(live.get("routing_saved_usd", 0.0) or 0.0)

    if session_id:
        cost = {
            "saved_usd": round(saved_usd, 6),
            "live_saved_usd": round(saved_usd, 6),
            "routing_saved_usd": round(routing_saved_usd, 6),
            "total_calls": int(session.get("total_tool_calls", 0) or 0),
        }
    else:
        cost = {
            "saved_usd": round(saved_usd, 6),
            "live_saved_usd": round(saved_usd, 6),
            "routing_saved_usd": round(routing_saved_usd, 6),
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
    ab_calibration = _summarize_ab_calibration(root_path)
    return {
        "calls_avoided": calls_avoided,
        "tokens_saved": tokens_saved,
        "saved_usd": saved_usd,
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
