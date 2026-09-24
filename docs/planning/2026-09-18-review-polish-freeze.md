# Review polish freeze

Date: 2026-09-18

Status: Pass 1 complete. Feature scope remains frozen for the first developer cohort.

## Rule

Until the polish pass is complete, Review work is limited to:

- visual hierarchy;
- information density;
- wording and naming;
- interaction clarity;
- loading, empty, success, warning, and failure states;
- keyboard/accessibility behavior;
- responsiveness;
- perceived and measured latency;
- consistency between equivalent controls;
- correctness bugs discovered while polishing.

Do not add new product capabilities unless a missing capability makes an existing shipped workflow unusable.

## Pass 1 completion

All 16 scheduled polish passes are complete on combined `main`. No new Review capability was added during this pass.

| Pass | Surface | Result | Representative commit |
| --- | --- | --- | --- |
| 1 | Reader shell + attention | duplicate revision/status chrome removed; compact shared actions | `67cbe806e` |
| 2 | Outline + search | denser file navigator, quieter sections, narrower sidebar | `7d11af5cd` |
| 3 | Diff stream + target headers | repeated state/count chrome removed; diff width prioritized | `3edf14a62` |
| 4 | Composer + inline threads | human message leads; exceptional states only; explicit submit labels | `92fa9645f` |
| 5 | Comments sheet | one dense review queue instead of card gallery | `d66939b2c` |
| 6 | Feedback handoff | one vocabulary: Review feedback → Send/Copy → Feedback sent | `a81d03da9` |
| 7 | Revision update + delta | reviewer language replaces reconciliation terminology | `7e9b57b1b` |
| 8 | History / compare | revisions first; raw checkpoint IDs and duplicate historical chrome removed | `a6d0fa021` |
| 9 | Overview + context | major changes / why / affected code lead; technical provenance collapses | `548f17bd0`, `37682c3b1` |
| 10 | Finish + readiness | verdict first; nonzero blockers only; explicit Finish anyway override | `ac17b5979` |
| 11 | Hosted people/provider | people and reviewer progress simplified; GitHub publication explicit and separate | `19f5ac986` |
| 12 | Inbox/directory | one review = one row; attention buckets are filters, not duplicated dashboards | `c7ef4dc45` |
| 13 | Specialized previews | web controls disclosed behind View; media/API/service/document chrome reduced | `d0979d856`, `5b3e822dc` |
| 14 | Loading / empty / error | honest progress, recoverable failures, retry states, usable partial content preserved | `e7f1df5c2`, `47e5f1848`, `1c85cc259` |
| 15 | Keyboard / accessibility / responsive | focus rings, modal focus recovery, narrow-laptop diff protection | `c169fc8e0`, `21532ea17` |
| 16 | CLI handoff | stable Review loop leads help; duplicate URL announcement removed | `099678d90` |

Release verification after Pass 1:

- frontend Review suite: 32 files / 260 tests passed;
- Review CLI suite: 106 tests passed;
- TypeScript check: passed;
- production frontend build: passed;
- `git diff --check`: passed.

Measured Reader production chunk after this pass: about 982 kB uncompressed / 279 kB gzip. Treat this as a measurement baseline; do not optimize it without browser-perceived latency evidence.

Validation still required with the first cohort (not feature work):

- measure first useful diff / perceived startup latency in a real browser;
- run the two-account hosted reviewer loop in real browser sessions;
- smoke exact-session Claude/Codex feedback delivery against a disposable live agent session.

## UX principles

1. **The diff owns the screen.** Chrome should earn every pixel.
2. **One fact, one primary place.** Avoid showing the same revision, status, count, or action in multiple adjacent surfaces.
3. **Current attention beats global metadata.** The reviewer should immediately see what needs action now.
4. **Progressive disclosure.** Common actions stay visible; configuration, history, and secondary information stay one interaction away.
5. **Human state is visually stronger than machine context.** Reviewed, changed, needs-changes, comments, and responses outrank generated context.
6. **No mystery states.** Loading, stale, unavailable, blocked, empty, and failed must explain what the reviewer can do next.
7. **Compact, not cramped.** Prefer 28–32 px controls, short labels, and restrained decoration, while preserving usable hit targets and focus states.
8. **No decorative dashboards.** Counts and summaries exist only when they help the next review decision.
9. **Stable spatial model.** Revision updates, search, comments, and context should not unexpectedly move the reviewer.
10. **Polish before breadth.** Do not reopen platform-parity work during this pass.

## Complete Review product inventory

### P0 — Core reading loop

#### 1. Review entry / CLI handoff
Features:
- `lc review` default working-tree review;
- staged and revision-range review;
- stable Review ID / URL;
- local vs hosted server selection;
- browser opening;
- terminal-only mode;
- review list/show/open;
- setup/surface detection.

Polish:
- first-run wording;
- URL/open feedback;
- errors when auth/server is unavailable;
- noisy terminal output;
- consistency between local and hosted behavior;
- clear distinction between “open review” and “create/update revision”.

#### 2. Review directory / hosted inbox
Features:
- review list;
- status/attention buckets;
- search;
- pagination;
- repository/author/focus metadata;
- reopen/discover durable reviews.

Polish:
- information hierarchy per row;
- empty states;
- search feedback;
- bucket naming;
- row density;
- scanability;
- current-user attention vs generic status;
- small-screen behavior.

#### 3. Reader shell
Features:
- back to reviews;
- title/change story;
- review identity;
- revision/history access;
- comments;
- hosted people/provider access;
- finish/reopen/restore;
- overflow/actions;
- focus mode;
- diff style;
- code theme;
- review order;
- product feedback.

Polish:
- remove duplicate metadata/actions;
- compact top chrome;
- establish one obvious primary action;
- responsive priority order;
- consistent control styling;
- overflow grouping;
- focus behavior.

#### 4. Attention / readiness strip
Features:
- in-progress;
- feedback ready;
- waiting on author;
- re-review;
- ready to finish;
- historical state;
- target progress;
- readiness blockers;
- open comments;
- publish feedback.

Polish:
- one sentence maximum;
- eliminate duplicate controls from header;
- stronger distinction between state and action;
- reduce color noise;
- make blocker count understandable at a glance;
- avoid “dashboard bar” feeling.

#### 5. Review outline / file navigation
Features:
- file list;
- search;
- sections by review state;
- per-file target counts;
- promoted/reason hints;
- collapse sections;
- active file;
- search result navigation.

Polish:
- filename/path hierarchy;
- row density;
- state icons;
- reason truncation;
- active/hover contrast;
- search affordance;
- collapsed-state clarity;
- large-repo scanning.

#### 6. Diff stream
Features:
- continuous multi-file stream;
- split/unified;
- syntax highlighting;
- lazy/streamed loading;
- file collapse;
- context expansion;
- line annotations;
- markdown/media/web/API/service previews;
- load-more by changed-line budget.

Polish:
- maximize code width;
- gutters and line-number contrast;
- whitespace/density;
- file boundaries;
- loading placeholders;
- collapsed-file affordance;
- long-line behavior;
- binary/unsupported/degraded states;
- prevent preview UI from overpowering code.

#### 7. Target header / per-target actions
Features:
- target label;
- reason/attention level;
- reviewed;
- needs changes;
- unreview;
- comment;
- context;
- target state.

Polish:
- reduce button count;
- stronger current-state indication;
- make review/changes actions mutually understandable;
- compact secondary reason text;
- hover/focus consistency;
- avoid repeating state in text and icon unnecessarily.

### P0 — Human feedback loop

#### 8. Comment composer
Features:
- line/range/file comments;
- comment vs request-change;
- optionally mark target needs-changes;
- draft retention;
- recovered draft after revision changes.

Polish:
- composer hierarchy;
- default action;
- request-change semantics;
- keyboard submit/cancel;
- draft recovery wording;
- prevent accidental loss;
- line/range context clarity.

#### 9. Inline annotation/thread card
Features:
- human comments;
- replies;
- request-change state;
- author response;
- resolve/reopen;
- source attribution;
- anchor status;
- versioned history.

Polish:
- distinguish comment body from metadata;
- human vs AI/service attribution;
- reply indentation;
- resolved visual weight;
- author-response prominence;
- anchor warning clarity;
- action placement;
- reduce badges.

#### 10. Comments sheet
Features:
- open;
- re-review;
- resolved;
- orphaned;
- all;
- reply counts;
- jump to source.

Polish:
- filters and counts;
- thread preview;
- path/line hierarchy;
- orphaned explanation;
- useful empty states;
- scanability for many comments.

#### 11. Feedback preparation / publish panel
Features:
- unpublished/open/addressed counts;
- exact feedback preview;
- direct agent delivery;
- destination identity;
- copy fallback;
- delivery states;
- operation identity.

Polish:
- “prepare” vs “publish” wording;
- destination confidence;
- success/failure feedback;
- retry language;
- preview expansion;
- remove implementation-shaped “round/operation” language where unnecessary.

#### 12. Waiting / author-response state
Features:
- published feedback state;
- author addressed response;
- response without source changes;
- automatic re-review attention.

Polish:
- make “sent” distinct from “fixed”;
- make author response obvious;
- clear next action;
- avoid stale success state after new feedback appears.

### P0 — Revision loop

#### 13. New revision available
Features:
- working-tree source probe;
- automatic settled refresh;
- manual refresh;
- frozen current diff until update;
- draft deferral;
- stale-response protection.

Polish:
- banner size;
- wording;
- auto-vs-manual behavior clarity;
- “Later” semantics;
- avoid surprise refresh;
- maintain scroll/target context.

#### 14. Revision delta bar
Features:
- changed/new/removed targets;
- preserved work;
- delta focus;
- “show all”;
- discarded verdicts.

Polish:
- explain only what changed;
- keep it compact;
- avoid raw reconciliation terminology;
- actionable changed-target navigation;
- clear dismissal semantics.

#### 15. History / revision compare
Features:
- revision list;
- activity;
- A/B compare;
- immutable revision permalinks;
- judgment history.

Polish:
- default tab;
- revision naming;
- time/actor hierarchy;
- compare selector usability;
- changed-file summary;
- avoid looking like a separate product.

#### 16. Historical revision mode
Features:
- read-only revision;
- replayed comments/judgments;
- back to latest.

Polish:
- persistent but unobtrusive read-only indicator;
- disable states;
- clear latest action;
- avoid duplicate historical banners.

### P1 — Understanding and verification

#### 17. Change overview
Features:
- major changes;
- target counts;
- verification;
- provenance;
- artifacts;
- surfaces.

Polish:
- short factual orientation;
- remove dashboard feel;
- evidence-linked claims;
- strong unknown/degraded states;
- reduce generated prose.

#### 18. Context drawer — impact
Features:
- callers/affected sites;
- symbols;
- related source;
- degraded analysis.

Polish:
- answer “why should I care?” first;
- concise reason before lists;
- path/line links;
- unknown reach must be visually explicit;
- reduce nested cards.

#### 19. Context drawer — checks
Features:
- pass/fail/not-run/unknown;
- file/review scope;
- verification rows.

Polish:
- outcome first;
- scope second;
- current revision indication;
- failing rows visually dominant;
- unknown != pass.

#### 20. Context drawer — evidence
Features:
- screenshots;
- video;
- traces;
- documents;
- live preview;
- stale prior-revision evidence;
- uploads.

Polish:
- current vs old separation;
- artifact preview sizing;
- upload affordance;
- stale labels;
- avoid turning drawer into asset manager.

#### 21. Context drawer — author/provenance
Features:
- author/session/model provenance;
- author notes/rationale.

Polish:
- factual provenance vs author claim;
- compact labels;
- hide low-value technical metadata by default;
- uncertainty wording.

#### 22. Context drawer — discussion/history
Features:
- anchored discussion;
- orphaned comments;
- judgment events.

Polish:
- avoid duplicating Comments sheet;
- current thread first;
- concise history;
- consistent human/agent/system presentation.

### P1 — Finish and collaboration

#### 23. Finish Review sheet
Features:
- target summary;
- outstanding comments;
- verification gaps;
- stale evidence/history;
- Comment/LGTM/Changes requested;
- finish with outstanding work.

Polish:
- make verdict primary;
- reduce count-table feel;
- group only blockers that matter;
- concise warning for unfinished work;
- unmistakable separation from GitHub approval.

#### 24. Readiness sheet
Features:
- blockers;
- target pending;
- discussions;
- failed/unknown checks;
- hosted policy;
- current reviewer outcome.

Polish:
- likely merge conceptually with Finish hierarchy without removing capability;
- remove duplicate numbers;
- state first, details second;
- avoid “policy dashboard” visual weight for ordinary local reviews.

#### 25. Hosted participants
Features:
- reviewers;
- requests;
- active/completed;
- reviewer-specific progress/attention.

Polish:
- reviewer identity hierarchy;
- progress readability;
- invite/share wording if present;
- empty state;
- avoid admin feel.

#### 26. Hosted provider / GitHub sheet
Features:
- linked PR state;
- open provider;
- explicit Comment/Approve/Request changes;
- selected human comments;
- publication history/retry.

Polish:
- provider identity;
- safer outcome selection;
- comment selection density;
- operation history simplification;
- make external-vs-LemonCrow state separation immediately understandable.

### P1 — Specialized review surfaces

#### 27. Markdown preview
Polish:
- source/rendered toggle;
- typography;
- diff context;
- image handling;
- scroll synchronization expectations.

#### 28. Media/PDF preview
Polish:
- fit/zoom controls;
- before/after clarity;
- loading/error states;
- avoid oversized viewer chrome.

#### 29. Web preview
Polish:
- route selection;
- old/new comparison;
- viewport controls;
- loading and runtime failure;
- security/refusal messages.

#### 30. API preview
Polish:
- request/response hierarchy;
- method/path prominence;
- old/new comparison;
- failure readability.

#### 31. Service/container preview
Polish:
- service identity;
- run state;
- logs/output density;
- actionable failures.

### P1 — System states

#### 32. Startup/loading
Features:
- source-first preparation;
- enrichment later;
- first visual diff gating.

Polish:
- avoid full-screen waiting where possible;
- honest stage labels;
- visible progress without fake percentages;
- minimize layout shift.

#### 33. Empty states
Cover:
- no targets;
- no search matches;
- no comments;
- no evidence;
- no revisions;
- no provider;
- no surfaces;
- completed revision delta.

Polish:
- one-line explanation;
- next action when one exists;
- no decorative illustration needed.

#### 34. Error/degraded states
Cover:
- file load failure;
- stale reader after revision advance;
- auth failure;
- server unavailable;
- unsupported preview;
- provider failure;
- delivery uncertain;
- clipboard failure;
- incomplete analysis.

Polish:
- distinguish recoverable vs blocking;
- one primary recovery action;
- preserve surrounding usable content.

#### 35. Accessibility / keyboard
Features:
- j/k, J/K;
- review/change/comment/context/focus;
- search;
- command palette;
- modal focus traps;
- Escape recovery.

Polish:
- focus rings;
- tab order;
- shortcut discoverability;
- no keyboard-only dead ends;
- labels for icon-only controls;
- reduced-motion behavior.

#### 36. Responsive layout
Polish:
- narrow laptop widths first;
- outline/context collision;
- header control priority;
- sheet widths;
- diff split behavior;
- avoid horizontal chrome overflow.

## Polish sequence

Work through these in order:

1. Reader shell + attention strip.
2. Outline + search.
3. Diff stream + target headers.
4. Comment composer + inline threads.
5. Comments sheet.
6. Feedback publish/correction state.
7. New revision + revision delta.
8. History/compare/historical mode.
9. Context drawer + overview.
10. Finish + readiness.
11. Hosted participants/provider.
12. Inbox/directory.
13. Specialized preview surfaces.
14. Loading/empty/error states across all surfaces.
15. Keyboard/accessibility/responsive sweep.
16. CLI wording and first-run handoff sweep.

Each pass should:
- make no new feature;
- add/update focused UI tests;
- build/typecheck;
- compare before/after hierarchy;
- commit separately so regressions are easy to bisect.
