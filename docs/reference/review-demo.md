# Reproduce the LemonCrow Review demo

This is the short version: create the prepared demo repository and open the same Review surface shown on the website and in the README.

From a LemonCrow source checkout:

```bash
./demo/review-surfaces/bootstrap.sh /tmp/lemoncrow-review-demo
uv run lc review --repo-root /tmp/lemoncrow-review-demo --setup
uv run lc review --repo-root /tmp/lemoncrow-review-demo --open
```

If your local `lc` command already points at this checkout, you can use `lc` instead of `uv run lc`.

## What to try

In the browser Review Reader:

1. Open a frontend change such as `frontend/src/main.js`.
2. Start on **Diff**.
3. Switch to **Preview** to inspect the rendered UI.
4. Switch to **Compare** to see the before/after rendering.
5. Open `media/demo.gif` (or the MP4, WAV, or PDF) and switch through **Preview / Compare / Source**.
6. Open the evidence pane to inspect impact, verification, and provenance when available.

The demo also contains Markdown, API, Docker Compose, and command-backed review surfaces if you want to explore further.

Reset the demo at any time by running the bootstrap command again.

For the complete Review workflow and CLI behavior, see the [full review walkthrough](review-walkthrough.md).
