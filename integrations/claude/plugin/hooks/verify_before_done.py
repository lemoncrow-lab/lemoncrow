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
LEMONCROW_VERIFY_BEFORE_DONE=0; opt out of the completeness checks alone with
LEMONCROW_VERIFY_COMPLETENESS=0; exclude specific file extensions from the nudge
(e.g. archival docs / data dumps) with LEMONCROW_VERIFY_SKIP_SUFFIXES=.md,.csv
(comma/space-separated, leading dot optional).
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
# Text/data deliverables. Many benchmark (and real) tasks grade a *written
# artifact* -- a csv, json, sql, fasta, config, ... -- not edited source. An
# artifact saved with no verification run is exactly the over-claim failure
# ("done: looks right") this hook exists to catch, so treat these like source.
_TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".xml",
        ".html",
        ".htm",
        ".svg",
        ".sql",
        ".proto",
        ".graphql",
        ".tex",
        ".fasta",
        ".fa",
        ".fastq",
        ".vcf",
        ".gff",
        ".bed",
        ".pdb",
        ".gcode",
        ".nc",
        ".dot",
        ".env",
        ".properties",
    }
)
# Prose docs: a graded deliverable in a benchmark (e.g. write a report to
# answer.md) but usually just documentation in normal dev. Verifiable only
# under bench mode, so ordinary README/docs edits are never nagged.
_DOC_SUFFIXES = frozenset({".md", ".rst", ".markdown", ".adoc", ".org"})
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
    v = os.environ.get("LEMONCROW_VERIFY_BEFORE_DONE")
    return v is not None and v.strip().lower() in {"0", "false", "off", "no"}


def _completeness_disabled() -> bool:
    v = os.environ.get("LEMONCROW_VERIFY_COMPLETENESS")
    return v is not None and v.strip().lower() in {"0", "false", "off", "no"}


def _skip_suffixes() -> frozenset[str]:
    """User-configured extensions to never nag about (LEMONCROW_VERIFY_SKIP_SUFFIXES).

    Comma/space-separated, leading dot optional -- e.g. ``.md,csv`` keeps
    archival docs and data dumps out of the verify nudge. Overrides code, text,
    and doc classification alike.
    """
    raw = os.environ.get("LEMONCROW_VERIFY_SKIP_SUFFIXES", "")
    out: set[str] = set()
    for tok in re.split(r"[,\s]+", raw.strip()):
        if tok:
            out.add((tok if tok.startswith(".") else "." + tok).lower())
    return frozenset(out)


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


def _is_verifiable_path(path: str, *, include_docs: bool = False) -> bool:
    """A path whose edit should demand a verification run: source, or a
    text/data deliverable. Prose docs count only when ``include_docs`` (bench)."""
    suf = Path(path.split("#")[0]).suffix.lower()
    if suf in _skip_suffixes():
        return False
    if suf in _CODE_SUFFIXES or suf in _TEXT_SUFFIXES:
        return True
    return include_docs and suf in _DOC_SUFFIXES


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
    edited, verified, _checked, _diffs, _prompt = scan_transcript_rich(transcript_path)
    return edited, verified


def scan_transcript_rich(
    transcript_path: str | None,
) -> tuple[list[str], bool, bool, list[tuple[str, str, str]], str]:
    """Return (edited files, tests-run?, deliverable-exercised?, edit diffs, first prompt).

    A test run only counts as verification when it happened AFTER the last
    code edit (a pre-edit run proves nothing about the change) and, when the
    outcome is detectable via the tool_result ``is_error`` flag, only when it
    succeeded. A run with no visible result is counted (fail-open).

    ``deliverable-exercised`` = a bash command names an edited file -- the real
    check for a data/artifact task that has no test suite to run.
    """
    edited: list[str] = []
    checked = False
    diffs: list[tuple[str, str, str]] = []
    prompt = ""
    last_edit_idx = -1
    test_runs: list[tuple[int, str]] = []  # (event order, tool_use id)
    failed_ids: set[str] = set()
    idx = 0
    cmds: list[str] = []
    if not transcript_path:
        return edited, False, checked, diffs, prompt
    p = Path(transcript_path)
    if not p.exists():
        return edited, False, checked, diffs, prompt
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return edited, False, checked, diffs, prompt
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
        if entry.get("type") == "user":
            if not prompt:
                prompt = _block_text(entry)
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        tid = str(block.get("tool_use_id") or "")
                        if tid:
                            failed_ids.add(tid)
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
            idx += 1
            name = str(block.get("name") or "").split("__")[-1].lower()
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            if _is_edit_tool(name):
                targets = [t for t in _edit_targets(tool_input) if _is_verifiable_path(t, include_docs=True)]
                edited.extend(targets)
                diffs.extend(d for d in _edit_diffs(tool_input) if _is_code_path(d[0]))
                if targets:
                    last_edit_idx = idx
            elif name in {"bash", "shell"}:
                cmd = str(tool_input.get("command") or "")
                cmds.append(cmd)
                if _TEST_RUN.search(cmd):
                    test_runs.append((idx, str(block.get("id") or "")))
    verified = any(i > last_edit_idx and (not tid or tid not in failed_ids) for i, tid in test_runs)
    # A data/artifact deliverable has no test suite -- exercising it (a bash command
    # naming the edited file) is the authoritative check. Code keeps the stricter
    # test-runner bar in decide(); the >=5 length guard avoids tiny-basename matches.
    bases = {b for b in (Path(p.split("#")[0]).name for p in edited) if len(b) >= 5}
    checked = any(b in c for c in cmds for b in bases)
    return edited, verified, checked, diffs, prompt


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
    # Code modules only -- detector B's "single source module" heuristic must not
    # be diluted by the text/data deliverables now collected into `edited`.
    return {Path(f.split("#")[0]).name for f in edited if _is_code_path(f) and not _is_test_path(f)}


def detector_b(prompt: str, edited: list[str]) -> tuple[str, list[str]] | None:
    tok = _second_scenario_token(prompt)
    if not tok:
        return None
    mods = _source_modules(edited)
    if len(mods) <= 1:
        return tok, sorted(mods)
    return None


_REASON = "FIXME (verify): edited {sample} but ran no test/verification -- run the authoritative check that proves the result (the task's stated validation, the project test suite, or a byte/behavior check) before finishing."


def _bench_mode_on() -> bool:
    """True only when LEMONCROW_BENCH_MODE is set to something other than 'off'."""
    raw = os.environ.get("LEMONCROW_BENCH_MODE")
    return raw is not None and raw.strip().lower() != "off"


def decide(payload: dict[str, Any]) -> dict[str, str] | None:
    if _disabled():
        return None
    if payload.get("stop_hook_active") is True:
        return None
    edited, verified, checked, diffs, prompt = scan_transcript_rich(payload.get("transcript_path"))
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

    # Code edits keep the strict bar (a snippet misses regressions the withheld suite
    # catches). A text/data deliverable has no suite -- exercising the artifact (a bash
    # command naming it) IS the check, so it clears the bar too.
    if verified or (checked and not any(_is_code_path(p) for p in edited)):
        return None
    source_edited = [p for p in edited if _is_verifiable_path(p, include_docs=_bench_mode_on())]
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
