# `lc review` — a hands-on walkthrough

The [CLI reference](cli.md#reviewing-agent-written-changes) lists every flag. This
page is the other thing: sit down for ten minutes, run the commands, and see what
the tool actually does — on a throwaway repository, so nothing of yours is
touched.

The short version of what you are looking for: **the first screen is a diff with
an order and a reason. The second screen is the one `git diff` cannot produce.**

---

## 0. A repository to play with

`lc review` needs a change to look at. On a clean tree it has nothing to say, and
says so:

```
working tree
HEAD..WORKDIR  0 files · +0 -0

no changes in this range
```

So build a scratch repo with a real call graph and a plausible agent change in
it. Everything below runs against `/tmp/lc-playground`, with its own store, so
your own `~/.lemoncrow` stays untouched:

```bash
P=/tmp/lc-playground; mkdir -p $P/repo/src $P/repo/tests $P/store; cd $P/repo
git init -q . && git config user.email you@example.com && git config user.name you

cat > src/auth.py <<'PY'
"""Token helpers."""

DEFAULT_TTL = 3600


def make_token(user, ttl=DEFAULT_TTL):
    """Mint an opaque session token."""
    return f"{user}:{ttl}"


def verify_token(token):
    """Return the user if the token is well formed, else None."""
    user, _, ttl = token.partition(":")
    return user if ttl else None
PY

cat > src/session.py <<'PY'
from src.auth import make_token, verify_token


class SessionManager:
    def __init__(self, store):
        self.store = store

    def open(self, user):
        token = make_token(user)
        self.store[token] = user
        return token

    def refresh(self, token):
        user = verify_token(token)
        if user is None:
            return None
        return make_token(user)
PY

printf 'def truncate(text, n=80):\n    return text[:n]\n' > src/util.py
printf 'from src.auth import make_token, verify_token\n\n\ndef test_roundtrip():\n    assert verify_token(make_token("ana")) == "ana"\n' > tests/test_auth.py
git add -A && git commit -qm "base: auth, sessions"
```

One convenience, because every command below points at that repo and that store
(the installer already gives you `lcr` for a plain `lc review`; this one is
playground-scoped, so it gets its own name):

```bash
lcp() { lc --root /tmp/lc-playground/store review --repo-root /tmp/lc-playground/repo "$@"; }
```

**Index it first.** Without an index there are no caller or centrality signals,
and the review order degrades to "biggest diff first" — the tool says so in its
`degraded:` line, but it is easy to miss and it is the difference between a
ranking and a sort:

```bash
lc --root /tmp/lc-playground/store code index --repo-root /tmp/lc-playground/repo
```

Now play the agent. Change a function everything calls, widen a signature, and
add a file:

```bash
cd $P/repo
sed -i 's/return f"{user}:{ttl}"/return f"{user}:{ttl}:v2"/' src/auth.py
sed -i 's/def verify_token(token):/def verify_token(token, *, strict=False):/' src/auth.py
printf 'def cache_get(key):\n    return None\n' > src/cache.py
```

---

## 1. What did the agent do?

```bash
lcp
```

```
working tree
HEAD..WORKDIR  3 files · +13 -3

ATTENTION

  ⚠ make_token changed   (src/auth.py)
    untouched call sites:
      src/session.py:L8    SessionManager.open
      src/session.py:L13   SessionManager.refresh
      tests/test_auth.py:L4   test_roundtrip

  ⚠ verify_token changed   (src/auth.py)
    untouched call sites:
      src/session.py:L13   SessionManager.refresh
      tests/test_auth.py:L4   test_roundtrip

START HERE
  1. src/auth.py
     modified · production · +4 -2
     - 3 known callers
     - 5 untouched impacted site(s)
     - high-centrality symbol

THEN
  2. src/cache.py  +6 -0
  3. src/util.py  +3 -1

EXECUTION EVIDENCE

  Generated with: unknown
    (no session records in the local history store for this window)

Human review  REQUIRED
```

Read it in this order:

| Block | What it is telling you |
| ----- | ---------------------- |
| `ATTENTION` | The changed definitions whose **callers were not touched** — where a change leaks past its own diff. This is the part `git diff` structurally cannot show. |
| `START HERE` / `THEN` | The review order, by risk, each row carrying the reasons it is where it is. No unexplained score. |
| `EXECUTION EVIDENCE` | Which agent session wrote this, and how that was established — `recorded at edit time` is a fact; a scored guess says so; `unknown` means nobody knows. |
| `Human review REQUIRED` | Always. The tool never approves anything. |
| `degraded: …` | What it could not compute, named rather than silently skipped. |

Other ranges, same screen: `lcp --staged` (what you are about to commit),
`lcp --base HEAD~1` (a commit), `lcp main..feature` (a branch).

---

## 2. Record that you read something

This is where a review stops being a printout. `--mark` and `--comment` write to
a durable session keyed on `(repo, subject, range)`; it is reopened, never
re-created.

```bash
lcp --units                                    # the reviewable units and their keys
lcp --mark src/auth.py                         # a path or fil: key: every target in that file
lcp --mark 'src/auth.py::mint'                 # one target, by the label the tool printed
lcp --comment "why the :v2 suffix?" --on src/auth.py:L8
lcp --marks --comments                         # still there tomorrow, and after a reboot
```

A path and the file's `fil:` key are the imprecise spelling of "I read this
file": they record the verdict on every review target in that file — never on
the file unit itself, which no progress or closure count is denominated in — and
name the targets they landed on. A `hun:`/`sym:` key or a printed label names one
judgment and is recorded exactly as typed. Labels are path-shaped, so if the
repository really contains a file spelled like some other file's label, the file
wins and the label it beat is named on stderr.

`--on` refuses a bare path on purpose: a comment attached to the top of a file is
a comment about something nobody read.

A unit that cannot be fingerprinted (a binary, a deleted file) records as
`unknown`, never `reviewed`, and says so at the moment it happens.

---

## 3. The agent revises. Now what do you re-read?

Play a second round — rewrite one file you reviewed, leave another alone but push
its lines down, add a new one:

```bash
cd $P/repo
printf 'def truncate(text, n=80):\n    if len(text) <= n:\n        return text\n    return text[: n - 3] + "..."\n' > src/util.py
sed -i '1i """Session tokens."""\n' src/auth.py     # your commented line moves down
lcp --since-my-review --comments
```

```
SINCE MY REVIEW  revision 2  (you last saw revision 1)
  changed since my review  1
  new                      3
  unchanged reviewed       0
  comments                 1 open · 0 orphaned

CHANGED SINCE MY REVIEW  (1)
  src/auth.py

NEW SINCE MY LAST REVIEW  (3)
  src/cache.py
  src/cache.py::cache_get

COMMENT ANCHORS  (1)
  relocated   src/auth.py L5→L7
    re-found by its text and both neighbours — selected text and both context blocks matched
```

That screen is the product:

- **`CHANGED SINCE MY REVIEW`** — you approved this content and an agent rewrote
  it. Nobody else's tool shows you this.
- **`UNCHANGED REVIEWED — nothing to re-read`** — the work you do not have to
  redo. A mark is bound to content, not to a line number: lines added above a
  function do not reopen it, a rewrite of its body does.
- **`COMMENT ANCHORS`** — your comment followed the code, and says by which rung
  it was found. If two locations fit, it is **orphaned rather than guessed**.

Hand the objections back to whoever will fix them:

```bash
lcp --feedback        # every comment as one Markdown bundle for the agent
lcp --finish          # record that you are done, with what was still outstanding
```

---

## 4. The browser workspace

```bash
lcp --open
```

Three panes — ATTENTION/FILES, the diff, and a CONTEXT pane that stays quiet
until you select something. `j`/`k` move, `]`/`[` jump between attention items,
`r` marks reviewed, `u` reopens, `s` toggles split/unified, `?` toggles context.

It is the same session as the terminal: press `r` in the browser and
`lcp --marks` prints it. There is exactly one authoritative review state.

The service binds `127.0.0.1` on an ephemeral port and refuses to start on any
other interface; each process mints a bearer token delivered in the URL fragment
and held in memory only. If no frontend bundle is installed, `--open` says so and
writes the static HTML report instead (see `LEMONCROW_REVIEW_BUNDLE_DIR`).

---

## 5. Try to break it

The honesty rules are the feature. They are worth testing yourself:

| Do this | What must happen |
| ------- | ---------------- |
| Rename a function you marked reviewed | The verdict is discarded **and announced** — `VERDICTS DISCARDED`, on every command, on every later run, until you move past it. |
| Then undo the rename | The comment returns to its original line, on the original symbol. No false orphan. |
| Duplicate the line you commented on | `orphaned` — *2 candidate locations*. It must refuse rather than pick one. |
| Delete the commented line, leaving its text as a prefix of a longer line | `orphaned`. A substring is not a location. |
| Add ten lines above a reviewed function | It stays `reviewed`. Nothing to re-read. |
| Change one character inside it | It reopens. |

---

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `no changes in this range` | Clean tree. Review a commit instead: `lcp --base HEAD~1`. |
| `START HERE` looks like a file list | No index for this repo — run `lc code index`. Without it there are no caller or centrality signals. |
| `Generated with: unknown` | No agent session recorded an edit in this window. Exact provenance is captured by the host hooks at edit time; install them with `lc init` in the repo the agent works in. |
| `--open` writes HTML instead | No frontend bundle in this install. Set `LEMONCROW_REVIEW_BUNDLE_DIR`, or read the HTML report. |
| `Error: no reviewable unit matches …` | The unit is not in the current revision — usually because the code moved on since you listed the keys. Re-run `--units`. |

When you are done: `rm -rf /tmp/lc-playground`.
