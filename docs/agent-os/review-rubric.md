# Review Rubric

This document defines the adversarial review discipline Atelier agents apply
when reviewing code changes. The machine-enforced counterpart lives in
`src/atelier/core/rubrics/rubric_code_review.yaml` and
`src/atelier/core/rubrics/rubric_verification_ladder.yaml`. Use `verify` to
run those rubrics against your checklist before recording a review.

---

## Core principle

**Task completion ≠ goal achievement.**

A file can exist without being functional, wired, or data-connected. Treat
every submitted change as containing defects until the codebase proves
otherwise. A review that doesn't surface a finding is not automatically clean
— it may be a review that went soft.

---

## Verification ladder

Apply all four levels to every change before concluding the review:

| Level | Check | What to confirm |
| --- | --- | --- |
| 1 | **Existence** | Artifact or change is present on disk |
| 2 | **Substantive** | Not a stub, placeholder, or empty skeleton |
| 3 | **Wired** | Imported, registered, or reachable from real call sites |
| 4 | **Data flow** | Real data reaches it at runtime (skip if purely static) |

Source rubric: `rubric_verification_ladder`

---

## Severity discipline

Every finding must carry a severity label. Unlabelled findings are not valid
review output — re-run until every item is classified.

| Label | When to use |
| --- | --- |
| **Blocker** | Incorrect behavior, security vulnerability, or data-loss risk. Must be fixed before shipping. |
| **Warning** | Degrades quality, robustness, or maintainability. Should be fixed. |

**Every Blocker must include:**
- `file:line` — exact location, never "somewhere in the file"
- Concrete fix — a code snippet or unambiguous corrective step

Do not downgrade a Blocker to Warning to seem less harsh. If it is a
correctness or security issue, it is a Blocker.

---

## Adversarial stance — how reviews go soft

Watch for these failure modes:

- Stopping at the obvious surface issues (empty catch, stray log statement) and
  assuming the rest is sound.
- Accepting plausible-looking logic without tracing edge cases: nulls, empty
  collections, boundary values, concurrent callers.
- Treating "tests pass" or "linter clean" as evidence of correctness.
- Reading only the file under review without checking the functions it calls.
- Downgrading findings from Blocker to Warning to avoid seeming harsh.

---

## Quick-scan patterns (pre-read pass)

Run these before reading full file contents for a fast first pass:

```bash
# Hardcoded secrets
grep -n -E "(password|secret|api_key|token|apikey)\s*[=:]\s*['\"][^'\"]+['\"]" <file>

# Dangerous calls
grep -n -E "eval\(|exec\(|system\(|shell_exec|dangerouslySetInnerHTML" <file>

# Debug artifacts
grep -n -E "console\.log|debugger;|TODO|FIXME|XXX|HACK" <file>

# Empty catch blocks
grep -n -E "catch\s*\([^)]*\)\s*\{\s*\}" <file>

# Commented-out code
grep -n -E "^\s*//.*[{};]|^\s*#.*:" <file>
```

---

## Out of scope

Do not flag these unless they are also correctness issues:

- Style preferences (naming, formatting, indentation)
- Performance concerns that do not also risk incorrect behavior
- Code duplication that works correctly

---

## Status vocabulary

| Status | Meaning |
| --- | --- |
| `clean` | Files were reviewed; no issues found |
| `issues_found` | One or more Blockers or Warnings found |
| `skipped` | No reviewable source files in scope — review was not performed |

`skipped` ≠ `clean`. Do not conflate them.

---

## Calling `verify`

At the end of every review, call `verify` with `rubric_id: rubric_code_review`
and report which checks passed or failed:

```
verify(
  rubric_id="rubric_code_review",
  checks={
    "all_findings_severity_classified": True,
    "all_blockers_have_file_line": True,
    "all_blockers_have_fix_snippet": True,
    "verification_ladder_applied": True,
    "skipped_vs_clean_correctly_distinguished": True,
    "no_style_preferences_as_blockers": True,
    "cross_file_impact_assessed": True,
  }
)
```
