---
phase: 23-silent-exception-audit
plan: 03
subsystem: benchmarks
tags: [silent-exceptions, observability, ruff, BLE001, logging]
requires: ["23-02"]
provides:
  - "Observable benchmark parse loops (6 pass + 3 continue in-scope sites)"
  - "BLE001 ignore shrink (4 benchmark files dropped)"
affects:
  - src/benchmarks/swe/compact_bench.py
  - src/benchmarks/swe/routing_bench.py
  - src/benchmarks/swe/routing_quality_bench.py
  - src/benchmarks/swe/routing_replay_bench.py
  - src/benchmarks/tool_bench/report.py
  - src/benchmarks/tool_bench/runner.py
  - pyproject.toml
tech-stack:
  added: []
  patterns:
    - "Module logger (logging.getLogger(__name__)) per cline.py:36 convention"
    - "Narrow parse handlers to realistic exception tuple; debug-log with exc_info=True"
key-files:
  created:
    - .planning/phases/23-silent-exception-audit/23-03-SUMMARY.md
  modified:
    - src/benchmarks/swe/compact_bench.py
    - src/benchmarks/swe/routing_bench.py
    - src/benchmarks/swe/routing_quality_bench.py
    - src/benchmarks/swe/routing_replay_bench.py
    - src/benchmarks/tool_bench/report.py
    - src/benchmarks/tool_bench/runner.py
    - pyproject.toml
decisions:
  - "Parse loops narrowed to realistic tuples (not JSONDecodeError alone) to keep best-effort per-record skips resilient (Pitfall 4 / T-23-07)"
  - "routing_replay_bench.py and report.py retain BLE001+T201 ignores (other broad handlers + T201 print debt owned by Phase 24)"
metrics:
  duration: "~10m"
  completed: "2026-05-29"
requirements: [QBL-EXC-02, QBL-EXC-03]
---

# Phase 23 Plan 03: Benchmark Silent Exception Audit Summary

Made the in-scope benchmark silent handlers observable (module loggers + `exc_info=True`
debug logging) and narrowed parse loops to realistic exception tuples, dropping the BLE001
per-file-ignore for the 4 benchmark files that became fully clean.

## What Was Done

### Task 1 — 4 fully-removable benchmark files narrowed; BLE001 ignores deleted
Added `import logging` + `logger = logging.getLogger(__name__)` to each of the 4 files
(none had a logger). Narrowed every broad `except Exception` to realistic tuples and
replaced silent `pass`/`continue` bodies with debug logging:

- **compact_bench.py** — `continue` site (`json.loads` line) → `(json.JSONDecodeError, ValueError)`;
  outer `pass` site (`_parse_session`, file read + loop) → `(json.JSONDecodeError, KeyError, ValueError, TypeError, OSError)`. Both log `exc_info=True`.
- **routing_bench.py** — same treatment for its `continue` (`json.loads`) and `pass` (`_parse_session_routing`) sites.
- **routing_quality_bench.py** — same treatment for its `continue` (`json.loads`) and `pass` (`_parse_events`) sites.
- **runner.py** — single `pass` site (`_mcp_call` MCP stdout JSONL line) → `(json.JSONDecodeError, ValueError)` with `logger.debug("mcp stdout parse skipped", exc_info=True)`.

Removed these 4 lines from `[tool.ruff.lint.per-file-ignores]`:
- `"src/benchmarks/swe/compact_bench.py" = ["BLE001"]`
- `"src/benchmarks/swe/routing_bench.py" = ["BLE001"]`
- `"src/benchmarks/swe/routing_quality_bench.py" = ["BLE001"]`
- `"src/benchmarks/tool_bench/runner.py" = ["BLE001"]`

(`compact_quality_bench.py`'s BLE001 ignore was correctly left intact — out of scope.)

### Task 2 — routing_replay_bench + report.py made observable (ignores retained)
- **routing_replay_bench.py** — added logger; narrowed the in-scope `_parse_tool_response`
  `pass` handler to `(json.JSONDecodeError, ValueError)` with `logger.debug(..., exc_info=True)`.
  Retains `["BLE001", "T201"]` (2 other broad handlers + T201 print debt remain).
- **report.py** — added logger; replaced the `print_enforcement_gap` front-matter regex parse
  `pass` with `logger.debug("enforcement-gap front-matter parse failed", exc_info=True)` plus a
  `# Best-effort` comment. Broad suppression kept intentionally (free-form front-matter).
  Retains `["BLE001", "T201"]` (3 other broad handlers + T201 print debt owned by Phase 24).

## Verification / Validations Run

| Check | Result |
|-------|--------|
| `uv run ruff check src --select BLE001` | ✅ exit 0 — All checks passed |
| `uv run ruff check src` (full) | ✅ exit 0 — All checks passed |
| Ruff check on all 6 touched source files | ✅ exit 0 |
| Pre-commit (black format + mypy type check) | ✅ passed |
| Task-1 files broad `except Exception` count | 0 in each of the 4 files |
| `getLogger(__name__)` present in all 6 files | ✅ 1 each |
| `exc_info=True` present | compact/routing/routing_quality = 2 each; runner/routing_replay/report = 1 each |
| 4 benchmark BLE001 ignore lines absent | ✅ ABSENT |
| routing_replay_bench + report.py ignores retained | ✅ both present (`["BLE001", "T201"]`) |
| Genuine tree-wide `except Exception:` + bare `pass` (precise) | 0 — all in-scope `pass` sites resolved |

### Tree-wide enumeration note
The plan's raw enumeration `grep -rn -A1 "except Exception" src --include='*.py' | grep -B1 "pass" | grep -c "except Exception"`
returns **2**, but both are **false positives** from the substring `pass` matching unrelated text:
- `src/atelier/gateway/cli/app.py:3648` — `except Exception as exc:` whose body is `raise click.ClickException("... pass --context-reduction-pct ...")` (the literal word "pass" is in the message string).
- `src/atelier/core/capabilities/context_compression/capability.py:195` — `except Exception as exc:` whose body is `_log.warning("Failed to archive sleeptime passages ...")` ("passages" contains "pass").

A precise enumeration of actual `except Exception:` blocks whose body is exactly `pass` returns **0**. All 28 in-scope `pass` sites plus the 3 in-scope `continue` sites are resolved across plans 01–03.

## Deviations from Plan

None affecting scope. One self-corrected editing slip during execution: the first
pyproject.toml edit briefly left `routing_quality_bench.py`'s ignore line in place; it was
removed in a follow-up edit before any commit. Final state is correct (4 lines removed,
`compact_quality_bench` retained).

## Threat Model Coverage

- **T-23-07 (DoS, parse loops)** — mitigated: handlers narrowed to realistic tuples
  (`KeyError`/`TypeError`/`OSError` still skipped per-record, not crashing).
- **T-23-08 (Repudiation, silent skips)** — mitigated: module loggers added; skipped
  records recorded at debug level with `exc_info=True`.

## Known Stubs

None.

## Self-Check: PASSED
- SUMMARY file created: `.planning/phases/23-silent-exception-audit/23-03-SUMMARY.md` ✅
- Source commit `f466ca9` exists ✅
- All 7 in-scope files modified and committed ✅
