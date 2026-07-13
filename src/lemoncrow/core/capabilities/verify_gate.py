"""Host-agnostic core for the verify-before-done gate.

A code change is not done until the project's own tests have been *run* against
it. The gate nudges once when a session edited source files but shows no
test-runner invocation. Only a real test runner counts -- an ad-hoc
``python -c`` / ``python repro.py`` snippet does NOT: a snippet checks only what
the author thought to check, so it sails past regressions in neighboring code
the change quietly broke; the project suite catches them.

Beyond "did you run tests", two *completeness* checks run regardless of whether
tests were run -- because running the existing suite cannot catch a fix that is
correct-but-incomplete when the discriminating test is withheld (e.g. SWE-bench
FAIL_TO_PASS tests injected only at grade time):

  A. Contract-change caller sweep (``detector_a``). If an edit flips a method's
     decorator from ``@staticmethod`` to ``@classmethod``, every bare
     ``name(...)`` call site that was NOT updated to ``self.``/``cls.`` still
     hard-binds the old class. (Born from sympy-12489.)

  B. Second-path coverage (``detector_b``). If the issue text says the bug
     "also reproduces" via a second named entry point but the change touches
     only ONE source module, the parallel code path is likely unfixed and
     untested. (Born from seaborn-3187.)

This module holds only the *pure* decision logic, parameterised on
:class:`VerifySignals` (what was edited, whether a real check ran, edit diffs,
the issue text). Each host builds those signals from its own state -- Claude
from its transcript JSONL, Codex/OpenCode from the run ledger -- and calls
:func:`decide`. Bounded and fail-open by design; opt out entirely with
LEMONCROW_VERIFY_BEFORE_DONE=0, the completeness checks alone with
LEMONCROW_VERIFY_COMPLETENESS=0, and specific extensions with
LEMONCROW_VERIFY_SKIP_SUFFIXES=.md,.csv (comma/space-separated, leading dot
optional).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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
# ("done: looks right") this gate exists to catch, so treat these like source.
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


def disabled() -> bool:
    v = os.environ.get("LEMONCROW_VERIFY_BEFORE_DONE")
    return v is not None and v.strip().lower() in {"0", "false", "off", "no"}


def completeness_disabled() -> bool:
    v = os.environ.get("LEMONCROW_VERIFY_COMPLETENESS")
    return v is not None and v.strip().lower() in {"0", "false", "off", "no"}


def bench_mode_on() -> bool:
    """True only when LEMONCROW_BENCH_MODE is set to something other than 'off'."""
    raw = os.environ.get("LEMONCROW_BENCH_MODE")
    return raw is not None and raw.strip().lower() != "off"


def skip_suffixes() -> frozenset[str]:
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


def is_code_path(path: str) -> bool:
    return Path(path.split("#")[0]).suffix.lower() in _CODE_SUFFIXES


def is_verifiable_path(path: str, *, include_docs: bool = False) -> bool:
    """A path whose edit should demand a verification run: source, or a
    text/data deliverable. Prose docs count only when ``include_docs`` (bench)."""
    suf = Path(path.split("#")[0]).suffix.lower()
    if suf in skip_suffixes():
        return False
    if suf in _CODE_SUFFIXES or suf in _TEXT_SUFFIXES:
        return True
    return include_docs and suf in _DOC_SUFFIXES


def is_test_path(path: str) -> bool:
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
        if is_test_path(fpath):
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
    return {Path(f.split("#")[0]).name for f in edited if is_code_path(f) and not is_test_path(f)}


def detector_b(prompt: str, edited: list[str]) -> tuple[str, list[str]] | None:
    tok = _second_scenario_token(prompt)
    if not tok:
        return None
    mods = _source_modules(edited)
    if len(mods) <= 1:
        return tok, sorted(mods)
    return None


_REASON = "FIXME (verify): edited {sample}, run test/verification."


# --- Fire-once-per-nudge state -----------------------------------------------
# One unresolved edit must produce ONE FIXME, not a repeat on every later Stop
# once the model has already seen it and made its call (fix, or knowingly move
# on) -- re-showing identical old news is just noise. Keyed by a host-supplied
# stable string (Claude: transcript_path; Codex/OpenCode: session id).
def _state_path(dedup_key: str) -> Path:
    digest = hashlib.sha256(dedup_key.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return Path(os.environ.get("TMPDIR", "/tmp")) / "lemoncrow-verify-before-done" / f"{digest}.json"


def _last_shown_signature(dedup_key: str) -> str | None:
    try:
        data = json.loads(_state_path(dedup_key).read_text(encoding="utf-8"))
        return data.get("signature") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001  # fail-open: unreadable/missing state -> still nudge
        return None


def _record_shown_signature(dedup_key: str, signature: str) -> None:
    try:
        p = _state_path(dedup_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"signature": signature}), encoding="utf-8")
    except Exception:  # noqa: BLE001  # fail-open: state persistence must never block
        pass


@dataclass
class VerifySignals:
    """Host-neutral inputs to the verify gate.

    edited:   verifiable files touched this session (source + data/doc deliverables).
    verified: a real test runner ran AFTER the last edit and did not fail.
    checked:  a bash command named an edited file (the check for a suite-less
              data/artifact task).
    diffs:    (path, old, new) for code edits -- feeds detector A.
    prompt:   the issue/first-user-prompt text -- feeds detector B.
    """

    edited: list[str] = field(default_factory=list)
    verified: bool = False
    checked: bool = False
    diffs: list[tuple[str, str, str]] = field(default_factory=list)
    prompt: str = ""


def decide(signals: VerifySignals, *, dedup_key: str = "", root: str = ".") -> dict[str, str] | None:
    """Return a ``{"decision": "block", "reason": ...}`` nudge, or None.

    Pure over ``signals`` plus process env (the LEMONCROW_VERIFY_* toggles) and,
    for detector A, a filesystem grep under ``root``. ``dedup_key`` gates the
    fire-once-per-nudge state; pass "" to disable dedup (always evaluate).

    Callers own the ``disabled()`` and per-host "already stopping" early-exits.
    """
    edited = signals.edited
    if not edited:
        return None

    candidate: dict[str, str] | None = None
    if bench_mode_on() and not completeness_disabled():
        a = detector_a(signals.diffs, root)
        if a is not None:
            sym, sites = a
            shown = ", ".join(sites[:12]) + (" .." if len(sites) > 12 else "")
            candidate = {
                "decision": "block",
                "reason": (
                    f"FIXME (completeness): `{sym}` became a classmethod but these call sites "
                    f"still hard-bind the old class: {shown} -- fix or confirm intentional."
                ),
            }
        if candidate is None:
            b = detector_b(signals.prompt, edited)
            if b is not None:
                tok, mods = b
                where = ", ".join(mods) if mods else "a single file"
                candidate = {
                    "decision": "block",
                    "reason": (
                        f"FIXME (completeness): `{tok}` also reproduces the bug but fix touches only "
                        f"{where} -- fix + test the parallel path too, without regressing this one; verify both."
                    ),
                }

    if candidate is None:
        # Code edits keep the strict bar (a snippet misses regressions the withheld suite
        # catches). A text/data deliverable has no suite -- exercising the artifact (a bash
        # command naming it) IS the check, so it clears the bar too.
        if signals.verified or (signals.checked and not any(is_code_path(p) for p in edited)):
            return None
        source_edited = [p for p in edited if is_verifiable_path(p, include_docs=bench_mode_on())]
        if not source_edited:
            return None
        uniq = sorted({Path(p.split("#")[0]).name for p in source_edited})
        reason = _REASON.format(n=len(uniq), sample=", ".join(uniq[:4]))
        candidate = {"decision": "block", "reason": reason}

    # Fire at most once per distinct nudge: once this exact reason has already
    # been shown for this dedup_key, showing it again -- with no new
    # edit/verification event since -- is not new information. `depth` (how many
    # qualifying edit calls have landed so far) disambiguates a genuinely new
    # repeat-edit to the same file from a stale unresolved one.
    if dedup_key:
        depth = len(edited) + len(signals.diffs)
        signature = f"{candidate['reason']}::depth={depth}"
        if _last_shown_signature(dedup_key) == signature:
            return None
        _record_shown_signature(dedup_key, signature)
    return candidate
