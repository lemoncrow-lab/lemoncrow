# Importing review rules

A team that already wrote down how its code should be reviewed does not have to write it again. To bring rules from another tool, copy them into a file in your repository, or point `import.rules_files` in `.lemoncrow/review.yaml` at the file they already live in. LemonCrow reads those files, and your `AGENTS.md` / `CLAUDE.md`, from the same trusted base as `.lemoncrow/review.yaml`, turns the rules into review rules, and says out loud what it did not use. Nothing to convert, nothing to upload. Early access, like the [GitHub Action](github-action.md) it runs in.

Rules that live only on another tool's servers (a dashboard, a learned or suggested rule) cannot be read: copy their text into a file once. Nothing else is read unless you list it, so to stop using an old rules file, delete it or take it out of the list.

```yaml
# .lemoncrow/review.yaml
import:
  rules_files:
    - docs/review-rules.md      # a path
    - review-rules/*.yaml       # or a glob
  critical: []                  # rule keys to treat as gates (below)
  agent_docs: true              # send AGENTS.md / CLAUDE.md to the investigator (the default)
```

## What is read, and from where

Everything is read from the pull request's **merge base**, never from the pull request's own branch. A pull request that adds or edits `.lemoncrow/review.yaml`, a rules file or `AGENTS.md` cannot change its own review, so it cannot switch off its own findings. The practical effect: a new or edited rules file takes effect for pull requests reviewed after it has landed on the base branch, and the pull request that edits it is still reviewed under the old one.

Highest precedence first:

1. `.lemoncrow/review.yaml`
2. the files `import.rules_files` lists
3. `AGENTS.md` and `CLAUDE.md` (context only: never a rule)

How precedence works:

- **Rules** are keyed `imported:<file>:<id>`, for example `imported:docs/review-rules.md:no-raw-sql`, and each keeps the file it came from. The same id in two files is two rules, never a silent override. A rule you write in `.lemoncrow/review.yaml` under the same key replaces the imported one.
- **`import.rules_files`** takes repository paths or globs. A path is read as written. A glob is expanded against the base's file list, in sorted order; only `.md`, `.markdown`, `.yaml`, `.yml` and `.json` files are taken, and `*` also crosses `/`, as in every other path scope here. A glob needs the base's file list, which the Action and the local CLI have; where there is none, the glob is reported and matches nothing, so list the files by path instead. A path the base does not hold, a file of another type, or a glob that matches nothing is a warning, never an error.

`rule_documents` in `.lemoncrow/review.yaml` is the other way to point at a rules document, and it stays as it is: exact paths, its own `stable_key` / `instruction` / `paths` schema, and every rule in it is a critical rule (a violation is P0). Use it for rules that must gate; use `import.rules_files` for rules that advise.

## File formats

**Markdown** (`.md`, `.markdown`). Every top-level bullet is a rule, with the nearest `##`-or-deeper heading above it as a prefix (the indented lines after a bullet stay with it). A `##`-or-deeper heading with prose and no bullets under it is one rule. A file with neither is one rule, all its prose. A lone `#` title is never a rule, and front matter and fenced code blocks are skipped. A markdown rule applies to every path, and its id is the first words of the bullet (or the heading).

```markdown
## Database
- Use the query builder, never string-built SQL
- Every migration must be reversible

## Logging
Use the logger, not print().
```

**YAML or JSON** (`.yaml`, `.yml`, `.json`), in LemonCrow's own schema:

```yaml
rules:
  - id: no-raw-sql
    rule: Use the query builder, never string-built SQL.
    scope: [src/db/**]      # optional: path globs, or one glob; no scope means every path
    enabled: true           # optional: false leaves the rule out
```

| Field | Meaning |
| --- | --- |
| `id` | Required. Part of the rule's key, so keep it stable: `import.critical` and overrides name the key. Anything but letters, digits, `.`, `_` and `-` becomes `-`. |
| `rule` | Required. The rule's text, cut at 800 characters. |
| `scope` | Optional. Repository-relative path globs. A scope that cannot be read (a path that climbs out of the repository, an empty list) skips the rule instead of widening it to every path. |
| `enabled` | Optional. `false` leaves the rule out; the view says it was disabled. |

Anything else, in a rule or at the top of the file, is ignored with a warning. A file that is not valid, or has no `rules` list, imports nothing and says so.

Settings of another tool's file (a strictness, ignore patterns, a severity per rule) are not carried over, by design. Their native counterparts are `review.strictness`, `review.ignore` and `review.rules` in `.lemoncrow/review.yaml`, read from the merge base like the rest; see [Reducing noise](github-action.md#reducing-noise). A rule's severity there is set by its key, which is the key `lc review config --effective` prints for an imported rule.

## Imported rules are advisory

A violation of a rule you wrote in `.lemoncrow/review.yaml` is a **P0**: a critical rule. An imported rule is not. Its violation is a **P1 or P2** by the ordinary severity ladder, whatever the model rates it, so it can never fail the `LemonCrow Review` check under `fail-on: p0` on its own.

Mark the ones you trust as gates in `.lemoncrow/review.yaml`:

```yaml
# .lemoncrow/review.yaml
import:
  rules_files: [docs/review-rules.md]
  critical: [imported:docs/review-rules.md:no-raw-sql]   # keys are shown by `lc review config --effective`
```

Or write the rule again under the same key to change its wording or scope; a rule in `.lemoncrow/review.yaml` always replaces the imported one and is critical. To stop sending `AGENTS.md` / `CLAUDE.md` to the investigator, set `import.agent_docs: false`.

An advisory rule ranks below every deterministic risk signal when LemonCrow chooses what to investigate, and its per-rule model check (`ai.rule_checks`) is asked after the critical rules, so a long imported list does not use up the few model calls a depth allows before a security or correctness candidate. Each rule that applies to a changed file can still use one of the per-rule checks, up to the ceiling (8 by default); the 20-call cap per review is unchanged.

## AGENTS.md and CLAUDE.md as project conventions

Both files are context for the investigator: how your team writes code. They are never turned into rules, are tagged as untrusted data in the prompt (instructions inside them cannot change the review), go through the credential redactor, and are cut to a token budget so they stay cheap. The excerpt is estimated at four characters per token, the same estimate as the [repository context](github-action.md#repository-context) pack, and the figures are ceilings per investigator call:

| Depth | Conventions per call (at most) |
| --- | --- |
| `quick` | 600 |
| `auto` | 1,000 |
| `thorough` | 1,500 |
| `deep` | 2,000 |

`LEMONCROW_REVIEW_CONVENTIONS_TOKENS` replaces the ceiling for every depth. A document is cut at a paragraph boundary with a visible ellipsis; a second document that no longer fits is left out and named. Editing `AGENTS.md` does not re-review unchanged code. A `CLAUDE.md` that is only `@AGENTS.md`, or a copy of it, is skipped.

A file is never included if the content-access policy denies it for the model route in use: an `ai_content_access` row in `.lemoncrow/review.yaml` for `AGENTS.md` (or `customer_local_only` on an external route) keeps it out.

## See what will apply

Before the first pull request:

```bash
lc review config --effective                      # the merged rules, with the file each came from
lc review config --effective --base main --json   # a specific trusted base, for scripts
lc review config --dry-run --pr 123               # which rules would apply to that pull request's files
lc review config --dry-run --head my-branch       # the same for a branch (--files a.py --files b.py for a list)
```

`--pr` uses the pull request's merge base as the trusted base, exactly as the Action does, so the answer is what its review will use. Nothing is sent to a model and nothing is written. The effective view lists the files it looked for and what the base held, every rule with its key, severity, scope and source, the paths the model will never read, the conventions with their size at each depth, and the warnings. The view covers the repository's files; it does not include an organization policy.

The pull request's own summary says when the import warned about something: on a clean review, the `What we checked` receipt shows `Imported config: 2 import warnings (run lc review config --effective)`.

## Limits

- At most 32 files are read (list entries, and files a glob matched), 40 rules and about 3,000 tokens of rule text are imported, each rule cut at 800 characters, and at most 400 entries of one file are looked at; what is over is named in the view. Unscoped rules ride in every prompt, so the set is small on purpose. A file larger than a million characters is not read.
- Imports run in the Action and the local CLI. The hosted service's trusted-base capture does not fetch these files yet.
- A head edit to a rules file is reported as future policy only for files `import.rules_files` names by path; a file only a glob matches is not tracked.
- Whether imported rules catch what the tool they came from caught is unmeasured.
