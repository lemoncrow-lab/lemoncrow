---
phase: 16-canonical-language-registry
verified: 2026-05-29T11:37:50Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
---

# Phase 16: Canonical Language Registry Verification Report

**Phase Goal:** All code-intel surfaces share one canonical language identity, fixing shell/bash drift.
**Verified:** 2026-05-29T11:37:50Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
| --- | ----- | ------ | -------- |
| 1 | Recognized file extensions resolve through one canonical registry, unknowns fall back to `"text"` (SC1, DLS-LANG-01/02) | ✓ VERIFIED | `languages.py` builds `EXTENSION_TO_LANGUAGE` from frozen `LANGUAGES` table; `language_for_path('x.xyz') is None`; `capability._language_for` maps `None→"text"` (verified: `C._language_for(Path('a.xyz'))=='text'`) |
| 2 | Shell files `.sh`/`.bash`/`.zsh` resolve to tree-sitter-compatible `bash` key (SC2, DLS-LANG-03) | ✓ VERIFIED | `Language("bash", {.sh,.bash,.zsh}, ...)`; behavioral check `language_for_path('x.sh').name=='bash'`; `test_shell_outline_reaches_treesitter_bash` proves `.sh` yields `outline.kind=="treesitter"` (not generic), `language=="bash"` — end-to-end payoff |
| 3 | Extension detection, tree-sitter outline config, repo-map tags, and SCIP binary lookup share one identity (SC3, DLS-LANG-04) | ✓ VERIFIED | `capability.py`, `tags.py`, `binaries.py` all import and delegate to `languages.py`; drift guard confirms all 12 `_LANG_CONFIG` keys resolve via `language_by_name` (0 missing) |
| 4 | Existing recognized languages resolve to prior/intentionally-canonicalized names (SC4, DLS-LANG-02) | ✓ VERIFIED | Parametrized `test_legacy_extensions_resolve_to_canonical_name` covers 35 legacy extensions; `csharp` canonical (not `c_sharp`); all pass |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `src/atelier/infra/code_intel/languages.py` | Language dataclass + tables + lookup helpers | ✓ VERIFIED | 97 lines; frozen `Language` with exactly 4 fields; stdlib-only imports (`dataclasses`, `pathlib`); no `atelier.core` import (no cycle); full `__all__` |
| `tests/infra/code_intel/test_languages.py` | Registry unit tests + drift guard | ✓ VERIFIED | Covers DLS-LANG-01/02/03/04; `-k extensions` (38) and `-k canonical` (39) selectors hit; drift-guard parametrized over `_LANG_CONFIG` keys |
| `src/atelier/core/capabilities/semantic_file_memory/capability.py` | `_language_for` delegates | ✓ VERIFIED | Imports `language_for_path`; body delegates with `None→"text"`; 0 non-comment `"shell"` matches (threat T-16-03 mitigated) |
| `src/atelier/infra/tree_sitter/tags.py` | `detect_language` delegates, `str\|None` preserved | ✓ VERIFIED | Imports `language_for_path`; returns `lang.name if lang else None`; behavioral check `detect_language(Path('a.xyz')) is None` |
| `src/atelier/infra/code_intel/scip/binaries.py` | indexer sourced from registry, env-vars byte-identical | ✓ VERIFIED | Imports `language_by_name`, uses `lang.scip_indexer`; `ATELIER_SCIP_PYTHON_BIN`/`ATELIER_SCIP_TYPESCRIPT_BIN` preserved verbatim |
| `tests/core/test_shell_outline.py` | `.sh`→bash→treesitter regression | ✓ VERIFIED | Asserts `kind=="treesitter"`, `language=="bash"`, signature kept/body stripped |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | -- | --- | ------ | ------- |
| `test_languages.py` | `languages.py` | `from atelier.infra.code_intel.languages import` | ✓ WIRED | Imports all 5 public symbols; tests pass |
| `capability.py` | `languages.py` | `import language_for_path` | ✓ WIRED | Imported (line 12) + used (line 88) |
| `tags.py` | `languages.py` | `import language_for_path` | ✓ WIRED | Imported (line 11) + used (line 89) |
| `binaries.py` | `languages.py` | `import language_by_name` | ✓ WIRED | Imported (line 9) + used (line 25) |
| `treesitter_ast._LANG_CONFIG` | `languages.py` | drift-guard (`language_by_name(key)`) | ✓ WIRED | All 12 keys resolve; 0 missing |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| -------- | ------------- | ------ | ------------------ | ------ |
| `capability.smart_read` | `payload["language"]` | `_language_for` → `language_for_path` → frozen `LANGUAGES` table | Yes — `.sh` yields `"bash"`, reaches live tree-sitter grammar producing `treesitter` outline | ✓ FLOWING |
| `binaries.discover_scip_binary` | `fallback` indexer | `language_by_name(lang).scip_indexer` | Yes — `scip-python`/`scip-typescript` resolved; env-var resolution proven by monkeypatch test | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| Registry resolves shell/csharp/unknown | `python -c` assertions | `bash`/`csharp`/`None` | ✓ PASS |
| capability + tags delegation | `python -c` assertions | `bash`/`text`/`bash`/`None` | ✓ PASS |
| Drift guard: all `_LANG_CONFIG` keys in registry | `python -c` diff | 0 missing | ✓ PASS |
| Phase test suites | `pytest test_languages test_shell_outline test_scip_adapter test_code_context -q` | 123 passed, 5 skipped (unrelated SCIP routing, tracked post-launch) | ✓ PASS |
| `-k extensions` selector | `pytest test_languages.py -k extensions` | 38 passed | ✓ PASS |
| `-k canonical` selector | `pytest test_languages.py -k canonical` | 39 passed | ✓ PASS |
| `-k shell` selector | `pytest test_shell_outline.py -k shell` | 1 passed | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| ----------- | ----------- | ----------- | ------ | -------- |
| DLS-LANG-01 | 16-01 | Canonical registry single source of truth | ✓ SATISFIED | `languages.py` exposes `Language`, `LANGUAGES`, `EXTENSION_TO_LANGUAGE`, `ALL_LANGUAGES`, `language_for_path`, `language_by_name` |
| DLS-LANG-02 | 16-01 | Extension detection delegates, preserves `"text"` fallback | ✓ SATISFIED | `capability._language_for` delegates; unknown→`None`→`"text"`; parametrized legacy test |
| DLS-LANG-03 | 16-02 | Shell extensions resolve to bash key | ✓ SATISFIED | `bash` Language entry; shell-outline regression proves treesitter path |
| DLS-LANG-04 | 16-02 | Tree-sitter config keys, repo-map tags, SCIP keys share canonical names | ✓ SATISFIED | tags.py + binaries.py delegate; drift guard enforces `_LANG_CONFIG ⊆ registry`; SCIP env-var contract test |

No orphaned requirements — all four DLS-LANG IDs mapped to phase 16 are claimed by plans and verified.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| ---- | ---- | ------- | -------- | ------ |
| — | — | None | — | No TBD/FIXME/XXX/HACK/PLACEHOLDER debt markers in any modified file |

### Human Verification Required

None. The phase is an internal code-intel refactor with no visual/UX/external-service surface; the end-to-end payoff (shell→treesitter outline) is verified programmatically by `test_shell_outline.py`.

### Gaps Summary

No gaps. All four ROADMAP success criteria and all four requirements (DLS-LANG-01..04) are verified against the codebase. The registry module is substantive (97 lines, stdlib-only, no import cycle), all three consumer surfaces delegate to it (verified by import + usage grep and behavioral checks), and the `_LANG_CONFIG` drift guard enforces canonical-identity sharing. The shell/bash drift bug is fixed end-to-end: a `.sh` file now produces a `kind:"treesitter"` outline with `language=="bash"`. SCIP env-var names (`ATELIER_SCIP_PYTHON_BIN`/`ATELIER_SCIP_TYPESCRIPT_BIN`) are preserved byte-identical and proven by a monkeypatch resolution test. The threat-model mitigation (no edits to the overloaded `"shell"` tool name) holds — `capability.py` has 0 non-comment `"shell"` matches.

**Note on repository gate:** Full-repo format/lint/typecheck/test gates are not Phase 16 blockers — they fail on pre-existing dirty-worktree changes outside phase scope (e.g., `context_compression/minify.py`, `scoped_context/prune.py`, `verification/checks/__init__.py`, `gateway/adapters/mcp_server.py`). The focused phase validation (`test_languages.py`, `test_code_context.py`, `test_shell_outline.py`, `test_scip_adapter.py`) is green: 123 passed, 5 skipped (unrelated SCIP routing, tracked post-launch in docs/launch-readiness.md).

---

_Verified: 2026-05-29T11:37:50Z_
_Verifier: the agent (gsd-verifier)_
