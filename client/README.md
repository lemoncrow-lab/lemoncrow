# lemoncrow-client

The public LemonCrow thin client. **Apache-2.0. Python standard library only.
No dependencies, no daemon, no listening port, no unit, no self-update, no
downloaded binary, no product telemetry, and no egress other than the endpoint
your IT team configured.**

Its auditability is the product. Everything above is asserted by
`tests/test_packaging_audit.py` against the installed package, under a Python
audit hook that records every socket, subprocess and file write the client
actually performs — not by this paragraph.

This repository documents the public client contract only. Enterprise server
installation, identity, policy, storage and operations are intentionally kept
with the proprietary enterprise distribution rather than linked from public
documentation.

## What it is

Exactly one LemonCrow MCP stdio process is started by the coding agent and it
dies when that host session ends. MCP `initialize` performs handshake, auth,
view open and sync inside that same process; there is no SessionStart helper.
Advertised tools keep their existing `mcp__lc__*` names. Legacy `mcp`, `agent`
and `workflow` are deliberately not advertised because their local
implementations start another MCP/LemonCrow process.

```
## Install and point it somewhere

```bash
pip install lemoncrow-client
export LEMONCROW_URL=https://lemoncrow.corp.example   # or: lemoncrow-client target <url>
export LEMONCROW_TOKEN=...                            # or: ~/.lemoncrow/token, mode 0600
lemoncrow-client install                              # auto-register every detected supported host
lemoncrow-client audit                                # prints the resolved claims
```

The registration command uses each coding agent's own MCP configuration CLI; LemonCrow never edits those host config files directly:

```bash
lemoncrow-client install --agent claude
lemoncrow-client install --agent codex
lemoncrow-client install --agent opencode
lemoncrow-client install --agent auto --dry-run
```

All registrations point to the same command: `lemoncrow-client mcp`. The installer is a one-time foreground process; it installs no LemonCrow daemon, listener, SessionStart hook, or second runtime process. During a coding-agent session there is still exactly one LemonCrow process.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEMONCROW_URL` | `http://127.0.0.1:7420` | The **only** product destination this client opens. |
| `LEMONCROW_TOKEN` | — | Bearer credential. |
| `LEMONCROW_TOKEN_FILE` | `~/.lemoncrow/token` | IT-provisioned credential. Must be mode 0600 or the client refuses it. |
| `LEMONCROW_HOME` | `~/.lemoncrow` | The only directory written outside the repository: the token, an `env` file, and `walk-cache/` (one file per worktree, recording which files were unchanged when they were last hashed — paths and digests, never content). |
| `LEMONCROW_LOCAL_FS` | `0` | Offer the same-host read optimization. Off by default. |
| `LEMONCROW_STARTUP_BUDGET_S` | `1.0` | Whole-of-MCP-initialize bootstrap budget. |
| `LEMONCROW_SCM_PROVIDER` / `_SCM_REPO_ID` | — | Canonical SCM identity, when your deployment issues one. |
| `LEMONCROW_STARTUP_BUDGET_S` | `1.0` | Whole-of-MCP-initialize bootstrap budget. |
| `LEMONCROW_SCM_PROVIDER` / `_SCM_REPO_ID` | — | Canonical SCM identity, when your deployment issues one. |

`LEMONCROW_URL` with credentials embedded is refused, and so is a redirect away
from it: the transport compares every request's origin against the configured
one before it is sent.

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
| `web_fetch` | server | keeps egress where policy can see it |
| `statusline_segment`, `review_*` | server | session state and review move server-side |
| `cache` | server, admin | administers server-side index state |

Three boundaries are drawn on purpose, and each is a typed refusal rather than a
silent degradation:

- **`bash` runs bounded foreground commands.** `bg` and `interactive` are
  refused: a client whose claim is "leaves no long-lived process" cannot also
  keep one alive between tool calls.
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

**Version mismatch refuses loudly.** The handshake happens *before*
authentication, so an out-of-date client gets `protocol_version_mismatch` and an
`upgrade_client` action, never a confusing 401 and never a silent downgrade.

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
| Server unreachable at MCP initialize | The same stdio process stays alive with client-side tools only; initialize instructions name the reason; `read` falls back to a bounded file read flagged `degraded`; **no local index is ever built** |
| Server unreachable mid-session | Server-side tools return the typed error; client-side tools unaffected |
| Version mismatch | Refused with `protocol_version_mismatch` + `upgrade_client` |
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

- no `socket.bind` and no listening socket, ever;
- ordinary product `socket.connect` / `urllib` requests go only to configured
  `LEMONCROW_URL`; hosted login adds only the Authward origin advertised by that
  server and verified again through exact-issuer OIDC discovery;
- the only program the session path runs is a read-only `git` query for the
  repository fingerprint and base revision, and the one shell `bash` spawns is
  reaped before the call returns — no child and no thread survives either;
- every file written is under the repository or `~/.lemoncrow`;
- no thread or child process survives the session;
- no unit, plist, `loginctl`, `~/.claude/settings.json` rewrite, tunnel,
  downloader or self-update path exists in the source, by AST scan — read as
  code, so the docstrings explaining their absence do not count as uses;
- the package imports no socket API at all (`socket`, `socketserver`,
  `http.server`, `asyncio`, `selectors`), and only `transport.py` reaches the
  network — a package that never holds a socket cannot bind one;
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
