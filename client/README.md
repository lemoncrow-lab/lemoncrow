# lemoncrow-client

The public LemonCrow thin client. **Apache-2.0. Python standard library only.
No dependencies, no daemon, no listening port, no unit, no product telemetry
on the standard session path. Product traffic uses the configured endpoint;
explicit `web_fetch` calls reach public pages from the client.**

**Product installer runs.** `lc update`, and the first use of a product that is
not installed yet, run the published LemonCrow `install.sh` for that product.
Set `LEMONCROW_NO_LAZY_INSTALL=1` to turn the first-use fetch off;
`LEMONCROW_RELEASE_BASE_URL` points it at another asset base. Pi host install
and update commands separately download the pinned, checksum-verified Pi binary.
Its auditability is the product. The claims above are asserted by
`tests/test_packaging_audit.py` against the installed package, under a Python
audit hook that records every socket, subprocess and file write the client
actually performs — not by this paragraph.

This repository documents the public client contract only. Enterprise server
installation, identity, policy, storage and operations are intentionally kept
with the proprietary enterprise distribution rather than linked from public
documentation.

## Review is standalone (no coding-agent MCP required)

The separate `lcr` executable and `lc review` product capture immutable Review
Snapshots through the hosted/local Review API; they do **not** start or require
Runtime MCP. The opt-in foreground P1 watcher adds capture scheduling while an
agent or editor writes the checkout:

```sh
lcr watch                        # settled worktree edits; Ctrl-C to stop
lc review watch --staged         # staged changes only
lcr watch --once --json          # one structured checkpoint for scripts
lcr watch --interval 1 --settle 2
```

Paired Hosted Review machines can separately inspect queued work without
claiming or executing it. **No Companion product/process is required** and
local OSS capture continues working without a paired machine:

```sh
lcr pending                         # scoped queue counts, read-only
lcr pending --watch                 # WSS hints with authenticated HTTPS fallback
lcr pending --class review_intelligence
lcr device check                    # optional signing-key diagnostic
lcr task run JOB_ID --help          # explicit, human-approved AI job options
```

`lcr pending` requires an already paired Hosted Review machine; it is not a
local OSS job queue. `lcr watch` is the canonical local/hosted capture workflow.
These optional commands start no daemon and execute no unapproved jobs.
The old `lcr companion ...` spellings remain as hidden compatibility aliases.

The watch process requires the same Review connection and credentials as
`lcr`; use `lc auth login` when using the hosted service. It never registers an
MCP tool, opens a listener, edits source, executes reviewed code or installs a
background service. Its JSON lines (`watch_started`, `captured`, `capture_failed`,
`source_error`, `watch_stopped`) make a simple hook or VPS process supervisor
possible. A watcher started on a clean checkout waits for the first change;
subsequent clean commits still advance an existing Review. The capture currently
reuses the **full View** upload protocol, not a true per-file delta transport.

Caution: Review capture can send changed, non-ignored repository content to the
configured Review server. Do not start this on a checkout containing sensitive,
non-ignored files until repository capture policy is qualified. Distinct
machine-scoped sandbox credentials, resume-on-restart and independent verification
workers are future phases, not implied by `watch`.

## What it is

Exactly one LemonCrow MCP stdio process is started by the coding agent and it
dies when that host session ends. MCP `initialize` immediately advertises tools;
handshake, auth, view open and sync run in a joined worker inside that same
process. Local tools stay available while repository tools wait for the required
revision. There is no SessionStart helper.
Advertised tools keep their existing `mcp__lc__*` names. Legacy `mcp`, `agent`
and `workflow` are deliberately not advertised because their local
implementations start another MCP/LemonCrow process.

```
## Install and point it somewhere

```bash
pip install lemoncrow-client
export LEMONCROW_URL=https://lemoncrow.corp.example   # or: lemoncrow-client target <url>
export LEMONCROW_TOKEN=...                            # or: ~/.lemoncrow/token, mode 0600
lemoncrow-client review --help                        # Review works without Runtime integration
lemoncrow-client audit                                # prints the resolved claims
```

Review installation does not modify coding-agent configuration. Runtime integration is explicit and uses each coding agent's own MCP configuration CLI; LemonCrow never edits those host config files directly:

```bash
lemoncrow-client runtime enable claude
lemoncrow-client runtime enable codex
lemoncrow-client runtime enable opencode
lemoncrow-client runtime enable auto --dry-run
```

The older `lemoncrow-client install --agent ...` spelling remains a compatibility path during migration. All Runtime registrations point to the same command: `lemoncrow-client mcp`. Registration is a one-time foreground action; it installs no LemonCrow daemon, listener, SessionStart hook, or second runtime process. During a coding-agent session there is still exactly one LemonCrow process.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEMONCROW_URL` | `http://127.0.0.1:7420` | The **only** product destination this client opens. |
| `LEMONCROW_TOKEN` | — | Bearer credential. |
| `LEMONCROW_TOKEN_FILE` | `~/.lemoncrow/token` | IT-provisioned credential. Must be mode 0600 or the client refuses it. |
| `LEMONCROW_HOME` | `~/.lemoncrow` | The only directory written outside the repository: the token, an `env` file, and `walk-cache/` (one file per worktree, recording which files were unchanged when they were last hashed — paths and digests, never content). |
| `LEMONCROW_LOCAL_FS` | `0` | Offer the same-host read optimization. Off by default. |
| `LEMONCROW_STARTUP_BUDGET_S` | `1.0` | Initial bootstrap HTTP timeout; background MCP bootstrap allows the request timeout. |
| `LEMONCROW_SCM_PROVIDER` / `_SCM_REPO_ID` | — | Canonical SCM identity, when your deployment issues one. |

`LEMONCROW_URL` with credentials embedded is refused, and so is a redirect away
from it: the transport compares every request's origin against the configured
one before it is sent.

Local HTTP and hosted HTTPS requests reuse connections automatically within the
client process, retaining at most four idle connections. Server-requested closes
open a fresh connection next time. A stale connection retries reads once; writes
and timeouts are not automatically replayed. Session teardown closes the pool.

## The routing table

Site is a property of the tool, and the table is data
(`lemoncrow_client/routing.py`). `lemoncrow-client routes` prints it; every row
is covered by a test, and `tests/test_routing.py` compares it tool-for-tool with
the server's own copy.

| Tool | Site | Why |
| --- | --- | --- |
| `bash` | client | subprocesses, process groups, cwd |
| `edit` | client | writes the working tree; pushes changed blobs after |
| `grep` | client | searches the worktree, no index needed |
| `blame` | client | git history is local |
| `scan`, `codemod` | client | ast-grep binary over local files |
| `sql` | client | local DSNs; `.env` discovery when the operator allows it |
| `index` | client → server | client enumerates and uploads; server builds |
| `code_search`, `read`, `relations`, `graph`, `search` | server | needs the index |
| `context`, `memory`, `verify`, `trace`, `rescue`, `compact`, `orient` | server | remote-routable |
| `web_fetch` | client | public pages use the host network; no server dependency |
| `statusline_segment`, `review_*` | server | session state and review move server-side |
| `cache` | server, admin | administers server-side index state |

`web_fetch` converts HTML locally, caches pages for five minutes per session,
and spills shortened content locally for `read`. PDF extraction uses
`pdftotext` on the client PATH; no program is installed automatically.

Three boundaries are drawn on purpose, and each is a typed refusal rather than a
silent degradation:

- **`bash` background runs belong to the session.** `bg=true` returns an id and
  the command keeps running between calls; `id=` with `action` `poll`, `status`,
  `kill` or `update` drives it. It is an ordinary child (own process *group*,
  same session, never `setsid`) with a hard deadline (at least an hour, at most
  six). It is killed when the session closes, and wrapped in `timeout(1)` where
  present so even a killed client cannot leave it past six hours. There is no
  thread, `atexit` or signal handler behind it; deadlines are enforced when the
  session next touches the run. A poll never blocks longer than 90 seconds.
  `interactive` and `action=send` are refused: this client has no stdin to hold.
- **`codemod` needs `ast-grep`** on `PATH` or at `LEMONCROW_AST_GREP_BIN`.
  Absent, the answer names the program; `scan` still runs its Python taint
  check and marks the rule pack skipped. Nothing is ever downloaded or
  installed. The client itself never starts another LemonCrow executable or
  MCP server.
- **`sql` speaks SQLite**, through the standard library. Another scheme gets a
  driver-required note: this package will not depend on a driver and will not
  fetch one. A read opens the database read-only; a write needs `write=true`
  and the operator's `LEMONCROW_SQL_ALLOW_WRITES=1`.

`grep` does **not** shell out to ripgrep. It walks the worktree with the same
`.gitignore`-respecting walk that builds the manifest, so local search and
server search describe the same set of files.

## The contracts that matter

**One enterprise-safe namespace.** `tools/list` advertises the routed subset of
the public MCP registry with unchanged names and schemas. `mcp`, `agent` and
`workflow` are explicit security exclusions because their legacy local
implementations start another process. The bundled registry data is still
diffed against upstream so a new tool cannot appear without an explicit route
or exclusion decision.

**Read-after-edit is a protocol guarantee.** `edit` and `codemod` report the
paths they wrote; the blob service uploads the content and commits an overlay
revision carrying `(expected_view_revision, client_seq)` **before** the tool
result returns. Every subsequent call names the revision it observed, so a read
that could not reflect the edit is refused rather than answered stale.

**Committed base plus local changes.** The server reuses the immutable generation
for the exact Git commit. The client enumerates Git tree metadata and caches blob
digests; a missing base is supplied from Git objects through the existing bounded
missing-content protocol, including unpushed commits. It never walks clean
working files. Git status selects staged, unstaged and unignored untracked paths;
only those contents are hashed and uploaded. Restored files reuse the committed
base, deletions carry metadata, and commit changes reopen a generation-bound View.
Tracked files remain included even when they match a `.gitignore` rule, as in Git.
Index construction remains server-side.

**Edits made elsewhere are synced before they are read.** Every server tool that
reads the View first checks the working tree (one `git status` against the last
synced fingerprint) and pushes only what changed; an unchanged tree sends
nothing. To take that work off the next call, the client also syncs right after
its own `bash`, and a small background sync (`autosync.py`) pushes host-tool and
editor changes early: Claude Code's Edit/Write/Bash hooks send a one-word nudge
to an owner-only Unix datagram socket under `~/.lemoncrow/sync/` (other hosts
send none), and while the session is in use one check runs every 30 seconds.
Freshness never depends on it. `LEMONCROW_AUTOSYNC=0` turns it off.

A path argument that names nothing in the workspace is refused as
`path_not_addressable` with suggestions, instead of being retried into a
misleading `blob_missing`.

**A blob miss is answered in the same turn.** A server tool that needs content
the server does not hold answers `{"need": [paths]}` with HTTP 200. The client
fills exactly those paths and retries once. A second unfilled answer is a typed
`blob_missing` — a bounded protocol, not a fill/retry loop. A client that cannot
do this is refused at handshake, which is why `blob_miss.v1` is a *required*
capability.

**The manifest is canonical and verified.** `manifest.py` mirrors the server's
row encoding, root digest and chunking exactly, and the server *proves* the root
when the last announced chunk lands. A warm session reuses a known root and
sends nothing; a cold one sends only the chunks and content the server names.

**Files over the server's `content_size_cap` are manifested, never uploaded.**
The cap is server policy; the client honours it rather than declaring its own.

**Version skew inside the window is served; outside it refuses loudly.** The
client speaks protocol N and N-1 (`protocol_window()`), the same two dates the
server serves, and steps down from N to N-1 once when a server one version
behind refuses N. Outside the window the handshake, which happens *before*
authentication, refuses with `protocol_version_mismatch` and names the side to
upgrade: `upgrade_client` ("upgrade the client") or `upgrade_server` ("the
server is older; upgrade the server"), never a confusing 401.

**Review features are gated on the server's manifest.** `product_capabilities.review`
(at discovery, in the handshake and at `GET /v1/reviews/manifest`) names every
Review feature, the capture modes and options the server honours and the packet,
evidence, policy and record schema versions it reads. A feature it lacks is
refused with `capability_unsupported` and "this server does not support X"
before any request is sent; a capture whose packet schema or options the server
would not apply is refused the same way instead of being applied in part.

**`local_fs` is an optimization, not a second path.** Offered only when the
operator opted in *and* the client can write a nonce into the worktree the
server is being asked to read. Add `.lemoncrow-local-fs-proof` to `.gitignore`.

**One implementation per local tool.** `lemoncrow_client.kit` holds the logic
of the local tools (today: the `edit` engine and its test-contract guard, the
`bash` command policy and output pipeline, the `grep` engine and the read-path
grammar, the `sql` engine and its renderer, the ast-grep adapter behind
`codemod`, and the SAST rule pack and taint check behind `scan`), and the main
`lemoncrow` package imports the same modules instead of keeping its own copy.
What only the main package has (the code index, `rg`, third-party engines, a
managed ast-grep download) plugs in through hooks. A behavior a client tool
needs is added to `kit`, never re-implemented beside it. `kit` follows every
rule in this document, imports only the standard library, and `kit/fsio.py` is
its only module that writes files.

## Degradation

| Condition | Behaviour |
| --- | --- |
| Server unreachable at MCP initialize | The same stdio process stays alive with client-side tools only; initialize instructions name the reason; ordinary `read` and other client-side tools continue locally; **no local index is ever built** |
| Server unreachable mid-session | Server-side tools return the typed error; client-side tools unaffected |
| Version outside N/N-1 | Refused with `protocol_version_mismatch` + `upgrade_client` or `upgrade_server` |
| Review feature the server lacks | Refused before sending with `capability_unsupported`: "this server does not support X" |
| Blob miss | `{need:[...]}` → client fills → one retry → typed `blob_missing` |
| Link or semantic layer cold | The answer comes back flagged `degraded` with the reason |

"Never came up" and "went away" are tracked separately (`SessionState.bootstrapped`
vs `SessionState.online`) because they require different behaviour and one
boolean could not tell them apart.

## MCP initialize owns bootstrap

There is no SessionStart hook and no second LemonCrow process. The coding agent
launches `lemoncrow-client mcp`; its MCP `initialize` request performs the
handshake, authentication, `views/open`, manifest negotiation and blob fill in
that same process. The process then holds the remote session/view until stdin
closes and closes it on exit.

`LEMONCROW_STARTUP_BUDGET_S` bounds this initialize bootstrap. The warm path is
measured against a real server over a 2,001-file repository by
`tests/test_mcp_startup_budget.py`; the enterprise-scale variant is
`tests/test_enterprise_scale_budget.py`.

### At enterprise scale

2,001 files is about 2% of an enterprise monorepo on a link with no round trip,
so the same MCP initialize path is also measured over a generated **50,001-file, 308 MB** tree
(mean 6,160 B, 2,085 directories, 10 languages) against the real private server
— `tests/test_enterprise_scale_budget.py`, opt-in with `LEMONCROW_SCALE_BENCH=1`
because its cold pass runs for minutes. One such run, after a **931.6 s** cold
session that uploaded and indexed the tree:

| Transport | min | median | slowest |
| --- | --- | --- | --- |
| plain loopback | 612 ms | 728 ms | 857 ms |
| plain loopback, walk cache unavailable | 1108 ms | 1122 ms | 1171 ms |
| loopback + 20 ms simulated RTT | 766 ms | 794 ms | 861 ms |
| TLS | 643 ms | 794 ms | 1280 ms |
| TLS + 20 ms simulated RTT | 1096 ms | 1308 ms | 1709 ms |

The budget the module enforces is the slowest warm run on plain loopback, and it
holds. It does **not** hold with TLS *and* a round trip: this client opens a
connection per request, so a handshake and a round trip are paid several times
per session. The `no walk cache` row is the same run with the client's stat
cache unavailable, which is what attributes the gain to `walkcache.py` rather
than to the rest of the restructuring.

These are wall-clock numbers from one machine and they move with load — the TLS
leg above exceeded 1 s at its maximum where an earlier run on the same tree did
not. Re-run the module rather than quoting a single figure.

## What an enterprise reviewer should run

```bash
cd client
uv venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                       # the whole suite, no network
.venv/bin/python -m pytest -q tests/test_packaging_audit.py -v
.venv/bin/python -m mypy src
lemoncrow-client audit
```

`tests/test_packaging_audit.py` is the deliverable. Under `sys.addaudithook`, it
drives a full client session — handshake, session open, view open, manifest
chunks, blob upload, a tool call, an edit, a push — against a loopback stub, and
then asserts what the client *did*:

- no network port is ever bound and no listening stream socket exists; the one
  bind is background sync's owner-only Unix datagram socket under
  `~/.lemoncrow/sync/`, the only datagrams sent go to it, and it is removed
  when the session closes;
- ordinary product `socket.connect` / `urllib` requests go only to configured
  `LEMONCROW_URL`; hosted login adds only the Authward origin advertised by that
  server and verified again through exact-issuer OIDC discovery;
- the only program the session path runs is a read-only `git` query for the
  repository fingerprint and base revision, and the one shell `bash` spawns is
  reaped before the call returns — no child and no thread survives either;
- every file written is under the repository or `~/.lemoncrow`;
- no thread or child process survives the session;
- no unit, plist, `loginctl`, `~/.claude/settings.json` rewrite, tunnel,
  unexpected downloader or background self-update path exists in the source;
  only explicit `lc update`, missing-product first use and the Runtime-only,
  pinned/checksummed Pi host installer may fetch release assets, and the client
  refuses to inherit an `ALLOW_UNVERIFIED` override for product installation;
- the package imports no network socket API (`socketserver`, `http.server`,
  `asyncio`, `selectors`); `socket` itself appears only in `autosync.py`, which
  may use only `AF_UNIX` datagrams (no `AF_INET`, stream, `listen`, `accept` or
  `connect`); network egress is confined to `transport.py` (product RPC),
  `webtransport.py` (explicit public-page fetch) and `pi_host.py` (pinned Pi
  installer), while product installations use only the reviewed CLI path;
- threads are started only by `autosync.py`, which joins them on close, and by
  the session's `lc-fingerprint` and the MCP server's `lc-call` worker pools,
  each shut down with its owner;
- every module the package imports is either the standard library or itself;
- the installed distribution's dependency set is **empty**.

The public client test suite is self-contained. Cross-package protocol,
execution-matrix and end-to-end qualification against the proprietary server is
run in the private enterprise source tree and intentionally is not documented
here.

Run the public client and public-registry conformance from this repository:

```bash
PYTHONPATH=client/src uv run python -m pytest client/tests -q
```

## What is not in this increment

- Removing the client-side daemons, units and the model-router rewrite from the
  existing local install. That is **11D**, deliberately one auditable diff, and
  until it lands the existing local path stays as the migration fallback.
- Routing `read`/`code_search`/`relations`/`search` onto `/v1/views/{id}/query`.
  The index answers all four today and `RemoteSession.query_index` reaches them;
  joining them to the tool path is 11D.
- Hosted interactive authentication is Authward-owned. `lc auth login` first
  discovers the exact Authward issuer/resource advertised by LemonCrow, then
  performs RFC 8628 device authorization directly against Authward. The client
  stores one private Authward credential pair under `LEMONCROW_HOME`, refreshes
  directly with Authward, and sends only the Authward access token to LemonCrow.
  Local machine credentials remain a separate account-free mechanism.
