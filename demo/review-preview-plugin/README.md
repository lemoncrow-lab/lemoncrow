# lemoncrow-review-csv

A minimal third-party LemonCrow Review preview plugin. Changed CSV files become
`data.table` surfaces, and each Snapshot side is rendered as a bounded HTML
table that the Reader shows in a sandboxed frame, side by side in Compare.

It demonstrates the whole contract in one file,
[`src/lemoncrow_review_csv/__init__.py`](src/lemoncrow_review_csv/__init__.py):

- `CsvTableSurfaceProvider` — a `lemoncrow.review_surface_providers` plugin
  that attaches a version-1 `PreviewDescriptor` (`display: html`) to each
  changed CSV file matched by the configured globs;
- `CsvTableRenderer` — a `lemoncrow.review_preview_renderers` plugin that reads
  one side from the host-materialized workspace and writes `table.html` into
  `request.output_dir`.

## Install
## Install

Into the Python environment that runs the LemonCrow Review server:

```bash
TOOL_PYTHON="${LEMONCROW_TOOL_DIR:-$HOME/.lemoncrow/uv-tools}/lemoncrow/bin/python"
uv pip install --python "$TOOL_PYTHON" ./demo/review-preview-plugin
lc review --repo-root . --setup     # lists csv-table under installed preview renderers
```

From a LemonCrow source checkout, `make review-demo-plugin` installs the example
into both the checkout and local-server environments.
The provider is opt-in. In the reviewed repository's `.lemoncrow/review.yaml`:

```yaml
version: 1
surfaces:
  - id: tables
    provider: csv-table
    paths: ["data/*.csv"]
    preview:
      max_bytes: 2000000
```

See [Review preview plugins](../../docs/reference/review-preview-plugins.md) for
the full contract and how to add another platform.
