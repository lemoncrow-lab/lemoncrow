"""Stop hook: verify-before-done.

A code change is not done until the project's own tests have been *run* against
it. This hook nudges once when a session edited source files but the transcript
shows no test-runner invocation.

Only a real test runner counts -- an ad-hoc ``python -c`` / ``python repro.py``
snippet does NOT. A snippet checks only what the author thought to check, so it
sails past regressions in neighboring code the change quietly broke; the project
suite catches them.

Beyond "did you run tests", two *completeness* checks run regardless of whether
tests were run -- because running the existing suite cannot catch a fix that is
correct-but-incomplete when the discriminating test is withheld (e.g. SWE-bench
FAIL_TO_PASS tests injected only at grade time):

  A. Contract-change caller sweep. If an edit flips a method's decorator from
     ``@staticmethod`` to ``@classmethod``, every bare ``name(...)`` call site
     that was NOT updated to ``self.``/``cls.`` still hard-binds the old class.
     (Born from sympy-12489: ``Permutation._af_new`` converted, 13 operator
     call sites left bare -> subclassing still broken.)

  B. Second-path coverage. If the issue text says the bug "also reproduces"
     via a second named entry point (e.g. ``scatterplot``) but the change
     touches only ONE source module, the parallel code path is likely unfixed
     and untested. (Born from seaborn-3187: only ``_core/scales.py`` edited,
     the classic ``seaborn/utils.py`` legend path left broken.)

Generic and language-agnostic where possible; A/B are intentionally narrow and
high-precision so a complete fix is not blocked. Bounded and fail-open by design
-- fires at most once per session (returns immediately when ``stop_hook_active``
is set) and any error exits 0 without blocking. Opt out entirely with
ATELIER_VERIFY_BEFORE_DONE=0; opt out of the completeness checks alone with
ATELIER_VERIFY_COMPLETENESS=0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".ipynb",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".cs",
        ".swift",
        ".m",
        ".mm",
        ".ex",
        ".exs",
        ".erl",
        ".clj",
        ".hs",
        ".ml",
        ".lua",
        ".dart",
        ".sh",
    }
)
_TEST_RUN = re.compile(r"""(?xi)
    \b(
        pytest | py\.test | nose2? | tox | nox
      | unittest | runtests
      | go\s+test | cargo\s+test | cargo\s+nextest | dotnet\s+test | mix\s+test | phpunit
      | jest | vitest | mocha | ava | rspec | minitest | ctest
      | bazel\s+test | ([./\w]*gradlew|gradle|mvn)\b[^\n]*\btest
      | (npm|pnpm|yarn|bun)\s+(run\s+\S+|test)
      | (rake|bundle\s+exec)\b[^\n]*\b(test|spec|rspec)
      | manage\.py\s+test
      | make\s+(?:test|check) | rails\s+test
    )\b
    """)


def _disabled() -> bool:
    v = os.environ.get("ATELIER_VERIFY_BEFORE_DONE")
    return v is not None and v.strip().lower() in {"0", "false", "off", "no"}


def _completeness_disabled() -> bool:
    v = os.environ.get("ATELIER_VERIFY_COMPLETENESS")
    return v is not None and v.strip().lower() in {"0", "false", "off", "no"}


def _is_edit_tool(name: str) -> bool:
    return name in {"edit", "write", "multiedit", "notebookedit"} or name.endswith("edit")


def _edit_targets(tool_input: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("file_path", "path", "filename"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            out.append(val)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                fp = entry.get("file_path") or entry.get("path")
                if isinstance(fp, str) and fp:
                    out.append(fp)
    return out


def _edit_diffs(tool_input: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (path, old, new) for every edit in an edit-tool input."""
    out: list[tuple[str, str, str]] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                p = entry.get("path") or entry.get("file_path")
                if isinstance(p, str) and p:
                    old = entry.get("old") or entry.get("old_string") or ""
                    new = entry.get("new") or entry.get("new_string") or ""
                    out.append((p, str(old), str(new)))
    else:
        p = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(p, str) and p:
            old = tool_input.get("old_string") or ""
            new = tool_input.get("new_string") or tool_input.get("new") or ""
            out.append((p, str(old), str(new)))
    return out


def _is_code_path(path: str) -> bool:
    return Path(path.split("#")[0]).suffix.lower() in _CODE_SUFFIXES


def _is_test_path(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    base = Path(low).name
    return (
        "/tests/" in low
        or "/test/" in low
        or low.startswith("tests/")
        or low.startswith("test/")
        or base.startswith("test_")
        or base.endswith("_test.py")
    )


def _block_text(entry: dict[str, Any]) -> str:
    """Concatenate the text of a user message (the issue prompt lives here)."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def scan_transcript(transcript_path: str | None) -> tuple[list[str], bool]:
    """Return (edited code files, whether a behavioral check was executed)."""
    edited, verified, _diffs, _prompt = scan_transcript_rich(transcript_path)
    return edited, verified


def scan_transcript_rich(
    transcript_path: str | None,
) -> tuple[list[str], bool, list[tuple[str, str, str]], str]:
    """Return (edited code files, tests-run?, edit diffs, first issue-prompt text)."""
    edited: list[str] = []
    verified = False
    diffs: list[tuple[str, str, str]] = []
    prompt = ""
    if not transcript_path:
        return edited, verified, diffs, prompt
    p = Path(transcript_path)
    if not p.exists():
        return edited, verified, diffs, prompt
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return edited, verified, diffs, prompt
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "user" and not prompt:
            prompt = _block_text(entry)
            continue
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "").split("__")[-1].lower()
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            if _is_edit_tool(name):
                edited.extend(t for t in _edit_targets(tool_input) if _is_code_path(t))
                diffs.extend(d for d in _edit_diffs(tool_input) if _is_code_path(d[0]))
            elif name in {"bash", "shell"}:
                cmd = str(tool_input.get("command") or "")
                if _TEST_RUN.search(cmd):
                    verified = True
    return edited, verified, diffs, prompt


# --- Detector A: contract-change caller sweep -------------------------------
_CONTRACT_DEC = re.compile(r"@(staticmethod|classmethod|property)\b")
_DEF_NAME = re.compile(r"\bdef\s+(\w+)\s*\(")


def _contract_changed_symbols(diffs: list[tuple[str, str, str]]) -> set[str]:
    """Symbols whose decorator flipped @staticmethod -> @classmethod in an edit."""
    syms: set[str] = set()
    for path, old, new in diffs:
        if not path.endswith(".py"):
            continue
        old_d = set(_CONTRACT_DEC.findall(old))
        new_d = set(_CONTRACT_DEC.findall(new))
        if "staticmethod" in old_d and "classmethod" in new_d and "classmethod" not in old_d:
            for m in _DEF_NAME.finditer(new):
                syms.add(m.group(1))
    return syms


def _bare_call_sites(symbol: str, root: str = ".") -> list[str]:
    """file:line of bare ``symbol(`` calls (not ``.symbol(``, not its def)."""
    bare = re.compile(r"(?<![.\w])" + re.escape(symbol) + r"\s*\(")
    defline = re.compile(r"\bdef\s+" + re.escape(symbol) + r"\b")
    hits: list[str] = []
    try:
        proc = subprocess.run(
            ["grep", "-rn", "--include=*.py", "-e", symbol + "(", root],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:  # noqa: BLE001  # fail-open: a hook must never crash the agent
        return hits
    for line in proc.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        fpath, lineno, body = parts
        if _is_test_path(fpath):
            continue
        if defline.search(body):
            continue
        if bare.search(body):
            hits.append(f"{fpath}:{lineno}")
    return hits


def detector_a(diffs: list[tuple[str, str, str]], root: str = ".") -> tuple[str, list[str]] | None:
    for sym in sorted(_contract_changed_symbols(diffs)):
        sites = _bare_call_sites(sym, root)
        if sites:
            return sym, sites
    return None


# --- Detector B: second-path coverage ---------------------------------------
_ALSO = re.compile(
    r"(?i)\balso\b[^.\n]{0,80}?\b(?:reproduc\w*|happen\w*|occur\w*|affect\w*|present\w*)\b([^.\n]{0,90})"
)


def _second_scenario_token(prompt: str) -> str | None:
    m = _ALSO.search(prompt or "")
    if not m:
        return None
    tail = m.group(1)
    bt = re.search(r"`(\w{3,})`", tail)
    if bt:
        return bt.group(1)
    word = re.search(r"\b(\w*plot\w*|\w+plot)\b", tail)
    if word:
        return word.group(1)
    return None


def _source_modules(edited: list[str]) -> set[str]:
    return {Path(f.split("#")[0]).name for f in edited if not _is_test_path(f)}


def detector_b(prompt: str, edited: list[str]) -> tuple[str, list[str]] | None:
    tok = _second_scenario_token(prompt)
    if not tok:
        return None
    mods = _source_modules(edited)
    if len(mods) <= 1:
        return tok, sorted(mods)
    return None


_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".rb", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift",
    ".kt", ".scala", ".sh", ".bash", ".sql",
}


def _is_source_file(path: str) -> bool:
    return Path(path.split("#")[0]).suffix.lower() in _SOURCE_SUFFIXES


_REASON = "FIXME (verify): edited {sample} but ran no tests -- run the tests covering it (or the suite) before finishing."


def _bench_mode_on() -> bool:
    """True only when ATELIER_BENCH_MODE is set to something other than 'off'."""
    raw = os.environ.get("ATELIER_BENCH_MODE")
    return raw is not None and raw.strip().lower() != "off"


def decide(payload: dict[str, Any]) -> dict[str, str] | None:
    if _disabled():
        return None
    if payload.get("stop_hook_active") is True:
        return None
    edited, verified, diffs, prompt = scan_transcript_rich(payload.get("transcript_path"))
    if not edited:
        return None

    if _bench_mode_on() and not _completeness_disabled():
        a = detector_a(diffs)
        if a is not None:
            sym, sites = a
            shown = ", ".join(sites[:12]) + (" .." if len(sites) > 12 else "")
            return {
                "decision": "block",
                "reason": (
                    f"FIXME (completeness): you converted `{sym}` to a classmethod, but these "
                    f"bare `{sym}(...)` call sites were not updated to `self.`/`cls.` and still "
                    f"hard-bind the original class: {shown}. Update each (or confirm it is "
                    f"intentional) so the change reaches every site before finishing."
                ),
            }
        b = detector_b(prompt, edited)
        if b is not None:
            tok, mods = b
            where = ", ".join(mods) if mods else "a single file"
            return {
                "decision": "block",
                "reason": (
                    f"FIXME (completeness): the issue says the bug also reproduces via `{tok}`, "
                    f"but your change touches only one source module ({where}). KEEP the path you "
                    f"already fixed passing, AND additionally fix + test the parallel path (it "
                    f"almost certainly lives in a different module and is unexercised by your "
                    f"current test) -- do not regress the working path. Verify both before finishing."
                ),
            }

    if verified:
        return None
    source_edited = [p for p in edited if _is_source_file(p)]
    if not source_edited:
        return None
    uniq = sorted({Path(p.split("#")[0]).name for p in source_edited})
    reason = _REASON.format(n=len(uniq), sample=", ".join(uniq[:4]))
    return {"decision": "block", "reason": reason}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        result = decide(payload)
        if result is not None:
            print(json.dumps(result))
    except Exception:  # noqa: BLE001  # fail-open: a hook must never crash the agent
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
