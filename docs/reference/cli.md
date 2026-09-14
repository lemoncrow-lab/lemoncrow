# LemonCrow CLI Reference

The `lc` command is the local control surface for the runtime, storage,
imports, benchmarks, background processing, and the optional visualization
stack.

Use the built-in help for the exact command tree:

```bash
lc -h
lc help runs
lc help benchmark
lc help background
```

| Flag           | Description                                                            |
| -------------- | ---------------------------------------------------------------------- |
| `--version`    | Show the installed LemonCrow version and exit.                           |
## Managed coding frontends

`lc code` and the permanent `lemoncode` wheel entry point run mature frontends over LemonCrow's owned runtime. Bare `lc` with no subcommand or flags also dispatches to `lc code` directly (`lc -h` still shows the full command tree). LemonCode remains the automatic default during the Pi migration; stock Pi v0.84.2 is available as an explicit managed engine. LemonCrow stays at the expensive boundary: it chooses the provider/model, owns tools and subagents, and applies cache, compaction, verification, and cost controls.

~~~bash
lc code                              # auto: LemonCode, Pi, Codex, Claude, native
lemoncode --engine lemoncode
lemoncode --engine pi
lemoncode --engine pi -p "fix the failing parser test"
lemoncode --engine pi --resume <pi-session-id>
lemoncode --engine codex -p "fix the failing parser test"
lc code --engine native              # original PromptToolkit fallback
~~~

Managed engines receive no outer tool calls: their UI and session store are reused, while a token-authenticated loopback gateway performs the agent loop. The LemonCode fork strips its redundant host prompt/tool schemas. Managed Pi loads one LemonCrow extension and disables Pi's tools, project resources, compaction, retries, telemetry, update checks, and package/model refresh; it stores config and sessions under the LemonCrow root. `lc code host install|update|status|build|remove --engine lemoncode|pi` manages the verified host binaries. Pi is pinned to reviewed upstream v0.84.2; LemonCode keeps its existing release-channel policy. Set `LEMONCROW_CODE_AUTO_ENGINE=pi` only for canary preference and remove it to roll back.
This avoids paying for the host's large system/tool prompt at the real model and
avoids duplicate host/LemonCrow tool execution. The gateway is started for the
CLI process and stopped on exit.

~~~bash
lc code --cache-policy 1h --budget cheap
lc code --model openai/gpt-5.4
lc code --max-cost 2.00
lc code --optimization-mode shadow   # default: measure, preserve provider behavior
lc code --optimization-mode enforce  # apply V2 policies
lc code --optimization-mode off      # one-setting V2 rollback
lc code --local-retrieval auto
lc code --local-retrieval force --local-retrieval-model ollama/qwen2.5-coder:7b
lc optimize decisions --json
lc code --no-mcp
~~~

Local retrieval `auto` skips the broad first-turn primer and exits before
retrieval fingerprinting/corpus scanning when the task names an explicit source
file (existing verified evidence is still validated), and it stays off for
small or non-retrieval tasks. Without
`--local-retrieval-model`, refinement is deterministic and makes zero planner
calls. Planner identifiers must start with `ollama/`, `lm_studio/`, or
`local/`; cloud model identifiers are ignored. `force` overrides only the
eligibility gate—use it with `--optimization-mode enforce` to inject the packet.

Direct Claude/Codex/OpenCode integrations advertise an eager core containing
normal coding tools, so code_search, read, edit, bash, and web_fetch never
require discovery. Rare capabilities use the deterministic tool broker. Set
LEMONCROW_MCP_TOOL_PROFILE=full to advertise every schema eagerly; managed
`lc code` does not expose this outer MCP catalog at all.

| Command                  | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `lc init`           | Initialize the runtime store under `--root`. Fully local; no login or account required. |
| `lc uninstall`      | Remove LemonCrow-managed host integrations and wrappers.         |
| `lc status`         | Show local plugin and runtime status.                          |
| `lc stack ...`      | Start, stop, inspect, or log the optional native UI/API stack. |
| `lc service ...`    | Manage the HTTP/API service surface.                           |
| `lc background ...` | Manage OS-level background services and auto-updates.          |
| `lc worker ...`     | Inspect, enqueue, and run worker jobs.                         |

Common examples:

```bash
lc init
lc background status
lc background restart
lc background logs controller
```

## Background Services & Auto-Update

Manage background components via your OS-native manager (systemd/launchd).

| Subcommand                      | Purpose                                                    |
| ------------------------------- | ---------------------------------------------------------- |
| `lc background install`    | Register services with systemd (Linux) or launchd (macOS). |
| `lc background uninstall`  | Unregister and stop background services.                   |
| `lc background status`     | Show service health and auto-update state.                 |
| `lc background restart`    | Trigger a clean restart of the entire environment.         |
| `lc background logs [svc]` | Stream logs for `controller` or `stack`.                   |

### Auto-Update Mechanism

The background controller automatically checks for git updates every hour (default).
If updates are found, it pulls the code, syncs dependencies, and restarts the
managed background services.

To configure the loop manually (not recommended for general use):

```bash
# Start the internal loop with custom auto-update settings
lc servicectl run --auto-update --auto-update-interval-seconds 3600
```

## Traces, Ledgers, and Operational State

LemonCrow persists observable execution state rather than hidden reasoning.

| Command              | Purpose                                             |
| -------------------- | --------------------------------------------------- |
| `lc runs ...`   | Record, list, and inspect run data.                 |
| `lc ledger ...` | Manage run ledgers and session state.               |
| `lc swarm ...`  | Fan out isolated child attempts into git worktrees. |
| `lc run explain <id>` | Explain one run from its recorded evidence, naming what it could not see. |

Examples:

```bash
lc runs list
lc ledger list
lc run explain            # the latest run
lc run explain <run-id> --json
```

`lc run explain` cites the record behind every line it prints, and omits a
category rather than clearing it — absence there is absence of evidence, not
evidence of absence.

## Reviewing Agent-Written Changes

`lc review` answers one question about a change: *what did the agent actually
do?* It is deterministic and LLM-free — no model is called and no prose is
generated. Everything it prints is derived from the diff, the local code index,
and the recorded session, and every gap is named instead of filled in.

| Command                       | Purpose                                                                                                        |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `lc review [REV]`             | Open the Review Reader for what the agent just did: risks first, then where to start reading, change impact, provenance and test evidence. Defaults to your uncommitted working tree. |
| `lc resume-context <session>` | Compact, source-linked continuation brief for one session.                                                     |
| `lc context doctor`           | What your agent context declares versus what your sessions actually use.                                       |

New to it? [The walkthrough](review-walkthrough.md) builds a throwaway repository
and runs the whole loop — review, mark, comment, agent revises,
`--since-my-review` — in ten minutes. This page is the reference.

**With no arguments, `lc review` reviews your uncommitted working-tree changes
against `HEAD`** — the moment the agent stops and you have to decide whether the
change is safe to commit. It is also the mode whose citations are exact by
construction: the index and the tree agree, so no caller line has to be
re-anchored to a blob. On a clean tree it reviews the last commit
(`HEAD~1..HEAD`) instead and prints `working tree clean — reviewing the last
commit instead` above the packet, so the substitution is never silent.

Range selection, in precedence order: `--staged`, `--working-tree`, explicit
`--base`/`--head`, `a...b` (merge base), `a..b` (literal), a single spec (merge
base with `HEAD`) — and with no argument at all, the working tree when it is
dirty, otherwise `HEAD~1..HEAD`.

| Flag | Effect |
| ---- | ------ |
| `--base <rev>` / `--head <rev>` | Explicit range; `--head` defaults to `HEAD`. |
| `--staged` | Review the staged index against `HEAD`. |
| `--working-tree` | Review uncommitted working-tree changes — now the default; still accepted, so existing scripts keep working. |
| `--all` / `-a` | Uncapped text: every ATTENTION finding, every call site, the `FILES` table, and the full reasons under each ranked file. Display only — `--json` and `--html` are never capped. |
| `--repo-root <dir>` | Repository to review (default: the resolved workspace root). |
| `--limit N` | Max files in the review order (default: 40). |
| `--no-impact` / `--no-provenance` | Skip change-impact analysis / agent-session correlation. |
| `--session-id <id>` | Force provenance correlation to this session id. |
| `--open` / `--no-open` | Browser Review Reader is the normal human default. `--no-open` keeps the compact terminal surface; explicit `--open` overrides terminal-oriented flags. |
| `--html <path>` | Write a self-contained, static HTML report to `<path>`. |
| `--json` / `--no-color` | Output controls. |
| `--track` | Remember this review: create or reopen a durable session and record a revision of it. |
| `--units` | List the reviewable units and their keys (implies `--track`). |
| `--mark <target>` | Record a verdict on a review target. `<target>` is the label a surface printed for it, a `hun:`/`sym:` unit key, or a repo-relative path. A path — or the file's `fil:` key — is the imprecise spelling: it widens onto **every** target in that file rather than marking the file itself, and names them on stderr (and in `path_marks` under `--json`). A path that exists in the repository beats a target label spelled the same way; the label it beat is named on stderr. Repeatable; implies `--track`. |
| `--mark-state <state>` | What `--mark` records: `reviewed` (default), `needs_changes`, `unreviewed`, `changed_since_review`, `unknown`. |
| `--marks` | Show the marks stored for this review (implies `--track`). |
| `--comment <text>` | Leave a comment. Requires `--on`; implies `--track`. |
| `--on <path:L10[-L20]>` | Where `--comment` attaches: a repo-relative path and a line or line range. A bare path is refused. |
| `--comment-kind <kind>` | The disposition `--comment` records: `comment` (default), `request_change`, `suggestion`, `looks_good`. |
| `--reply-to <annotation-id>` | Attach `--comment` to an existing comment as a reply. |
| `--comments` | Show the comments stored for this review, anchors included (implies `--track`). |
| `--since-my-review` | Reconcile the current tree against the revision you actually reviewed and report what changed since you looked (implies `--track`). |
| `--feedback` | Render every comment as one Markdown bundle to hand back to the agent. Renders and prints; delivers nothing (implies `--track`). |
| `--finish` | Record that you finished this review, with a tally of what was still outstanding when you decided (implies `--track`). |
| `--reopen-review` | Undo `--finish`, or explicitly restore a discarded review before it can be changed again. |
| `--discard-review` | Make the review read-only and disposable. Retention may permanently delete the review, revisions, comments, blobs, and evidence after the retention window. |

```bash
lc review                        # open Review Reader; clean tree -> the last commit
lc review --no-open              # compact terminal summary instead
lc review --all                  # terminal packet with nothing capped
lc review main..feature          # explicit range, still opens Review Reader
lc review --staged               # staged change, still opens Review Reader
lc review --working-tree --json  # machine-readable; does not open a browser
lc review --json --open          # explicit override when you really want both
lc review --units                # list durable raw-unit keys for terminal actions
lc review --mark <unit-key>      # remember a judgment from the terminal
lc review --marks                # ... and it is still there tomorrow
lc review --comment "ttl is hardcoded" --on src/auth.py:L81-L88 --comment-kind request_change
lc review --comments             # ... every comment, and whether it still points anywhere
lc review --since-my-review      # the agent has been back: what do I re-read?
lc review --feedback             # every comment as one Markdown bundle for the agent
lc review --finish               # ... and record that you are done with it
```

### The review workspace

A normal `lc review` starts a **loopback-only** workspace and opens it in your
browser. `--no-open` opts back into terminal-only review; `--open` is retained as
an explicit override for combinations such as `--json --open`. The browser path
implies durable tracking, because the workspace reads and writes the same review
session the terminal does: a target judgment made in the browser is backed by
the same durable mark store that `lc review --marks` reads. There is one
authoritative review state.

The default surface is deliberately code-first:

- **REVIEW OUTLINE** (left) — a compact file outline derived from the current
  review-target states. It is collapsible and is not the progress denominator.
- **READER** (center) — one continuous, virtualized multi-file diff stream.
  Progress is over non-overlapping `ReviewTarget`s: changed symbols where
  identity is trustworthy, uncovered hunks otherwise, and a file fallback for
  changes that cannot be represented more precisely.
- **CONTEXT** — closed by default. Open it only when you need Impact, Checks,
  Evidence, Author context or Discussion. It overlays the diff unless you pin
  it explicitly.

Keys: `j`/`k` move between targets, `J`/`K` move between files, `]`/`[` jump
between high-priority targets, `r` records Reviewed and advances, `u` reopens a
target, `x` records Needs changes, `c` comments, `e` toggles Context, `f`
toggles focus mode, `/` focuses search, and `s` toggles split/unified diff.

The service binds `127.0.0.1` on an ephemeral port and **refuses to start** on
any other interface. Each process mints a fresh bearer token, handed to the page
in the URL *fragment* (never a query string, so it is not sent in the HTTP
request or `Referer`). The fragment intentionally remains in the address bar so
copy/paste produces a working reopen URL; the credential is also retained only
in tab-scoped `sessionStorage`, never a cookie or `localStorage`. Requests
carrying a foreign `Origin`, `Referer` or `Host` are refused, and a file outside
the repository — or inside it but not part of this review — is a 404 before
anything is read. The terminal prints the URL with the token redacted.

`LEMONCROW_REVIEW_BUNDLE_DIR` points the workspace at a built frontend bundle.
When there is no bundle (a plain `pip install` ships none), `--open` says so on
stderr, writes the static HTML report instead, and still exits `0`.
### Reviewed state that survives the terminal

`--track` turns a review into something that outlives the invocation. The
session is keyed on `(repo, subject, range)` and is **reopened**, never
re-created, so a second `lc review --track` on the same change finds the marks
you already made. Each run records a *revision*: if nothing changed, the
fingerprint matches and no second revision is manufactured.

A mark is bound to the content it was made against, not to a line number. Ten
lines added above a function do not reopen it; a rewrite of its body does. That
is why `--marks` reports `same` / `changed` / `gone` beside every mark — the
comparison is a fact about the content, and what it means for the mark is the
review frontier's decision, not the listing's.

A unit whose content could not be fingerprinted (a binary, a deleted file, an
unreadable blob) is recorded as `unknown` rather than `reviewed`, even when you
ask for `reviewed`. Unknown is a state, not a gap, and it must never read as
done — so the downgrade is printed at the moment it happens (on stderr, and as
`downgraded_marks` in the `--json` state document), not left to be inferred
from a count.

### Comments, in the terminal

A comment is the second half of review state, and it does not need the browser:

```bash
lc review --comment "ttl is hardcoded and audit() runs before the record is validated" \
          --on src/service.py:L13-L14 --comment-kind request_change
```

```
COMMENT RECORDED  ann-01a0817e-c993-7a5c-89c7-d548e90ae03d
  request_change  src/service.py:L13-L14  open_session
```

The dispositions are the four the review model has — `comment`,
`request_change`, `suggestion`, `looks_good` — and there is no fifth. A verdict
vocabulary a caller can extend is not a vocabulary. `--reply-to <id>` threads a
reply under an existing comment; `--comments` lists the whole thread.

`--on` takes `PATH:L10` or `PATH:L10-L20` and refuses a bare path, because a
comment silently attached to the top of a file is a comment about something
nobody read. The *anchor* is captured from the file's own text at that moment —
the selected lines, the three lines either side, their hashes, the owning
symbol and its fingerprint, and the blob sha — so the comment can be found
again after the code around it moves. The browser half captures exactly the same
anchor through the same server-side code path: a line number rendered by a diff
viewer is never the record.

The open comment count rides in the session headline, not only under
`--comments`. An unresolved objection you have to ask for is one that gets
committed over.

### What changed since *I* reviewed it

`--since-my-review` is the question a diff viewer cannot answer. It takes a new
revision, reconciles it against the one your marks were made on, and reports
four groups:

```
SINCE MY REVIEW  revision 3  (you last saw revision 2)
  changed since my review  2
  new                      0
  still unresolved         0
  unchanged reviewed       9
  not yet reviewed         498

CHANGED SINCE MY REVIEW  (2)
  src/lemoncrow/pro/capabilities/review/anchors.py
  src/lemoncrow/pro/capabilities/review/anchors.py::_symbol_rung

UNCHANGED REVIEWED  (9)
  9 units still valid — nothing to re-read
```

*Changed since my review* is first because it is the only group nobody else's
tool shows you: code you approved and an agent then rewrote. A unit whose
content is byte-identical to what you signed off stays reviewed; a unit whose
content moved reopens. **`reviewed` is never silently preserved across changed
content** — a stale mark is worse than no review state at all, because no review
state makes you look and a stale one tells you not to.

Comments follow the same discipline. Each one is re-anchored on the new revision
by a six-rung ladder — identical blob, exact text plus context, the owning
symbol, a unique occurrence, the surrounding context — and if more than one
location fits, it is **orphaned rather than guessed**. A comment on a deleted
file becomes `obsolete`; a file that could not be read at all is `unresolved`,
never orphaned, because "we did not look" and "it moved" are different facts.
Every attempt, successful or not, is appended to the anchor-event log, so no
comment can move to a different line without a recorded reason.

Every surviving comment states which rung carried it, in words, on both
surfaces:

```
COMMENTS
  open       comment         src/svc.py:L3
    prices can be None here
    anchor: re-found by its text alone · in total_price

COMMENT ANCHORS  (1)
  relocated   src/svc.py same line
    re-found by its text alone — selected text occurs exactly once
```

Only rung 1 — a byte-identical file — reports `unchanged`, because it is the
only rung whose line number was never re-derived. A re-find that happens to land
on the line it started on is still a re-find, and drawing it like an untouched
file lends an exact fact's credibility to a heuristic. The owning definition is
re-derived on every relocation too, so a comment never prints `in compute_total`
under code that is now `total_price`.

Running it twice on an untouched tree is free and says so: the tree fingerprints
to the revision already stored, no revision is created, and no mark moves.

Marks are per unit, and the listing names the unit: `src/app.py` for the file,
`src/app.py#2` for a hunk, `src/app.py::parse` for a symbol. Marking three
different units of one file records three different claims, and they print as
three different rows.

Two definitions with one name in one file are two rows a human can tell apart.
The name is qualified by its nesting where the code supplies one
(`src/svc.py::Reader.run` and `src/svc.py::Writer.run`, never `run` twice), and
where nesting cannot separate them the label carries the line
(`src/svc.py::run@L2`). The keys were always distinct; the rows were not, and a
row you cannot act on sends you to the wrong method.

### Sending feedback, and finishing

`--feedback` renders every comment as one Markdown document — open comments in
attention order, then the ones that lost their anchor with the reason, then the
review context and what is not known. It prints it and stops: **nothing is sent
to any agent, host or pull request**, and the output says so, because a reviewer
who assumed otherwise would stop watching for a reply that is never coming. The
workspace's *Send feedback* button renders the same bundle through the same
code.

`--finish` records that a human finished the review and prints what was still
outstanding at that moment — unreviewed units, units that changed after you
reviewed them, open comments. It never refuses to close a review over an open
objection and it never renders as `APPROVED`: the decision is yours, and the
tally is what you are owed next to it. `--reopen-review` undoes it.

`--discard-review` is deliberately different from finishing. It makes the
review read-only and eligible for retention cleanup, which may permanently
delete its revisions, comments, frozen blobs, screenshots, traces, and other
evidence after the retention window. Opening the same range does **not** silently
revive it; use `--reopen-review` to restore it explicitly before continuing.

```text
REVIEW FINISHED  512 units
  reviewed                 9
  still unreviewed         503
  comments still open      1
```

State lives in `lemoncrow_reviews.db` under your store root, alongside the
packet artifact for every revision — the record of what you were actually shown,
kept because a working-tree blob is gone the moment the file is saved again. The
packet and the fingerprints come from one read of your tree, so a file you save
while the review is still running cannot end up stored as one thing and marked
as another. LemonCrow's own review directory carries a `.gitignore`, so none of
it can land in your commits.

The default view is a triage screen: the risks come first, capped at the three
highest-priority findings with five locations each, then the one file to start
reading. Every cap states the count it withheld and names the flag that lifts it
— an elision you cannot tell apart from "nothing else was found" would be worse
than the scrolling it saves.

```text
fix: reject traversal ids on filesystem-backed service routes
983c233f2^..983c233f2   7 files · +300 -31

ATTENTION

  ⚠ args_signature changed   (src/lemoncrow/pro/foundation/watchdogs.py)
    untouched call sites:
      src/lemoncrow/gateway/adapters/runtime.py:L194   RuntimeSession.record_tool_call
      src/lemoncrow/pro/capabilities/tool_supervision/loop_review.py:L47   call_signature

  ⚠ find_session_dir changed   (src/lemoncrow/core/foundation/paths.py)
    untouched call sites:
      integrations/claude/plugin/hooks/stop.py:L632   _refresh_statusline_frames
      src/lemoncrow/core/capabilities/plugin_runtime.py:L3126   _codex_run_file
      src/lemoncrow/core/capabilities/plugin_runtime.py:L3143   _codex_ledger_session_id
      src/lemoncrow/core/capabilities/reporting/dashboard.py:L248   _render_dashboard_impl
      src/lemoncrow/core/capabilities/savings_summary.py:L1003   _find_savings_sidecar
      … and 9 more

  ⚠ load_report changed   (src/lemoncrow/infra/runtime/session_report.py)
    untouched call sites:
      src/lemoncrow/gateway/cli/commands/sessions.py:L176   session_report_cmd
      tests/infra/test_session_report.py:L394   test_load_report_missing_returns_none
      tests/infra/test_session_report.py:L399   test_load_report_reads_run_file
      tests/test_session_importer_tokens.py:L105   TestClaudeImporterTokens.test_claude_token_fields

  … and 1 more (lc review --all)

START HERE
  1. src/lemoncrow/core/foundation/paths.py
     modified · production · +43 -0
     - 34 untouched impacted site(s)
     - 88 known callers
     - high-centrality symbol

THEN
  2. src/lemoncrow/infra/runtime/session_report.py  +10 -8  · 10 known callers · … and 2 more
  3. src/lemoncrow/pro/foundation/watchdogs.py  +2 -2  · 2 untouched impacted site(s) · … and 2 more
  4. src/lemoncrow/core/service/api.py  +88 -19
  5. src/lemoncrow/gateway/integrations/openmemory_lifecycle.py  +8 -1
  6. tests/gateway/test_service_api_path_traversal.py  +131 -0  · test coverage for this change
  7. tests/gateway/test_service_api_team.py  +18 -1  · test coverage for this change

EXECUTION EVIDENCE

  Generated with: unknown
    (no session recorded an edit to any of the 7 reviewed files (best 0.60 of 85 candidates, scored on workspace and timing alone))

Human review  REQUIRED
index: fresh · degraded: ambiguous_symbol_counts, index_outline_incomplete, provenance_unmatched
```

`lc review --all` prints the same packet with every finding, every call site,
the full reasons under each ranked file, and the `FILES` table — the
`git diff --stat` reprint the default view leaves out because it is not the
question anyone runs this command to answer. On the commit above that is 110
lines against the default's 50.

When a session *can* be correlated, the evidence block names it on one line and
keeps the hedge on that same line:

```text
EXECUTION EVIDENCE

  Generated with: claude · claude-opus-5 · c7e0f86b-5465-4647-b2b0-3f3244e10b13  [possible match, confidence 0.45]
    correlated by score, not recorded: host, model and session are unconfirmed
  Matched on: workspace path, 1/84 changed files (score 0.45, below the 0.60 match floor)
  Agent inspected: not recorded for this host

  Focused tests    NOT_RUN
  Full suite       NOT_RUN
  Typecheck        NOT_RUN
  Lint             NOT_RUN
  Migration check  UNKNOWN

Human review  REQUIRED
index: stale · degraded: agent_reads_unrecorded, ambiguous_symbol_counts, blob_unreadable, provenance_ambiguous, symbol_line_ranges, test_evidence_unavailable — run `lc code index` for caller and centrality signals
```

Read those lines literally. Three limits are structural, and the renderer states
them rather than hiding them:

- **Session ↔ commit correlation is a heuristic, not a join.** Sessions recorded
  before the run ledger's git anchor existed carry no commit sha, so the match is
  scored from workspace path, recorded edits and time window. Three things then
  have to hold before a host is named at all: some session must have recorded an
  **edit to a reviewed file** (being in the right directory at the right time is
  presence, not authorship), no other candidate may be within 0.15 of it (a tie
  is two answers, so it resolves to `unknown`), and the winner is still printed
  as `[probable match, ...]` or `[possible match, ...]` with `unconfirmed`
  spelled out. Only `--session-id` and the run ledger's git anchor print without
  a hedge, because only those two are recorded rather than scored. `Matched on:`
  always names the evidence that fired, and an unresolvable match prints
  `Generated with: unknown` followed by the reason.
- **Files-read capture is Claude-only.** Other host importers record edits but
  not reads, so the line reads `Agent inspected: not recorded for this host` —
  never `0 files`, which would read as a finding rather than a gap.
- **Test-execution evidence is thin.** Status is derived only from recorded
  command exit codes; a record without an exit code is `UNKNOWN`, nothing is ever
  synthesized as `PASS`, and the packet ends with `Human review REQUIRED`.
- **Only definitions become symbols.** The tag extractor calls anything that
  introduces a name a "definition", which includes an imported binding, a JSON /
  TOML / YAML key, a Ruby `attr_accessor` call and — in a `.tsx` file, parsed
  with the JSX-less TypeScript grammar — a bare string literal. None of them may
  earn a caller count, a centrality rank, an impacted site or a markable unit:
  they are dropped and the file is named `non_definition_symbols` in `degraded`.
  The gate is an allowlist of node kinds, so a language the outliner learns after
  this list was written under-claims until someone classifies it.
- **Impact stops at this repository's edge.** The code index is keyed to the
  workspace, and a workspace contains whatever is checked out under it, so a
  submodule or nested clone was cited as a call site of a change it cannot see.
  A path under a directory with its own `.git` is never reported; when one is
  dropped the packet says `cross_repository_sites`.

The `index:` / `degraded:` footer is the fourth limit. With no code index,
caller counts and centrality are unavailable; with a stale one the index is
still used, but symbol line ranges are flagged as unreliable. Everything that
degraded is named there — run `lc code index` to restore those signals.

`lc resume-context <session-id>` is the bounded companion — the brief you hand
the next session instead of re-reading a transcript. It reuses the same test
evidence, so it cannot claim more than `lc review` does:

```text
RESUME CONTEXT  003e0173-ae66-4bf4-a792-c6b6d446616e
claude · claude-opus-5 · generated 2026-09-08T01:52:43.848900+00:00

GOAL
  ...

TESTS
  Focused tests   NOT_RUN
  Full suite      NOT_RUN
  Typecheck       NOT_RUN
  Lint            NOT_RUN
  Migration check UNKNOWN

Bounded brief — run `lc review` for the full change packet.
```

Pass `--symbols` to include important symbols (requires a code index),
`--repo-root` to point symbol lookup elsewhere, or `--json`.

`lc context doctor` is the compact view of `lc audit context`: it reads the same
MCP server configs, skill files, and session history, and reaches the same
verdicts. `--days` sets the look-back window (default 7), `--threshold` the
context-token level above which an unused item is flagged (default 500), and
`--json` emits the audit payload with one added key per item,
`declared_context_tokens` (see below).

```bash
lc context doctor
lc context doctor --days 30 --threshold 1000
lc context doctor --json
```

```text
SOURCE                              DECLARED     USED    ACTION
integ/antigravity/benchmark s…    ~3,779 tok        —    no data in window
integ/claude/benchmark skill      ~3,779 tok        —    no data in window
integ/codex/benchmark skill       ~3,779 tok        —    no data in window
integ/antigravity/perf-review…    ~3,434 tok        —    no data in window

0 sessions observed in the last 7 days -- USED is unknown, not zero, so no ACTION is recommended for any row.
DECLARED = estimated tokens this source contributes when configured; not a measurement of host loading.
```

**DECLARED is not a measurement.** It is an estimate of what a source
contributes *when configured*, computed from `.mcp.json` and `SKILL.md` /
`AGENTS.md` on disk. Nothing in LemonCrow observes what a host actually loaded
into a context window, so the column is named for what is known. The same
applies to the flagged total at the bottom of a populated table
(`N declared tok/turn recoverable if disabled.`): it is a projection of what
would stop being declared, not an observed saving.

The last two lines are the rule for this whole family of commands: an empty
window reports unknown, never zero, and an unmeasured row carries no advice.

In `--json`, each item carries `declared_context_tokens`. The older
`est_context_tokens` key holds the same value and remains for one release so
existing consumers do not break silently; prefer the new name.

## Swarm Harness

`lc swarm` is LemonCrow's multi-run harness. It creates one git worktree and
one isolated `LEMONCROW_ROOT` per child, launches the same child agent command in
each sandbox, collects structured result JSON, and merges accepted
improvements onto a coordinator-owned integration base.

```bash
lc swarm start program.md --runs 3 --continuous \
  --runner ollama-claude \
  --runner-model qwen3.6 \
  --validate "make lint" \
  --validate "uv run pytest tests/gateway/test_cli_swarm.py -q"
```

What the harness guarantees today:

- one detached git worktree per child under a deterministic `*-swarm-worktrees/<run_id>/` pool
- one isolated `LEMONCROW_ROOT` plus `LEMONCROW_WORKSPACE_ROOT` / `CLAUDE_WORKSPACE_ROOT` per child
- a copied program spec at `.lemoncrow/swarm/program.md` in each child worktree
- structured child artifacts with summary, files changed, validations, cost/tokens (when available), final status, and live stdout/stderr previews
- persisted coordinator state under `--root/swarm/runs/<run_id>/state.json`
- a dedicated integration worktree whose accepted patches become the base for the next wave
- optional continuous mode that keeps running until a full wave produces no accepted improvements or you stop the job

Useful child environment variables:

| Variable                                          | Meaning                                                                                            |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `LEMONCROW_SWARM_SPEC_PATH`                         | Copied spec path inside the child worktree                                                         |
| `LEMONCROW_SWARM_RESULT_PATH`                       | Final structured result artifact written by the wrapper                                            |
| `LEMONCROW_SWARM_METADATA_PATH`                     | Optional child-authored JSON metadata (`summary`, `token_count`, `cost_usd`, `validation_results`) |
| `LEMONCROW_SWARM_RUN_ID` / `LEMONCROW_SWARM_CHILD_ID` | Stable coordinator and child identifiers                                                           |

Inspection commands:

```bash
lc swarm list
lc swarm status <run_id>
lc swarm logs <run_id> --child-id wave-03-run-01
lc swarm stop <run_id> --cleanup
```

If you omit the swarm spec path, LemonCrow resolves `program.md` relative to the
selected project root. The command fails clearly if that file is missing or if a
supplied spec path escapes the project root.

Built-in runner profiles:

| Runner          | Command shape                                           |
| --------------- | ------------------------------------------------------- |
| `claude`        | `claude --model <model> -p "<prompt>"`                  |
| `codex`         | `codex exec -m <model> "<prompt>"`                      |
| `copilot`       | `copilot --model <model> -p "<prompt>" --allow-all`     |
| `opencode`      | `opencode run -m <provider/model> "<prompt>"`           |
| `lemoncode`     | `lemoncode run -m <provider/model> "<prompt>"`          |
| `ollama-claude` | `ollama launch claude --model <model> -- -p "<prompt>"` |

You can still bypass profiles entirely and pass any raw child command after `--`
for custom API wrappers or other CLIs.

How patch acceptance works:

- children from the same wave are ranked
- successful, validated children with diffs are tried in score order
- disjoint or cleanly mergeable patches stack onto the integration base
- conflicting patches are rejected once a higher-ranked accepted patch already owns that space

Current limitation: the coordinator owns the isolation/runtime/merge harness,
but the actual child agent command is still supplied after `--` so you can plug
in Claude/Codex/Copilot or another runner that speaks LemonCrow MCP inside that
isolated environment. The current harness does **not** provide first-class
OpenAI or LiteLLM child execution; the dashboard only exposes the real CLI
runner path today.

## Retrieval, Search, and Code-Aware Helpers

Code retrieval, file reads, grep/search, and symbol lookup are exposed as
LemonCrow **MCP tools** (`read`, `grep`, `search`, `explore`, `codemod`)
rather than standalone CLI commands. Invoke them through your agent host or via
`lc tools call <name>`. (LemonGraph — call-graph and reference relations —
callers, callees, usages — fold into one `explore` call.)

| Command         | Purpose                                                               |
| --------------- | --------------------------------------------------------------------- |
| `lc code index` | Build or refresh the code index for a repository.                      |
| `lc optimize`   | Show session cost optimization recommendations.                        |

Examples:

```bash
lc code index --repo-root .
lc dashboard open  # choose Map in the existing dashboard
lc tools call grep --args '{"path":".","content_regex":"TODO"}'
```

### Excluding files from indexing

`lc code index` walks git-visible files (`git ls-files`, so `.gitignore` is
already honored) plus a built-in skip list for caches/build output. To
exclude specific files -- data dumps, fixtures, generated JSON -- that are
tracked in git and therefore not covered by `.gitignore`, add gitignore-syntax
patterns to `.lemoncrow/.ignore`:

```gitignore
# .lemoncrow/.ignore
fixtures/
*.snapshot.json
data/**/*.csv
```

Patterns in `.lemoncrow/.ignore` are a union with `.gitignore`, not a
replacement for it -- both are applied. Re-run `lc code index --reindex`
after changing the file.

## Knowledge, Lessons, and Failure Workflows

These commands manage the reusable knowledge layer and failure review flows.

| Command                      | Purpose                                         |
| ---------------------------- | ----------------------------------------------- |
| `lc lesson ...`         | Review and promote lesson candidates.            |
| `lc eval ...`           | Run eval suites (`mcp`, `retrieval`, `fitness`). |
| `lc report`             | Generate an engineering governance report.       |
| `lc import-style-guide` | Draft lesson candidates from Markdown guidance.  |
| `lc proof ...`          | Run cost-quality proof gate workflows.           |

## Imports and Host Integrations

LemonCrow ships import and integration commands for supported agent hosts.

| Command                | Purpose                                               |
| ---------------------- | ----------------------------------------------------- |
| `lc import` | Import sessions from all supported hosts in one pass. |

Supported session import hosts are defined in the runtime registry, not in the
docs. Use `lc help import` to inspect the exact flags and options
supported by your installed build.

## Benchmarks, Savings, and External Reports

These commands support performance validation and cost-accounting workflows.

| Command                   | Purpose                                        |
| ------------------------- | ---------------------------------------------- |
| `lc benchmark ...`   | Run benchmark suites (`mini`, `harbor`, `codebench`, `swe`, `local`). |
| `lc benchmark local` | BYO-repo A/B: LemonCrow vs vanilla on your repo. |
| `lc usage`           | Where usage went, by host / model / project / day. |
| `lc usage explain`   | Cost and token decomposition for one run.      |
| `lc usage optimize`  | Realized savings plus the Optimization Advisor. |
| `lc usage rows`      | Emit the canonical usage rows for scripting (`--json` only). |
| `lc savings`         | Aggregate cost and token savings _(hidden alias of `lc usage optimize`; kept for installed statusline scripts)_. |
| `lc session replay`  | Replay a past session; mark what one-shot search would collapse. |
| `lc dashboard`       | Show the spend & savings dashboard.            |

Examples:

```bash
lc benchmark mini --dry-run --json
lc usage --since 30d --by model
lc usage explain <run-id>
lc usage optimize --json
lc usage rows --since 30d --limit 500 --json
lc session replay --last 1
```

`lc usage` groups by `host`, `provider`, `model`, `project`, `session`, or `day`
(`--by`), over a `--since` window of `7d`, `12h`, `30m`, and narrows with
`--host` / `--model` / `--project`. `lc usage rows` is the same read model
without the grouping, for scripting.

`lc usage` reports what was actually spent and never renders an unknown price
as `$0.00`: a local or self-hosted model with no rate card shows `local`, and
subscription usage shows `seat`. The counterfactual ("what you saved") lives
only under `lc usage optimize`.

Every total carries the basis it was computed on, and unpriced rows are counted
out loud rather than folded into the total at zero:

```text
USAGE  ·  last 7d  ·  by model

MODEL                      SESSIONS  ROWS   TOKENS       COST  BASIS
claude-opus-5                    25    25    1.39B    $829.04  estimated
claude-sonnet-5                  41    41    1.51B    $712.90  estimated
gpt-5.6-sol                      14    14  121.89M     $91.31  estimated
unknown                           4     4        0          —  unknown
------------------------------------------------------------------------
TOTAL                            81    84    3.02B  $1,633.25  estimated+unknown

4 of 84 rows carry no price and are not in any COST above (4 model id unresolved).
```

`lc usage explain <run-id>` decomposes one run into fresh input, cache read,
cache write, output, and thinking. When a run ledger records per-turn
accounting, those buckets agree with `lc session report` to the cent; when a
multi-model session has no ledger to say which turn used which model, every
bucket reports `null` with a note rather than a split that would mis-attribute
the dollars.

The total and the split carry **separate** provenance, because they are
separate facts. `$532.90 estimated` is the session's own total, summed from
each recorded call at that call's rate card — no vendor invoice reaches this
command, so nothing it prints is ever labelled `billed`. The split beneath that
total is derived a second time over, because nothing records tokens per bucket
per call (and no vendor itemises an invoice by cache-read against cache-write
either). The line under the headline says which — `split: derived,
not billed — each recorded turn priced at its own model, rescaled to the total`
— and every row carries the same word in its `BASIS` column, with `unseparable`
for the buckets whose tokens are folded into the others. In `--json` these are
`breakdown_basis` and `breakdown_basis_approximate` alongside the per-bucket
`basis`, distinct from `cost_provenance`, which describes `total_cost_usd`
only.

`lc session replay` reconstructs a recorded session (Claude Code, Codex, or
opencode) from its transcript and replays it turn by turn — assistant text,
thinking, tool calls and outputs. For each native call it then invokes the
**real** LemonCrow tool that would have replaced it and shows the actual output:
grep/read loops collapse into a real `code_search` (whose ranked hit is checked
against the file the loop landed on), whole-file reads show the real `read`
outline, and `edit`/`bash` are shown as **safe previews** — never written or
executed. No model is re-run. By default it prints the terminal timeline, writes
an HTML page, and opens it in the browser.

| Flag | Effect |
| ---- | ------ |
| `--session-id <id> --host claude\|codex\|opencode\|lemoncode` | Locate a session under a host's store. |
| `--file <path.jsonl>` | Replay a specific transcript directly (any host). |
| `--last N` | Replay the N most recent sessions. |
| `--repo <path>` | Repo root for real `code_search`/`read` (default: cwd). |
| `--no-live` | Structural view only — skip calling real LemonCrow tools. |
| `--no-open` | Do not open the HTML in a browser. |
| `--html <path>` / `--json` / `--no-color` | Output controls. |

```bash
lc session replay --last 1                          # most recent session (+ opens HTML)
lc session replay --session-id <id> --host codex    # a specific session
lc session replay --file ./session.jsonl --repo .   # explicit transcript + repo
lc session replay --last 1 --no-live --no-open      # structural only, no browser
```

`lc benchmark local` is the user-facing BYO benchmark, also surfaced as the
`/benchmark` skill: point it at your own git repo and supply your own coding
prompts to compare LemonCrow against a vanilla Claude Code baseline on the same
model. It prints an up-front cost estimate and asks to confirm before any spend.

```bash
lc benchmark local --repo . --prompt "add a docstring to the entry point"
lc benchmark local --repo . --prompt "x" --estimate-only
```

Wire capture is off by default — cost comes from the CLI receipts, so no
mitmproxy or MITM CA cert is needed. Pass `--capture` to opt into mitmproxy
wire-level cost verification (requires `mitmproxy` and its CA cert).

The internal/dev suites are `lc benchmark {codebench,swe}` and
`lc eval {mcp,retrieval,fitness}`.

## Bring Your Own Model

`lc model` registers any OpenAI-compatible endpoint — vLLM, Ollama, LM Studio,
or an internal gateway — and probes what it can actually do, so routing works
from measured capabilities rather than assumptions.

| Command                     | Purpose                                                                     |
| --------------------------- | ----------------------------------------------------------------------------- |
| `lc model add <endpoint>`   | Register an OpenAI-compatible endpoint and probe its models.                 |
| `lc model list`             | List registered custom endpoints and their probed capabilities.              |
| `lc model probe <model-id>` | Re-probe one registered model and persist the result.                        |
| `lc model remove <name>`    | Remove a registered endpoint and its models (`-f` skips the confirmation).   |

| Flag on `lc model add` | Effect |
| ---- | ------ |
| `--name <alias>` | Endpoint alias (default: derived from host and port). |
| `--api-key-env <VAR>` | Env var holding the key — **preferred**; nothing is written to disk. |
| `--api-key <key>` | Literal key, stored in `providers.json` (mode `0600`). |
| `--model <id>` | Only register these model ids. Repeatable. |
| `--tier cheap\|high` | Routing tier to register the models under (default: `cheap`). |
| `--context-window N` | Override the reported context limit. |
| `--no-probe` | Register without running the capability probe. |
| `--probe-vision` | Also probe vision (sends a 1x1 image). |
| `--timeout <seconds>` | Per-request discovery and probe timeout (default: `30.0`). |
| `--json` | Output JSON instead of text. |

```bash
lc model add http://localhost:11434/v1 --name ollama
lc model add https://gateway.internal/v1 --api-key-env MY_GATEWAY_KEY --tier high
lc model list --json
lc model probe ollama/qwen2.5-coder:7b
lc model remove ollama -f
```

With nothing registered, `lc model list` says so and shows the next step instead
of printing an empty table:

```text
no custom endpoints registered.
  lc model add http://localhost:8000/v1 --api-key-env MY_GATEWAY_KEY
```

Self-hosted models have no public rate card, so their usage is reported as
unpriced — `local` in `lc usage`, never `$0.00`.

## Configuration and Optional Account

The `lc account` commands are an **optional** convenience for linking a hosted
account. They gate nothing — LemonCrow is fully local and every feature works
without them; they are never required and never prompted. Anonymous remote
telemetry is **on by default**; turn it off with `lc telemetry remote off` (see
[Privacy & network behavior](../setup/privacy.md)).

| Command             | Purpose                                                            |
| ------------------- | ----------------------------------------------------------------- |
| `lc settings ...`   | Manage local plugin settings.                                     |
| `lc telemetry ...`  | Inspect or toggle telemetry; remote telemetry is on by default.   |
| `lc account login`  | Optional: link a hosted account. Gates nothing; never required.   |
| `lc account logout` | Remove the optional local account link.                           |
| `lc account status` | Show whether an optional account link is present.                 |
| `lc share`          | Render referral or share text.                                    |
| `lc domain ...`     | Manage internal domain bundles.                                   |
| `lc letta ...`      | Manage the self-hosted Letta sidecar.                             |

Inspect or change any of them with `lc settings`:

```bash
lc settings show --category mcp
```

### Where edits are allowed to write

LemonCrow's read tools accept any absolute path; its **write** tools (`edit`,
batch edit, `bash`) are confined to the workspace root plus `/tmp`. Widen that
boundary in one of three ways:

- `permissions.additionalDirectories` in `~/.claude/settings.json` or
  `~/.claude/settings.local.json` (all workspaces) — **live**, no restart.
- `permissions.additionalDirectories` in `<workspace>/.claude/settings.json` or
  `<workspace>/.claude/settings.local.json` — **live**, no restart.
- `lc settings set mcp.additional_edit_dirs "$HOME/notes:/srv/shared/plans"` (the
  `LEMONCROW_ADDITIONAL_DIRS` env var on the `lc` MCP server entry) — `:`-separated
  (a comma is not a separator) and takes effect only after the MCP connection
  reconnects.

Every entry must be absolute once a leading `~` is expanded; relative entries and
blanket grants (`/` or a bare `~`) are skipped with a warning.

The legacy top-level `additionalDirectories` key is read too. Matching is
component-wise, so allowing `/srv/plans` does not allow `/srv/plans-secret`. See
[setup/installation.md](../setup/installation.md#widening-the-edit-write-boundary)
for the full table.

## JSON Output

Many commands accept `--json` when the output is intended for automation or
other tools. Prefer the built-in help for each command path because JSON support
is command-specific rather than universal.

## Related References

- [README.md](https://github.com/lemoncrow-lab/lemoncrow#readme)
- [setup/installation.md](../setup/installation.md)
- [sdk/mcp.md](../sdk/mcp.md)
