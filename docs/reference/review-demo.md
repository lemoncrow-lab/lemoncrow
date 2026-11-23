# Reproduce the LemonCrow Review demo

This is the short version: create the prepared demo repository and open the same Review surface shown on the website and in the README.

From a LemonCrow source checkout:

```bash
./examples/demo/bootstrap.sh /tmp/lemoncrow-review-demo
uv run lc review --repo-root /tmp/lemoncrow-review-demo --setup
uv run lc review --repo-root /tmp/lemoncrow-review-demo --open
```

If your local `lc` command already points at this checkout, you can use `lc` instead of `uv run lc`.

The same steps as `make` targets (all accept `DEMO_DIR=/path`, default `/tmp/lemoncrow-review-demo`):

```bash
make review-demo          # create or reset the demo repository and open it in the Reader
make review-demo SCENARIO=orders   # pick a scenario: standard (default), empty, orders, orders-revision-2, large, tenant-cache
make review-demo-diff     # show the prepared, uncommitted demo change
make review-demo-empty    # the empty state: baseline only, nothing to review (examples/empty)
make review-demo-plugin   # install the example CSV preview plugin
```

## What to try

In the browser Review Reader:

1. Open the web surface: only the 3 changed pages (`/pricing`, `/surfaces`, `/`) are in its picker, ranked by how much each changed; the unchanged `/team` and `/settings` are not listed.
2. Start on **Diff**.
3. Switch to **Preview** to inspect the rendered UI.
4. Switch to **Compare** to see the before/after rendering.
5. Open `media/demo.gif` (or the MP4, WAV, or PDF) and switch through **Preview / Compare / Source**.
6. Open the Storybook surface for `frontend/src/components/SurfaceCard.jsx` and compare the old and new story screenshots; the added `Components` story explains that it does not exist on the old side. The API surface likewise lists only the 3 changed requests of 6, ranked by response difference.
7. Open the evidence pane to inspect impact, verification, and provenance when available.

To see a third-party preview plugin, install the example CSV plugin and reopen the Review; `data/plans.csv` then gets an old/new table preview:

```bash
make review-demo-plugin
```

That target installs the plugin into both the source checkout and the local Review
server environment. Without it, setup names the missing `csv-table` provider and
the CSV stays reviewable as source. See [Review preview plugins](review-preview-plugins.md).
The demo also contains Markdown, API, Docker Compose, and command-backed review surfaces if you want to explore further.

Reset the demo at any time by running the bootstrap command again.

For the complete Review workflow and CLI behavior, see the [full review walkthrough](review-walkthrough.md).
