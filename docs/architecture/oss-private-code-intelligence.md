# OSS / private code-intelligence boundary

Workspaces Lite is the public local intelligence kernel. Advanced commercial code intelligence is physically owned by the proprietary enterprise runtime.

## Public OSS source

Public source contains:

- `lemoncrow_client/lite/`: SQLite/FTS Workspaces Lite, deterministic lexical search, exact/basic symbols/imports/relations;
- local filesystem/edit/shell primitives;
- `lemoncrow.core.capabilities.code_context_contract`: DTOs and provider protocols;
- `lemoncrow.core.capabilities.code_intelligence_provider`: the single optional private-provider discovery seam;
- neutral code-context rendering, evidence schemas, budget packing and workspace configuration;
- hosted/thin-client protocols and auditable request/response contracts.

Public source must not contain the advanced engine implementation, learned ranking assets, semantic embedding implementation, ANN/vector retrieval, advanced ranking/reranking, graph/lineage algorithms, cross-repository router implementation, or equivalent private intelligence.

## Private enterprise source

The advanced implementation lives physically under:

`enterprise/runtime/src/lemoncrow/pro/capabilities/code_context/`

That private tree owns the legacy `CodeContextEngine` plus embeddings, ANN, ranking/reranking, advanced graph/lineage, cache/store and workspace-routing implementation. The enterprise distribution contributes those modules only when the private package is installed.

Private discovery is centralized through:

`lemoncrow.core.capabilities.code_intelligence_provider`

Public callers do not import private engine modules directly. `lemoncrow_enterprise_runtime.code_intelligence.provider` supplies the implementation to the public factory.

The learned Explore reranker model lives at:

`enterprise/runtime/src/lemoncrow_enterprise_runtime/code_intelligence/explore_reranker_model.json`

Substrates whose only consumer is private moved private on 2026-10-04: the engine's LSP client, Markdown symbol extractor, cross-language edge store and resolvers, git-history blame/summarizer/embedder, the `generated_files` / `output_policy` helpers and the fusion-weight `eval_harness` now live in the private `code_context` overlay (`...code_context.lsp`, `.markdown`, `.cross_lang`, `.git_history`); `mcp_connectors` and Review `change_edits` moved to `lemoncrow_server`. `scripts/audit_oss_source_boundary.py` (`PRIVATE_ONLY_PUBLIC_PATHS`) keeps them out of public source. The rest of `lemoncrow.pro.code_intel.git_history` (walker, renames, deleted-symbol store) stays public: Review and the basic Dashboard use it.

## Product behavior without private code

An OSS-only install remains valid:

- `lc with <host>` uses Workspaces Lite;
- `lc rt enable <host>` persists Workspaces Lite;
- `lc with <host>` and persistent local integrations use Workspaces Lite locally; `lc code` is reserved for the hosted coding agent, which is not yet available;
- `lc runtime index` builds the Lite index;
- Review keeps its independent product boundary;
- hosted thin-client protocol code remains importable.

Advanced customer paths are explicit and remote/dedicated. The old `lc code --pro` mode is removed entirely. Local private-engine execution survives only as guarded LemonCrow engineering tooling; public Workspaces Lite operates without the private package installed.

## Enforcement

`scripts/audit_oss_source_boundary.py` fails if any extracted advanced module, trained/model asset, or direct import of an advanced module reappears under public `src/lemoncrow`. In the private monorepo it additionally verifies that the expected modules and reranker asset exist on the enterprise side. The audit is also safe in an OSS-only checkout where `enterprise/` does not exist.
