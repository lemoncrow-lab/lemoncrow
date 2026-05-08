# Rubric Authoring

Rubrics define the safety and quality gates that agents must pass before or after executing a task.

## Creating a Rubric

Rubrics use a simple boolean assertion model:

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
```

## Integrating with the Runtime

When an agent attempts a task associated with this rubric, Atelier forces the agent to explicitly prove how it satisfied each `required: true` check. If it fails, the execution trace is marked as a failure, and a rescue procedure may be triggered.

## Bundling Rubrics in a Pack

```bash
atelier pack create my-rubric-pack --type rubrics
# Add your .yaml rubric files under my-rubric-pack/rubrics/
atelier pack validate ./my-rubric-pack
atelier pack install ./my-rubric-pack
```

Rubrics are installed and used locally. No external distribution infrastructure is required.
