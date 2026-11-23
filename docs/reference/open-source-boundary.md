# Open-source boundary

This repository is the open-source (Apache-2.0) part of LemonCrow. It is a
complete local product on its own. Workspaces Lite, the local mode, is open
source and needs no account and no server. A separate, private LemonCrow
distribution adds advanced commercial code intelligence and hosted
organization services; nothing in this repository requires it.

## What this repository owns

- **Workspaces Lite**: the local code index and search used by `lc with <host>`,
  `lc rt enable <host>` and `lc runtime index`: SQLite/FTS lexical search,
  exact and basic symbol, import and relation lookup. It runs inside the
  agent's own stdio MCP process; no LemonCrow server, daemon or listener is
  involved. The opt-in remote MCP gateway (`lc runtime mcp serve`) is part of
  the public client and runs Workspaces Lite in its own process.
- **Workspaces Lite agent tasks**: `lc verify` and the local `lc task` flow,
  which gives each task its own worktree and lands it on `main` when the
  checks pass. The client side of `lc task --server URL` is public too.
- **Local Review and the basic Dashboard**: `lc review`, the optional local
  server and frontend. The local server carries Review and a read-only
  Dashboard (sessions, usage, savings, settings, system, and the deterministic
  Code map over the Workspaces Lite index, whose relationships are import-level);
  it executes no Workspaces tools.
- **Local execution primitives**: read, edit, shell, web fetch, source
  projection, ast-grep and zoekt adapters, git primitives, memory and
  verification.
- **Neutral contracts**: provider protocols, request/response models, index
  layer ports and evidence schemas that a private implementation can plug into.
- **Auditable hosted clients**: the thin clients and protocols that talk to a
  hosted LemonCrow service. Their requests are visible in this source.

## What is not in this repository

- The advanced code-context engine and its ranking, fusion and graph analysis,
  and the substrates only it uses (LSP client, cross-language edges, git-history
  blame and commit summaries).
- Learned or model-based components: code-embedding models, cross-encoder
  rerankers, learned ranking models, and the tooling that trains them.
- Approximate-nearest-neighbour (ANN) and semantic vector indexes, including
  the server's semantic search layer.
- Server-side tool execution (the hosted server's tool dispatch, server code
  search and per-call tracing) and the Dashboard pages beyond the basic set
  (knowledge, swarms, Code Ask, the editor launcher, precise references and
  module purposes).
- Hosted organization, policy and service behaviour.
- The Workspaces server, which runs agent tasks in sandboxes and lands them
  through Review. It is not a public download: LemonCrow offers it
  within Enterprise.

## Behaviour without the private distribution

Everything above works unchanged. Paths that need the private distribution say
so explicitly instead of degrading silently:

- public `lc code` is a Hosted client entry point; the OSS build contains no local paid/Pro execution mode;
- the old `lc code --pro` compatibility mode is removed entirely;
- guarded private-development commands fail with `Private code intelligence is not installed/available.` when the proprietary package is absent;
- advanced model/embedding configuration is not a public Workspaces Lite control.
- The local server's semantic layer reports the state `unavailable`
  (`semantic_engine_unavailable`). Semantic queries return a degraded, empty
  answer and no semantic background work is queued.
- The MCP `context` tool still returns playbook and memory context. Its
  repository bootstrap stays `cold` and no bootstrap job is queued, because the
  bootstrap plan is built by the private engine.

The private distribution plugs in through two public seams:
`lemoncrow.core.capabilities.code_intelligence_provider` (engine, embedders,
training) and `lemoncrow_server_core.index.semantic` (server semantic layer).
Public code never imports private modules directly.

## Enforcement

- `scripts/audit_oss_source_boundary.py` checks every file the public mirror
  publishes, by content rather than by file name: model, ANN and
  learned-ranking libraries and classes, imports or names of private modules
  outside the two seams, ANN-index implementations, learned model assets, and
  model dependencies in `pyproject.toml` or `requirements*.txt`.
- `scripts/audit_release_boundary.py` applies the same content rules to every
  public wheel, including the Review engine artifact.
- `tests/test_mirror_denylist.py` checks that published files never reference
  a script the mirror omits, never read a tracked file the mirror omits (paths
  built from `Path(__file__)`) and never link one from Markdown, and that private
  submodules are never named in the public `.gitmodules`.
- Tests of private code live under `tests/private/`, which is never published
  and is skipped when the private distribution is absent. A public test that
  needs the private engine for one case carries the `private_engine` marker.

## Earlier releases

Releases up to and including v0.7.4, and the public repository state mirrored
on 2026-09-24, published the advanced engine source, the BGE and nomic
embedders, the cross-encoder reranker and the learned Explore reranker model
under Apache-2.0. Those published versions keep their license. Later versions
no longer contain that code.
