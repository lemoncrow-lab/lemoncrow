# Pi managed host

Pi is an opt-in frontend for LemonCrow's owned coding runtime. LemonCrow pins the reviewed upstream Pi release **v0.84.2** at commit `914cf1472e715297caa30db4b9535d534a9eb718`; the managed installer verifies the platform asset against the release SHA-256 before replacing the host binary.

```bash
lc code host install --engine pi
lc code host status --engine pi
lc code --engine pi
lc code --engine pi -p "fix the failing parser test"
lc code --engine pi --resume <pi-session-id>
```

`auto` still prefers LemonCode. To canary Pi without changing that default, set `LEMONCROW_CODE_AUTO_ENGINE=pi`. Remove the variable for the immediate rollback path.

## Managed boundary

The launcher isolates Pi under `<LEMONCROW_ROOT>/hosts/pi/`, loads only `integrations/pi/managed.mjs`, and starts Pi with project trust, context-file discovery, built-in tools, skills, prompt templates, package refresh, telemetry, update checks, compaction, and retries disabled. Pi talks only to the token-authenticated LemonCrow loopback gateway. The extension removes Pi system/developer messages and host tool transcripts before the provider request, blocks model tool calls, and replaces `!` shell execution with a denied result. LemonCrow remains the only model/tool execution loop.

Pi's model picker is populated from LemonCrow's exact runnable provider/model catalog. Selecting a model therefore pins the request that the gateway receives rather than selecting an independent Pi provider.

## Status and sessions

The extension renders LemonCrow's structured status sidecar in Pi TUI mode using Pi's status/widget APIs; print mode performs no UI work. Sessions are stored in the isolated managed Pi session directory. `lc import --host pi` also discovers the external Pi default session directory and imports v3 JSONL without modifying it. The importer preserves the complete raw branching file while traces and replay use the active parent chain.

```bash
lc import --host pi
lc import --host pi --path "/path/with spaces/session.jsonl"
```

## Source build and removal

```bash
lc code host build --engine pi --source /path/to/pi
lc code host update --engine pi
lc code host remove --engine pi --yes
```

Source builds are a developer path; release installs are the normal pinned path. LemonCode remains supported during the migration and is not removed by any Pi host command.
