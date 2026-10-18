# Privacy & network behavior

Local Runtime repository intelligence and tool execution run on the developer machine. Using the Runtime plugin with Claude Code, Codex, OpenCode, or Cursor does not require a LemonCrow account and does not upload repository source to LemonCrow.

Your local coding agent can still make its normal model-provider requests when using Runtime Lite integrations such as `lc with`. Those provider calls are not LemonCrow telemetry. `lc code` is different: it is the Hosted LemonCrow-owned agent and follows the network/data boundary of that Hosted deployment.

Review, Hosted Runtime, Enterprise Runtime, and self-hosted/shared-service deployments have different network boundaries. This page calls those out separately rather than treating every LemonCrow product as one local server.

The installed `lc` client (Review and Runtime Lite) and the Local Review server it installs send no product telemetry: `lc audit` prints `"product_telemetry": false`, and there is no switch to turn off. The anonymous aggregate telemetry described under [Telemetry](#telemetry) belongs to the full LemonCrow engine you run from a source checkout (`uv run lc ...`). It is on by default there and contains no source code or prompts. Turn it off with `uv run lc rt telemetry remote off`, `DO_NOT_TRACK=1`, or `LEMONCROW_TELEMETRY=off`.

## What runs locally

For the local Runtime path, repository indexing, code search, reads, edits, shell/tool execution, and generated repository state stay on the machine. Runtime Lite does not require a resident LemonCrow daemon or listening Runtime port.

The local repository index lives under `.lemoncrow/workspace/` inside the Git worktree.

## Which actions make network requests

| Command / action | Destination | When |
|---|---|---|
| Existing-agent model calls | The provider/account already used by Claude Code, Codex, OpenCode, Cursor, etc. | According to that host's normal behavior |
| `lc auth login` | The configured hosted LemonCrow/Authward deployment | Only when you sign in |
| Hosted / Enterprise Runtime | The deployment you explicitly selected | Only when you use that managed path |
| `lc update` | The release installer on GitHub Releases for `lemoncrow-lab/lemoncrow` | Only when you run it |
| Optional dependency/host bootstrap | Upstream project releases used by an enabled feature or managed frontend | When you install/update that component |
| Daily update check (full engine) | GitHub Releases API for `lemoncrow-lab/lemoncrow` | Only when enabled in settings |
| Remote telemetry (full engine only) | The configured telemetry destination | On by default in a source checkout; disable with `uv run lc rt telemetry remote off`. The installed client sends none |
| Make a wish (Review menu) | `lemoncrow.com` | Only when you explicitly press **Send wish** |

Provider traffic is separate from LemonCrow telemetry.

## Telemetry

This section applies to the full LemonCrow engine run from a source checkout
(`uv run lc ...`). The installed `lc` client and the Local Review server send no
product telemetry, and `lc rt telemetry` in the installed client only says
that. In a source checkout, the commands below are `uv run lc ...`.

In the full engine, anonymous remote telemetry is **ON by default**. Turn it off
with `lc rt telemetry remote off`, or set `DO_NOT_TRACK=1` or
`LEMONCROW_TELEMETRY=off`.

- **Inspect** what is collected locally: `lc rt telemetry show`
- **See status:** `lc rt telemetry status --json`
- **Turn off** anonymous remote telemetry: `lc rt telemetry remote off`
- **Turn it back on:** `lc rt telemetry remote on`
- **Global kill switch:** set `DO_NOT_TRACK=1` or `LEMONCROW_TELEMETRY=off` in
  your environment; remote telemetry is then never emitted regardless of config.

Two anonymous daily rollups are sent, and both are deliberately minimal:

- **Savings rollup** — aggregate counts, bucketed durations, and dollar
  estimates, plus a SHA-256 install key, a SHA-256 session key, the LemonCrow
  version, the host source, the retrieval domain, and a timestamp. The exact
  payload is `_payload` in `src/lemoncrow/core/service/telemetry/public_rollup.py`.
- **Event counts** — once per elapsed UTC day, how many times each product
  event happened, grouped by low-cardinality labels only: the event name, the
  command or MCP tool name, the agent host, the outcome (`ok`, an exception
  class name, or an error code), and a duration bucket; plus the same SHA-256
  install key, the LemonCrow version, and the day. Individual events, session
  ids, and repository ids are never sent. The exact rows are built by
  `row_labels` in `src/lemoncrow/core/service/telemetry/event_rollup.py`. Days
  that elapse while telemetry is off are skipped, never sent later.

It contains **no** source code, **no** prompts, **no** file or repository paths,
**no** symbol names, and **no** account lookup. Command arguments are scrubbed
before emission. There is no crash reporting.

Once you turn telemetry off, **nothing leaves the machine** except the update
check you run yourself (`lc update`), and a wish you explicitly send with
**Make a wish** (below).

### Wishes ("Make a wish")

The Review action menu's **Make a wish** dialog sends what you typed to
`lemoncrow.com` when you press **Send wish**. A wish is **not telemetry** and
deliberately ignores the telemetry switch: it is a single, labelled action you
took, not an aggregate about you, and gating it would discard the one thing you
asked to send. Nothing is sent until you press the button, and nothing is sent
from the Review, Code, or any other surface on its own.

What is sent: the message, the contact email **only if you typed one**, the
surface (`review`), and the LemonCrow version. Never code, paths, review
contents, install or account identifiers. The browser never contacts
lemoncrow.com directly — the local server sends it (`submit_feedback` in
`src/lemoncrow/core/service/feedback.py`), and the sink is
`POST https://lemoncrow.com/api/feedback`, a LemonCrow-operated service whose
implementation is not part of this repository; it keeps each wish for 180 days. If
the send fails, nothing is stored locally: the dialog keeps your text and
offers an email instead.

### Where telemetry goes

Local events and the local UI (**Settings → Telemetry**) never leave the machine.
Only the two daily rollups go to the **destination**, which you pick in that page
or with `lc rt telemetry destination`:

| Destination | Sends to |
|---|---|
| `auto` (default) | your LemonCrow server when it is hosted (`LEMONCROW_URL` is not loopback), otherwise `lemoncrow.com` |
| `off` | nothing; local only |
| `lemoncrow` | `https://lemoncrow.com` |
| `server` | the LemonCrow server this install uses, with the token it already presents to that server |
| `custom <url>` | any HTTPS host, with an optional `--token`, that serves `/api/telemetry/rollup` and `/api/telemetry/events` |

```bash
lc rt telemetry destination                         # show the effective destination
lc rt telemetry destination custom https://telemetry.corp.example --token "$TOKEN"
lc rt telemetry destination off
```

The per-event PostHog export and the usage sync are LemonCrow's own services, so
they run only when the destination is `lemoncrow`.

**Administrators** can fix the destination for every user of a machine with a
policy file at `/etc/lemoncrow/telemetry.toml` (macOS:
`/Library/Application Support/LemonCrow/telemetry.toml`; Windows:
`%ProgramData%\LemonCrow\telemetry.toml`):

```toml
[telemetry]
destination = "custom"          # off | lemoncrow | server | custom
endpoint = "https://telemetry.corp.example"
token = "..."                   # optional bearer token
```

Under a policy, the UI and CLI show the destination as managed and refuse
changes, and the development URL overrides are ignored. A policy file that is
unreadable or invalid locks telemetry **off**. `DO_NOT_TRACK=1` and
`LEMONCROW_TELEMETRY=off` still turn remote telemetry off.

## Local installation identifier

LemonCrow stores a single random, locally-generated identifier
(`~/.config/lemoncrow/telemetry_id`, a UUID) so that repeated telemetry events
can be de-duplicated. It is:

- Randomly generated locally — **never** derived from hardware, MAC address,
  hostname, username, or disk serial.
- Only ever transmitted as a SHA-256 hash, and not at all once you turn remote
  telemetry off.
- Resettable at any time: `lc rt telemetry reset-id`.

## Commit attribution

LemonCrow can add a `Co-Authored-By: lemoncrow+<model>` trailer (just `lemoncrow`
when the model is unknown) to commits made through a LemonCrow-backed agent.
It carries no session id. This is **off by default**. Opt in at install time with
`LEMONCROW_ATTRIBUTION=1`, which installs a workspace-local
`.git/hooks/prepare-commit-msg` hook. `lc uninstall` (or, for the full engine, `scripts/uninstall.sh`) removes the hook (and
the block it appended to any pre-existing hook).

## Local identity versus hosted identity

Local Runtime plugin use does not require a LemonCrow account. Generated repository intelligence stays local, and the external coding agent continues using its own provider identity.

A deliberately configured local Review/shared service can use a machine credential plus a Review browser capability. If that service binds only to loopback, do not forward its loopback port to other machines as though it were a hardened remote deployment.

Hosted mode is separate. When `LEMONCROW_URL` points at a hosted LemonCrow server, `lc auth login` performs device authorization against that deployment. Hosted credentials are stored separately from local machine/service credentials: in hosted mode the client ignores the local `~/.lemoncrow/token` file (`$LEMONCROW_HOME/token`) and never sends a local machine token to a hosted origin. To supply an operator-provided credential instead of signing in, set `LEMONCROW_TOKEN_FILE` to its path or `LEMONCROW_TOKEN` to its value. Remote browser Review uses the hosted authorization flow. Signing in does not become a prerequisite for local Runtime use and does not silently change an existing local integration into hosted execution.

## Removal

See the README's *Removal* section. Use `lc uninstall all`
(full engine: `scripts/uninstall.sh --purge`) to remove all LemonCrow-managed local state,
including the telemetry identifier and config.
