# Quickstart — Installed Atelier in 5 Minutes

This guide assumes you want to use the installed product, not work from a source checkout.

## Step 1 — Install Atelier

```bash
curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
```

That installs the `atelier` and `atelier-mcp` commands as user-level console scripts, initializes `~/.atelier`, starts the detached `servicectl` loop, and attempts to start the optional visualization stack when Docker is available.

## Step 2 — Verify the Installed Runtime

```bash
atelier --version
atelier-mcp --version
atelier servicectl status
atelier stack status
```

Expected outcome:

- `atelier` resolves on `PATH`
- `atelier-mcp` resolves on `PATH`
- `servicectl` reports a running background controller
- `atelier stack status` shows whether the optional UI/API stack is already running

## Step 3 — Inspect the Installed Command Surface

The fastest way to orient yourself is to inspect the installed command tree.

```bash
atelier -h
atelier help servicectl
ATELIER_DEV_MODE=1 atelier help context
```

`context`, `search`, `rescue`, and `verify` are developer-mode commands. Without
`ATELIER_DEV_MODE=1` they intentionally return passive `noop` responses.

## Step 4 — Fetch Context for a Task (Dev Mode)

```bash
ATELIER_DEV_MODE=1 atelier context \
  --task "Fix generated output that drifts back after refresh" \
  --domain source.truth \
  --file src/content/generate.py
```

This returns a rendered context block built from the current ReasonBlock store.

## Step 5 — Ask for Rescue After a Repeated Failure (Dev Mode)

```bash
ATELIER_DEV_MODE=1 atelier rescue \
  --task "Apply a live config update" \
  --domain state.change \
  --error "known dead end triggered during apply"
```

Use this when you have enough evidence that the current path is failing and you
want the nearest stored recovery procedure.

## Step 6 — Run a Rubric Gate After the Work (Dev Mode)

```bash
echo '&#123;
  "canonical_identifier_used": true,
  "pre_change_state_captured": true,
  "read_after_write_completed": true,
  "observed_state_matches_intent": false
&#125;' | ATELIER_DEV_MODE=1 atelier verify rubric_state_change_safety
```

Expected: `status: blocked` because a required verification check failed.

## Step 7 — Record an Observable Trace

```bash
echo '&#123;
  "agent": "quickstart",
  "domain": "state.change",
  "task": "Apply a live config update",
  "status": "partial",
  "errors_seen": ["known dead end triggered during apply"],
  "output_summary": "Rescue requested before retrying"
&#125;' | atelier trace record
atelier trace list --limit 5
```

## Step 8 — Check Background Processing and the Optional Stack

The installed runtime includes a detached offline processor.

```bash
atelier servicectl status
atelier worker list
atelier stack status
```

If you want to trigger work manually:

```bash
atelier worker enqueue consolidate_reasonblocks
atelier worker run-once
```

If the installer did not start the UI stack for you, or if you stopped it:

```bash
atelier stack start
```

Then open:

- [http://localhost:3125](http://localhost:3125) for the frontend
- [http://localhost:8787](http://localhost:8787) for the service API

## Step 9 — Connect an Agent Host

The installer already tries to wire supported hosts automatically. If you want to inspect or customize that setup, continue with:

- [docs/hosts/all-agent-clis.md](hosts/all-agent-clis.md)
- [docs/hosts/claude-code-install.md](hosts/claude-code-install.md)
- [docs/hosts/copilot-install.md](hosts/copilot-install.md)
- [docs/hosts/codex-install.md](hosts/codex-install.md)

## Source Checkout Instead?

If you are contributing to Atelier itself rather than using the installed product, use the source workflow from [installation.md](installation.md) and [engineering/contributing.md](engineering/contributing.md).
