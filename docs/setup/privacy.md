# Privacy & network behavior

LemonCrow's public/local runtime runs on your machine and works fully offline
after install: indexing, search, edits, memory, and local Review do not require
a LemonCrow account or commercial license. The one LemonCrow-bound request made
by default is an anonymous telemetry rollup — counts, durations, and dollar
estimates, never code or prompts. Turn it off with `lc telemetry remote off`, or
set `DO_NOT_TRACK=1` or `LEMONCROW_TELEMETRY=off`.

## What runs locally

Everything core: initialization, indexing a repository, code search and graph
queries, context assembly, memory, the MCP server, host integrations, reports,
and status. None of these require an account, and none of them send your source
code, prompts, or file paths anywhere. The only LemonCrow-bound traffic is the
aggregate telemetry rollup described below.

## Which commands make network requests

| Command / action | Destination | When |
|---|---|---|
| `lc update` | GitHub Releases API for `lemoncrow-lab/lemoncrow` | Only when you run it |
| Model / embedding calls | The provider **you** configured (Anthropic, OpenAI, Ollama, …) | Only when a capability calls your configured model, using your API key |
| Optional dependency bootstrap | Upstream project releases (e.g. ast-grep, Hugging Face models) | Only for optional features you enable; checksum-verified where applicable |
| Startup auto-update | GitHub (`origin`) | **Opt-in only**: set `LEMONCROW_AUTO_UPDATE=1` |
| Daily update check | GitHub Releases API for `lemoncrow-lab/lemoncrow` (one anonymous GET, no LemonCrow data) | **Opt-in only**: the interactive installer offers Auto-update, Notify only, or Don't check automatically (the default); or use `lc settings set cli.update_check true`. Off for non-interactive installs |
| Remote telemetry | `lemoncrow.com` rollup endpoint — see below | **On by default**; opt out with `lc telemetry remote off` |

User-configured model-provider calls are the product's core function and are
**not** LemonCrow telemetry.

## Telemetry

Anonymous remote telemetry is **ON by default**. Turn it off with `lc telemetry
remote off`, or set `DO_NOT_TRACK=1` or `LEMONCROW_TELEMETRY=off`.

- **Inspect** what is collected locally: `lc telemetry show`
- **See status:** `lc telemetry status --json`
- **Turn off** anonymous remote telemetry: `lc telemetry remote off`
- **Turn it back on:** `lc telemetry remote on`
- **Global kill switch:** set `DO_NOT_TRACK=1` or `LEMONCROW_TELEMETRY=off` in
  your environment; remote telemetry is then never emitted regardless of config.

What is sent has not changed, and the payload is deliberately minimal: aggregate
counts, bucketed durations, and dollar estimates, plus a SHA-256 install key, a
SHA-256 session key, the LemonCrow version, the host source, the retrieval
domain, and a timestamp. The exact payload is `_payload` in
`src/lemoncrow/core/service/telemetry/public_rollup.py`.

It contains **no** source code, **no** prompts, **no** file or repository paths,
**no** symbol names, and **no** account lookup. Command arguments are scrubbed
before emission. There is no crash reporting.

Once you turn telemetry off, **nothing leaves the machine** except the update
check you run yourself (`lc update`).

## Local installation identifier

LemonCrow stores a single random, locally-generated identifier
(`~/.config/lemoncrow/telemetry_id`, a UUID) so that repeated telemetry events
can be de-duplicated. It is:

- Randomly generated locally — **never** derived from hardware, MAC address,
  hostname, username, or disk serial.
- Only ever transmitted as a SHA-256 hash, and not at all once you turn remote
  telemetry off.
- Resettable at any time: `lc telemetry reset-id`.

## Commit attribution

LemonCrow can add a `Co-Authored-By: lemoncrow` trailer to commits made through a
LemonCrow-backed agent. This is **off by default**. Opt in at install time with
`LEMONCROW_ATTRIBUTION=1`, which installs a workspace-local
`.git/hooks/prepare-commit-msg` hook. `scripts/uninstall.sh` removes the hook (and
the block it appended to any pre-existing hook).

## Local identity versus hosted identity

Local mode uses a machine credential stored under `LEMONCROW_HOME` and a
separate Review-only browser capability. Neither requires a LemonCrow account.
The local server binds to loopback and is not a supported remote-exposure
surface. Standard reverse-proxy forwarding headers disable local browser trust;
a raw TCP tunnel can be indistinguishable from true loopback traffic, so do not
forward port `7420` to other machines.

Hosted mode is separate. When `LEMONCROW_URL` points at a hosted LemonCrow
server, `lc auth login` discovers the server's Authward issuer and performs RFC
8628 device authorization directly with Authward. Authward access/refresh
credentials are stored separately from the local machine credential. Remote
browser Review uses Authward authorization-code + PKCE through the hosted BFF.
Signing in to hosted LemonCrow does not become a prerequisite for local use.

## Removal

See the README's *Removal* section and `scripts/uninstall.sh`. Use
`scripts/uninstall.sh --purge` to remove all LemonCrow-managed local state,
including the telemetry identifier and config.
