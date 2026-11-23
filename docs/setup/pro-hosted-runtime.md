# Workspaces Pro: hosted retrieval

Workspaces Pro gives your coding agents LemonCrow's hosted retrieval. That means hosted repository search, semantic retrieval and code-relationship (graph) retrieval, served to Claude Code, Codex or OpenCode over remote MCP.

Workspaces Pro does not run agent tasks. Agent tasks run locally with Workspaces Lite; see the [Workspaces Lite quickstart](./workspaces-lite-quickstart.md).

Pro is sold by the LemonCrow team and set up by invitation. There is no self-serve checkout. To start, email [contact@lemoncrow.com](mailto:contact@lemoncrow.com?subject=Pro%20hosted%20retrieval) with the subject `Pro hosted retrieval`. Once your organization is provisioned, LemonCrow emails an invitation to the administrator you named.

## What Pro includes

| Included in Pro | Not included |
| --- | --- |
| Hosted retrieval for Claude Code, Codex and OpenCode (`lc rt enable <host> --hosted`) | LemonCrow Review. [Hosted Review](./hosted-review.md) is sold separately |
| Hosted code search, semantic retrieval and graph retrieval for the repositories you work in | Enterprise Workspaces in your own infrastructure |
| Sign-in with your LemonCrow identity, approved in the browser | |

**Not yet available**, in Pro or anywhere else today:

- Managed model and provider routing.
- The Hosted owned coding loop (`lc code`).
- Spend controls and cache orchestration.
- Cross-workspace context outside Review.
- Self-serve checkout.

If you call a product your plan does not include, the server refuses it and says why, for example: `the LemonCrow Pro plan includes Workspaces Pro only; LemonCrow Review is not part of this plan. Ask your LemonCrow contact to add it`. Hosted Review is sold separately; to add it, ask your LemonCrow contact.

## 1. Accept the invitation

Open the invitation link from LemonCrow and sign in with the identity the invitation was sent to. If you signed in before your organization was ready, you saw **Invite required**. That is expected, and the invitation clears it.

After sign-in the LemonCrow web app opens on **Account**, which names your plan and shows the setup commands from steps 2 to 4. From there an administrator invites the team and manages people, audit history and usage under **Organization**. Review is not in Pro, so the web app shows no Review pages or Review settings. A Review link opens a page that says so.

## 2. Install the Workspaces client

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product workspaces --hosted
```

A bare install is local, so pass `--hosted` for the LemonCrow cloud (`https://api.lemoncrow.com`). With `--product workspaces --hosted` the installer installs the Workspaces client configured for that endpoint. Check it:

```bash
lc --version
lc status
```

See [Installation](./installation.md) for the other installer flags.

## 3. Enable hosted retrieval for your coding agent

```bash
lc rt enable claude --hosted     # or: codex, opencode
```

This registers LemonCrow's hosted retrieval MCP server (`lc rt mcp`) with the agent. It does not sign you in. Without `--hosted`, `lc rt enable` registers the local Workspaces Lite instead. Signing in never silently converts a local integration into a hosted one.

Restart the agent so it loads the `lc` tools, then check the integration:

```bash
lc rt status
```

The agent's row should read `enabled · Hosted`.

## 4. Sign in and approve this device

```bash
lc auth login
```

`lc auth login` uses the device flow. It prints a verification page and a one-time code:

```text
Open https://...
Code: ABCD-EFGH
```

It also opens that page in your browser. On a machine without a browser, use `lc auth login --no-browser` and open the page on another device. Sign in with your LemonCrow identity, confirm the code matches, and approve the device. The command then prints `Signed in to https://api.lemoncrow.com`.

Check the session at any time:

```bash
lc auth status      # hosted: signed in to https://api.lemoncrow.com with Authward
```

If `lc auth login` prints `Local LemonCrow needs no login.`, run `lc status` and look at the **Deployment** lines:

- **Workspaces shows `lite`**: the Workspaces client was installed without `--hosted`. Rerun the installer exactly as in step 2 (`--product workspaces --hosted`).
- **Workspaces shows `hosted` but Review shows `local`**: this machine also has Local Review, and `lc` is the Review client, whose local install needs no login. The Workspaces client is installed next to it as `lemoncrow-runtime-client`. Sign in with that instead: `lemoncrow-runtime-client auth login`, then check with `lemoncrow-runtime-client auth status`. Your Local Review is not changed.

## 5. Verify that search works

First confirm the setup: `lc status` should show Workspaces with **Deployment** `hosted`, and `lc rt status` should show your agent as `enabled · Hosted`. Neither command contacts the server; the search below is what proves the hosted connection works.

Inside a Git worktree, start your agent and ask it to use LemonCrow search. For example: "Use the lc `code_search` tool to find where we parse configuration." The agent should call `code_search` and return matching files and line ranges from your repository. The first search in a repository also uploads and indexes the files it needs, so give it a moment.

If a tool call is refused, read the message:

- `not signed in` or an expired session: run `lc auth login` again.
- `the verified identity is not mapped to an organization on this server`: your identity is not yet in your organization. Ask your LemonCrow contact.
- A product named outside hosted retrieval (for example Review): that product is not in Pro, as listed above.
- A quota refusal names the quota and the scope that was reached. Retry later if it says `retry_later`. Otherwise ask your LemonCrow contact for a higher limit.

## Remove hosted retrieval

```bash
lc rt disable claude
lc auth logout
```

See [Troubleshooting](./troubleshooting.md) for general installation problems and [Privacy & network](./privacy.md) for what leaves your machine.
