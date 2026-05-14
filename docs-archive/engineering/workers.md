# Workers

Workers handle background processing tasks for Atelier — primarily failure analysis, eval running, and periodic summarization.

## Starting Workers

```bash
cd atelier && make worker
# or
atelier worker start
```

## What Workers Do

Workers are optional. Core foreground operations such as context lookup,
trace recording, and rubric verification are synchronous and do not require
workers.

Workers process:

- **Failure clustering**: periodically re-clusters failure traces to surface patterns
- **Eval execution**: runs eval cases in the background when triggered
- **Savings summarization**: computes token and call savings metrics

## Worker Configuration

Workers use the same environment variables as the rest of Atelier (see [docs/installation.md](../installation.md)).

Workers require a running store (SQLite or PostgreSQL). For PostgreSQL, workers can run concurrently on multiple machines without coordination.

## Without Workers

If workers are not running:

- Failure clustering is done on-demand via `atelier failure list` or `ATELIER_DEV_MODE=1 atelier analyze-failures`
- Benchmark and replay-style evaluation runs are triggered manually from the `atelier benchmark ...` surfaces
- Savings metrics are computed at query time via `atelier savings`

For most development workflows, workers are not needed.
