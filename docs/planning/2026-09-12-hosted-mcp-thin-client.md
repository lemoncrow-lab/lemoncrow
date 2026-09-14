# Hosted MCP + thin client — design

Status: draft for review
Date: 2026-09-12
Supersedes: nothing yet; constrains all packaging work after it

## 1. Problem

Enterprise security review rejected the current LemonCrow install. Three separate
objections, all confirmed against the code:

1. **Unvetted local execution.** The install requires a Python package or binary on
   PATH, plus network-fetched binaries at runtime: cloudflared (~60 MB, executed after
   download, `mcp_serve.py:194`), the LemonCode host tarball (`cli/lemoncode_host.py:141`),
   HF model weights on first embed (`infra/embeddings/nomic.py:112`), `go install …@latest`
   × 4 (`cli/commands/code.py:38`), and Docker image pulls.
2. **Background daemons and ports.** Twenty distinct long-lived processes or units. A
   per-workspace MCP daemon **auto-spawns on the first tool call** with
   `start_new_session=True` (`mcp_daemon.py:420`). `lc mcp serve --persistent` writes
   `~/.config/systemd/user` / `~/Library/LaunchAgents` units with `Restart=always` and runs
   `loginctl enable-linger` (`_mcp_service.py:121`), so the process runs with nobody logged
   in. Listening ports: 8787, 3125, 4000, 8899, 8765, plus ephemeral. The router daemon
   rewrites `~/.claude/settings.json` `env.ANTHROPIC_BASE_URL` (`router_daemon.py:120`) and
   the rewrite outlives the proxy.
3. **Unapproved egress.** Product telemetry ships to `https://us.i.posthog.com/i/v0/otlp`
   with `remote_enabled: bool = True` (`telemetry/config.py:26`). `lc mcp serve`
   default-launches a Cloudflare quick tunnel exposing the full tool surface — including
   `bash` — to the public internet.

A fourth constraint is ours, not theirs: LemonCrow must run on low-resource laptops. The
code index for this repo alone is ~1.1 GB on disk (`code_context.sqlite` 398M,
`intel.sqlite` 530M, `fts.sqlite` 170M) and is built by a detached local `lc code index`
subprocess. Every worktree pays that cost again.

Source code leaving the developer's machine is **not** an objection: the server is
deployed on-premises by the customer.

## 2. Goals

- The local install is a Claude Code plugin plus one short-lived, auditable process. No
  daemons, no listening ports, no units, no binary downloads, no auto-update, no egress
  except to the configured LemonCrow server.
- Heavy work — indexing, symbol resolution, embeddings, search — happens on the server.
- **One server, two transports.** The same server binary runs on `127.0.0.1` for a solo
  developer and on-prem for a team. Moving from local to remote changes a URL, not a code
  path.
- An index is built once per unique file content and reused across worktrees, branches and
  developers.
- No loss of tool surface: every `mcp__lc__*` tool that exists today still exists.

## 3. Non-goals

- Multi-tenant SaaS hosting. The server is single-tenant, customer-operated.
- Offline operation. With no reachable server, LemonCrow degrades to a documented
  reduced mode; it does not silently rebuild a local index.
- Preserving the consumer local-daemon architecture as a second supported path. This
  design replaces it. "Local" becomes a server bound to loopback.

## 4. Architecture

```
Claude Code
     | stdio
     v
lemoncrow-client            <- pip package, starts and dies with the session
     |-- bash / edit / grep / index / blame / scan / codemod   executed in-process
     |-- blob service                                          answers server pulls
     '-- everything else ---- HTTPS ----> LemonCrow server
                                          (blob store, blob index, view index,
                                           link index, embeddings, memory, verify)
```

**One MCP entry, one namespace.** The shim is the MCP endpoint Claude Code talks to. Tools
keep the `mcp__lc__*` names they have today. The split between client and server execution
is invisible to the model and to agent prompts.

**The shim is not a daemon.** It is a stdio child of the Claude Code session: no port, no
pid file, no socket, no unit, no `atexit` respawn, no auto-update, no idle-reaper thread.
It exits when stdin closes. Dependencies: Python stdlib only (`urllib.request`, matching
`remote_client.py`), so the audit surface is one package with no transitive tree.

**The server is what exists today, minus the client.** `mcp_http.py` already serves
`POST /mcp`, `GET /mcp` (SSE) and `/.well-known/mcp.json` over the same dispatcher and tool
registry, and `mcp_oauth.py` already has the OAuth scaffolding. The server-side work is
largely reorganising what is already there, not writing a new service.

**Porting local to remote.** The plugin reads `LEMONCROW_URL` (default
`http://127.0.0.1:8899`). `lc target set https://lemoncrow.corp.example` rewrites it. That
is the whole migration.

## 5. Tool execution matrix

Site is a property of each tool, not a separate product tier. The shim routes by table.

| Tool | Site | Why |
| --- | --- | --- |
| `bash` | client | subprocesses, process groups, cwd |
| `edit` | client | writes the working tree; pushes changed blobs after |
| `grep` | client | ripgrep over the working tree, no index needed |
| `index` | client→server | client enumerates and uploads; server builds |
| `blame` | client | git history is local |
| `scan`, `codemod` | client | ast-grep binary over local files |
| `mcp` (proxy) | client | spawns other stdio MCP servers the user configured |
| `agent`, `workflow` | client | orchestrate local executors |
| `code_search` | server | needs the index |
| `read` | server | resolves ranges/outlines/symbols from the index; falls back to a client pull on a blob miss |
| `relations`, `graph` | server | link index |
| `search` (semantic) | server | embeddings |
| `context`, `memory`, `verify`, `trace`, `rescue`, `compact`, `orient` | server | already remote-routable via `RemoteClient` |
| `web_fetch` | server | pure network; keeps egress on the server where policy can see it |
| `statusline_segment` | server | session state moves server-side |
| `sql` | client | local DSNs and `.env` discovery |
| `review_*` | server, except screenshot capture | capture drives a local browser |

`read` deserves a note: it answers from the server's copy of the blob, which is guaranteed
current because `edit` and the PostToolUse hook push changed content before returning. On a
miss the server replies `{need: [paths]}` and the shim fills it in the same turn.

## 6. Index model

The single most important change. Today the index is per-workspace and monolithic, so N
worktrees cost N × 1.1 GB and share nothing. Replace it with three layers keyed by content.

### Layer 1 — blob index (content-addressed, shared, immutable)

Key: `(blob_sha, path)`. Value: FTS/trigram rows, symbol definitions, imports/exports,
language, embedding vector.

Path is part of the key because module identity depends on it — the same bytes at
`a/util.py` and `b/util.py` are different modules — but this does not weaken dedup, because
two worktrees of the same repo agree on both path and content for the ~99% of files they
share.

Written exactly once for a given key, ever. Never invalidated; content change means a new
key.

### Layer 2 — view manifest (per worktree, tiny)

A view is one worktree at one moment: `{path → blob_sha}`, computed as the base commit's
tree plus an overlay for dirty and untracked files, whose content the client hashes locally
as virtual blobs. A few thousand rows, on the order of 100 KB.

Keying on blob rather than commit is what makes this general. Commit-keyed indexes share
nothing between two worktrees on different commits, even when those commits differ by three
files. Blob-keyed, they share every unchanged file automatically — across worktrees, across
branches, and across developers on the hosted server.

### Layer 3 — link index (per view, incremental)

Cross-file edges: call graph, import graph, symbol references. This is the only
view-specific layer, because resolution depends on which blobs are present.

Built incrementally from the nearest cached ancestor view: take its edge set, invalidate
edges whose source or target path changed, plus dependents reachable through a reverse
`symbol → dependents` index, then re-resolve only those. A full rebuild is the fallback when
no ancestor is within a distance threshold.

### Query path

Search filters shards by joining against the view's membership table — a few thousand rows,
negligible. No per-view copy of any Layer 1 data.

### Storage

N worktrees of this repo: `1.1 GB + N × (overlay + membership)`, single-digit MB each,
instead of `N × 1.1 GB`.

### Lifecycle (required, not optional)

- **Blob GC.** Refcount blobs by referencing view; mark-and-sweep unreferenced entries after
  a retention window. Without this the store grows monotonically with every commit anyone
  ever checks out.
- **View eviction.** TTL on views not touched in N days; evicting a view is just deleting
  its manifest, membership and link rows.
- Both run as server-side scheduled work, never on the client.

### Risk

Link-layer invalidation is where staleness bugs will live. Mitigation is a differential
test: for randomised edit sequences, assert the incrementally-updated link index equals a
from-scratch rebuild of the same view. Any divergence is classified fail-safe (missing edge,
degrades recall) or fail-open (wrong edge, produces a false answer); fail-open divergences
block the change.

## 7. Sync protocol

One protocol for local and hosted. No localhost special case in the tool path.

```
SessionStart
  client -> POST /v1/views/open   {repo_id, base_commit, manifest:[{path, blob_sha, size}]}
  server -> {view_id, missing:[blob_sha], have_link_ancestor: bool}
  client -> POST /v1/blobs        gzip, batched, content-addressed
  server -> builds/loads Layer 1 for new blobs, derives Layer 3 from ancestor

During the session
  edit / PostToolUse hook -> POST /v1/views/{id}/overlay  {path, blob_sha, content}
  server blob miss        -> tool result {need:[paths]} -> client fills, retries once

SessionEnd (best effort)
  client -> POST /v1/views/{id}/close   (view becomes evictable)
```

Rules:

- `.gitignore` is respected; binaries and files over a size cap are manifested but not
  uploaded (searchable by path, not content).
- `repo_id` is derived from the git remote URL so developers on the same repo share blobs.
  With no remote, it falls back to a local id and shares nothing.
- Uploads are content-addressed, so a retry or a second worktree costs a 409-equivalent, not
  a transfer.
- **Same-host optimisation**: during `views/open` the client may offer a `local_fs` capability
  with a proof token; a server on the same host may then read blobs from disk instead of
  requesting an upload. This is a negotiated capability inside the same protocol, not a
  second code path — and CI runs the full suite with it disabled so the upload path never
  rots.

First-session cost on a cold server for this repo: a manifest of a few thousand entries plus
a one-time gzip upload of the tracked source. Second developer on the same repo: manifest
only.

## 8. SessionStart check

One bounded hook, target under ~1 s, fail-open, replacing the work the daemons used to do
implicitly:

1. Reachability and version handshake with the server (client/server compatibility, refuse
   loudly on mismatch rather than misbehaving).
2. Auth: `LEMONCROW_TOKEN` from env or an IT-provisioned file, else OAuth device flow
   against the configured base URL.
3. `views/open` — manifest negotiate and blob fill (§7).
4. Session register, statusline seed, `additionalContext` (cwd, git state) as today.

When the server is unreachable, the hook emits a visible one-line reason and Claude Code
continues with client-side tools only (`bash`, `grep`, `edit`, `read` from disk). It never
falls back to building a local index.

## 9. Daemon disposition

All twenty from the inventory.

| Becomes the server | Deleted client-side | Off by default |
| --- | --- | --- |
| per-workspace MCP daemon | stdio bridge (shim replaces it) | PostHog telemetry → on-prem OTLP endpoint or nothing |
| zoekt-webserver + docker runtime | servicectl controller + tick child | |
| code-index warmer subprocess | systemd/launchd unit installers, `loginctl enable-linger` | |
| embedder warm threads | cloudflared quick + named tunnels | |
| stack service (`:8787`) | persistent per-hostname MCP units | |
| frontend dev server (`:3125`) | model-router daemon **and its `~/.claude/settings.json` rewrite** | |
| letta / openmemory sidecars | mitmdump proxy | |
| idle reaper, heartbeat, dormant refresher | auto-update paths (`git pull` + `install.sh`) | |

The client keeps no pid file, no lock file, no socket, no unit, and writes nothing under
`~/.config/systemd`, `~/Library/LaunchAgents`, `~/.cloudflared` or `~/.claude/settings.json`.

## 10. Hook disposition

Eighteen hook entries today; twelve import the `lemoncrow` package. Under the shim they
become thin:

- **Server-backed** (post an event, render what comes back): ledger writes
  (`post_tool_use.py`, `post_tool_use_bash.py`, `user_prompt.py`), stats/savings
  (`stop.py`, `session_telemetry.py`), compaction (`compact.py`), output shrinking
  (`bash_output_shrink.py`, `mcp_output_shrink.py`), verify gate
  (`verify_before_done.py` — currently a hard top-level import with no `try/except`, which
  must become fail-open), cap nudge.
- **Pure local, stdlib only**: `pre_tool_discipline.py`, `mcp_read_allow.py`,
  `agent_redirect.py`, `loop_discipline_post.py`, `post_tool_use_failure.py`.
- **Deleted**: `live_review.py` — it detaches a `lemoncrow.pro…live_reviewer.child`
  process, exactly the pattern under objection. Live review moves server-side, triggered by
  the overlay push.

Every hook must be fail-open with a bounded timeout; a hook that blocks on an unreachable
server is worse than no hook.

## 11. Security posture

What the enterprise reviewer should be able to verify quickly:

- The client is one pip package, stdlib-only, no subprocess spawning except the tools the
  user invoked, no downloads, no self-update, no telemetry.
- Exactly one egress destination, configured by IT, on-prem.
- No listening sockets, no persistence across the session, no writes outside the repo and
  `~/.lemoncrow`.
- Agent prompts do not ban native tools any more than necessary, so a failed shim cannot
  strand a developer.

Two pre-existing findings to fix regardless of this design: `OPENAI_API_KEY` is interpolated
in plaintext into the OpenMemory launchd plist (`background.py`), and the local
`telemetry.db` on this machine has a 267 MB database behind a **29 GB WAL** — an unbounded
local write with no checkpoint or retention.

## 12. Degradation

| Condition | Behaviour |
| --- | --- |
| Server unreachable at SessionStart | Client-side tools only; one-line visible reason; no local index build |
| Server unreachable mid-session | Server-side tools return a typed error the agent can act on; client tools unaffected |
| Version mismatch | Refuse with an explicit message; never degrade silently |
| Blob miss on `read`/`code_search` | `{need:[paths]}` → client fills → one retry |
| Link index cold | Falls back to lexical/FTS results with a `degraded: true` flag in the response |

## 13. Testing

- Differential index test (§6) — the highest-value test in this design.
- Protocol conformance run twice in CI: `local_fs` capability on and off, identical results.
- A no-network test that asserts the client makes zero connections other than to
  `LEMONCROW_URL`.
- A packaging test that asserts the client package spawns no process, binds no port, writes
  no file outside the repo and `~/.lemoncrow`, and installs no unit.
- Worktree test: three worktrees on three branches; assert Layer 1 storage stays ~1× and
  each additional view costs single-digit MB.

## 14. Rollout

1. Server: extract the tool dispatcher behind `mcp_http` as the supported entry; keep stdio
   working for the transition.
2. Index: land the three-layer model behind a flag, with the differential test green, before
   anything else depends on it.
3. Client: ship `lemoncrow-client` with the routing table; prove parity against the current
   stdio server on a fixed task suite.
4. Plugin: repoint MCP config at `LEMONCROW_URL`; rewrite the ten agent prompts' tool rules.
5. Delete: remove the client-side daemons in §9 in one change, so the audit story is a diff.

## 15. Open questions

- `repo_id` derivation when a repo has multiple remotes or none.
- Whether the server should hold git credentials to mirror repos directly, which would make
  first-session cost zero — deferred; streaming from the client is the decided model.
- Per-view resource limits on a shared server: one developer's monorepo view should not
  starve the rest.
- Whether `agent`/`workflow` orchestration eventually moves server-side; client for now.
