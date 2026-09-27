# Review preview plugins

LemonCrow Review can show more than source diffs: a changed web route, an API
response, a Compose service, a Storybook story. This page is for people who want
to add a new kind of preview — a component explorer, a diagram, a data file, a
mobile flow — without changing LemonCrow or the Reader.

Review surfaces use four independent extension points:

| Extension point | Answers | Entry point group |
| --- | --- | --- |
| Surface provider | *What* can be reviewed for this change | `lemoncrow.review_surface_providers` |
| Runner | *How* a revision executes (Docker, command, HTTP service) | `lemoncrow.review_runners` |
| Preview renderer | *How* old/new Snapshot artifacts are produced | `lemoncrow.review_preview_renderers` |
| Surface comparator | *How different* old/new artifacts or safe run results are | `lemoncrow.review_surface_comparators` |

A provider attaches a versioned **preview descriptor** to a surface. The Reader
presents the resulting artifact from the descriptor's `display` alone, so a new
platform needs a provider and a renderer — no Reader change, no LemonCrow
release. Surfaces with no descriptor and a kind the Reader does not know still
render, as a generic card with the surface's facts, affected files, metadata and
(when bound to a runtime) Run/Compare actions.

The provider and runner interfaces remain compatible; existing plugins keep working. Two-sided preview plugins are automatically rankable without adding a comparator: `image` uses deterministic pixel delta, `json` canonical JSON delta, `text`/`html` normalized text delta, and `download` a binary hash change. A plugin only needs a custom comparator when those host defaults are not meaningful for its domain.

The Reader is change-focused: surfaces with no `affected_paths` are not shown in affected-surface pickers. A comparison failure never hides the surface; it remains visible and unranked.

## Contract version 1

### Preview descriptor

Returned by a provider on `ReviewSurface(preview=...)`:

```python
from lemoncrow.pro.capabilities.review.surfaces import PreviewDescriptor

PreviewDescriptor(
    renderer="csv-table",      # renderer_id that produces the artifacts
    display="html",            # image | html | text | json | download
    media_type="text/html",    # expected artifact type
    label="CSV table",         # shown in the Reader
    sides=("old", "new"),      # which Snapshot sides exist for this preview
    scope="data",              # repository subtree the host materializes
    options={"path": "data/prices.csv"},  # opaque, renderer-owned JSON
    version=1,
)
```

| `display` | Allowed media types | Reader presentation |
| --- | --- | --- |
| `image` | `image/png`, `image/jpeg`, `image/gif`, `image/webp` | Image; Compare uses the before/after overlay |
| `html` | `text/html` | `<iframe sandbox="">` (no scripts, forms, navigation or same-origin access) |
| `text` | `text/plain`, `text/markdown`, `text/csv`, `text/x-diff` | Preformatted text, side by side in Compare |
| `json` | `application/json` | Pretty-printed JSON, side by side in Compare |
| `download` | any | Download buttons per side |

SVG is not an image here: it can carry script. Render SVG to PNG, or offer it as
`download`.

### Comparison and ranking

For a normal two-sided preview, do nothing. LemonCrow derives the comparator from `display` and exposes one normalized impact score (`changed`, `magnitude` from 0 to 1, a human label, reasons, and comparator-specific metrics). The Reader sorts comparable affected surfaces by that magnitude.

Executable surfaces are different because running them may have side effects. To permit automatic old/new execution, the provider must explicitly declare both the `auto_compare` capability and a comparison descriptor:

```python
from lemoncrow.pro.capabilities.review.surfaces import ComparisonDescriptor, ReviewSurface

ReviewSurface(
    ...,
    capabilities=("source", "execute", "results", "compare", "auto_compare"),
    comparison=ComparisonDescriptor(
        source="run",
        comparator="run-result",   # or a custom comparator id
        label="CLI output difference",
        auto=True,
    ),
)
```

Without that opt-in LemonCrow never executes the runner merely to rank the surface. The surface still appears and manual Run/Compare remains available.

A domain-specific comparator is optional:

```python
from lemoncrow.pro.capabilities.review.comparisons import ImpactScore

class SchemaComparator:
    comparator_id = "schema-semantic"
    contract_version = 1

    def compare(self, request):
        # request.old / request.new contain artifact paths or runner results.
        return ImpactScore(
            comparator=self.comparator_id,
            changed=True,
            magnitude=0.8,
            label="2 breaking schema changes",
            reasons=("required field removed", "type narrowed"),
        )
```

Register it with:

```toml
[project.entry-points."lemoncrow.review_surface_comparators"]
schema-semantic = "my_plugin:SchemaComparator"
```

Then set `comparison=ComparisonDescriptor(source="preview", comparator="schema-semantic")` (or `source="run"` with the explicit safe-execution opt-in above). Comparator plugins run as trusted server-side Python, just like providers/renderers; reviewed repository code still may execute only through the existing sandboxed renderer/runner paths.

### Renderer

```python
from lemoncrow.pro.capabilities.review.previews import PreviewArtifact, PreviewRenderRequest


class MyRenderer:
    renderer_id = "my-platform"
    contract_version = 1

    def render(self, request: PreviewRenderRequest) -> PreviewArtifact:
        ...
```

`render` is called once per side, on a worker thread. `request` carries:

| Field | Meaning |
| --- | --- |
| `side` | `old` or `new` |
| `descriptor` | The descriptor the provider attached |
| `surface`, `context` | The surface and the Review revision it was discovered from |
| `workspace` | A private copy of exactly this revision side, limited to `descriptor.scope` |
| `output_dir` | The only directory an artifact may be written to |
| `sandbox` | The only way to execute reviewed code (see below) |
| `limits` | Effective time and size limits |
| `config` | This surface's `.lemoncrow/review.yaml` entry, if it has one |
| `path(rel)` | Resolve a repository-relative path inside `workspace`, refusing escapes |

Return one of:

```python
PreviewArtifact.ready(path, "image/png", filename="story-new.png", summary="1200x800")
PreviewArtifact.unavailable("story does not exist on the old side")
PreviewArtifact.failed("build failed", output=log_tail)
```

or raise `PreviewUnavailable("reason")`. Any other exception becomes a `failed`
result naming the renderer and the exception. The reason is shown to the
reviewer verbatim, so make it specific.

### Sandbox

Renderer Python code runs inside the LemonCrow server process: installing a
plugin means trusting it. Code from the *reviewed repository* is not trusted and
must only run through `request.sandbox`:

```python
modules = request.sandbox.node_modules("web")            # lockfile-proven checkout deps, read-only
result = request.sandbox.run(["node_modules/.bin/tool", "build"], cwd="web", node_modules=modules)
shot = request.sandbox.screenshot(request.path("web/out"), "/index.html", width=1280, height=800)
```

- `run(argv, cwd=, timeout=, env=, node_modules=, browser=)` runs in the same
  bubblewrap policy as web previews and command runtimes: no network, read-only
  system and dependency mounts, the workspace at `/workspace`, `output_dir` at
  `/capture`, a private `/tmp`, and a cleared environment. `PATH`, `HOME` and
  loader variables cannot be overridden. It returns `exit_code`, the last 4000
  characters of output, `duration_ms`, and `limit_note` when the host limits
  most likely killed the command (for example the memory ceiling).
- `node_modules(project_root)` borrows the checkout's dependencies only when the
  reviewed `package.json` and lockfile match the checkout's, and installs them
  once from the lockfile with lifecycle scripts disabled when they are missing.
- `screenshot(site, page, width=, height=, filename=)` serves a static
  directory from the workspace on sandbox loopback and captures one page with
  headless Chrome into `output_dir`.
- `capture(...)` takes the same arguments and returns `path` plus the `dom`
  Chrome rendered, from the same browser run. Use it to report a page that shows
  an error or loading state as a failure instead of screenshotting it.

Without bubblewrap, sandbox calls fail with a specific reason instead of running
unsandboxed.

### Host capacity

Every sandboxed job — plugin builds and captures, web preview builds and
captures, dependency installs, and runtime runs — passes one host gate:

- **Concurrency.** At most `LEMONCROW_PREVIEW_CONCURRENCY` jobs run at once
  (default and `0`: half the CPU cores, 1–4; at most 16). Others queue; a job that waits
  longer than its own time budget (at most 180 s) reports "sandbox capacity is
  busy" instead of starting.
- **Memory.** Each job runs under a `LEMONCROW_PREVIEW_MEMORY_MB` ceiling
  (default 4096, 256–65536; `0` disables it). With systemd user scopes the
  ceiling covers the whole process tree in its own cgroup, swap is disabled and
  tasks are capped at 1024; otherwise each process gets an `RLIMIT_DATA`
  ceiling. A job killed at the ceiling is reported with a reason that names it.

Set both in the environment of the LemonCrow server, or persist them with
`lc settings set review.preview_concurrency 2` and
`lc settings set review.preview_memory_mb 6144`, then restart the server.

### What the host guarantees

- **Revision pinning.** The Reader requests a preview for one explicit Snapshot
  (`revision_id`) and side. The host resolves the surface from that revision and
  materializes that side from the immutable revision — never from the live
  checkout. Workspaces and artifacts are keyed by that side's content identity
  (plus renderer, config and the checkout's dependency state), so an old side
  can never be served for a new side or for different content — and Snapshots
  of a Review that share a side (usually the unchanged old side) render it once.
- **Path confinement.** `scope`, `request.path()`, sandbox `cwd` and artifact
  paths must stay inside the workspace or `output_dir`; symlinks are resolved
  before the check.
- **Limits.** 120 s per render by default and 300 s at most, shared across all
  sandbox calls of one render; 25 MB per artifact at most. Both can be lowered
  per surface in `.lemoncrow/review.yaml`.
- **Validation.** The media type must fit the `display`; images must carry a
  real PNG/JPEG/GIF/WebP signature; `html`/`text`/`json` must be UTF-8 and JSON
  must parse.
- **Serving.** Artifacts are served by the authenticated Review API
  (`GET /api/reviews/{id}/surface-preview`) behind the same token, Origin, Host
  and Review-permission checks as every other Review route, with
  `Content-Security-Policy: sandbox; default-src 'none'; ...`, `nosniff` and
  `no-store`. Downloads are sent as attachments.
- **Failure isolation.** A plugin that fails to import, is not a valid
  provider/renderer, reuses a taken id, raises during discovery, or fails while
  rendering is reported with a specific reason — in `lc review --setup`, in the
  Reader's "Some review context is unavailable" notice, or in the preview pane.
  Other surfaces and all source review stay available.
- **Retention.** Rendered artifacts live in the Review store under
  `review/surface-previews/<review>/sides/`, web preview builds under
  `review/web-previews/<review>/builds/`. The eight most recently used entries
  per Review are kept; older ones are removed once idle for an hour.

### Versioning

`version` on the descriptor and `contract_version` on the renderer are both `1`.
A LemonCrow that does not support a declared version reports the preview as
unavailable with the supported versions named; it never guesses. New optional
descriptor fields may be added within version 1; anything a version-1 renderer
could misread bumps the version.

## Built-in: Storybook component previews

The `storybook` provider turns changed `*.stories.*` files — and components
with a sibling story file, such as `Button.tsx` next to `Button.stories.tsx` —
into one surface per story. The renderer builds the static Storybook of each
Snapshot side once, inside the sandbox, and captures
`iframe.html?id=<story-id>&viewMode=story`. A story that exists on only one side
is reported as unavailable on the other. A story that throws while rendering is
reported as failed with Storybook's error message, not shown as a screenshot of
the error screen.

It works without configuration for a project with `.storybook/main.*`. It needs
bubblewrap, Chrome/Chromium, and the Storybook CLI in the checkout's
`node_modules` (`storybook build`, or `build-storybook` for Storybook 6).

```yaml
surfaces:
  - id: components
    provider: storybook
    root: frontend               # the directory containing .storybook/
    config_dir: .storybook       # optional
    stories_dir: src             # used to derive titles for stories without `title`
    viewport: [1200, 800]        # optional capture size
    # build_command: [node_modules/.bin/storybook, build, -o, .lemoncrow-storybook]
    # output_dir: .lemoncrow-storybook
    preview:
      timeout: 180               # seconds; capped at 300
```

Story ids follow Storybook's rules (`Design/Button` + `PrimaryButton` →
`design-button--primary-button`). Titles and exports are read statically; stories
filtered by `includeStories`/`excludeStories` or with computed titles may not be
discovered.

## Installing a plugin
Install the plugin into the **same Python environment as the LemonCrow Review
server**. LemonCrow's local installer keeps that environment under
`LEMONCROW_TOOL_DIR` (default `~/.lemoncrow/uv-tools`):

```bash
TOOL_PYTHON="${LEMONCROW_TOOL_DIR:-$HOME/.lemoncrow/uv-tools}/lemoncrow/bin/python"
uv pip install --python "$TOOL_PYTHON" ./demo/review-preview-plugin
# or a published plugin:
uv pip install --python "$TOOL_PYTHON" lemoncrow-review-csv
```

In a LemonCrow source checkout, `make review-demo-plugin` installs the example
plugin into both the checkout environment used by `uv run lc` and the local
Review server environment. Reinstalling or upgrading LemonCrow recreates its
tool environment, so install plugins again afterwards. Plugins are discovered
when a Review loads its surfaces; restart the local server (`bash
scripts/local_server.sh restart` from a checkout) if it was running when the
plugin was installed.
surfaces; restart the local server (`bash scripts/local_server.sh restart` from a
checkout) if it was running when the plugin was installed.

The pre-compiled binary from the quick installer has no Python environment to
install into; use the developer install to run plugins.

Check the result:

```bash
lc review --repo-root . --setup
```

The setup report includes installed preview renderers and surface comparators;
a broken provider, renderer, or comparator appears as an `error:` with its
specific reason.

## Registering a plugin

Declare entry points in the plugin's `pyproject.toml`. The value may be a class
(instantiated with no arguments) or an instance:

```toml
[project.entry-points."lemoncrow.review_surface_providers"]
csv-table = "lemoncrow_review_csv:CsvTableSurfaceProvider"

[project.entry-points."lemoncrow.review_preview_renderers"]
csv-table = "lemoncrow_review_csv:CsvTableRenderer"

# Optional: only when the host's image/json/text/binary comparator is not enough.
# [project.entry-points."lemoncrow.review_surface_comparators"]
# csv-semantic = "lemoncrow_review_csv:CsvSemanticComparator"
```

`provider_id` and `renderer_id` must be unique. A plugin that reuses a built-in
id (`web`, `service`, `bruno`, `storybook`) is ignored and reported.

## Configuring surfaces in `.lemoncrow/review.yaml`

Every surface entry has an `id` and a `provider`; everything else is passed to
the provider unchanged. Set `metadata["surface_id"]` to the entry's `id` on the
surfaces you discover so the host can apply that entry's `preview:` settings:

```yaml
version: 1
surfaces:
  - id: tables
    provider: csv-table
    paths: ["data/*.csv"]
    preview:
      enabled: true              # false hides the preview but keeps the surface
      timeout: 30                # seconds, 5-300
      max_bytes: 2000000         # bytes, 1 KB-25 MB
```

## Example: a CSV table plugin

[`demo/review-preview-plugin`](../../demo/review-preview-plugin/) is a complete
third-party plugin in about 150 lines: a provider that turns changed CSV files
into `data.table` surfaces, and a renderer that turns each side into a bounded
HTML table. It reads only from the host-materialized workspace, so it needs no
sandbox; an added file reports "does not exist on the old side (added by this
change)" for the old side.

## Adding another platform

1. **Pick the unit a reviewer looks at** — a story, a diagram, a screen, a
   notebook cell. That is one surface. Choose a stable `id` that does not change
   between revisions (a story id, a file path), so Compare and the Reader's
   selection survive a refresh.
2. **Write the provider.** In `discover(context)`, look only at
   `context.changed_paths` and files under `context.repo_root` (the new side of
   the revision). Never execute code here. Return `ReviewSurface`s with
   `affected_paths`, `capabilities=("preview", "compare", "source")` and a
   `PreviewDescriptor` whose `scope` is the smallest subtree the renderer needs.
   Implement `setup_candidates(repo_root)` to offer config for `--setup`, or
   return `()` to stay opt-in.
3. **Write the renderer.** Read inputs from `request.workspace`, execute
   anything from the repository through `request.sandbox`, write the artifact to
   `request.output_dir`, and return `PreviewArtifact.ready(...)`. When the
   subject does not exist on a side, return `unavailable` with a sentence a
   reviewer understands. Reuse expensive work (a build) by writing it into the
   workspace; it is private to this revision side and renderer.
4. **Choose a `display`.** Prefer `image` for anything visual, `html` for
   static documents, `text`/`json` for data, `download` for everything else.
5. **Register both entry points, install, and run `lc review --setup`.**
6. **Test both sides.** Build a two-commit repository, record a revision, and
   call `render_surface_preview(context, surface, side=...)` for `old` and
   `new`; `tests/gateway/test_review_preview_plugins.py` shows the pattern,
   including how to fake the sandbox.

If the difference is *how* something runs rather than *what* is shown — a new
container engine, a device farm — write a runner (`lemoncrow.review_runners`)
instead and bind surfaces to it with `runtime:`; the generic Reader view will
offer Run and Compare for it.
