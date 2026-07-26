# Task: implement the `deadline` feature in `jobrunner`

`jobrunner` is a small Python library for running an ordered pipeline of
"steps" against a shared context. The baseline behavior (running steps in
order, storing outputs, retries, failure handling, reporting) already works and
is covered by tests.

A cross-cutting **deadline** feature has been started but is only half done: the
public exception type exists and the context carries a `deadline` field, but the
actual enforcement is missing. Several tests describe the intended behavior and
currently fail.

Your job: implement the deadline feature end-to-end so that the **entire test
suite passes**. Running

```
uv run pytest -q
```

must exit `0`. The existing baseline tests must keep passing — do not weaken or
delete tests to make them green.

## Required semantics

- **Injectable clock.** Time comes from a zero-argument callable that returns a
  float. It is injectable so behavior is deterministic and nothing ever sleeps
  (in the library or the tests). The default clock is `time.monotonic`.

- **Expiry check happens before each step.** Before running a step, check
  whether the deadline has passed. A step already chosen to run is never
  interrupted mid-flight; only not-yet-started steps are affected.

- **Inclusive boundary.** The deadline is reached when `clock() >= deadline`
  (equal counts as expired).

- **Skipping.** When the deadline has passed before a step, that step and every
  step after it are recorded as `skipped`, in order. The run then stops. The
  report must mark that a deadline was exceeded and record which step tripped it
  (the first step that was not run). Skipped steps must not execute, so their
  outputs must not appear in the context.

- **`deadline` vs `timeout` resolution.** A caller may specify an absolute
  `deadline` or a relative `timeout`. If `deadline` is given it is used as-is.
  Otherwise, if `timeout` is given, the effective deadline is
  `started_at + timeout`. If neither is given there is no deadline and **every**
  step runs (a pure regression of the baseline behavior), with the report
  indicating no deadline was exceeded.

- **Strict mode.** The high-level entry point accepts a `strict` flag. When
  `strict=True` and the run tripped a deadline, raise `DeadlineExceeded`
  carrying the tripped step's name (plus the elapsed time and the deadline).
  When `strict=False` (the default), return the report with its
  deadline-exceeded flag set instead of raising.

- **Context helpers.** The context must be able to report the time remaining
  until the deadline (or that there is none) and whether it has expired, both
  driven by the injected clock and respecting the inclusive boundary above.

- **Summary.** When a run trips a deadline, the human-readable report summary
  must make that visible (mention the deadline and the tripped step).

## Rules

- Discover which files need changing yourself.
- Do not add real sleeping anywhere; keep time injectable.
- Keep the public API intact: `run_pipeline`, `Pipeline`, `Step`, `Context`,
  `Report`, `StepRecord`, `JobError`, `StepFailed`, `DeadlineExceeded` must all
  remain importable from the `jobrunner` package.
- Success is judged solely by `uv run pytest -q` exiting `0`.
