# Dogfooding

Atelier is dogfooded against itself and against the packaged baseline. This
document lists the current scenarios and the CLI surfaces used to exercise
them.

## Prerequisites

- Initialize a fresh store with `atelier init`
- Enable developer-only retrieval and rubric commands with `ATELIER_DEV_MODE=1`
- Run from the repository root when using `make verify`

## Verified Scenarios

### Scenario 1: Context Retrieval Surfaces Dead-End Guidance

```bash
ATELIER_DEV_MODE=1 atelier context \
  --task "Apply a live state change" \
  --domain state.change \
  --error "Resolving target from URL slug alone" \
  --json
```

Expected: the retrieved context includes the state-change guidance that rejects
slug-only workflows in favor of a canonical identifier.

### Scenario 2: Rubric Verification Passes for a Safe State Change

```bash
echo '&#123;
  "canonical_identifier_used": true,
  "pre_change_state_captured": true,
  "read_after_write_completed": true,
  "observed_state_matches_intent": true,
  "rollback_plan_available": true,
  "user_visible_surface_checked": true
&#125;' | ATELIER_DEV_MODE=1 atelier verify rubric_state_change_safety --json
```

Expected: `&#123;"status": "pass"&#125;`

### Scenario 3: Rubric Verification Blocks Missing Checks

```bash
echo '&#123;
  "canonical_identifier_used": true,
  "pre_change_state_captured": false
&#125;' | ATELIER_DEV_MODE=1 atelier verify rubric_state_change_safety --json
```

Expected: `&#123;"status": "blocked", "failed_checks": ["pre_change_state_captured", ...]&#125;`

### Scenario 4: Trace Recording

```bash
cat <<'JSON' | atelier trace record --input -
&#123;
  "agent": "claude-code",
  "domain": "state.change",
  "task": "Dogfood: Apply live change 123",
  "status": "success",
  "commands_run": ["resolve-target", "api.write", "api.read"],
  "errors_seen": [],
  "diff_summary": "Applied change using canonical identifier",
  "output_summary": "Read-after-write verification passed"
&#125;
JSON
```

Expected: the command prints a trace id and stores the observable record.

### Scenario 5: Extract a Candidate ReasonBlock from a Trace

```bash
TRACE_ID=$(atelier trace list --json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
atelier block extract "$TRACE_ID" --json
```

Expected: a candidate block with `confidence > 0` and at least one extraction
reason.

### Scenario 6: Rescue a Repeated Failure

```bash
ATELIER_DEV_MODE=1 atelier rescue \
  --task "Fix repeated pytest failure" \
  --error "AssertionError: expected 200 got 500" \
  --domain debugging \
  --json
```

Expected: rescue output contains a procedural sequence and any matched block ids.

### Scenario 7: Failure Cluster Review

```bash
atelier failure list --json
```

Expected: accepted and pending clusters are listed once enough traces exist to
form a pattern.

### Scenario 8: On-Demand Failure Analysis

```bash
ATELIER_DEV_MODE=1 atelier analyze-failures --since 7d --json
```

Expected: one or more clusters with fingerprints and suggested procedural
guidance fields.

### Scenario 9: Runtime Benchmark Harness

```bash
atelier benchmark run --json
```

Expected: a current benchmark report is written and emitted with runtime
metrics.

### Scenario 10: Host and Bundle Coverage Benchmark

```bash
atelier benchmark packs --json
```

Expected: the report includes domain coverage and any pack validation failures
for the installed seed/bundle set.

## Running the Full Dogfood Suite

```bash
cd atelier && make verify
```

The pytest suite in `tests/test_golden_fixtures.py` overlaps with the scenarios
above and acts as the executable regression check for dogfooding changes.

## `rubric_state_change_safety` Checks Reference

| Check                           | Description                                        |
| ------------------------------- | -------------------------------------------------- |
| `canonical_identifier_used`     | Target identified by a stable canonical identifier |
| `pre_change_state_captured`     | State snapshot taken before the change             |
| `read_after_write_completed`    | Authoritative readback executed                    |
| `observed_state_matches_intent` | Observed state matches intended state              |

## Dogfood Results Log

See `AGENT_README.md` for the latest pass/fail scorecard.
