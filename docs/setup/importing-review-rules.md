# Importing review rules

A team that already wrote down how its code should be reviewed does not have to write it again. To bring a config over from another review tool, run `lc review config import <folder>` ([Moving from another review tool](#moving-from-another-review-tool)): it maps the settings, lists the rules, writes `.lemoncrow/review.yaml` and prints what it carried, mapped and could not carry. To bring rules by hand, copy them into a file in your repository, or point `import.rules_files` in `.lemoncrow/review.yaml` at the file they already live in. LemonCrow reads those files, and your `AGENTS.md` / `CLAUDE.md`, from the same trusted base as `.lemoncrow/review.yaml`, turns the rules into review rules, and says out loud what it did not use. Nothing to upload. Early access, like the [GitHub Action](github-action.md) it runs in.

Rules that live only on another tool's servers (a dashboard, a learned or suggested rule) cannot be read: copy their text into a file once. Nothing else is read unless you list it, so to stop using an old rules file, delete it or take it out of the list.

```yaml
# .lemoncrow/review.yaml
import:
  rules_files:
    - docs/review-rules.md      # a path
    - review-rules/*.yaml       # or a glob
  critical: []                  # rule keys to treat as gates (below)
  agent_docs: true              # send AGENTS.md / CLAUDE.md and other tools' rule files to the investigator (the default)
  agent_docs_globs: []          # more instruction files to send, besides the ones found by name (below)
```

## What is read, and from where

Everything is read from the pull request's **merge base**, never from the pull request's own branch. A pull request that adds or edits `.lemoncrow/review.yaml`, a rules file or `AGENTS.md` cannot change its own review, so it cannot switch off its own findings. The practical effect: a new or edited rules file takes effect for pull requests reviewed after it has landed on the base branch, and the pull request that edits it is still reviewed under the old one.

Highest precedence first:

1. `.lemoncrow/review.yaml`
2. the files `import.rules_files` lists
3. `AGENTS.md`, `CLAUDE.md` and the instruction files other coding tools use (context only: never a rule; see "Instruction files from other tools")

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
| `severity` | Not read from a rules file (a rules file holds rules, not settings). `lc review config import` writes it to `review.rules.<key>.severity`; until then the view warns and names the key. |

A key LemonCrow has never heard of, in a rule or at the top of the file, is ignored with a warning. A key that is a *setting* of another review tool's file (a strictness, ignore patterns, comment types, a free-text brief, a severity per rule, a pull request filter) gets a warning that says what it would have been and where it goes. A rules file only holds rules, so those settings live in `review:` of `.lemoncrow/review.yaml`, read from the merge base like the rest (see [Reducing noise](github-action.md#reducing-noise)), and the warning goes away once that file holds them. A rule's severity there is set by its key, which is the key `lc review config --effective` prints for an imported rule. A file that is not valid, or has no `rules` list, imports nothing and says so.

## Imported rules are advisory

A violation of a rule you wrote in `.lemoncrow/review.yaml` is a **P0**: a critical rule. An imported rule is not. Its violation is a **P1 or P2** by the ordinary severity ladder, whatever the model rates it, so it can never fail the `LemonCrow Review` check under `fail-on: p0` on its own.

Mark the ones you trust as gates in `.lemoncrow/review.yaml`:

```yaml
# .lemoncrow/review.yaml
import:
  rules_files: [docs/review-rules.md]
  critical: [imported:docs/review-rules.md:no-raw-sql]   # keys are shown by `lc review config --effective`
```

Or write the rule again under the same key to change its wording or scope; a rule in `.lemoncrow/review.yaml` always replaces the imported one and is critical. To stop sending `AGENTS.md`, `CLAUDE.md` and the other instruction files to the investigator, set `import.agent_docs: false`.

An advisory rule ranks below every deterministic risk signal when LemonCrow chooses what to investigate, and its per-rule model check (`ai.rule_checks`) is asked after the critical rules, so a long imported list does not use up the few model calls a depth allows before a security or correctness candidate. Each rule that applies to a changed file can still use one of the per-rule checks, up to the ceiling (8 by default); the 20-call cap per review is unchanged.

## AGENTS.md and CLAUDE.md as project conventions

Both files (and the other instruction files below) are context for the investigator: how your team writes code. They are never turned into rules, are tagged as untrusted data in the prompt (instructions inside them cannot change the review), go through the credential redactor, and are cut to a token budget so they stay cheap. The excerpt is estimated at four characters per token, the same estimate as the [repository context](github-action.md#repository-context) pack, and the figures are ceilings per investigator call:

| Depth | Conventions per call (at most) |
| --- | --- |
| `quick` | 600 |
| `auto` | 1,000 |
| `thorough` | 1,500 |
| `deep` | 2,000 |

`LEMONCROW_REVIEW_CONVENTIONS_TOKENS` replaces the ceiling for every depth. A document is cut at a paragraph boundary with a visible ellipsis; a second document that no longer fits is left out and named. Editing `AGENTS.md` does not re-review unchanged code. A `CLAUDE.md` that is only `@AGENTS.md`, or a copy of it, is skipped.

A file is never included if the content-access policy denies it for the model route in use: an `ai_content_access` row in `.lemoncrow/review.yaml` for `AGENTS.md` (or `customer_local_only` on an external route) keeps it out.

## Instruction files from other tools

A team that has been using another coding or review tool usually already has instruction files for it. They are found by name in the trusted base and used as conventions, the same way `AGENTS.md` and `CLAUDE.md` are: background for the investigator, never a rule, so none of them can make a P0. Nothing to list and nothing to convert.

| File | Applies to |
| --- | --- |
| `AGENTS.md`, `CLAUDE.md` at the root | every path |
| `.claude/CLAUDE.md` | every path |
| `.github/copilot-instructions.md` | every path |
| `.claude/rules/**/*.md` | every path, or the globs in its front matter (`paths:`) |
| `.cursor/rules/**/*.mdc` (and `.md`) | every path, or the globs in its front matter (`globs:`; `alwaysApply: true` means every path) |
| `.cursorrules` | every path |
| `AGENTS.md`, `CLAUDE.md` in a subdirectory | that directory and below |
| `.cursor/rules/**` and `.cursorrules` in a subdirectory | that directory and below (front matter globs are not used there) |

**Scope.** A file that applies to part of the repository is shown to an investigation of code under it and to no other, so a `src/billing/AGENTS.md` never costs tokens on a change to `src/web/`. When a review looks at several files, it gets the files that apply to any of them. Front matter is read for its scope and is not sent to the model. A front matter scope that cannot be read (a path that climbs out of the repository, a value that is not a glob or a list of them) skips the file with a warning; it is never widened to every path.

**Budget.** The same per-call ceilings as `AGENTS.md` (table below) cover all of the files together. The file nearest the code comes first (the deepest directory), then the repository-wide ones in the order of the table, so when something has to be cut it is the most general text. What did not fit is named in the prompt (the first few names, then a count) and counts against the ceiling like the text does. At most 24 files beyond the root `AGENTS.md` and `CLAUDE.md` are read (the ones `agent_docs_globs` adds included, which come after the ones found by name), repository-wide kinds first and then by depth, the same files every time; what is over is named in the view. A file that is only `@AGENTS.md`, or a copy of another one, is skipped.

**Access.** Every file goes through the content-access policy and the credential redactor, one by one: an `ai_content_access` row for `.cursor/**` keeps those files out and names them.

**Not searched.** `node_modules/`, `vendor/`, `third_party/`, `.venv/` and `.git/` are not looked inside. A path or glob in `agent_docs_globs` is read wherever it points.

**Turning it off or adding to it.**

```yaml
# .lemoncrow/review.yaml
import:
  agent_docs: false                    # none of the above, the root AGENTS.md and CLAUDE.md included
```

```yaml
# .lemoncrow/review.yaml
import:
  agent_docs_globs:
    - docs/ai/*.md                     # more instruction files: paths, or globs
    - .windsurfrules
```

`agent_docs_globs` takes repository paths or globs (at most 32). A listed file applies to every path unless its name or front matter scopes it, as above. A glob needs the list of files in the trusted base, which the Action and the local CLI have; where there is none it is reported and matches nothing, so list the files by path. A path the base does not hold, or a glob that matches nothing, is a warning. With `agent_docs: false` the list is ignored, and the view says so.

## Moving from another review tool

```bash
lc review config import team-review/            # a folder: reads config.json and rules.md in it
lc review config import team-review/config.json # or one settings file, or one .md rules file
lc review config import team-review/ --dry-run  # the report only; writes nothing
```

One path in, a working `.lemoncrow/review.yaml` out, and a short report. The command reads only the path you give it. It maps each setting of the file to the closest setting LemonCrow has, lists the rules under `import.rules_files` (where they are read as advisory rules, see above), writes the result, and prints which settings it **carried over**, which it **mapped to something close**, and which it **did not carry**, each with the reason and the closest thing LemonCrow has. Nothing is sent anywhere and no model is called.

| The file says | LemonCrow gets | Notes |
| --- | --- | --- |
| `strictness` 1 to 3 | `review.strictness`, the same number | Same scale (1 reports everything, 3 never comments on P2). The default differs: at 2 LemonCrow holds P2 in a collapsed block with no inline comment, so use 1 for inline P2 comments. |
| `ignorePatterns` (gitignore lines) | `review.ignore` (path globs) | A name matches at any depth, a leading `/` or a slash in the middle anchors it, `dir/` means everything under it. LemonCrow also hides an ignored file from the model as context, not only from the review. Negated lines (`!keep/**`) cannot be expressed and a pattern that would ignore everything is refused; both are named. |
| `commentTypes` | nothing to set for `logic` and `syntax`; `[]` becomes `review.inline_comments: false` | LemonCrow reports defects only. `style` and `info` have no counterpart and are dropped with the closest advice (state the convention as a rule, or list the style guide in `import.agent_docs_globs`). There is no repository-wide switch for a finding category. |
| `instructions` (free text) | `.lemoncrow/review-instructions.md`, listed in `import.agent_docs_globs` | **Context, not an order.** Free text is not a rule (a rule is checked for violations), and project conventions are background the model is told never to follow as an instruction, inside a 600 to 2,000 token budget shared with `AGENTS.md`. "Do not comment on X" is therefore not enforced; the enforced settings are `review.ignore` (paths) and `review.strictness` (volume). |
| a rule's `severity` (`low`, `medium`, `high`) | `review.rules.<key>.severity`: `high` is `p1`, `medium` and `low` are `p2` | **Never above P1.** A P0 needs a critical rule, or evidence from outside the model's own code (`criticality.severity_for`); a settings file is neither, so a file that asks for `critical` or `p0` is written as `p1` and the report says so. LemonCrow has no rule severity below P2, so `medium` and `low` both become `p2`. A finding that cites the rule gets that severity in place of the ordinary ladder's, so to make a rule a gate later, list it under `import.critical` and delete its `review.rules` entry (a severity there would keep it at P1 or P2). |
| `fileChangeLimit` | `review.max_files` | A pull request over the limit is not skipped: the highest-risk files are reviewed first and the next run takes the rest. |
| `updateSummaryOnly` | `review.inline_comments: false` | Summary only. |
| `autoReview`, `labels`, branch and author filters, `ignoreKeywords`, `effort`, `statusCheck`, `fixWithAI`, summary sections, `hideFooter`, `autoApprove`, `disabledRules` and the rest of a tool's switches | dropped | Each is reported with why LemonCrow has no setting (often: the workflow decides which pull requests run, or the layout is fixed) and the closest thing it has. |

What it writes, and what it never does:

- `.lemoncrow/review.yaml` with `review:` and `import:` blocks, and `.lemoncrow/review-instructions.md` when there are instructions. Commit them to your default branch: a review reads its config from the merge base, so the pull request that adds them is still reviewed under the old settings.
- It never overwrites what you wrote. A `.lemoncrow/review.yaml` with no `review:` or `import:` block gets them appended and keeps every character of its own text. One that already has either block is left alone, the block to add is printed, and the exit status is 1; `--force` merges instead (your values win and are named in the report, lists are joined, and the file is rewritten, so its comments are not kept). A different instructions file is replaced only with `--force`. Running it again changes nothing.
- A folder is read at the level you name. LemonCrow has one policy file and no per-folder cascade, so run it for the folder that holds the settings you want; settings in nested folders are not merged.
- A rule's key is `imported:<file>:<id>` (the report and `lc review config --effective` print it), and every imported rule is advisory until you list it under `import.critical`.

The report, shortened:

```text
Imported review config from team-review/ (config.json, rules.md)

Carried over (2)
  strictness  ->  review.strictness: 3
  ignorePatterns  ->  review.ignore: 3 globs
Mapped to the closest LemonCrow setting (2)
  instructions  ->  .lemoncrow/review-instructions.md, listed in import.agent_docs_globs
  severity: high (1 rule: no-raw-sql)  ->  review.rules.<key>.severity: p1
Not carried (2)
  commentTypes: style    LemonCrow posts no style comments and cannot be told to. Closest: ...
  labels                 Which pull requests are reviewed is decided by the workflow's own filters ...
Rules: 4 read through import.rules_files (config.json, rules.md) as advisory rules ...
Wrote .lemoncrow/review.yaml
```

**Why the folder is not found by itself.** LemonCrow never names another vendor's product in its code or its docs (a repository test enforces it), so it cannot look for another tool's folder by name. Pointing at the folder once is the discovery: the command writes the `import` block that makes it found from then on, and nothing in a repository is read as another tool's config until a person says so.

What lives only on the other tool's servers (a dashboard, a rule it learned, a suggested rule, an organization-enforced rule) cannot be read: copy the text of a rule into a file once. The instruction files of your coding tools (`AGENTS.md`, `.cursor/rules` and the rest, above) need no import. Run `lc review config --effective` on your default branch to see exactly what applies, then `lc review init-action` to write the workflow ([GitHub Action](github-action.md)).

## See what will apply

Before the first pull request:

```bash
lc review config --effective                      # the merged rules, with the file each came from
lc review config --effective --base main --json   # a specific trusted base, for scripts
lc review config --dry-run --pr 123               # which rules would apply to that pull request's files
lc review config --dry-run --head my-branch       # the same for a branch (--files a.py --files b.py for a list)
```

`--pr` uses the pull request's merge base as the trusted base, exactly as the Action does, so the answer is what its review will use. Nothing is sent to a model and nothing is written. The effective view lists the files it looked for and what the base held, every rule with its key, severity, scope and source, the paths the model will never read, the conventions with their size at each depth, and the warnings. The view covers the repository's files; it does not include an organization policy.

The effective view also lists every instruction file it found, with what it is and the paths it applies to (`src/billing/AGENTS.md  applies to: src/billing/**`).

The pull request's own summary says when the import warned about something: on a clean review, the `What we checked` receipt shows `Imported config: 2 import warnings (run lc review config --effective)`.

## Limits

- At most 32 files are read (list entries, and files a glob matched), 40 rules and about 3,000 tokens of rule text are imported, each rule cut at 800 characters, and at most 400 entries of one file are looked at; what is over is named in the view. Unscoped rules ride in every prompt, so the set is small on purpose. A file larger than a million characters is not read.
- Imports run in the Action and the local CLI. The hosted service's trusted-base capture does not fetch these files yet, and finding the files of other tools by name needs the base's file list, which the hosted capture does not have either.
- Instruction files that scope to a directory are matched by path prefix; there is no rule precedence between a nested file and a root one, both are context.
- A head edit to a rules file is reported as future policy only for files `import.rules_files` names by path; a file only a glob matches is not tracked.
- Whether imported rules catch what the tool they came from caught is unmeasured.
