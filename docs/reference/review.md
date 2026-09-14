# `lc review`

**Understand what your coding agent actually changed — deterministically, before you commit it.**

Agents produce diffs faster than anyone can read them. `lc review` is the gate
between "the agent says it's done" and "you decide it's safe": it answers what
changed, what that change touches *outside* the diff, which agent session wrote
it, and what evidence exists that it was tested. No model is called, nothing is
summarized or paraphrased, and anything it cannot establish is printed as
`unknown` instead of guessed. The verdict is always yours — the tool never
prints `APPROVED`.

Two other pages cover the rest of the ground:

- **New to it?** [The walkthrough](review-walkthrough.md) builds a throwaway
  repo and runs the full loop — review, mark, comment, the agent revises,
  `--since-my-review` — in about ten minutes.
- **Need a flag?** [The CLI reference](cli.md#reviewing-agent-written-changes)
  lists every flag and every output field.

This page is the middle distance: what the tool is for, what the default
screen means, and the shape of the loop you'll actually run day to day.

---

## Quick start

```bash
cd your-project
lc code index        # once per repo — without it, ranking degrades to "biggest diff first"
lc review             # opens the Review Reader for uncommitted changes against HEAD
```

If the installer set up your shell, `lcr` is a short alias for `lc review`.

On a clean tree there is nothing to review, so it reviews the last commit
instead (`HEAD~1..HEAD`) and says so — the substitution is never silent. Other
ranges:

```bash
lc review --staged        # what you're about to commit
lc review --base HEAD~1   # a specific commit
lc review main..feature   # a branch, against main
```

## What the terminal summary tells you

The Review Reader is the default human surface. Use `lc review --no-open` when
you specifically want the compact terminal summary instead:

```
ATTENTION
  ⚠ make_token changed   (src/auth.py)
    untouched call sites:
      src/session.py:L8    SessionManager.open

START HERE
  1. src/auth.py
     modified · production · +4 -2
     - 3 known callers
     - high-centrality symbol

EXECUTION EVIDENCE
  Generated with: unknown

Human review  REQUIRED
```

| Block | What it means |
| ----- | -------------- |
| `ATTENTION` | Changed definitions whose **callers were not touched** — a change leaking past its own diff. Plain `git diff` cannot show this; it comes from the local code index. |
| `START HERE` / `THEN` | The files in review order, ranked by risk (callers, centrality, blast radius), with the reasons attached to each row — never an unexplained score. |
| `EXECUTION EVIDENCE` | Which agent session produced this change and how that was established. `recorded at edit time` is a fact; anything else says so. |
| `Human review REQUIRED` | Always present. Nothing in this tool approves a change. |
| `degraded: …` | What it could not compute (usually: no index yet), named instead of silently skipped. |

## The review loop

A review is not a one-shot printout — it's a durable session keyed on
`(repo, subject, range)`, reopened rather than re-created on every run. That's
what makes the rest of this useful:

```bash
lc review --mark src/auth.py                          # record that you read this
lc review --comment "ttl is hardcoded" --on src/auth.py:L81 --comment-kind request_change
#   ... the agent revises the code ...
lc review --since-my-review                           # what changed since I looked?
lc review --feedback                                  # every open comment, as one bundle for the agent
lc review --finish                                     # record that you're done, with what's still open
```

A mark is bound to the *content* you reviewed, not a line number — ten lines
added above a function you approved don't reopen it; a rewrite of its body
does, and `--since-my-review` tells you exactly that. A comment is re-anchored
by its text and surrounding context on every later revision; if more than one
location could be right, it's reported **orphaned rather than guessed**.

## The browser workspace

```bash
lc review             # default: open the Review Reader
lc review --no-open   # terminal summary only
lc review --open      # explicit form; useful when combining with terminal-oriented flags
```

The local workspace has a compact review outline, one continuous virtualized
multi-file diff reader, and Context on demand as an overlay/pinnable surface for
impact, checks, evidence, author context, and discussion. It reads and writes
the same durable session as the terminal, so pressing `r` in the browser shows
up in `lc review --marks`. The server binds `127.0.0.1` only, on an ephemeral
port, with a per-process bearer token — see
[the workspace section of the CLI reference](cli.md#the-review-workspace)
for the full security model. With no frontend bundle installed, the default
browser path falls back to writing and opening a static HTML report instead of
failing.

## Machine-readable output

```bash
lc review --json    # the full packet, uncapped, for scripts and CI
lc review --html out.html
```

## Where to go next

- Run the [ten-minute walkthrough](review-walkthrough.md) on a scratch repo.
- Look up a specific flag or output field in the
  [CLI reference](cli.md#reviewing-agent-written-changes).
- Hit something unexpected? The walkthrough's
  [troubleshooting table](review-walkthrough.md#troubleshooting) covers the
  common cases (no index, no session recorded, clean tree, orphaned comments).
