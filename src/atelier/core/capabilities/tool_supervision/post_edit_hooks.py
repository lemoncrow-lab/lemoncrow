"""Post-edit hook pipeline: format -> organize-imports -> lint-autofix -> diagnostics.

Called automatically after every successful tool_smart_edit unless post_edit_hooks=False.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from atelier.core.capabilities.tool_supervision.bash_exec import run_command

# -- language detection -------------------------------------------------------

_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
}


def _detect_language(path: str) -> str:
    return _EXT_TO_LANGUAGE.get(Path(path).suffix.lower(), "unknown")


# -- tool detection -----------------------------------------------------------

def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


# -- command builders ---------------------------------------------------------

def _format_cmds(language: str, paths: list[str]) -> list[list[str]]:
    """Return list of commands to format the given files."""
    if not paths:
        return []
    if language == "python":
        if _has("ruff"):
            return [["ruff", "format", "--quiet", *paths]]
    elif language in ("typescript", "javascript"):
        if _has("prettier"):
            return [["prettier", "--write", "--log-level", "warn", *paths]]
    elif language == "rust":
        if _has("rustfmt"):
            return [["rustfmt", *paths]]
    return []


def _import_cmds(language: str, paths: list[str]) -> list[list[str]]:
    if not paths:
        return []
    if language == "python":
        if _has("ruff"):
            return [["ruff", "check", "--fix", "--select", "I,F401", "--quiet", *paths]]
    elif language in ("typescript", "javascript"):
        if _has("eslint"):
            return [["eslint", "--fix", "--rule", "{import/order: error}", *paths]]
    return []


def _lint_fix_cmds(language: str, paths: list[str], _repo_root: Path) -> list[list[str]]:
    if not paths:
        return []
    if language == "python":
        if _has("ruff"):
            return [["ruff", "check", "--fix", "--quiet", *paths]]
    elif language in ("typescript", "javascript"):
        if _has("eslint"):
            return [["eslint", "--fix", *paths]]
    elif language == "rust":
        # cargo fix requires the workspace root, not individual files
        if _has("cargo"):
            return [["cargo", "fix", "--allow-dirty", "--allow-staged"]]
    return []


def _diagnostic_cmds(language: str, paths: list[str], repo_root: Path) -> list[tuple[str, list[str]]]:
    """Return list of (source_name, command) for diagnostic collection."""
    if not paths:
        return []
    cmds: list[tuple[str, list[str]]] = []
    if language == "python":
        if _has("ruff"):
            cmds.append(("ruff", ["ruff", "check", "--output-format", "json", *paths]))
    elif language in ("typescript", "javascript"):
        tsconfig = repo_root / "tsconfig.json"
        if _has("tsc") and tsconfig.exists():
            cmds.append(("tsc", ["tsc", "--noEmit", "--pretty", "false"]))
    elif language == "rust":
        if _has("cargo"):
            cmds.append(("cargo_clippy", ["cargo", "clippy", "--message-format", "json", "--quiet"]))
    return cmds


# -- diagnostic parsers -------------------------------------------------------

@dataclass
class DiagnosticItem:
    file: str
    line: int | None
    col: int | None
    severity: str  # "error" | "warning" | "info" | "hint"
    message: str
    code: str | None
    source: str


def _parse_ruff_json(stdout: str, source: str = "ruff") -> list[DiagnosticItem]:
    try:
        items = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        result.append(DiagnosticItem(
            file=str(item.get("filename") or item.get("file") or ""),
            line=item.get("location", {}).get("row"),
            col=item.get("location", {}).get("column"),
            severity=str(item.get("message", "")).lower() if "error" in str(item.get("code", "")).lower() else "warning",
            message=str(item.get("message") or ""),
            code=str(item.get("code") or ""),
            source=source,
        ))
    return result


def _parse_tsc_output(stdout: str) -> list[DiagnosticItem]:
    """Parse tsc --pretty false output: file(line,col): error TS1234: message"""
    import re
    pattern = re.compile(r"^(.+?)\((\d+),(\d+)\): (error|warning|info) (TS\d+): (.+)$")
    result = []
    for line in stdout.splitlines():
        m = pattern.match(line.strip())
        if m:
            result.append(DiagnosticItem(
                file=m.group(1),
                line=int(m.group(2)),
                col=int(m.group(3)),
                severity=m.group(4),
                message=m.group(6),
                code=m.group(5),
                source="tsc",
            ))
    return result


def _parse_cargo_clippy_json(stdout: str) -> list[DiagnosticItem]:
    """Parse cargo clippy --message-format json output."""
    result = []
    for line in stdout.splitlines():
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(msg, dict) or msg.get("reason") != "compiler-message":
            continue
        inner = msg.get("message", {})
        if not isinstance(inner, dict):
            continue
        level = str(inner.get("level") or "warning")
        text = str(inner.get("message") or "")
        code_obj = inner.get("code") or {}
        code = str(code_obj.get("code") or "") if isinstance(code_obj, dict) else ""
        spans = inner.get("spans") or []
        primary = next((s for s in spans if isinstance(s, dict) and s.get("is_primary")), spans[0] if spans else None)
        if primary and isinstance(primary, dict):
            result.append(DiagnosticItem(
                file=str(primary.get("file_name") or ""),
                line=primary.get("line_start"),
                col=primary.get("column_start"),
                severity=level,
                message=text,
                code=code or None,
                source="cargo_clippy",
            ))
    return result


# -- hook config and result ---------------------------------------------------

@dataclass
class HookConfig:
    run_format: bool = True
    run_organize_imports: bool = True
    run_lint_autofix: bool = True
    run_diagnostics: bool = True
    timeout_per_step_s: float = 10.0
    total_timeout_s: float = 30.0


@dataclass
class HookResult:
    steps_ran: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    diagnostics: list[DiagnosticItem] = field(default_factory=list)
    total_ms: int = 0


# -- main runner --------------------------------------------------------------

def run_post_edit_hooks(
    touched_paths: list[str],
    *,
    repo_root: Path,
    config: HookConfig | None = None,
) -> HookResult:
    """Run format -> organize-imports -> lint-autofix -> diagnostics on touched_paths.

    Each step is attempted independently; failure of one does not abort the rest.
    Missing tools are silently skipped and recorded in steps_skipped.
    """
    cfg = config or HookConfig()
    result = HookResult()
    budget_remaining = cfg.total_timeout_s
    wall_start = time.perf_counter()

    # Group paths by language
    by_lang: dict[str, list[str]] = {}
    for p in touched_paths:
        lang = _detect_language(p)
        if lang != "unknown":
            by_lang.setdefault(lang, []).append(p)

    if not by_lang:
        return result

    def _run_step(step_name: str, cmds: list[list[str]], *, cwd: str | None = None) -> None:
        nonlocal budget_remaining
        if budget_remaining <= 0:
            result.steps_skipped.append(step_name)
            return
        if not cmds:
            result.steps_skipped.append(step_name)
            return
        t0 = time.perf_counter()
        all_ok = True
        for cmd in cmds:
            step_timeout = min(cfg.timeout_per_step_s, budget_remaining)
            cmd_str = " ".join(cmd)
            run_result = run_command(cmd_str, cwd=cwd or str(repo_root), timeout=int(step_timeout))
            elapsed = time.perf_counter() - t0
            budget_remaining -= elapsed
            if run_result.exit_code not in (0, 1):  # 1 is "found issues" for linters, not fatal
                all_ok = False
        if all_ok:
            result.steps_ran.append(step_name)
        else:
            result.steps_failed.append(step_name)

    def _run_diag_step(step_name: str, cmds: list[tuple[str, list[str]]], *, cwd: str | None = None) -> None:
        nonlocal budget_remaining
        if budget_remaining <= 0:
            result.steps_skipped.append(step_name)
            return
        if not cmds:
            result.steps_skipped.append(step_name)
            return
        for source, cmd in cmds:
            step_timeout = min(cfg.timeout_per_step_s, budget_remaining)
            t0 = time.perf_counter()
            cmd_str = " ".join(cmd)
            run_result = run_command(cmd_str, cwd=cwd or str(repo_root), timeout=int(step_timeout))
            budget_remaining -= time.perf_counter() - t0
            if source == "ruff":
                result.diagnostics.extend(_parse_ruff_json(run_result.stdout, "ruff"))
            elif source == "tsc":
                result.diagnostics.extend(_parse_tsc_output(run_result.stdout))
            elif source == "cargo_clippy":
                result.diagnostics.extend(_parse_cargo_clippy_json(run_result.stdout))
        result.steps_ran.append(step_name)

    for lang, paths in by_lang.items():
        cwd = str(repo_root)

        if cfg.run_format:
            _run_step(f"format:{lang}", _format_cmds(lang, paths), cwd=cwd)

        if cfg.run_organize_imports:
            _run_step(f"imports:{lang}", _import_cmds(lang, paths), cwd=cwd)

        if cfg.run_lint_autofix:
            _run_step(f"lint_fix:{lang}", _lint_fix_cmds(lang, paths, repo_root), cwd=cwd)

        if cfg.run_diagnostics:
            _run_diag_step(f"diagnostics:{lang}", _diagnostic_cmds(lang, paths, repo_root), cwd=cwd)

    result.total_ms = int((time.perf_counter() - wall_start) * 1000)
    return result


__all__ = ["DiagnosticItem", "HookConfig", "HookResult", "run_post_edit_hooks"]
