# Phase 19 Patterns — Expanded SCIP Registry and Lazy Indexing

## Existing patterns

- `binaries.py` handles operator env-var overrides and `shutil.which()` fallback discovery.
- `indexer.py` owns repo-local SCIP cache paths and artifact discovery.
- Tests for SCIP discovery live under `tests/infra/code_intel/scip/`.

## Implementation pattern

- Extend `languages.py` metadata only with bare fallback binary names.
- Add `ScipBinarySpec` in `binaries.py` with:
  - canonical language
  - explicit env var
  - fallback command
  - extra argv prefix/subcommands
  - output strategy
  - required context checks
- Keep `discover_scip_binary(language)` returning `Path | None`.
- Add a separate spec lookup for runner code.
- Add `ScipIndexer.index_language(language, timeout_seconds=...)`:
  - discover binary
  - verify required repo context
  - create cache root
  - build argv list
  - run subprocess without shell
  - normalize output into `cache_root / f"{language}.scip"`
  - return a structured result

## Test pattern

- Mock env and PATH discovery; do not require real SCIP binaries.
- Mock subprocess execution and create fake output files in the expected location.
- Parameterize env-var/fallback/argv tests by language.
- Assert missing context returns a skipped result and does not call subprocess.
