# Architecture

## What Atelier Is

Atelier is a **reasoning/procedure/runtime layer**. It sits between an AI agent host (Claude Code, Codex, Copilot, opencode, Gemini CLI) and the actual codebase operations. Its job is to make agent runs more reliable by:

1. Injecting known-good procedures before runs (ReasonBlocks)
2. Blocking known-bad plans before execution (dead end detection)
3. Verifying outputs against domain requirements (Rubric gates)
4. Recording observable traces for failure analysis and block extraction

## What Atelier Is NOT

| Not this              | Why                                                            |
| --------------------- | -------------------------------------------------------------- |
| Memory layer          | No user preferences, no personalization, no session continuity |
| Semantic memory / RAG | No hidden chain-of-thought stored, no vector DB required       |
| Chatbot history       | No conversation storage                                        |
| Test replacement      | Rubric gates are agent guards, not unit tests                  |
| OpenMemory wrapper    | OpenMemory bridge is an optional stub, disabled by default     |

## System Components

```
┌──────────────────────────────────────────────────────────────┐
│                    Agent Host Layer                          │
│  Claude Code  │  Codex  │  Copilot  │  opencode  │  Gemini  │
└────────────────────────┬─────────────────────────────────────┘
                         │ MCP (stdio) / CLI / Python SDK
┌────────────────────────▼─────────────────────────────────────┐
│                    Atelier Runtime                           │
│                                                              │
│  ┌──────────────────┐    ┌──────────────────────────────┐   │
│  │   MCP Server     │    │   CLI                        │   │
│  │  (atelier-mcp)   │    │   (atelier ...)       │   │
│  └────────┬─────────┘    └──────────────┬───────────────┘   │
│           │                             │                   │
│  ┌────────▼─────────────────────────────▼───────────────┐   │
│  │              ReasoningRuntime                        │   │
│  │                                                      │   │
│  │  reasoning()       lint()      │   │
│  │  rescue()          verify()          │   │
│  │  trace()    extract_candidate()       │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │                                 │
│  ┌────────────────────────▼─────────────────────────────┐   │
│  │              ReasoningStore                          │   │
│  │                                                      │   │
│  │  SQLite + FTS5 (default)                             │   │
│  │  PostgreSQL + pgvector (optional)                    │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
    ~/.atelier/db      ./.knowledge/blocks/   ~/.atelier/traces/
    (SQLite index)     (*.md source)          (*.json mirrors)
```

## Core Data Models

### ReasonBlock

A named, reviewable procedure that an agent should follow in a specific domain context.

```python
class ReasonBlock:
    id: str              # e.g. "read-after-write-verification"
    title: str           # Human-readable name
    domain: str          # e.g. "state.change"
    procedure: str       # The actual instruction text (markdown)
    status: str          # "active", "retired", "candidate"
    tags: list[str]      # For filtering
    created_at: datetime
    updated_at: datetime
```

Blocks are stored in SQLite AND mirrored to `./.knowledge/blocks/*.md` for human review in PRs.

### Rubric

A set of named boolean checks that must all pass for a domain operation to be considered correct.

```python
class Rubric:
    id: str              # e.g. "rubric_state_change_safety"
    domain: str
    title: str
    checks: dict[str, str]  # check_name → description
    required: list[str]  # which checks must be true
```

### Trace

An observable execution record. No chain-of-thought. Only what happened externally.

```python
class Trace:
    id: str
    agent: str
    domain: str
    task: str
    status: Literal["success", "failed", "partial"]
    files_touched: list[str]
    tools_called: list[str]
    commands_run: list[str]
    errors_seen: list[str]
    repeated_failures: list[str]
    diff_summary: str
    output_summary: str
    validation_results: dict
    created_at: datetime
```

All string fields are run through a redaction filter before persistence.

## Information Flow

### Plan Check Flow

```
Agent proposes plan (list of steps)
    │
    ▼
check_plan(task, steps, domain)
    │
    ├─→ FTS5 search blocks for dead-end patterns
    ├─→ Load domain environment constraints
    ├─→ Match step text against known dead-end signatures
    │
    ▼
CheckPlanResult(status="pass"|"blocked", warnings=[...])
    │
    ├── status=pass, exit 0 → agent proceeds
    └── status=blocked, exit 2 → agent revises plan
```

### Reasoning Context Flow

```
Agent starts task
    │
    ▼
reasoning(task, domain, files, tools, errors)
    │
    ├─→ FTS5 search for relevant blocks (domain + query)
    ├─→ Load environment for domain (constraints, tool patterns)
    ├─→ Format structured prompt block
    │
    ▼
Structured context string injected into agent's system prompt
```

### Failure Rescue Flow

```
Agent hits repeated failure
    │
    ▼
rescue(task, error, domain, recent_actions)
    │
    ├─→ Search traces with matching errors
    ├─→ Find blocks in same domain
    ├─→ Run FailureAnalyzer to cluster recent failures
    │
    ▼
RescueResult(procedure=..., related_blocks=[...], confidence=...)
```

## Concurrency Model

- SQLite mode: single-writer, multi-reader. Suitable for single-machine, single-agent use.
- PostgreSQL mode: multi-writer safe. Use for shared multi-agent environments.
- MCP server: stateless — each tool call opens and closes a store connection.
- HTTP service: stateless FastAPI — store is injected per-request.

## Smart Tool Cache

`read` and `search` are default-on Atelier
augmentations for repeated, bounded reads/searches. Cache entries use call
arguments plus content fingerprints where file contents matter, expire after
600 seconds by default, and are invalidated across git `HEAD` changes when the
workspace is inside a git repository.

Host-native `Read`, shell `rg`, `grep`, and repository search remain available
for exact raw access. Set `ATELIER_CACHE_DISABLED=1` to bypass Atelier cache
reads and writes.

## Inspirations

| Concept                    | Source               | Atelier equivalent        |
| -------------------------- | -------------------- | ------------------------- |
| Reusable code blocks       | External inspiration | ReasonBlocks              |
| Failure cluster analysis   | Lemma                | FailureAnalyzer           |
| Rubric-based verification  | Educational rubrics  | Rubric gates              |
| Plugin UX / agent personas | Claude Code plugins  | Claude Code plugin agents |
