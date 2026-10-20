# Switching from Greptile

A team that moves its pull request reviews from Greptile to LemonCrow keeps the rules it wrote. LemonCrow reads Greptile's repository config and your `AGENTS.md` / `CLAUDE.md` from the same trusted base as `.lemoncrow/review.yaml`, turns what it can into review rules and excluded paths, and says out loud what it did not use. Nothing to convert, nothing to upload. Early access, like the [GitHub Action](github-action.md) it runs in.

This page covers the repository files. Dashboard rules (Greptile's Custom Context, org-enforced rules, learned or suggested rules) live on Greptile's servers and cannot be read: copy those into `.greptile/rules.md` or `.lemoncrow/review.yaml` once.

## What is read, and from where

Everything is read from the pull request's **merge base**, never from the pull request's own branch. A pull request that adds or edits any of these files cannot change its own review, so it cannot switch off its own findings. This is the one deliberate difference from Greptile, which reads its config from the pull request's source branch. The practical effect: a new or edited config takes effect for pull requests reviewed after it has landed on the base branch, and the pull request that edits it is still reviewed under the old one.

Highest precedence first:

1. `.lemoncrow/review.yaml`
2. `.greptile/` (`config.json`, `rules.md`; a nested `.greptile/` folder adds the rules for its own folder)
3. `greptile.json` (the legacy root file)
4. `AGENTS.md` and `CLAUDE.md` (context only: never a rule)

How precedence works:

- **Rules** merge by rule key. The same key in a higher source replaces the lower one, and each rule keeps the file it came from. A rule you write in `.lemoncrow/review.yaml` under the key of an imported rule (`greptile:<id>`) replaces it.
- **The single settings** (`ignorePatterns`, `customInstructions`, `strictness`, `commentTypes`) are not merged: the highest source that sets one wins, and the lower one is listed as overridden.
- Both `.greptile/` and `greptile.json` are read when both exist. Greptile itself lets `.greptile/` win; here its rules win the same way, and `greptile.json` still contributes whatever `.greptile/` does not say.

## How each Greptile field maps

| Greptile | Becomes in LemonCrow | Applied? |
| --- | --- | --- |
| `rules[]` in `.greptile/config.json`, or `customContext.rules[]` / `rules` in `greptile.json`: `id`, `rule`, `scope` | A Review rule with key `greptile:<id>` and the `scope` globs as its path scope. No `scope`: every path. A scope that cannot be read skips the rule instead of widening it | Yes, **advisory** (below) |
| `rules[].enabled: false`, `disabledRules` | The rule is left out. A `disabledRules` entry that matches no imported rule (a dashboard rule) is reported | Yes |
| `rules[].severity` | Kept for the view. It does not set a severity here | Recorded only |
| `.greptile/rules.md` | One advisory rule per top-level bullet (the nearest `##` heading prefixes it); with no bullets, one per `##` section; with neither, the whole file as one rule | Yes, advisory |
| `ignorePatterns` (gitignore syntax) | `ai_content_access: ai_denied` rows: the model never reads those paths, so they get no comments. A negation (`!path`) cannot be expressed and is dropped with a warning | Yes |
| `customInstructions` | Project conventions: bounded context for the investigator, like `AGENTS.md`. Not a rule | Context only |
| `AGENTS.md`, `CLAUDE.md` | Project conventions (below). A `CLAUDE.md` that is only `@AGENTS.md`, or a copy of it, is skipped | Context only |
| `strictness` (1 to 3) | Closest knob: `review.finding_threshold` (1 `suggestions`, 2 `medium`, 3 `high`). The AI pull request reviewer has no such setting yet: it always holds P2 findings back | **Imported, not applied** |
| `commentTypes` | None. LemonCrow categorizes findings by risk (correctness, security, persistence and so on), not by comment type, and posts no style or info comments | **Imported, not applied** |
| `triggerOnUpdates`, `triggerOnDrafts`, `skipReview` | Workflow settings, not repository config: the Action's `on-push` and `review-drafts` inputs and your workflow's `on:` triggers | Not imported (noted) |
| `shouldUpdateDescription`, `autoApprove`, `context` | LemonCrow never edits a description or approves, and does not import cross-repository context from this file | Not imported (noted) |
| Anything else | Ignored, with a warning | Ignored |

"Imported, not applied" is shown rather than silently dropped or silently obeyed: in the summary's `What we checked` receipt on a clean review (`Imported config: strictness 3 (closest: review.finding_threshold: high): imported, not applied`) and in `lc review config --effective`.

The rule text key in a rule entry is not part of Greptile's documented fields that this import was built from, so any of `rule`, `description`, `instruction` or `text` is accepted.

## Imported rules are advisory

A violation of a rule you wrote in `.lemoncrow/review.yaml` is a **P0**: a critical rule. An imported rule is not. Its violation is a **P1 or P2** by the ordinary severity ladder, whatever the model rates it and whatever Greptile's `severity` said, so it can never fail the `LemonCrow Review` check under `fail-on: p0` on its own.

Mark the ones you trust as gates in `.lemoncrow/review.yaml`:

```yaml
# .lemoncrow/review.yaml
import:
  critical: [greptile:no-raw-sql]   # keys are shown by `lc review config --effective`
```

Or write the rule again under the same key to change its wording or scope; a rule in `.lemoncrow/review.yaml` always replaces the imported one and is critical. To stop reading either source:

```yaml
import:
  greptile: false      # ignore .greptile/ and greptile.json
  agent_docs: false    # do not send AGENTS.md / CLAUDE.md to the investigator
```

An advisory rule ranks below every deterministic risk signal when LemonCrow chooses what to investigate, and its per-rule model check (`ai.rule_checks`) is asked after the critical rules, so a long imported list does not use up the few model calls a depth allows before a security or correctness candidate. Each rule that applies to a changed file can still use one of the per-rule checks, up to the ceiling (8 by default); the 20-call cap per review is unchanged.

## AGENTS.md and CLAUDE.md as project conventions

Both files are context for the investigator: how your team writes code. They are never turned into rules, are tagged as untrusted data in the prompt (instructions inside them cannot change the review), go through the credential redactor, and are cut to a token budget so they stay cheap. The excerpt is estimated at four characters per token, the same estimate as the [repository context](github-action.md#repository-context) pack, and the figures are ceilings per investigator call:

| Depth | Conventions per call (at most) |
| --- | --- |
| `quick` | 600 |
| `auto` | 1,000 |
| `thorough` | 1,500 |
| `deep` | 2,000 |

`LEMONCROW_REVIEW_CONVENTIONS_TOKENS` replaces the ceiling for every depth. A document is cut at a paragraph boundary with a visible ellipsis; a second document that no longer fits is left out and named. Editing `AGENTS.md` does not re-review unchanged code.

A file is never included if the content-access policy denies it for the model route in use: an `ai_content_access` row in `.lemoncrow/review.yaml` for `AGENTS.md` (or `customer_local_only` on an external route) keeps it out. An imported `ignorePatterns` entry such as `*.md` means "do not comment on these files", not "this is confidential", so it does not hide `AGENTS.md` from the conventions.

## See what will apply

Before the first pull request:

```bash
lc review config --effective                      # the merged rules, with the file each came from
lc review config --effective --base main --json   # a specific trusted base, for scripts
lc review config --dry-run --pr 123               # which rules would apply to that pull request's files
lc review config --dry-run --head my-branch       # the same for a branch (--files a.py --files b.py for a list)
```

`--pr` uses the pull request's merge base as the trusted base, exactly as the Action does, so the answer is what its review will use. Nothing is sent to a model and nothing is written. The effective view lists the files it looked for and what the base held, every rule with its key, severity, scope and source, the paths the model will never read, the settings that were imported but not applied, the conventions with their size at each depth, and the warnings. The view covers the repository's files; it does not include an organization policy.

## Limits

- Only fields Greptile's documentation shows are mapped (rules with `id`, `rule`/text, `scope`, `severity`, `enabled`; `disabledRules`; `ignorePatterns`; `customInstructions`; `strictness`; `commentTypes`; `customContext.rules`). The exact field names come from public documentation snippets that could not be checked against Greptile's live pages; an unknown key is ignored with a warning, never guessed.
- At most 40 rules and about 3,000 tokens of rule text are imported, each rule cut at 800 characters; what is over is named in the view. Unscoped rules ride in every prompt, so the set is small on purpose.
- `strictness` and `commentTypes` change nothing (above). There is no per-directory strictness or tier.
- A nested `.greptile/` folder adds rules for its own folder; Greptile's override-by-id between a folder and its parent is not replicated, so a child rule with the same id as a parent rule is a separate rule. Nested folders are found only where the base can be listed (the Action and the CLI).
- Ignored paths are withheld from the model entirely, as context too: ignore your tests and a changed function's tests do not appear in its repository context.
- Imports run in the Action and the local CLI. The hosted service's trusted-base capture does not fetch these files yet.
- Quality against Greptile on the same rules has not been measured.
