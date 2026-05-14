---
id: WP-47
title: Rebuild `LessonPromoter` clustering on real embeddings + Reflexion-style lesson body
phase: I
boundary: Atelier-core
owner_agent: atelier:code
depends_on: [WP-33, WP-36]
supersedes: [WP-15]
status: done
---

# WP-47 — Lesson promoter on real embeddings + real lesson bodies

## Why

V2 `LessonPromoter` ingests failed traces, embeds them, clusters them, and proposes
ReasonBlock additions. The pipeline shape is good. Two things are broken:

1. **Clustering signal:** V2 embeds via `stub_embedding` (SHA-256 feature hash), so "semantic
   clustering" is actually string-prefix-fingerprint clustering with hash collisions. The V2
   acceptance test "precision ≥ 0.7 on 200-trace fixture" was filed but never met.
2. **Lesson body quality:** even when clusters form correctly, the lesson body is a
   mechanical concatenation of raw `errors_seen + diff_summary`. It is not procedural
   knowledge; a reviewer has to rewrite it from scratch.

WP-47 fixes both:

- **Clustering** runs on real `Embedder.embed` vectors (now possible after WP-33 deletes
  `stub_embedding`).
- **Lesson body** is generated via a Reflexion-style step (Shinn et al. 2023, Voyager 2023):
  after a cluster forms, an Ollama call drafts a one-paragraph procedural reflection from the
  cluster's traces. The reflection is the candidate's body. Human review gate is unchanged.

WP-47 also adds **Iranti's refinement-pass retry**: when `memory_recall(query)` returns
empty, retry once with a widened query before returning empty. Tiny addition; large impact on
recall miss rate.

## Files touched

### Clustering on real embeddings

- **EDIT:** `src/atelier/core/capabilities/lesson_promotion/capability.py`:
  - Replace `_fingerprint = errors_seen[0][:160]` clustering with cosine-similarity
    clustering on `Embedder.embed(commands_run + errors_seen + diff_summary)`.
  - Cluster threshold: `cosine ≥ 0.85` (config-tunable).
  - Minimum cluster size before promotion: 3 (unchanged from V2).
- **EDIT or NEW:** `src/atelier/core/capabilities/lesson_promotion/clustering.py` —
  extract clustering logic into its own module if not already factored.
- **NEW:** `src/atelier/cli/reembed.py` — `atelier reembed` subcommand:
  - Reads rows from `lesson_candidate` and `archival_passage` where
    `embedding_provenance = 'legacy_stub'`.
  - Calls the configured `Embedder` to compute fresh vectors.
  - Updates rows in batch; clears the legacy flag.
  - Idempotent — safe to re-run.

### Reflexion-style lesson body

- **NEW:** `src/atelier/core/capabilities/lesson_promotion/reflection.py`:
  - `draft_lesson_body(cluster_traces) -> str` that:
    - Composes a stable prompt: _"From these failed attempts and the eventual fix, write a
      one-paragraph procedural reflection: what was the dead-end, what worked, when does
      this apply?"_
    - Calls `internal_llm.ollama_client.summarize` with the cluster's traces.
    - Returns the reflection paragraph.
  - On `OllamaUnavailable`, falls back to the V2 mechanical concatenation (so the lesson
    pipeline still produces _something_, just lower quality). Logs at INFO.
- **EDIT:** `src/atelier/core/capabilities/lesson_promotion/capability.py` — when a cluster
  reaches threshold, call `draft_lesson_body` and store the result as
  `LessonCandidate.body`. Keep the raw cluster traces in `LessonCandidate.evidence` so
  reviewers can audit.

### Refinement-pass retry on empty recall

- **EDIT:** `src/atelier/core/capabilities/archival_recall/...` — when the first pass of
  `memory_recall(query)` returns 0 rows, retry once with:
  - Lowercase + remove punctuation.
  - Drop quoted strings (often noisy in coding errors).
  - Loosen any boolean AND filters to OR.
  - Truncate query to top-3 terms by IDF if available.
    Return whatever the second pass produces. If still empty, return empty.
- Telemetry: record `recall_pass=1|2` and `recall_widened_hits` so we can measure how often
  the retry actually rescues a query.

### Tests + fixture

- **NEW:** `tests/infra/test_lesson_promotion_precision.py` — V2 acceptance test that was
  filed but unmet:
  - Loads a 200-trace fixture (synthetic but representative).
  - Runs the promoter.
  - Asserts `precision ≥ 0.7` (precision = correct_promotions / total_promotions).
- **NEW:** `tests/core/test_lesson_body_reflection.py` — with a mocked Ollama:
  - Asserts `draft_lesson_body` calls Ollama once with the fixed prompt template.
  - Asserts the candidate body is the returned reflection (not raw concatenation).
  - Asserts fallback path on `OllamaUnavailable` produces the V2 concatenation.
- **NEW:** `tests/infra/test_reembed_idempotent.py` — running `atelier reembed` twice
  produces the same result; legacy rows are flagged once and not re-flagged.
- **NEW:** `tests/infra/test_recall_refinement_retry.py` — empty first pass triggers second
  pass with widened query; matches expected hits; non-empty first pass does NOT retry.
- **NEW:** `benchmarks/lesson_promoter/200_trace_fixture.yaml` — labelled fixture.
  Synthetic traces; labels indicate which clusters _should_ form a lesson.

## How to execute

1. **Read the V2 promoter code.** Identify the V2 fingerprint + cluster paths. Note every
   place `stub_embedding` was used — those become `Embedder.embed`.

2. **Rebuild clustering.** Use a simple agglomerative approach:
   - Embed each new candidate.
   - Compute cosine to centroids of existing open clusters.
   - If max cosine ≥ threshold: assign to that cluster.
   - Else: open a new cluster.
   - Promote when cluster size ≥ 3.
     Intentionally simple — V3 is not the place to introduce a new clustering algorithm.

3. **Add Reflexion step.** Goes between "cluster reaches threshold" and "candidate written
   to DB". Failing Ollama is non-fatal; promoter logs and falls back.

4. **Build the fixture honestly.** 200 traces, labelled. Half are bug-fix-on-shopify-publish
   (should cluster). A quarter are pdp-schema variants (should cluster). The rest are noise.
   The fixture is committed to the repo and used in CI.

5. **Implement `atelier reembed`.** Required because legacy rows have unusable
   `legacy_stub` vectors. Without back-fill, the clustering can't see V2 history.

6. **Add refinement-pass retry** to `memory_recall`. Keep it simple — one retry, widened
   query, no recursion.

7. **Hit the precision target.** If the simple clustering doesn't hit `0.7`, tune the
   embedding text composition before reaching for fancier algorithms.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv run pytest tests/infra/test_lesson_promotion_precision.py \
                     tests/core/test_lesson_body_reflection.py \
                     tests/infra/test_reembed_idempotent.py \
                     tests/infra/test_recall_refinement_retry.py -v

# Manual smoke: re-embed a small dataset.
ATELIER_LOCAL_DB=/tmp/test.db LOCAL=1 atelier reembed --dry-run
ATELIER_LOCAL_DB=/tmp/test.db LOCAL=1 atelier reembed

make verify
```

## Definition of done

- [ ] `LessonPromoter` clusters via `Embedder.embed` (no fingerprint string-prefix path).
- [ ] `draft_lesson_body` calls Ollama via `internal_llm.ollama_client`; falls back to V2
      concatenation on `OllamaUnavailable`.
- [ ] `atelier reembed` back-fills legacy rows; idempotent; clears the `legacy_stub` flag.
- [ ] 200-trace fixture committed; precision test passes at ≥ 0.7.
- [ ] `memory_recall` retries with widened query on empty first pass; telemetry records
      `recall_pass` and `recall_widened_hits`.
- [ ] No fingerprint-style clustering remains in `lesson_promotion/`.
- [ ] `make verify` green.
- [ ] V3 INDEX updated. Trace recorded.
