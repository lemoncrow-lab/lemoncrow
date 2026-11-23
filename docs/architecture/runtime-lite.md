# Workspaces Lite architecture

Workspaces Lite is LemonCrow's Apache-2.0 local repository-intelligence kernel. It runs on the developer machine, requires no LemonCrow account or application server, and does not upload repository source to LemonCrow.

## Product model

Repository intelligence for your coding agent comes in three deployments:

1. **Workspaces — local / open source.** Workspaces Lite powers `lc with claude|codex|opencode|pi`, Cursor's installed integration, `lc runtime index`, and persistent local integrations such as `lc rt enable claude`. Pi host lifecycle is also public under `lc rt host`.
2. **Workspaces Pro — hosted retrieval, private / LemonCrow-operated.** Deeper retrieval and richer code relationships, served to the agent over remote MCP. Workspaces Pro is the commercial plan that grants access to hosted retrieval. Cross-workspace context, model/provider routing, budgets, prompt-cache orchestration, and managed execution are not yet available.
3. **Enterprise Workspaces — private / customer-controlled.** The same class of private engine capabilities can run in customer-controlled or dedicated infrastructure with identity, policy, managed workspaces, governance, and audit.

Agent tasks are a separate axis. Workspaces Lite runs them on the developer machine (`lc task`). The Workspaces server, which runs them in sandboxes, is offered only within Enterprise; it is not a self-run download. See [Workspaces and Review](workspaces-and-review.md).

`Pro` is a plan name, not a Workspaces engine, package, or execution location. There is no customer-facing Local Pro Workspaces.

## Local repository state

Durable Workspaces Lite lexical intelligence lives at:

```text
<repo>/.lemoncrow/workspace/runtime-lite-v3.sqlite3
```

When the optional semantic helper is enabled, its compact generation files live separately at .lemoncrow/workspace/runtime-lite-semantic/ inside the repository. The helper reuses unchanged quantized chunks across generations and atomically switches generation files. Stale generation files are pruned after the switch; already-mapped readers keep their open file mapping on platforms that permit unlinking mapped files.

The repository walker excludes `.lemoncrow`. LemonCrow adds `.lemoncrow/workspace/` to the clone-local `.git/info/exclude`, so the index stays out of commits without modifying the committed `.gitignore`.

SQLite uses WAL. The schema-versioned filename keeps older MCP sessions on their own schema: upgrading a newer runtime never rewrites an older session's legacy database. On first use of v3, a supported legacy v1/v2/v3 database can be copied through SQLite's online backup before the v3 copy is migrated; the source is preserved. Incremental generations allow readers to keep using the last committed generation while one writer updates changed files.

A linked Git worktree whose repository has a main checkout with a current index stores an overlay on that index (schema 4, `lite/index.py`): its own rows hold only files that differ from the main checkout or are absent from it, and an `inherit` table lists the files whose content equals the main checkout's, by digest. A main-checkout row is visible to the worktree only while its digest still equals the inherited one, so a change in the main checkout never serves wrong content; the worktree refreshes when the main index's generation moves. Reads attach the main index read-only and serve unqualified `files`, `symbols` and `imports` as views over own plus visible inherited rows; full-text matches from both indexes are merged by rank (`lc-debt`: the two BM25 corpora differ). A bare repository's worktrees, and a worktree whose main checkout has no index yet, keep a full standalone index. `open_index_reader` is the read entry point for code outside the MCP server.

There is no resident Workspaces Lite daemon, no Workspaces Lite listening port, and no watcher service. Indexed operations refresh stale local state when needed.

The stdio MCP server answers `initialize` immediately and brings the index current on one background thread owned by that session; EOF stops and joins it. A cold build of a large repository can take minutes, longer than a host's MCP connect timeout. `code_search`, `relations`, `graph` and `read symbol=` wait up to 20 seconds for the build. After that `relations`, `graph` and `read symbol=` return `[lc:index_building]` with files done/total and elapsed time, and `code_search` answers by grep. File reads, grep, edit and bash never wait. `lc runtime index` builds the same index in the foreground with a progress bar.

`code_search` never makes an agent retry because of the index. When the index failed, needs a newer tool, is still building or is empty, or when a search finds nothing in a healthy index, it answers from the files with the same grep engine a folder without Git uses: one walk for the whole query and each of its terms, keeping the files that hold the most of them. If a bound stops that walk early the answer says `[lc:grep_partial]` rather than `no matches`. The answer is a success that starts with `[lc:index_fallback reason=...]`, with `structuredContent.mode` set to `grep`. A root with member repositories greps up to eight members whose indexes cannot answer and names the rest as not searched; a healthy member with no matches is not grepped. Every fallback is recorded in the usage trace as `outcome.degraded_reason` (`index_failed`, `index_upgrade_required`, `index_building`, `index_empty`, `no_index_matches`, `no_git`), and `lc runtime search-stats` reports the counts. Shell `grep` and `rg` are never denied.

## Member repositories

One session root can hold several repositories (`client/src/lemoncrow_client/workspace_members.py`). A Git repository with checked-out submodules has itself plus its submodules, recursively from `.gitmodules`, as members. A folder that is not a Git repository has its immediate child repositories, each with its submodules, as members; nothing deeper is scanned. A linked worktree is never a second member: checkouts are grouped by Git common directory, the main checkout represents the repository, and the others stay reachable by path through their own index. A repository without submodules is served exactly as described above. An explicit `LEMONCROW_WORKSPACE_ROOT` inside a work tree is served as that repository's top level.

Each member keeps its own index in its own checkout, excluded through that checkout's own `info/exclude`, so a session started inside a member shares it and a parent repository's `git status` stays clean. A non-Git root holds no LemonCrow state. Paths are root-relative in `code_search`, `read`, `edit`, `relations`, `graph` and `blame`; one `edit` can change several members, and Git runs per member (`bash cwd=<member>`). Member builds run on a two-worker pool owned by the session, members a call names first. An index-backed answer that could not cover every member carries a `workspace` coverage block and names what it left out (`[lc:index_building]`, `[lc:lite_failed]`, `[lc:workspace_partial]`). Per-member scores are merged as if comparable (`lc-debt` in `lite/mcp.py`). The optional semantic helper is not used in a session with members.

`lc workspace` works over the same members (`workspace_cli.py`); every command takes `--root DIR`. `status|branch|commit|push|pr` (`workspace_changes.py`) take one step in every changed repository at once: status per member with submodule drift, one branch name across them, commits with submodules first and their new commits then recorded in the repository that declares them, pushes in the same order, and one pull request per pushed repository through `gh`, each linking the others. `graph|impact` (`workspace_graph.py`) find dependency edges between members from their tracked files: submodules, local path dependencies, package names, imports, `.proto` services, OpenAPI documents, HTTP routes and event topics, each with evidence and a confidence. They also list the members a change reaches through those edges. The routable `graph` tool gives the same answers with `kind="workspace"` and `kind="impact"`; it is not advertised to models, so session instructions name the commands. A checkout inside a repository that does not track it (a worktree, a clone) is never one of its changes: `status` names it, `commit` leaves it out, `impact` skips it. Test files, calls to public hosts and discovery routes make no edge. Nothing is written to disk: within one process each file's facts are reused while its stat is unchanged, so a fresh `lc workspace` command still scans cold (`lc-debt` in `workspace_graph.py`). `lc review` and `lc review --ai`, typed by a person, show the members a reviewed change reaches in a section after the findings (`src/lemoncrow/pro/capabilities/review/workspace_impact.py`), as does the Reader's Brief for a review on the local server, which scans around the checkout the client proved it holds and adds the answer to the Snapshot in place, waiting up to 20 seconds so the first read usually has it. That section is never a finding; a git hook, a pull request run and hosted Review never scan.

This covers one developer's local checkouts under one root. It is not the Hosted cross-workspace context below. Design and later phases: `docs/planning/2026-11-01-multi-repo-workspace.md`.

## Retrieval pipeline

```text
query
  -> identifier interpretation + code-syntax noise filtering
  -> exact and partial symbol rows
     + Porter-stemmed SQLite FTS5 lexical rows
     + path/name matches
     + parsed imports and local import-target resolution
  -> deterministic evidence fusion
     + multi-term coverage
     + bounded import-hop expansion
     + intent-aware source/test/docs/vendor path priors
  -> bounded source snippets + ranked candidate-path tail
```

Import rows come from statement readers for Python, JavaScript/TypeScript, Go, Rust and Java/Kotlin (`client/src/lemoncrow_client/lite/imports.py`): multi-line, relative, re-export, dynamic and `require` imports, Go import blocks, Rust `use` groups and `mod` declarations. Other files keep the original line patterns. `ImportResolver` maps a row to indexed files using only the repository's files: Python packages (relative imports, `src/` layouts, namespace packages), `tsconfig.json`/`jsconfig.json` `baseUrl`, `paths` and `extends`, `package.json` `imports`, `exports` and workspace package names, `go.mod` module paths, and Cargo crate names. An installed package stays external and is never reported missing. The local Code map and `relations kind=imports` read these rows. Index schema 3 introduced them. An older index is preserved for older MCP processes; the v3 copy re-reads its rows once, without mutating the legacy index.

The default Workspaces Lite path deliberately contains no embeddings, ANN/vector retrieval, Torch, SentenceTransformers, learned reranking, private semantic/hybrid fusion, advanced graph/lineage, or cross-workspace intelligence.

An **optional local semantic recall mode** is available as a separate public helper through the lemoncrow lite-semantic extra and LEMONCROW_LITE_SEMANTIC=1. It uses the MIT-licensed minishlab/potion-code-16M-v2 static code model over syntax-aware roughly 750-character chunks, embedding each chunk together with its repository-relative file path and the nearest public Lite structural symbol name when available. Vectors are stored as signed 6-bit values in int8 cells and scanned exactly in bounded blocks; there is no ANN service, listening port, persistent daemon, hosted dependency, or private/enterprise intelligence in this path. The standard-library lemoncrow-client still has zero dependencies and communicates with the helper only over session-owned stdio. Missing, cold, or stale semantic state fails open to normal lexical Lite, and lexical rank one is never displaced by semantic fusion.

Install the optional extra with pip install 'lemoncrow[lite-semantic]'; enable a session with LEMONCROW_LITE_SEMANTIC=1 lc with claude. Workspaces sessions are offline: the helper sets Hugging Face offline mode and never downloads model weights on the search path. Point LEMONCROW_LITE_SEMANTIC_MODEL at a local Model2Vec directory or pre-populate the model cache during an explicit installation/setup step.

This helper is bounded local recall, not the private Hosted/Enterprise semantic engine. Advanced semantic intelligence, organization context, cross-workspace retrieval, learned reranking, and managed indexing remain Hosted/Enterprise capabilities.

## Local integrations

Claude Code, Codex, and OpenCode support a temporary integration:

```bash
lc with claude
lc with codex
lc with opencode
```

For persistent local integration:

```bash
lc rt enable claude
lc rt enable codex
lc rt enable opencode
```

Cursor uses its installed integration rather than `lc with`. `lc code` is deliberately not another local launcher: it is reserved for LemonCrow's hosted coding agent, which is not yet available. The public Pi binary is managed separately with `lc rt host`; Pi can also be launched for one Lite session with `lc with pi`.

`lc rt mcp` runs Workspaces Lite in its own process unless Workspaces targets a hosted server, in which case it runs the thin client. The opt-in remote MCP gateway (`lc rt mcp serve [--persistent]`) is served by the public client in the same way; neither path starts a local LemonCrow server. Persistence never changes a Workspaces Lite trial into hosted behavior implicitly.

Each Workspaces Lite tool call leaves one local usage record (the client trace schema, site `lite`, one session id per stdio process) under `<LEMONCROW_HOME>/runtime/traces/`, readable with `lc runtime trace`. Arguments are kept except bulk content (edit bodies, file text, diffs), which is stored only as size and digest. Retention is 14 days / 256 MB; `LEMONCROW_RUNTIME_TRACE=off` disables the record. Nothing is uploaded.

A call that saved something also appends one row to the host session's `savings.jsonl` (`<LEMONCROW_HOME>/sessions/YYYY/MM/DD/<host>/<session>/`), the ledger the stop hook, statusline and savings reports read. The formulas are the full server's: `bash` output compaction (capped at the host Bash's own 30k-char cut), `grep` ranking, HTML-to-markdown `web_fetch`, batched `read` files and cross-file `edit` batches past the first, non-exact edit recovery, and `code_search` source shown inline (once per file per session). Lite has no price table, so a row carries the measured tokens and calls, the model, and for avoided calls the live context and average turn output read from the host transcript; readers price it. The session comes from the host's session environment variable (`CLAUDE_CODE_SESSION_ID` and the like); without one no row is written.

None of these local paths requires a LemonCrow account or uploads repository source to LemonCrow.

## Hosted retrieval

Hosted use is explicit, for example:

```bash
lc rt enable claude --hosted
```

That is a real remote/shared Workspaces boundary using the configured LemonCrow deployment. It is not the old local private-engine process under a new name.

The hosted server already provides revision-bound remote code search, semantic retrieval where configured, relationship/graph queries, source/view synchronization, authentication, and tenant scoping. The CLI reserves `lc code` for the Hosted owned agent; current builds fail closed until the remote owned-loop transport is wired. Provider routing, spend controls, prompt-cache orchestration, and the owned execution loop remain Hosted/Enterprise implementation capabilities.

## Internal private-development path

LemonCrow engineering still needs to run the private engine locally for development, qualification, and benchmarks. That path is deliberately hidden from public help and guarded by `LEMONCROW_INTERNAL_PRIVATE_RUNTIME=1` plus `--internal-private`. It is not a supported customer install/update/entitlement flow.

The old `lc code --pro` mode has been removed. `--pro` is not a compatibility flag; Pro is a commercial plan for hosted retrieval.

## Public tool placement

`lemoncrow_client.lite.policy.TOOL_PLACEMENT` is the executable ownership map. The model-visible Workspaces Lite allowlist is exactly:

- `bash`
- `code_search`
- `edit`
- `read`
- `web_fetch`

Other local helper implementations may remain routable internally without being advertised to the model. Advanced semantic search and shared/organization capabilities belong to hosted retrieval and Enterprise Workspaces.

## Local to managed

| Deployment | Repository intelligence | Shared infrastructure | Cross-workspace | Customer boundary |
| --- | --- | --- | --- | --- |
| Workspaces Lite | Lightweight deterministic local intelligence | No | No; only the local member repositories of one root ([Member repositories](#member-repositories)) | Developer machine; Apache-2.0 |
| Hosted retrieval (Pro) | Private advanced intelligence | Yes | Not yet available | LemonCrow-operated |
| Enterprise Workspaces | Private advanced intelligence | Dedicated/customer-controlled | Yes, subject to policy | Customer-controlled/dedicated |

Signing in never silently turns local Workspaces Lite into hosted retrieval.
