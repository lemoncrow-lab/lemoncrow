---
phase: 08
slug: context-lineage
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2025-07-15
---

# Phase 08 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (existing) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/infra/code_intel/git_history/ -q` |
| **Full suite command** | `uv run pytest tests/infra/code_intel/git_history/ tests/core/test_code_context.py -q` |
| **Estimated runtime** | ~30 seconds (fast path, no bootstrap) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/infra/code_intel/git_history/ -q`
- **After every plan wave:** Run `uv run pytest tests/infra/code_intel/git_history/ tests/core/test_code_context.py -q && make lint && make typecheck`
- **Before `/gsd-verify-work`:** Full suite must be green + CQEVAL-02 benchmark passes
- **Max feedback latency:** ~30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 08-01a-01 | 01a | 1 | LINEAGE-01 | — | N/A | unit | `uv run python -c "from atelier.infra.code_intel.git_history.models import CommitRecord, CommitSummary, CommitChunk; print('OK')"` | ❌ W0 | ⬜ pending |
| 08-01a-02 | 01a | 1 | LINEAGE-01 | — | N/A | unit | `uv run pytest tests/infra/code_intel/git_history/test_walker_resume.py -q` | ❌ W0 | ⬜ pending |
| 08-01a-03 | 01a | 1 | LINEAGE-01/05 | T-05 prompt injection | Commit msg XML-encapsulated; truncated to 500 chars | unit | `uv run pytest tests/infra/code_intel/git_history/test_summarizer.py -q` | ❌ W0 | ⬜ pending |
| 08-01a-04 | 01a | 2 | LINEAGE-01 | — | N/A | unit | `uv run pytest tests/infra/code_intel/git_history/test_embedder.py -q` | ❌ W0 | ⬜ pending |
| 08-01b-01 | 01b | 2 | LINEAGE-01/02 | — | N/A | integration | `uv run pytest tests/infra/code_intel/git_history/test_walker_resume.py -q` | ❌ W0 | ⬜ pending |
| 08-01b-02 | 01b | 2 | LINEAGE-03/04/06 | — | provenance preserved end-to-end | unit | `uv run pytest tests/infra/code_intel/git_history/test_search_merge.py -q` | ❌ W0 | ⬜ pending |
| 08-01b-03 | 01b | 3 | LINEAGE-01/02 | — | fail-open; no crash on missing DB | unit | `uv run python integrations/claude/plugin/hooks/post_commit.py; echo "Exit: $?"` | ❌ W0 | ⬜ pending |
| 08-01b-04 | 01b | 3 | LINEAGE-01..06 | — | all behaviors green | unit+integration | `uv run pytest tests/infra/code_intel/git_history/ -q` | ❌ W0 | ⬜ pending |
| 08-02-01 | 02 | 4 | CQEVAL-01 | — | N/A | structure | `ls tests/benchmarks/context_quality/` | ❌ W0 | ⬜ pending |
| 08-02-02 | 02 | 4 | CQEVAL-02 | — | N/A | benchmark | `uv run pytest tests/benchmarks/context_quality/M1_lineage.py -q -m slow` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/infra/code_intel/git_history/test_summarizer.py` — stubs for LINEAGE-01, LINEAGE-05
- [ ] `tests/infra/code_intel/git_history/test_embedder.py` — stubs for LINEAGE-01
- [ ] `tests/infra/code_intel/git_history/test_search_merge.py` — stubs for LINEAGE-03, LINEAGE-04, LINEAGE-06
- [ ] `tests/infra/code_intel/git_history/test_walker_resume.py` — stubs for LINEAGE-01, LINEAGE-02
- [ ] `tests/benchmarks/context_quality/README.md` — covers CQEVAL-01
- [ ] `tests/benchmarks/context_quality/M1_lineage.py` — covers CQEVAL-02

All test files are created as part of PLAN-01 Task 8 and PLAN-02 Task 1/2.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Bootstrap ingests ≥100 commits in daemon thread without blocking search | LINEAGE-01/02 | Requires live LLM backend + 4-5 min runtime | Set `ATELIER_LLM_BACKEND=openai`, run `uv run atelier code index`, then verify `commit_chunks` row count ≥100 |
| M1 benchmark scores ≥7/10 on real repo | CQEVAL-02 | Requires bootstrapped DB on Atelier repo | `ATELIER_REPO_ROOT=$(pwd) uv run pytest tests/benchmarks/context_quality/M1_lineage.py -v -m slow -s` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
