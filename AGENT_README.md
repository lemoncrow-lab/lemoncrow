# Atelier — Agent Reasoning Runtime

**For agents by agents.** This document is structured for automated consumption, not human reading.

---

## ⚡ AGENT QUICK-START

**DECISION TREE: Which agent are you?**

```
Are you editing code?
├─ YES → Use atelier:code
│        First: call reasoning
│        Then: draft plan
│        Then: call lint (MUST PASS before editing)
│        Then: edit code
│        Then: if 2+ failures → call rescue
│        Finally: call trace
│
├─ NO, just reading/exploring?
│  └─ Use atelier:explore
│     - Call reasoning only
│     - Read files, grep, search
│     - Never edit, never mutate
│
├─ NO, I'm reviewing a patch?
│  └─ Use atelier:review
│     - Call reasoning
│     - Call lint on the patch
│     - Call verify for high-risk domains
│     - Report verdict: pass | warn | block
│
└─ NO, same error failed 2+ times?
   └─ Use atelier:repair
      - Call reasoning
      - Call rescue
      - Apply patch
      - Re-run test
      - Call trace
```

---

## 🎯 AGENT WORKFLOWS (Copy-Paste Ready)

### atelier:code (Main Coding Agent)

```
1. call reasoning({
     task: "one-sentence task",
     domain: "beseam.shopify.publish" | null,
     files: ["src/..."],
     errors: []
   })
   → Read returned ReasonBlocks

2. Draft plan: 3-8 imperative steps

3. call lint({
     task: same,
     plan: [...],
     domain: same,
     files: [...],
     tools: [...]
   })
   → IF status == "blocked": use suggested_plan, re-check
   → IF status == "warn": address warnings
   → IF status == "ok": proceed

4. Edit code (aligned with validated plan)

5. IF test/command fails same way twice:
   call rescue({
     task: same,
     error: "error message (redacted)",
     files: [...],
     recent_actions: ["tried X", "tried Y"]
   })
   → Apply rescue before retrying

6. IF domain in [beseam.shopify.publish, beseam.pdp.schema,
                 beseam.catalog.fix, beseam.tracker.classification]:
   call verify({
     rubric_id: "rubric_shopify_publish" | ...,
     checks: {
       "check_name_1": true,
       "check_name_2": false,
       "check_name_3": null
     }
   })
   → MUST have status != "blocked"

7. call trace({
     agent: "atelier:code",
     domain: same | null,
     task: same,
     status: "success" | "failed" | "partial",
     files_touched: [...],
     tools_called: [{name, count}, ...],
     commands_run: [...],
     errors_seen: [...],
     diff_summary: "one sentence",
     output_summary: "one sentence",
     validation_results: [{name, passed, detail}, ...]
   })
```

### atelier:explore (Read-Only Investigator)

```
1. call reasoning({
     task: "...",
     domain: "...",
     files: ["..."],
     errors: []
   })

2. Read files, grep, glob

3. DO NOT:
   - Edit files
   - Run mutations (git commit, rm, npm install)
   - Call record_trace or extract_reasonblock

4. Return tight summary (~30 lines):
   - Matched ReasonBlock ids
   - File/line citations
   - Key findings
```

### atelier:review (Verifier/Gatekeeper)

```
1. call reasoning({
     task: "review patch for X",
     files: [changed_files],
     domain: "beseam.shopify.publish" | ...
   })

2. Identify dead-end overlaps

3. call lint({
     task: "implement the diff",
     plan: [implied_steps_from_diff],
     domain: same,
     files: [changed_files]
   })
   → IF blocked: STOP, report verdict: block

4. IF high-risk domain:
   call verify({
     rubric_id: ...,
     checks: {...}
   })
   → IF status == "blocked": STOP, report verdict: block

5. Return verdict:
   {
     verdict: "pass" | "warn" | "block",
     findings: [...],
     required_actions: [...]
   }

DO NOT:
  - Edit code
  - Call record_trace
  - Approve "block" verdicts
```

### atelier:repair (Repair Specialist)

```
1. call reasoning({session_id: "" | "specific_id"})
   → Extract: current_plan, verified_facts, hypotheses_tried,
              hypotheses_rejected, errors

2. Form ONE new hypothesis not in hypotheses_tried or rejected

3. call rescue({
     task: same,
     error: "error signature (redacted)",
     files: [...],
     recent_actions: [...]
   })
   → Read matched ReasonBlocks

4. Apply smallest patch addressing rescue

5. Re-run failing test/command

6. IF still fails same way:
   call trace({
     session_id: same,
     add_hypothesis: {
       hypothesis: "...",
       rejected: true,
       reason: "..."
     }
   })
   → STOP after 2 rejections

7. call trace({
     agent: "atelier:repair",
     domain: same,
     task: same,
     status: "success" | "failed" | "partial",
     files_touched: [...],
     ...
   })

DO NOT:
  - Call record_trace without observable facts
  - Propose same hypothesis twice
  - Loop more than 2 times
  - Skip get_run_ledger
```

---

## 🔧 MCP TOOLS (Machine-Readable Reference)

### CORE WORKFLOW (5 Tools)

```json
{
  "core_tools": [
    {
      "name": "reasoning",
      "input": {
        "task": "string (required)",
        "domain": "string (optional)",
        "files": ["string"],
        "errors": ["string"],
        "tools": ["string"],
        "max_blocks": "integer (default: 5)"
      },
      "output": {
        "context": "markdown ReasonBlocks"
      },
      "use_when": "Starting any task, before planning"
    },
    {
      "name": "lint",
      "input": {
        "task": "string (required)",
        "plan": ["string"] (required, 3-8 steps)",
        "domain": "string (optional)",
        "files": ["string"],
        "tools": ["string"],
        "errors": ["string"]
      },
      "output": {
        "status": "ok | warn | blocked",
        "matched_blocks": ["block_id"],
        "suggested_plan": ["string"],
        "warnings": ["string"]
      },
      "exit_codes": {
        "0": "status ok or warn",
        "2": "status blocked — DO NOT PROCEED"
      },
      "use_when": "Before editing code, after drafting plan"
    },
    {
      "name": "rescue",
      "input": {
        "task": "string (required)",
        "error": "string (required, redacted)",
        "files": ["string"],
        "domain": "string (optional)",
        "recent_actions": ["string"]
      },
      "output": {
        "rescue": "procedure string",
        "matched_blocks": ["block_id"]
      },
      "use_when": "Same error fails 2+ times"
    },
    {
      "name": "trace",
      "input": {
        "agent": "string (required: atelier:code|explore|review|repair)",
        "domain": "string (optional)",
        "task": "string (required)",
        "status": "string (required: success|failed|partial)",
        "files_touched": ["string"],
        "tools_called": [{"name": "string", "count": "int"}],
        "commands_run": ["string"],
        "errors_seen": ["string (redacted)"],
        "diff_summary": "string",
        "output_summary": "string",
        "validation_results": [
          {"name": "string", "passed": "bool", "detail": "string"}
        ]
      },
      "output": {
        "id": "trace_id",
        "session_id": "session_id"
      },
      "use_when": "Task complete, failed, or partial"
    },
    {
      "name": "verify",
      "input": {
        "rubric_id": "string (required)",
        "checks": {
          "check_name_1": true,
          "check_name_2": false,
          "check_name_3": null
        }
      },
      "output": {
        "status": "pass | fail | blocked",
        "failures": ["check_name"]
      },
      "exit_codes": {
        "0": "status pass",
        "1": "status fail",
        "2": "status blocked"
      },
      "use_when": "Before marking success on high-risk domains"
    }
  ]
}
```

### EXTENDED TOOLS (9 Tools)

```json
{
  "extended_tools": [
    {
      "name": "reasoning",
      "input": {"session_id": "string (optional)"},
      "output": {"ledger": {"session_id", "task", "plan", "verified_facts", "hypotheses_tried", "hypotheses_rejected", "blockers", "errors"}}
    },
    {
      "name": "trace",
      "input": {
        "session_id": "string",
        "set_plan": ["string"],
        "add_hypothesis": {"hypothesis": "string", "rejected": "bool", "reason": "string"},
        "record_test": {"name": "string", "passed": "bool", "output": "string"},
        "set_blocker": "string"
      },
      "output": {"updated": "bool"}
    },
    {
      "name": "atelier_extract_reasonblock",
      "input": {"trace_id": "string"},
      "output": {"candidate": {"id", "title", "domain", "procedures", "dead_ends"}}
    },
    {
      "name": "trace",
      "input": {
        "event_type": "SecondGuessing|Thrashing|BudgetExhaustion|RepeatedFailure|WrongDirection",
        "severity": "low|medium|high",
        "message": "string"
      },
      "output": {"recorded": "bool"}
    },
    {
      "name": "compact",
      "input": {"session_id": "string"},
      "output": {"summary": "string"}
    },
    {
      "name": "reasoning",
      "input": {"domain": "string"},
      "output": {
        "environment": {"id", "title", "domain_match", "required_validations", "forbidden_phrases", "top_procedures"}
      }
    },
    {
      "name": "read",
      "input": {"file_path": "string"},
      "output": {"content": "string", "cache_hit": "bool", "tokens_saved": "int"},
      "boundary": "Atelier augmentation — host-native Read stays the raw-access fallback"
    },
    {
      "name": "search",
      "input": {"query": "string", "path": "string"},
      "output": {"results": ["string"], "cache_hit": "bool", "calls_avoided": "int"},
      "boundary": "Atelier augmentation — host-native search/grep stays available"
    },
    {
      "name": "search",
      "input": {"pattern": "string", "path": "string"},
      "output": {"matches": ["string"], "cache_hit": "bool"},
      "boundary": "Atelier augmentation — host-native grep stays available"
    }
  ]
}
```

### V2 MEMORY TOOLS (5 Tools)

```json
{
  "v2_memory_tools": [
    {
      "name": "memory",
      "input": {"agent_id": "string", "label": "string", "value": "string", "pinned": "bool (optional)", "read_only": "bool (optional)", "description": "string (optional)"},
      "output": {"id": "string", "version": "int"},
      "use_when": "Store a persistent fact or value in the agent's memory block",
      "example": "memory({agent_id: 'atelier:code', label: 'last_gid', value: 'gid://shopify/Product/12345'})"
    },
    {
      "name": "memory",
      "input": {"agent_id": "string", "label": "string"},
      "output": {"MemoryBlock": {"id", "agent_id", "label", "value", "pinned", "read_only", "version", "updated_at"}},
      "use_when": "Retrieve a specific named memory block"
    },
    {
      "name": "memory",
      "input": {"agent_id": "string", "query": "string", "top_k": "int (optional)", "tags": ["string"] "(optional)"},
      "output": {"passages": [{"id", "text", "score", "source_ref"}], "recall_id": "string"},
      "use_when": "FTS + vector semantic search over archival memory",
      "example": "memory({agent_id: 'atelier:code', query: 'Shopify product GID pattern', top_k: 5})"
    },
    {
      "name": "memory",
      "input": {"agent_id": "string", "text": "string", "source": "string", "source_ref": "string (optional)", "tags": ["string"] "(optional)"},
      "output": {"id": "string", "dedup_hit": "bool"},
      "use_when": "Persist a text passage to archival memory (dedup guard included)"
    },
    {
      "name": "memory",
      "input": {"session_id": "string"},
      "output": {"tokens_pre": "int", "tokens_post": "int", "summary_md": "string", "evicted_event_ids": ["string"]},
      "use_when": "Summarize and compact the run's sleeptime memory (reduces context window)"
    }
  ]
}
```

### V2 LESSON PIPELINE TOOLS (2 Tools)

```json
{
  "v2_lesson_tools": [
    {
      "name": "atelier lesson inbox",
      "input": { "domain": "string (optional)", "limit": "int (optional)" },
      "output": ["LessonCandidate"],
      "use_when": "List pending lesson candidates awaiting promote/reject decision",
      "example": "atelier lesson inbox({domain: 'beseam.shopify.publish', limit: 10})"
    },
    {
      "name": "atelier lesson decide",
      "input": {
        "lesson_id": "string",
        "decision": "approve|reject",
        "reviewer": "string",
        "reason": "string"
      },
      "output": { "status": "string", "promotion_id": "string (optional)" },
      "use_when": "Approve or reject a lesson candidate; approved lessons become ReasonBlocks"
    }
  ]
}
```

### V2 CONTEXT-SAVINGS TOOLS (4 Tools)

```json
{
  "v2_context_savings_tools": [
    {
      "name": "search",
      "input": {"query": "string", "path": "string (optional)", "max_files": "int (optional)", "max_chars_per_file": "int (optional)"},
      "output": {"matches": [{"path", "line_start", "line_end", "snippet", "lang_outline?"}], "total_chars": "int", "cache_hit": "bool"},
      "use_when": "Token-saving combined search + read for repeated context gathering",
      "boundary": "Atelier augmentation — host shell/search stays available for raw access",
      "example": "search({query: 'publish_product function', path: 'src/'})"
    },
    {
      "name": "edit",
      "input": {"edits": [{"path": "string", "old_string": "string|null", "range": "{start,end}|null", "new_string": "string", "fuzzy": "bool (optional)"}]},
      "output": {"applied": [{"path", "hunk"}], "failed": [{"path", "error"}]},
      "use_when": "Apply multiple deterministic edits in one call; host-native Edit/MultiEdit remains default",
      "boundary": "Atelier augmentation — optional batch executor; never intercepts host edit tools"
    },
    {
      "name": "atelier sql inspect",
      "input": {"connection_alias": "string", "sql": "string"},
      "output": {"rows": ["..."], "columns": ["string"], "affected": "int", "truncated": "bool"},
      "use_when": "Read-only SQL schema/data introspection; never an interactive DB client or migration tool",
      "boundary": "Atelier augmentation — read-only inspection only"
    },
    {
      "name": "compact",
      "input": {"session_id": "string"},
      "output": {"should_compact": "bool", "preserve_blocks": ["string"], "pin_memory": ["string"], "suggested_prompt": "string"},
      "use_when": "Get advice before triggering host-native /compact — Atelier preserves runtime facts for re-injection",
      "boundary": "Host-native /compact stays owned by the host; Atelier only advises and reinjects facts"
    }
  ]
}
```

### V2 ROUTING TOOLS (2 Tools)

```json
{
  "v2_routing_tools": [
    {
      "name": "route",
      "input": {"agent_request": "AgentRequest", "policy": "ContextBudgetPolicy"},
      "output": {"RouteDecision": {"model", "tier", "budget_tokens", "rationale"}},
      "use_when": "Quality-aware model routing — select best model/tier for task within cost budget"
    },
    {
      "name": "route",
      "input": {"route_decision_id": "string", "validation_results": ["..."], "changed_files": ["string"], "rubric_status": "string"},
      "output": {"VerificationEnvelope": {"verified", "escalated", "reason"}},
      "use_when": "Verification-gated escalation — promote to higher-tier model when output quality is insufficient"
    }
  ]
}
```

---

## 🚫 ERROR HANDLING (Decision Trees)

### When lint returns status="blocked"

```
IF status == "blocked":
  ├─ STOP editing code
  ├─ Read suggested_plan from response
  ├─ Replace your plan with suggested_plan
  ├─ Call lint again with new plan
  └─ Repeat until status != "blocked"
```

### When verify returns status="blocked"

```
IF status == "blocked":
  ├─ DO NOT mark task success
  ├─ Read failures list
  ├─ Fix each failed check in code
  ├─ Call verify again
  └─ Repeat until status="pass"
```

### When same command/test fails 2+ times

```
IF count_failures >= 2 AND same_error_signature:
  ├─ Call rescue
  ├─ Read matched ReasonBlocks
  ├─ Apply rescue procedure
  ├─ Re-run test/command
  ├─ IF still fails same way:
  │  └─ Call rescue again (max 3 times total)
  └─ IF 3rd attempt still fails:
     └─ STOP and ask human for help
```

### When reasoning returns empty

```
IF context is empty:
  ├─ Verify domain is correct
  ├─ Verify files exist
  ├─ Try without domain filter
  └─ IF still empty: domain has no ReasonBlocks yet (normal)
```

---

## 📊 HIGH-RISK DOMAINS (Require Rubric Gate)

```json
{
  "high_risk_domains": [
    {
      "domain": "beseam.shopify.publish",
      "rubric_id": "rubric_shopify_publish",
      "required_checks": 5,
      "description": "Publishing product data to Shopify"
    },
    {
      "domain": "beseam.pdp.schema",
      "rubric_id": "rubric_pdp_schema",
      "required_checks": 4,
      "description": "Changing PDP schema or indexes"
    },
    {
      "domain": "beseam.catalog.fix",
      "rubric_id": "rubric_catalog_fix",
      "required_checks": 3,
      "description": "Fixing catalog data integrity"
    },
    {
      "domain": "beseam.tracker.classification",
      "rubric_id": "rubric_ai_referral_classification",
      "required_checks": 4,
      "description": "AI referral classification changes"
    }
  ]
}
```

**Rule:** If domain is in high_risk_domains, MUST call verify before marking task success.

---

## ⚠️ HARD RULES (Never Break)

```
1. DO NOT skip lint before editing code in high-risk domains
2. DO NOT ignore status="blocked" from lint or verify
3. DO NOT propose same hypothesis twice in repair loop
4. DO NOT call trace without observable facts
5. DO NOT store secrets, API keys, tokens, or PII in traces
6. DO NOT edit code after review agent reports verdict="block"
7. DO NOT loop more than 2 times on rescue
8. DO NOT skip reasoning when in repair agent
9. DO NOT call trace during exploration (explore agent only reads)
10. DO NOT mark task success if verify status != "pass"
```

---

## 🔍 HOW TO GET HELP

```
# See all available commands:
uv run atelier -h

# Get help on specific command:
uv run atelier COMMAND -h

# Examples:
uv run atelier check-plan -h
uv run atelier context -h
uv run atelier list-blocks -h
uv run atelier record-trace -h
uv run atelier rescue -h
uv run atelier run-rubric -h
```

---

## 📁 DIRECTORY STRUCTURE

- `src/atelier/` — Core engine (models, store, runtime, CLI, MCP, service)
- `tests/` — Test suite (214 passing)
- `docs/` — Human-readable documentation
- `integrations/` — Host adapter configs (Claude, Codex, opencode, Copilot, Gemini)
- `frontend/` — React dashboard
- `.atelier/` — Runtime data (gitignored)

---

## 🔗 LINKS FOR HUMANS (Not for agents)

- **Quick reference card:** [QUICK_REFERENCE.md](QUICK_REFERENCE.md)
- **Architecture:** [atelier/docs/](docs/)
- **Installation:** [atelier/README.md](README.md)

## 🛠 V2 IMPLEMENTATION (For coordinator + subagents)

If you are dispatched to extend Atelier itself (memory, lessons, context savings) read:

1. [docs/architecture/IMPLEMENTATION_PLAN_V2.md](docs/architecture/IMPLEMENTATION_PLAN_V2.md) — vision, three pillars, success metrics
2. [docs/architecture/IMPLEMENTATION_PLAN_V2_DATA_MODEL.md](docs/architecture/IMPLEMENTATION_PLAN_V2_DATA_MODEL.md) — every new model + DDL
3. [docs/architecture/work-packets/INDEX.md](docs/architecture/work-packets/INDEX.md) — pick one packet, follow the standing loop, mark `done`
