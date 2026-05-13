# Dogfooding

Atelier is dogfooded against itself and against the packaged generic baseline. This document describes the verified scenarios and how to run them.

## Verified Scenarios

### Scenario 1: Dead-End Plan Detection (State Change)

**Setup:** Agent proposes a plan using a URL slug (a known dead end) instead of a canonical identifier.

**Test:**

```bash
atelier check-plan \
  --task "Apply a live state change" \
  --domain state.change \
  --step "Resolve target from URL slug alone" \
  --step "Apply the change" \
    --json
```

**Expected:**

```json
&#123;
  "status": "blocked",
  "exit": 2,
  "warnings": [
    "dead end: resolve target from url slug alone"
  ]
&#125;
```

### Scenario 2: Canonical-Identifier Plan Passes

**Test:**

```bash
atelier check-plan \
  --task "Apply a live state change" \
  --domain state.change \
  --step "Resolve and record the canonical identifier" \
  --step "Capture pre-change state" \
  --step "Apply the change" \
  --step "Read back the state and diff against intent" \
    --json
```

**Expected:** `&#123;"status": "pass", "exit": 0&#125;`

### Scenario 3: Rubric Gate — Full Pass

**Test:**

```bash
echo '&#123;
  "canonical_identifier_used": true,
  "pre_change_state_captured": true,
  "read_after_write_completed": true,
  "observed_state_matches_intent": true,
  "rollback_plan_available": true,
  "user_visible_surface_checked": true
&#125;' | atelier run-rubric rubric_state_change_safety --json
```

**Expected:** `&#123;"status": "pass"&#125;`

### Scenario 4: Rubric Gate — Blocked (Missing Checks)

**Test:**

```bash
echo '&#123;
  "canonical_identifier_used": true,
  "pre_change_state_captured": false
&#125;' | atelier run-rubric rubric_state_change_safety --json
```

**Expected:** `&#123;"status": "blocked", "failed_checks": ["pre_change_state_captured", ...]&#125;`

### Scenario 5: Trace Record

```bash
echo '&#123;
  "agent": "claude-code",
  "domain": "state.change",
  "task": "Dogfood: Apply live change 123",
  "status": "success",
  "commands_run": ["resolve-target", "api.write", "api.read"],
  "errors_seen": [],
  "diff_summary": "Applied change using canonical identifier",
  "output_summary": "Read-after-write verification passed"
&#125;' | atelier record-trace --json
```

**Expected:** `&#123;"id": "trace_<hash>"&#125;` with exit 0.

### Scenario 6: Extract Block from Trace

```bash
TRACE_ID=$(atelier trace list --json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
atelier extract-block "$TRACE_ID" --json
```

**Expected:** A candidate block with `confidence > 0` and `reasons` list.

### Scenario 7: Repeated Pytest Loop Rescue

**Task:** failing pytest loop, same signature repeated.

```bash
atelier rescue \
  --task "Fix repeated pytest failure" \
  --error "AssertionError: expected 200 got 500" \
  --domain "debugging" \
  --json
```

**Expected:** rescue output contains a procedural sequence and matched block IDs.

### Scenario 8: Failure Cluster Analysis → ReasonBlock Proposal

```bash
curl -s -X POST http://127.0.0.1:8787/v1/failures/analyze \
  -H "Authorization: Bearer $&#123;ATELIER_API_KEY&#125;" \
  -H "Content-Type: application/json" \
  -d '&#123;"limit": 100&#125;'
```

**Expected:** one or more clusters with fingerprints and suggested procedural guidance fields.

### Scenario 9: Eval Generated from Cluster

```bash
LOCAL=1 uv run python -m pytest tests/test_swe_benchmark_harness.py -q
```

**Expected:** benchmark/eval harness generates valid run + evaluation artifacts for configured cases.

### Scenario 10: Pack Install + Benchmark

```bash
atelier --root .atelier pack install src/atelier/packs/official/atelier-pack-coding-general --json
atelier --root .atelier benchmark packs --json
```

**Expected:** install succeeds and benchmark reports baseline vs host+core vs host+core+pack metrics.

## Running the Full Dogfood Suite

```bash
cd atelier && make verify
# 209 passed, 9 skipped — skips are Postgres-gated and expected
```

The pytest suite in `tests/test_golden_fixtures.py` covers all the above scenarios programmatically.

## `rubric_state_change_safety` Checks Reference

The required checks for the state-change safety rubric (as of last dogfood):

| Check                           | Description                                        |
| ------------------------------- | -------------------------------------------------- |
| `canonical_identifier_used`     | Target identified by a stable canonical identifier |
| `pre_change_state_captured`     | State snapshot taken before the change             |
| `read_after_write_completed`    | Authoritative readback executed                    |
| `observed_state_matches_intent` | Observed state matches intended state              |

## Dogfood Results Log

See `AGENT_README.md` → Dogfooding Scorecard section for the latest pass/fail record.
