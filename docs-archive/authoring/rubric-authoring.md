# Rubric Authoring

Rubrics define the explicit checks Atelier evaluates before accepting a result.
They are YAML documents validated against the `Rubric` model in
`src/atelier/core/foundation/models.py`.

## Current File Format

Built-in rubrics live under `src/atelier/core/rubrics/` and look like this:

```yaml
id: rubric_state_change_safety
domain: state.change
required_checks:
  - canonical_identifier_used
  - pre_change_state_captured
  - read_after_write_completed
  - observed_state_matches_intent
block_if_missing:
  - canonical_identifier_used
  - read_after_write_completed
  - observed_state_matches_intent
warning_checks:
  - rollback_plan_available
  - user_visible_surface_checked
escalation_conditions:
  - target_identity_ambiguous
  - live_system_drift_detected
  - rollback_failed
related_blocks:
  - canonical-identifier-over-display-name
  - read-after-write-verification
```

## Field Guide

- Required: `id`, `domain`
- Optional routing hints: `triggers`, `related_blocks`
- Optional content filters: `forbidden_phrases`
- Gate definitions: `required_checks`, `block_if_missing`, `warning_checks`, `escalation_conditions`

## Current Contributor Workflow

1. Add or edit a YAML file under `src/atelier/core/rubrics/`.
1. Validate a clean import:

```bash
ATELIER_ROOT=/tmp/atelier-docs-check uv run atelier init
```

1. Exercise the rubric with the live CLI:

```bash
echo '{
  "canonical_identifier_used": true,
  "pre_change_state_captured": true,
  "read_after_write_completed": true,
  "observed_state_matches_intent": true
}' | ATELIER_DEV_MODE=1 uv run atelier verify rubric_state_change_safety
```

1. Add targeted tests when the rubric changes a safety-critical contract.

## Important Note

Older docs referred to `atelier pack create/install` for rubrics. That is not
the current public CLI workflow.
