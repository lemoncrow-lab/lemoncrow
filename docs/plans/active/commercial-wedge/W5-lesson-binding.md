# W5 — Adaptive lesson binding (closed loop, narrow)

> Parent: [`index.md`](index.md). Driving spec: [`day90/11-federated-learning.md`](../../../specs/day90/11-federated-learning.md) (W5 is the local precursor).
> Depends on: `outcome_capture.py`, `lesson_promotion/capability.py`, W3 (router consumes lessons).

## Goal

Close the loop the assessment flagged as "partial": lessons promoted out of
the human review inbox actually **reshape future routing and memory
behaviour**, automatically, without another human in the loop. Narrow on
purpose: only **two lesson kinds** at launch — `route-preference` and
`cost-cap`. Anything else is W5+.

Today the lesson pipeline scores route/compact decisions and surfaces them
for review in `cli.py` (`lesson review`). Promoted lessons are filed but
nothing downstream binds them. W5 turns lessons into typed records that
the router and outcome-capture consult on every decision.

## Why now

- W3 (cross-vendor routing) just shipped an advisor; without W5 it's a
  static config + heuristics. With W5 it gets feedback that compounds.
- W6 (team) needs typed lessons to attach team-scope policies to — "the
  team has decided Gemini Flash for reads" is a route-preference lesson
  scoped to `team_id`. Build the single-user version first.
- W2's counterfactual report becomes the *justification* for lessons: a
  lesson can carry a `source_session_id` pointing at the counterfactual
  that motivated it.

## Where the code lives

```
src/atelier/core/capabilities/lesson_promotion/
  capability.py              EXISTS — reused; promotion still human-in-loop
  store.py                   NEW — typed lesson store (SQLite)
  models.py                  NEW (or extend existing) — Lesson + LessonKind
  bindings/                  NEW package — one binding adapter per consumer
    route_preference.py      NEW — applied by W3 router
    cost_cap.py              NEW — applied by the within-vendor router + advisor
src/atelier/core/capabilities/cross_vendor_routing/
  router.py                  EXTEND — consults lesson store on every recommend
src/atelier/infra/runtime/outcome_capture.py
                             EXTEND — emits lesson candidates with typed shape
src/atelier/gateway/adapters/cli.py
                             EXTEND — `lesson list/show/disable/enable`
tests/core/capabilities/lesson_promotion/
  test_route_preference_binding.py
  test_cost_cap_binding.py
  test_lesson_expiry_and_decay.py
  test_lesson_scope_isolation.py        # foundation for W6 team scoping
```

## The two launch kinds

### `route-preference`
```json
{
  "kind": "route-preference",
  "id": "lesson-a91c",
  "scope": "user",                              // "user" now; "team" in W6
  "match": {"tool": "Read", "phase": "explore"},
  "prefer": {"vendor": "google", "model": "gemini-flash"},
  "confidence": 0.86,
  "source_session_id": "7c2f8a",
  "captured_at": "...",
  "expires_at": "...",
  "decay_half_life_days": 30
}
```
W3's router reads matching lessons before its scoring pass; a high-confidence
preference becomes a soft pin that costs the alternative an extra weight.
Hard pins are still user config (`route.yaml`), not lessons.

### `cost-cap`
```json
{
  "kind": "cost-cap",
  "id": "lesson-b42e",
  "scope": "user",
  "limit_usd_per_session": 5.00,
  "on_breach": "downgrade-one-tier",            // or "warn", "block"
  "captured_at": "...",
  "expires_at": null
}
```
The router consults the cap each turn; on projected breach it picks the
next tier down. Outcome capture annotates the session report with "cost-cap
fired N times".

These two shapes plus the decay/expiry / scope machinery are the W5
deliverable. Anything else is W5+.

## Promotion path

1. `outcome_capture.py` emits a `LessonCandidate` whenever a recurring
   pattern crosses a threshold (e.g. "Gemini Flash beat Sonnet on 14 of 14
   read turns in the last 30 days, cost diff $4.20").
2. Candidate lands in the existing review inbox (`lesson review`) —
   **still human-approved before binding**, by design, in v1.
3. Approval writes a typed `Lesson` into the store.
4. Consumers (router, advisor) consult the store on every decision.

W5 does **not** auto-promote lessons. The loop is closed on the *binding*
side; the *approval* side stays human. Auto-promotion is W5+ once we have
confidence in the candidates.

## Decay and expiry

- Default decay half-life: **30 days** (matches W3's quality-prior decay).
- Optional explicit `expires_at` overrides decay.
- Lessons below confidence 0.4 are invisible to consumers but stay in the
  store for audit; `lesson list --include-inactive` surfaces them.

## Scope (foundation for W6)

- v1 scope is always `user`. The field exists so W6 can introduce `team`
  and `workspace` scopes without a schema migration.
- Consumers must check scope on every read. A `team`-scoped lesson never
  applies on a machine not part of that team.

## User-visible CLI

```
$ atelier lesson list
  ID         Kind              Scope  Confidence  Last applied
  a91c       route-preference  user   0.86        2026-05-17
  b42e       cost-cap          user   1.00        2026-05-18

$ atelier lesson show a91c
$ atelier lesson disable a91c        # soft-disable; stays in store
$ atelier lesson enable a91c
```

## Validation

- `test_route_preference_binding` — fixture lesson reshapes W3's router
  ranking for the matching tool/phase only.
- `test_cost_cap_binding` — synthetic session crosses the cap; router
  downgrades on the next turn.
- `test_lesson_expiry_and_decay` — past expiry → invisible; decay reduces
  confidence over time.
- `test_lesson_scope_isolation` — `team`-scoped lesson (forward-compat
  field) does not bind on a `user` scope.
- `test_disabled_lesson_does_not_bind` — disable hides from consumers,
  enable restores.
- `test_outcome_capture_emits_candidate_for_recurring_pattern` — fixture
  history triggers the candidate writer.

## Exit criteria

- Typed lesson store exists with the two launch kinds.
- W3 router consults the store on every recommend; behaviour is observable
  in `route status`.
- `lesson list/show/disable/enable` work and round-trip.
- Scope field is enforced on every read, with a unit test specifically
  proving `team` scope is invisible to a non-team machine.
- Outcome capture emits candidates with the typed shape.
- No auto-promotion path exists yet (approval stays human in v1).
- Trace recorded via `mcp__atelier__record`.

## Out of scope (this milestone)

- **Auto-promotion of lessons** without human approval — W5+.
- **More lesson kinds** (memory-pin, compact-policy, tool-allowlist) —
  future, when there's a concrete consumer asking for them.
- **Federated lessons** across users (the day90/11 spec). W5 is the local
  closed loop; cross-user is a separate plan.
- **Team-scoped lessons** at full fidelity — W6 ships scope semantics on
  the team side; W5 ships only the field.

## Open questions

1. **Confidence floor.** 0.4 — guess. Default: **0.4 floor, 0.7 to apply
   without a tiebreaker**, tunable per kind.
2. **Decay model.** Half-life vs linear. Default: **half-life**; matches
   W3 quality prior.
3. **Where does the lesson store live physically?** Same SQLite as
   `sqlite_memory_store`, or its own DB? Default: **own DB**
   (`~/.atelier/lessons.sqlite`), so W6's team variant is a swappable
   backend.
