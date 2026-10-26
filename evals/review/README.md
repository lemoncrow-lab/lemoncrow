# Review evaluation corpus, Gate I and Gate H

The Review evaluation corpus, its recorded fixtures and the Gate I baseline, plus the Gate H behavior-evidence corpus, results and baseline (`gate-h.yaml`, `gate-h/`). See [docs/reference/review-evaluation.md](../../docs/reference/review-evaluation.md).

```bash
uv run lc eval review run       # measure every selection, offline from fixtures
uv run lc eval review gate      # compare with baseline.json (report mode until thresholds are approved)
uv run lc eval review gate-h run      # Gate H: flake runs, labeled-case accuracy, double renders
uv run lc eval review gate-h report   # Gate H qualification vs gate-h/baseline.json
```

Every case in `corpus/builtin` is synthetic, Apache-2.0, and states its provenance in its own file. Never add customer Reviews here: organization corpora live outside this checkout and need an explicit opt-in.

`real_prs.yaml` is the real-pull-request benchmark corpus (public pull requests pinned by commit, labels only where a later fix commit proves the bug) and `reports/` is where `lc dev eval review benchmark` writes its reports. See [docs/reference/review-benchmark.md](../../docs/reference/review-benchmark.md).
