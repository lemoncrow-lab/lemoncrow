# Empty Review state

The first-run, nothing-to-review state: the billing app's baseline is committed, there is no change on top of it, and no Review has been captured. Use it to see what LemonCrow Review shows before any work exists.

This is a thin entry. It carries no copy of the app; the full billing app and every scenario live in [`../demo`](../demo).

From the LemonCrow checkout:

```bash
./examples/empty/bootstrap.sh /tmp/lemoncrow-review-empty
uv run lc review --repo-root /tmp/lemoncrow-review-empty --open
```

Or `make review-demo-empty [DEMO_DIR=/path]`. The script works from any working directory and forwards extra arguments to `../demo/bootstrap.sh` (it is the same as `../demo/bootstrap.sh <dir> --scenario empty`).
