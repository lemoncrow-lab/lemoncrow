# Pi in LemonCrow Workspaces

Pi is a public coding-agent host supported by LemonCrow. It is not a paid Workspaces mode and it is not hidden behind Hosted or Enterprise.

LemonCrow manages a reviewed, pinned Pi binary through the public `lc rt host` surface:

```bash
lc rt host install --engine pi
lc rt host status --engine pi
lc rt host update --engine pi
lc rt host remove --engine pi --yes
```

Source builds are also explicit:

```bash
lc rt host build --engine pi --source /path/to/pi
```

The managed-host lifecycle verifies the pinned release and keeps Pi separate from the LemonCrow CLI itself.

Running `lc rt host install --engine pi`, registers
LemonCrow in `~/.pi/agent/settings.json`. The extension lives at
`~/.pi/agent/lemoncrow/lite.mjs`. Both paths respect `PI_CODING_AGENT_DIR`.
Existing models, authentication, settings, and other extensions are preserved.
Normal Pi launches inside a Git repository load LemonCrow automatically.

The downloaded executable remains at `~/.lemoncrow/bin/pi-host`.
`lc rt host remove --engine pi --yes` removes the executable and LemonCrow’s Pi
registration; uninstalling LemonCrow also removes its Pi integration.

## Relationship to Workspaces Lite

Pi is a coding agent, just like Claude Code, Codex, and OpenCode. The public Pi host lifecycle is therefore part of the open-source Workspaces distribution.

`lc with pi` launches the normal Pi agent with LemonCrow Workspaces Lite tools. It reuses the installed extension when present and otherwise loads a temporary extension without writing Pi settings. Pi keeps its own provider/model/session behavior; this is not LemonCrow-owned execution. Installing or using Pi this way does not turn on hosted retrieval or require a Pro plan.

## Relationship to Hosted `lc code`

`lc code` is reserved for LemonCrow's hosted coding agent, which is not yet available. LemonCrow's hosted execution may use Pi as one implementation/frontend choice, but that does not make Pi itself private. The private boundary is LemonCrow's hosted execution, routing, policy, budgets, cache orchestration, and advanced intelligence—not the Pi binary.

A Pro plan grants access to hosted retrieval. Enterprise Workspaces can place the same class of private LemonCrow execution in customer-controlled or dedicated infrastructure.

## Internal managed-mode qualification

LemonCrow engineering also uses Pi in a fail-closed managed mode when qualifying the private owned loop. In that mode Pi-native tools and competing model/tool loops are disabled while LemonCrow owns execution. That internal managed-mode wiring is separate from the public `lc rt host` lifecycle above.

Managed Pi reaches models only through the LemonCrow gateway. It runs with its own configuration directory under the LemonCrow store (`PI_CODING_AGENT_DIR`), so it never reads the authentication or settings in `~/.pi/agent`. Pi's built-in providers also sign in from environment variables, and the pinned Pi release has no setting that turns a built-in provider off. LemonCrow therefore removes those variables from the managed Pi process: every `*_API_KEY` and `AWS_*` variable, plus `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_OAUTH_TOKEN`, `COPILOT_GITHUB_TOKEN`, `HF_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `GCLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`. Without this, a credential in your shell would list that provider's models next to LemonCrow's and let the session switch to it, bypassing the gateway. Your shell keeps these variables, and normal Pi launches and `lc with pi` are unaffected.
