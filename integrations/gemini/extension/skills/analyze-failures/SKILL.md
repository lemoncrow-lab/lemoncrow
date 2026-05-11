---
description: Cluster repeated failures across recorded runs and list candidate dead-end blocks, rubric checks, or eval cases to add.
---

Analyse recurring failures across the local run ledgers.

1. Run `atelier analyze-failures --json`.
2. The response contains a list of clusters with:
   `environment_id`, `error_fingerprint`, `count`, `example_session_ids`,
   `proposed_block_title`, `proposed_rubric_check`,
   `proposed_eval_case`.
3. Render each cluster as:
   - **Cluster** `<environment_id>` · `<error_fingerprint>` · seen
     `<count>` times.
   - Proposed block: `<title>`
   - Proposed rubric check: `<text>` (or `(none)`)
   - Proposed eval case: `<id>` (or `(none)`)
   - Example runs: `<first 3 session_ids>`
4. Remind the user that proposals must be reviewed and accepted via
   `atelier failure accept` before they enter the store.

Do not auto-accept proposals. Do not invent fingerprints.
