# Review base across capture paths — identity, compatibility and qualification

**Program:** Review Base-Freeze program (`2026-10-10-review-base-on-every-capture-plan.md`, private planning record) (owner decisions D1–D6).
**Record:** P4 at integration base `df9df50e0` (2026-10-13). This is an **intended contract and an integration-branch observation**, not a released feature. No claim is made for `main`, PostgreSQL or end-to-end qualification.

## Authority and identity

The existing ReviewSession, ReviewBinding, revision, recorded `review_base_sha` and `review_base_policy` remain authoritative; there is **no second Review identity, status or policy system**. Mutable capture means `working_tree` or `staged`. A new branch-bound Review records the base from its first revision. Subsequent capture compares against that Review base after applying the *existing* integration-tip advance rule, rather than blindly against every new `HEAD`.

| Capture pair | New mutable-Review identity | Who looks up the base | Historical fallback |
| --- | --- | --- | --- |
| Review engine → local server | Repository + branch (+ staged discriminator); detached HEAD uses worktree subject | Local active branch Review and engine's Git/pygit2 `review_branch_base` | Detached/unbound: current `HEAD`; no branch base recorded |
| Thin client (`lcr`) → local server | Repository + branch (+ staged discriminator); no stable temp-worktree identity | Client reads local active Review and advances base with git CLI, without pygit2 | Missing branch/unbound: current `HEAD`; preserve legacy routing |
| Thin client (`lcr`) → hosted | Repository + branch + authenticated author (+ staged discriminator) | Thin client reads author-scoped active Review before freezing/uploading base and head Views | No `workspace_branch`: historical per-repository unbound Review, client-selected `HEAD` |
| Review engine → hosted | Repository + branch + authenticated author (+ staged discriminator) | Engine reads author-scoped hosted active Review then applies existing Git advance rule | No branch/detached HEAD: historical unbound Review and current `HEAD` |

Hosted never has enough Git history to determine ancestry: the **capture client** selects the base View. A server's recorded SHA is a statement of Review identity and an auditable reference, **not** server-side proof that a git range is correct. Hosted branch lookup must authorize by caller and repository, and a same-named branch owned by a different author must not expose or adopt another author's Review. Commit-range, named-range and provider Reviews remain unchanged.

## Base policy and movement

`integrated` is the default for **new branch-bound Reviews** (D2). It preserves the initially recorded Review base while advancing only over integrated work: if `HEAD` no longer descends from the recorded base, first fall back to the merge base; for remote-tracking integration tips on `origin` or `upstream` (`HEAD`, `main`, `master`), advance to a merge base descending from the current base. A developer's own commits remain in the Change; a pull/rebase of integrated work is not presented as new unjudged author work. The thin client's command-line rule must be differentially proved against the engine's pygit2 implementation (D3); unknown tips may leave additional work to judge.

`fixed` (explicit `--new` in the engine's established Review lifecycle) **never** advances the recorded base. Do not invent a hosted policy override or silently promote a legacy unbound Review to either policy. Base advancement changes what belongs to a revision, **not** the validity of judgments on unchanged content. A commit amendment, switch away and back, and a rebase need separate parity proofs.

If the checkout is detached, the branch identity is unavailable, or the recorded base commit is absent (for example, a shallow/partial clone), fall back to current `HEAD` for that capture; do not invent ancestry. The Reader must make the degraded/no-frozen-base situation visible. No capture is refused solely because an older client lacks `workspace_branch`.

## Compatibility matrix (D4 and D5)

| Existing Review and incoming client | Capture target | Base interpretation | User-visible contract |
| --- | --- | --- | --- |
| Existing unbound hosted Review + old client lacking `workspace_branch` | Reopen historical per-repository Review | Client-selected current `HEAD`, no frozen `review_base_sha` | Keep prior behavior; Reader states base is **not frozen** |
| Existing unbound Review + new branch-aware client | Create/reuse a *separate* bound Review | New Review's first base is frozen | Old Review and links remain; no automatic adoption |
| New bound Review + same branch and author | Reuse same bound Review | Recorded base, advanced only under `integrated` | Commit under Review remains unjudged |
| New bound Review + other author, same branch name | Different bound Review | That author's recorded base | No cross-author judgment sharing |
| Bound Review exists + old client lacking branch | Reopen/create unbound per-repository Review | `HEAD` per capture | Never enter or change bound Review |
| New branch-aware thin client + older local server lacking the base lookup | Refuse the capture; do not guess `HEAD` | No new revision | Upgrade local Review server; old clients omitting the branch remain supported |
| No branch / detached HEAD | Existing unbound routing fallback | `HEAD` (not an invented branch base) | Not frozen; informational frontier notes when applicable |
| Staged capture | Distinct staged subject (`#staged`) if branch present | Same policy/fallback as working tree | Staged and unstaged Reviews are not conflated |

`workspace_branch` is **optional/additive**, not a new minimum protocol version. N/N-1 negotiation and older request shapes must continue to work; a supported old client must not be forced to upgrade just to capture. The existing protocol-skew suite simulates N-1 with the **current** implementation, which does not prove field omission. P4 adds a separate test of actual omitted-branch hosted capture and mixed old/new clients. It does not claim a complete wire-level old-binary or PostgreSQL test.

## D6 — unexplained base movement frontier note

For each subsequent revision, compare its incoming `base_sha` with **both** the Review's recorded base (if present) **and** the immediately previous revision's base. When it differs from both, append an **informational** note to the existing reviewer frontier's `notes` projection; no new state, trust item, issue or blocker. With no recorded base, changes from a previous revision base are still noteworthy (this is the signal available for old clients). No previous revision means no comparison signal. The note must name unexplained base movement without alleging it was malicious or changing readiness/verdict/judgments. A rebase, shallow clone, detached fallback or integration-tip advancement can legitimately cause it. The server sees revision SHAs but cannot establish Git ancestry.

**Integration condition:** P4 adds a pure `base_movement_notes` projection and one explicit hook in the shared Reader's existing `frontier.notes` response, with narrowly scoped unit tests. It has been cherry-picked into `feat/base-freeze-integrate@ff3075c4b` and its unit tests pass there. Local and hosted SQLite parity tests read that frontier; a local FastAPI HTTP Reader test now verifies the D6 notice and readiness neutrality. Hosted PostgreSQL Reader qualification remains to be completed. Do not call D6 shipped until those checks and the `main` merge pass.

## First-public-client compatibility decision (2026-10-13)

The product owner approved a **one-time exception** to the otherwise-required
published older-`lcr` executable qualification **only for the first published
standalone thin-client release**. No prior published standalone `lcr` was found;
the public full-distribution `v0.7.4` release predates the first standalone
thin-client source commit and contains no `lcr` entry point. This exception
is not a substitute for backward compatibility: a separately installed
`lemoncrow-client` wheel built from pre-change `main@2d119cb7a` was exercised
as an actual subprocess over HTTP against the reconciled local and hosted
SQLite servers. Both retained the unbound legacy Review, left its recorded
base empty, and used current HEAD for subsequent revision comparisons.

**For every later release where a previous standalone thin-client binary has
been published, the real published-binary compatibility gate is mandatory.**
The exception cannot be reused or generalized to arbitrary older artifacts.
Approval does not authorize the G11 release or mark Base-Freeze shipped.

## Reader presentation (P5, integrated 2026-10-13)

The Reader shows **Base not frozen** for mutable Reviews without `review_base_sha` (legacy and unbound captures). Branch-bound Reviews show the recorded Review base and current revision base separately. History identifies each revision's effective comparison base, naming base-only movement separately from changed review areas. The existing Reader consumes D6 `frontier.notes` only when present, without turning the informational note into a judgment, warning, readiness requirement or new navigation. Historical revision views do not reuse current frontier notes. Legacy servers with no `notes` property display normally.

P5's isolated `6768c5766` was cherry-picked as `4fe6e85d9` into `feat/base-freeze-integrate`. At that commit, 271 focused frontend tests, TypeScript typecheck and the production Vite build passed. These are integration-branch qualifications only, **not** a declaration of release on `main`.

## Proof and release gate

The owner plan's §7 matrix is the qualification authority: all four capture pairs; local, hosted SQLite and hosted PostgreSQL; commit, pull, rebase, amend, switch/back, detached HEAD, absent base commit; hosted author isolation, old/new clients and old unbound Review; differential git CLI/pygit2 no fail-open; mutation probes for routing, recorded base, lookup and advance rule. Add D6 notes and Reader's legacy not-frozen representation to that matrix.

P0–P4 are committed to the isolated integration branch (`ff3075c4b`); the coordinator verified client/server unit paths, 44 differential Git-rule tests and the local/hosted SQLite base-freeze parity cases. **Qualified on those paths is not PostgreSQL qualification, merge to `main`, or shipped.** See the coordinator qualification ledger (`2026-10-06-readiness-coordinator-intake.md`, private planning record) for gate ownership.
