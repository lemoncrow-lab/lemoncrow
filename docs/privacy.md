# Privacy & network behavior

LemonCrow is a **local runtime**. It runs on your machine, works fully offline
after install, and — by default — makes **no network requests to
LemonCrow-controlled servers**. There is no account, no license check, and no
usage reporting.

## What runs locally

Everything core: initialization, indexing a repository, code search and graph
queries, context assembly, memory, the MCP server, host integrations, reports,
and status. None of these contact a LemonCrow server or require an account.

## Which commands make network requests

| Command / action | Destination | When |
|---|---|---|
| `lc update` | GitHub Releases API for `lemoncrow-lab/lemoncrow` | Only when you run it |
| Model / embedding calls | The provider **you** configured (Anthropic, OpenAI, Ollama, …) | Only when a capability calls your configured model, using your API key |
| Optional dependency bootstrap | Upstream project releases (e.g. ast-grep, Hugging Face models) | Only for optional features you enable; checksum-verified where applicable |
| Startup auto-update | GitHub (`origin`) | **Opt-in only**: set `LEMONCROW_AUTO_UPDATE=1` |
| Remote telemetry | See below | **Opt-in only** |

User-configured model-provider calls are the product's core function and are
**not** LemonCrow telemetry.

## Telemetry

Remote telemetry is **OFF by default** and strictly opt-in.

- **Inspect** what is collected locally: `lc telemetry show`
- **See status:** `lc telemetry status --json`
- **Enable** anonymous remote telemetry: `lc telemetry remote on`
- **Disable** again: `lc telemetry remote off`
- **Global kill switch:** set `DO_NOT_TRACK=1` or `LEMONCROW_TELEMETRY=off` in
  your environment; remote telemetry is then never emitted regardless of config.

When telemetry is disabled (the default), **nothing leaves the machine**: no
identifier, no repository path, no source code, no symbol names, and no account
lookup.

Even when you opt in, the event schema is deliberately minimal: event names,
bucketed durations, and SHA-256-hashed identifiers only — never raw source code,
file paths, symbol names, or command arguments (these are scrubbed before
emission). There is no crash reporting.

## Local installation identifier

LemonCrow stores a single random, locally-generated identifier
(`~/.config/lemoncrow/telemetry_id`, a UUID) so that, *if* you opt into
telemetry, repeated events can be de-duplicated. It is:

- Randomly generated locally — **never** derived from hardware, MAC address,
  hostname, username, or disk serial.
- Never transmitted unless you opt into telemetry.
- Resettable at any time: `lc telemetry reset-id`.

## Commit attribution

LemonCrow can add a `Co-Authored-By: lemoncrow` trailer to commits made through a
LemonCrow-backed agent. This is **off by default**. Opt in at install time with
`LEMONCROW_ATTRIBUTION=1`, which installs a workspace-local
`.git/hooks/prepare-commit-msg` hook. `scripts/uninstall.sh` removes the hook (and
the block it appended to any pre-existing hook).

## Optional hosted account

`lc account login` is an optional convenience for linking a hosted account. It
is never required, never prompted, and never gates any feature. If you never run
it, LemonCrow behaves identically.

## Removal

See the README's *Removal* section and `scripts/uninstall.sh`. Use
`scripts/uninstall.sh --purge` to remove all LemonCrow-managed local state,
including the telemetry identifier and config.
