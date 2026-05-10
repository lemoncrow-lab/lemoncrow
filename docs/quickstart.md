# Quickstart — Installed Atelier in 5 Minutes

This guide assumes you want to use the installed product, not work from a source checkout.

## Step 1 — Install Atelier

```bash
curl -fsSL https://raw.githubusercontent.com/leanchain/atelier/main/scripts/install.sh | bash
```

That installs the `atelier` and `atelier-mcp` commands, initializes `~/.atelier`, and starts the detached `servicectl` loop.

## Step 2 — Verify the Installed Runtime

```bash
atelier --version
atelier-mcp --version
atelier servicectl status
```

Expected outcome:

- `atelier` resolves on `PATH`
- `atelier-mcp` resolves on `PATH`
- `servicectl` reports a running background controller

## Step 3 — Check a Plan Before Doing the Work

The fastest way to feel what Atelier does is to run a plan check.

```bash
atelier lint \
  --task "Apply a live config update" \
  --domain state.change \
  --step "Resolve target from URL slug alone" \
  --step "Apply the change"
```

Expected output looks like:

```text
status: blocked
exit: 2
warnings:
  - dead end: resolve target from url slug alone
  - suggested plan uses a canonical identifier plus read-after-write verification
```

Now try the safer version:

```bash
atelier lint \
  --task "Apply a live config update" \
  --domain state.change \
  --step "Resolve and record the canonical identifier" \
  --step "Capture pre-change state" \
  --step "Apply the change" \
  --step "Read back the state and diff against intent"
```

Expected: `status: pass`

## Step 4 — Get Reasoning Context for a Task

Before an agent starts work, fetch relevant procedures and constraints:

```bash
atelier reasoning \
  --task "Fix generated output that drifts back after refresh" \
  --domain source.truth \
  --file src/content/generate.py
```

This returns a structured context block with matched ReasonBlocks, dead ends, and runtime guidance.

## Step 5 — Run a Rubric Gate After the Work

```bash
echo '&#123;
  "canonical_identifier_used": true,
  "pre_change_state_captured": true,
  "read_after_write_completed": true,
  "observed_state_matches_intent": false
&#125;' | atelier verify rubric_state_change_safety
```

Expected: `status: blocked` because a required verification check failed.

## Step 6 — Check Background Processing

The installed runtime includes a detached offline processor.

```bash
atelier servicectl status
atelier worker list
```

If you want to trigger work manually:

```bash
atelier worker enqueue consolidate_reasonblocks
atelier worker run-once
```

## Step 7 — Start the Optional UI Only If You Want It

The UI is not required for CLI or MCP usage.

```bash
atelier stack start
```

Then open:

- [http://localhost:3125](http://localhost:3125) for the frontend
- [http://localhost:8787](http://localhost:8787) for the service API

## Step 8 — Connect an Agent Host

The installer already tries to wire supported hosts automatically. If you want to inspect or customize that setup, continue with:

- [docs/hosts/all-agent-clis.md](hosts/all-agent-clis.md)
- [docs/hosts/claude-code-install.md](hosts/claude-code-install.md)
- [docs/hosts/copilot-install.md](hosts/copilot-install.md)
- [docs/hosts/codex-install.md](hosts/codex-install.md)

## Source Checkout Instead?

If you are contributing to Atelier itself rather than using the installed product, use the source workflow from [installation.md](installation.md) and [engineering/contributing.md](engineering/contributing.md).
