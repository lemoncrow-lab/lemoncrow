# Failure Cluster Authoring

Failure clusters are the normalized records Atelier derives from repeated failed
traces. They are validated against the `FailureCluster` model in
`src/atelier/core/foundation/models.py`.

## Current File Format

If you need to author or fixture one manually, match the current schema:

```yaml
id: cluster-react-stale-closure
domain: frontend.react
fingerprint: warning-maximum-update-depth-exceeded
trace_ids: []
sample_errors:
  - Warning: Maximum update depth exceeded
suggested_block_title: Stabilize effect dependencies before state updates
suggested_rubric_check: no_render_loop_after_state_change
suggested_eval_case: detect-react-render-loop
suggested_prompt: Check effect dependencies and state-update feedback loops before retrying.
severity: high
```

## How Clusters Are Normally Produced

The normal workflow is trace-driven rather than hand-installed:

```bash
uv run atelier analyze-failures --json
uv run atelier failure list --json
uv run atelier eval-from-cluster <cluster-id> --json
```

Use manual YAML fixtures only for contributor tests, migrations, or explicitly
curated source data.

## Guidance

- Make the `fingerprint` stable and specific enough to group the same failure
  mode without collapsing unrelated errors.
- Use `sample_errors` as observable evidence, not hidden reasoning.
- Keep `suggested_block_title`, `suggested_rubric_check`, and
  `suggested_eval_case` aligned with real remediation paths.

## Important Note

Older docs referred to `atelier pack install` for failure clusters. That is not
the current public workflow.
