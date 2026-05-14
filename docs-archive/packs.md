# Knowledge Bundles and Seed Content

Older Atelier documentation referred to a public `atelier pack ...` workflow.
That is no longer the current top-level CLI surface.

## What Exists Today

Atelier currently ships knowledge content through three main paths:

- built-in seed ReasonBlocks loaded from `src/atelier/infra/seed_blocks/`
- built-in rubrics loaded from `src/atelier/core/rubrics/`
- internal domain bundle metadata exposed through `atelier domain list` and `atelier domain info`

`atelier init` is the public CLI entrypoint that creates the store and imports the
bundled seed blocks and rubrics into a fresh runtime root.

## What to Use Instead of `atelier pack ...`

For contributor-managed knowledge changes:

1. Author or edit the YAML artifacts described in the authoring docs.
2. Validate them by running `uv run atelier init` against a fresh store.
3. Run targeted tests, benchmarks, or evals if the change affects retrieval,
   routing, rescue, or savings claims.

Helpful references:

- [authoring/reasonblock-authoring.md](authoring/reasonblock-authoring.md)
- [authoring/rubric-authoring.md](authoring/rubric-authoring.md)
- [authoring/failure-cluster-authoring.md](authoring/failure-cluster-authoring.md)

## Benchmark Coverage

The current CLI still includes `atelier benchmark packs`, but that is a benchmark
coverage surface rather than a public install/manage workflow.

## Registry Status

There is no public registry and no supported community `pack install` surface on
the current CLI.
