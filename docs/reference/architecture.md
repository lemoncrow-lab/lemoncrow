# LemonCrow architecture

LemonCrow is infrastructure around coding agents. The architecture is split by product responsibility rather than one monolithic local/server runtime.

## Product layers

```text
                         LemonCrow
                            |
          +-----------------+-----------------+
          |                 |                 |
      Workspaces         Review          Enterprise
          |                 |                 |
  agent context/tools   human review     shared policy,
  + optional Pro loop   + evidence       workspaces, audit
```

Workspaces comes first in the coding flow; Review is the human inspection surface after or during that work. Enterprise adds shared infrastructure and organization controls without changing that basic model.

## Workspaces: keep your agent

Claude Code, Codex, OpenCode, and Cursor can keep owning their normal agent/model UX while LemonCrow adds repository intelligence and tools around them.

```text
existing coding agent
        |
LemonCrow plugin/integration
        |
local repository intelligence
        |
     your repo
```

The local Workspaces path:

- keeps the host's model/provider account and saved settings;
- builds/reuses an incremental repository index under `.lemoncrow/workspace/`;
- does not require a LemonCrow account;
- does not upload repository source to LemonCrow;
- does not require a resident Workspaces daemon or listening port.

Claude Code, Codex, and OpenCode support `lc with <host>` for a temporary launch and `lc rt enable <host>` for persistent integration. Cursor uses its installed IDE/CLI integration.

See [Workspaces Lite architecture](../architecture/runtime-lite.md).

## Workspaces: LemonCrow owns the coding loop

`lc code` is reserved for LemonCrow's hosted coding agent, which is not yet available. Workspaces Lite stays local through existing-agent integrations such as `lc with`, persistent host integration, and `lc runtime index`.

```text
        Pi / Codex / Claude
                 |
        LemonCrow-owned loop
          /       |        \
      model      tools    evidence
                 |
              your repo
```

Hosted/Enterprise owned execution may use Pi, Codex, or Claude as implementation/frontends. Pi remains a public managed host; Pro is a Hosted access plan.

`lc code` always means the Hosted owned agent, which is not yet available. Local Workspaces Lite uses the agent integrations instead. Hosted retrieval, which a Pro plan grants, is the remote/private path for deeper retrieval and richer relationships; cross-workspace context, model routing, cache policy, spend ceilings, and managed execution are not yet available. Enterprise Workspaces places that private capability in customer-controlled or dedicated infrastructure. Pro is a commercial plan, not an engine.

The owned loop can start short-lived local helper/gateway processes as implementation details. Those are not the same thing as requiring a persistent LemonCrow Workspaces service.

## Review

Review is a separate product boundary.

```text
working tree / commit / PR
          |
       Revision
          |
  Review Reader + evidence
          |
   human judgment/state
```

A Review is durable across revisions of the same change. Each capture creates an immutable revision. Human review state is reconciled against the next revision: unchanged reviewed work can stay reviewed; changed or new work returns to the queue.

Review stores and renders:

- the actual diff;
- review targets and reading order;
- impact and provenance where available;
- recorded checks/evidence;
- comments/discussion;
- durable human review state;
- author/reviewer handoff state.

Local and hosted Review can use shared HTTP/application services because Review is a collaborative browser product. That service boundary should not be confused with the local Workspaces plugin path.

See [Review reference](review.md).

## Enterprise

Enterprise adds shared infrastructure around Workspaces and Review:

```text
developer clients / agents
          |
 enterprise Workspaces + server plane
   /          |             \
identity    workspaces      policy/audit
   \          |             /
       model providers
```

Enterprise capabilities include:

- remote/shared code intelligence;
- identity and access control;
- organization policy and provider boundaries;
- the Workspaces server, which runs agent tasks in sandboxes and lands them through Review (offered within Enterprise);
- usage attribution and chargeback;
- audit/evidence retention;
- customer-controlled or dedicated deployment;
- Workspaces controls around broader long-running agent workloads.

Coding is the shipped and measured workload for Workspaces today. Broader agent workloads belong to the Enterprise scope rather than the core `/runtime` story.

## Public and private code-intelligence boundary

Workspaces Lite is public/open-source and intentionally lightweight. It includes deterministic local lexical/indexed repository intelligence and local coding tools.

The private/Pro engine can add capabilities that are deliberately absent from Lite, including advanced semantic/hybrid retrieval, learned ranking, richer graph/lineage, and cross-workspace intelligence.

Public code must not silently import or depend on the private engine. See [OSS/private code-intelligence boundary](../architecture/oss-private-code-intelligence.md).

## Evidence, not hidden reasoning

LemonCrow records observable execution evidence: tool calls, provider usage where available, checks, diffs, provenance, and Review state. It does not expose or depend on hidden model chain-of-thought as a product feature.

Different hosts expose different evidence. Claude Code, Codex, OpenCode, Cursor, Pi, and other integrations therefore have different capture confidence. See the [host capability matrix](../hosts/host-capability-matrix.md).

## Network boundaries

There is no single network story for all LemonCrow products:

- **Local Workspaces plugin:** repository intelligence and tool execution stay local; model traffic follows the external host/provider.
- **`lc code`:** LemonCrow's hosted coding agent (not yet available).
- **Workspaces Pro:** hosted retrieval, a remote private engine for search and code relationships, explicitly selected by the user.
- **Enterprise Workspaces:** the private engine in customer-controlled/dedicated infrastructure.
- **Review:** local or hosted depending on configured Review deployment.
- **Enterprise:** uses the customer-controlled or dedicated infrastructure chosen for that deployment.

See [Privacy & network behavior](../setup/privacy.md).

## Low-level protocol layer

Host integrations use different native surfaces: plugins, hooks, config files, session stores, and protocol adapters. Most users should not need to know which transport a host uses.

Integration authors can use [Protocol / MCP reference](../sdk/mcp.md) for the low-level transport/tool contract.
