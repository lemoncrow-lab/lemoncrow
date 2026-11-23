# Data flow engines side by side: Joern and Opengrep (2026-11-15)

**Result: Opengrep is the better base for P10-7, but neither engine is ready to back the data flow difference as P10-7 describes it.** Of 60 sampled flows per engine, 56 of Opengrep's were real and 26 of Joern's. In the configurations that finished, Opengrep ran in 5 to 72 seconds and under 650 MiB. Joern took 28 to 48 seconds and 1.8 to 2.3 GiB. When TypeScript imports were resolved at depth 3 or more, Opengrep ran out of memory or time. Opengrep's base-versus-head diff had no noise, while Joern gives different paths for the same input on two runs. Recall was low for both: against flows traced by hand from 30 entry points, each engine found 6 of 60 sink units in Flagsmith, 0 to 3 of 52 in Grist, and about 30 of 92 in Apollo. Across the three pull requests, neither engine reported the one flow change that the shared catalog should have caught.

This was a measurement only. No product code changed and neither engine was added to the product. Scripts, catalogs, clones, outputs and logs are in the session scratchpad (`dataflow-spike/`), which may be cleared.

## What was chosen and why

- **Engines.** These are the two engines the P10-6 research left standing:
  - Joern v4.0.683 (Apache-2.0), from the release zip `joern-cli-linux-arm64.zip`, checked by SHA-512, on `eclipse-temurin:21.0.9_10-jdk-noble`. The image is 4.77 GB.
  - Opengrep v2.0.0 (LGPL-2.1), from the release binary `opengrep_linux_aarch64` (sha256 `6257b654…d6d8`), on `debian:bookworm-20251103-slim`. The image is 361 MB. Opengrep v2.0.0 was two days old when it was measured.
- **Repositories.** Each repository needed a permissive licence, HTTP entry points and database writes. Candidates were sized before the choice by counting non-blank, non-test first-party lines:
  - **Python:** Flagsmith (Django and DRF, BSD-3-Clause) was chosen. healthchecks (13k lines) was too small. Netflix dispatch (FastAPI, 60k, Apache-2.0) also qualified.
  - **TypeScript:** Grist (`grist-core`, Express and TypeORM, Apache-2.0) was chosen; its server side is 80k lines. etherpad-lite (26k) was too small, Unleash is AGPL-3.0, and Flowise's licence has commercial portions.
  - **Java:** Apollo (Spring Boot and Spring Data JPA, Apache-2.0) was chosen. Shopizer (83k, Apache-2.0) also qualified. Why Apollo was picked over Shopizer was not written down.

  No repository was swapped after results were seen.
- **Analysed input ("views").** Both engines read the same tree, copied out of each checkout:
  - Flagsmith: `api/` without tests and migrations. Base is 57,037 lines in 868 files.
  - Grist: `app/{server,gen-server,common,plugin}` without TypeORM migrations. Base is 80,464 lines in 380 files.
  - Apollo: every module's `src/main/java`. Base is 48,751 lines in 528 files, just under the 50k aim.
- **Pull requests.** One recent merged pull request per repository that adds an HTTP endpoint and touches server code, picked from the list of merged pull requests before any engine ran:

  | Repository | Pull request | Base | Head |
  | --- | --- | --- | --- |
  | Flagsmith | #8813, an endpoint that issues HubSpot chat visitor tokens | `3de69a9bfc10` | `f4735b64807f` |
  | Grist | #1775, enable and disable user endpoints with a `disabledAt` column | `b3f193fcae6e` | `7b6b1521fee7` |
  | Apollo | #5665, batch create, update and delete of items in the OpenAPI | `5d438a23f5b6` | `39b8d491bdf4` |

## Method

- **Containers.** Docker ran in the colima VM on an Apple-silicon machine (linux/arm64, 6 CPUs, 16 GB). Every analysis ran in a fresh container:
  - with `--network none`;
  - limited to 4 CPUs and 10 GiB, the same for both engines, with swap equal to memory;
  - with a 20-minute kill.

  The view and the scripts were copied in, and results were copied out. Nothing from a clone ran on the host. Wall time is measured inside the container, and peak memory is the container cgroup's `memory.peak`. Joern ran with `-J-Xmx8g` for the frontend and for the query. Images were built with network access. The whole spike used about 1.5 hours of machine time, inside the 8-hour box.
- **Other load on the VM.** Other containers shared the VM throughout: `rr-review-pg`, Temporal, Redis, a buildkit builder, and a set of `ecom-repo-*` containers in a crash loop. Of those, `nango-server` used 1.1 to 2.7 CPUs and `nocodb` up to 1.2. Wall times are therefore noisy:
  - Opengrep on Flagsmith base took 52 and 67 seconds on two runs of the same input. The 52-second run overlapped a small Joern toy run; the 67-second rerun overlapped no other spike run.
  - Opengrep on Grist with tsconfig at depth 2 took 38 seconds on base and 72 seconds on head, for about the same work.
  - A two-second Opengrep toy run overlapped the start of Joern's Grist base run.

  Peak memory did not vary in the same way.
- **Engine settings.**
  - Joern: the language frontend (`pysrc2cpg`, `jssrc2cpg.sh`, `javasrc2cpg`), then `importCpg` with the default overlays. Sinks were found by callee text and their arguments and receivers taken; then `sinkArgs.reachableByFlows(sources)` ran with `EngineConfig(maxCallDepth = 4)`.
  - Opengrep: `opengrep scan --enable-interfile --interfile-depth 4 --interfile-dedup-by=source-sink --dataflow-traces --json -j 4`, with `options: {interfile: true}` on every rule.
  - Both engines used depth 4 by default. Depth 2, 3 and 8 were tried where noted.
- **Catalog.** There is one catalog per framework, kept as equivalent as possible across the engines:
  - Sinks are a shared TSV of category and regex. The regex is matched against the callee text, which is the call minus its argument list (`Widget.objects.create`). There are four categories: db, http, file and log.
  - Joern applies the TSV directly. A 31-line generator turns the TSV and a sources file into Opengrep taint rules. Each category gets one rule with three sink forms: taint in an argument, taint in the receiver (`focus-metavariable`), and `new T(...)`.
  - Sources are written per engine: Scala queries for Joern and YAML patterns for Opengrep. They cover request fields and getters, view kwargs, DRF handler and `perform_*` parameters, Spring parameter annotations, `MultipartFile` and `HttpServletRequest` getters.
  - All rules were written for this spike. No Semgrep registry rule was used or copied.

  | Framework | Sink TSV | Opengrep sources (YAML) | Joern sources (Scala) | Generated Opengrep rules |
  | --- | --- | --- | --- | --- |
  | Django / DRF | 10 regexes | 29 lines | 18 lines | 193 lines |
  | Express | 4 regexes | 8 lines | 5 lines | 149 lines |
  | Spring | 9 regexes | 16 lines | 6 lines | 190 lines |

  The hand-written catalog is 112 lines in all. The shared Joern driver (`flows.sc`) is 71 lines.
- **Entry points and ground truth.**
  - **Selection.** For each repository, a fixed regex per framework listed every write handler (POST, PUT, PATCH or DELETE) in the base view: 173 in Flagsmith, 83 in Grist, 92 in Apollo. Ten were drawn with `random.Random(20261115).sample`, before any engine output existed.
  - **Tracing.** Three agents, one per repository, traced every flow from each handler by reading code only. They worked from a shared brief and saw no engine output. They recorded the source, the sink, the line, `value` or `selector` (the request data is written, or it only picks the row), the function path, and what the flow crosses (async, signal, decorator, framework-generic code or dynamic dispatch).
  - **Size.** The result was 115 flows in Flagsmith, 57 in Grist and 236 in Apollo. Recall is counted in sink units (entry point and sink location): 60, 52 and 92.
  - **Judgment calls.** These are recorded with the ground truth:
    - Grist treats `req.org` as a source, because middleware builds it from the Host header.
    - Grist core's audit logger does nothing.
    - Flagsmith counts Celery `.delay()` calls as sinks, and does not count `request.user` as a source.
    - Apollo takes the `restTemplate.exchange` call inside `RetryableRestTemplate.doExecute` as the HTTP sink, and counts the unannotated `operator` parameter as a source.
- **Recall rule.** An engine flow hits a unit when two things hold:
  - its sink is in the same file within 3 lines;
  - its source is inside the handler or within 3 lines of a traced source.

  A first pass missed real Joern flows whose source Joern places deeper, for example the `req.params.oid` read inside `getOrgKey(req)` where the trace recorded the call. A relaxed rule therefore also accepts a flow whose path passes the traced source location. Every hit from the depth 4 runs that only the relaxed rule accepted was then read by hand. Two Grist hits were real; the other seven (six Flagsmith, one Grist) came from false flows. The numbers below count only checked hits. The depth 2 Grist figure counts strict hits only.
- **Precision.** For each repository and engine, 20 distinct flows were drawn from the base run (seed 20261115). The 40 flows for a repository were shuffled into one sheet with the engine name removed. One agent per repository judged each flow by reading the code: real by the listed path, real by another path, or false. I re-checked a sample of the verdicts by reading the code: G08 and F12, plus the false-flow chains behind the Flagsmith relaxed hits.
- **Diff.**
  - **Key.** The flow key is the source symbol, the sink symbol and the ordered list of functions on the path, never a line number:
    - The source symbol is the enclosing function plus the normalised source text.
    - The sink symbol is the enclosing function plus the callee text.
    - Enclosing functions come from tree-sitter. An anonymous route handler is named by its route, as in `post('/api/orgs')`, even when a wrapper such as `expressWrap(...)` sits between them.
  - **Classification.** A (source, sink, category) pair only on one side is added or removed. A pair on both sides with different path sets is rerouted.
  - **In the pull request or noise.** A change counts as in the pull request when its path touches a file the pull request changed; otherwise it is noise.
  - **A fix during the run.** An earlier normaliser numbered anonymous handlers by position. When the pull request inserted new handlers, that turned 20 Joern flows on Grist into 20 "removed" and 20 "added". Naming handlers by route removed that artefact. The numbers below use the fixed normaliser.

## Results per repository and engine

All runs finished within 20 minutes and 10 GiB unless stated otherwise. Flows are what the engine reported: Opengrep findings, or Joern paths. Keys are distinct diff keys. Peak memory is in MiB, the higher of base and head.

| Repository | Engine | Base time | Head time | Peak memory | Flows (keys) | Recall (units hit) | Precision (of 20) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Flagsmith | Opengrep | 52–67 s | 36 s | 333 | 231 (168) | 6 / 60 | 20 (19 by the listed path) |
| Flagsmith | Joern | 34 s | 48 s | 1,894 | 1,392 (638) | 6 / 60 | 3 |
| Grist | Opengrep, as found | 23 s | 21 s | 490 | 58 (40) | 0 / 52 | 19 |
| Grist | Opengrep, flattened tsconfig, depth 4 | over 1,200 s, killed | stopped | 8,898 | none | not measured | not measured |
| Grist | Opengrep, flattened tsconfig, depth 2 | 38 s | 72 s | 600 | 346 (241) | 3 / 52 | not measured |
| Grist | Opengrep, flattened tsconfig, depth 3 | out of memory at 517 s | not run | 10,240 (the limit) | none | not measured | not measured |
| Grist | Joern | 42 s | 32 s | 2,367 | 642 (245) | 2 / 52 | 6 (4 by the listed path) |
| Apollo | Opengrep | 4.7 s | 4.7 s | 334 | 598 (479) | 29 / 92 | 17 |
| Apollo | Opengrep, depth 8 | 6.1 s | not run | 331 | 619 | 29 / 92 | not measured |
| Apollo | Joern | 33 s | 28 s | 2,122 | 2,019 (910) | 30 / 92 | 17 |
| Apollo | Joern, depth 8 | 45 s | not run | 2,782 | 3,208 | 42 / 92 | not measured |

Totals at depth 4 on the configurations as first set up:

| Engine | Precision | Recall |
| --- | --- | --- |
| Opengrep | 56 / 60 real (93 %) | 35 / 204 units (17 %) |
| Joern | 26 / 60 real (43 %) | 38 / 204 units (19 %) |

Where the time goes in Joern: the frontend takes 4 to 21 seconds. Loading the CPG with its overlays takes 4 to 23 seconds. The flow query takes 2 to 6 seconds (17 at depth 8). The rest is JVM start-up.

### Recall by kind of flow

| Repository | Units: value / selector / no framework crossing | Opengrep | Joern |
| --- | --- | --- | --- |
| Flagsmith | 37 / 23 / 27 | 6 / 0 / 3 | 6 / 0 / 3 |
| Grist | 26 / 26 / 24 | 0 / 0 / 0 (depth 2 with tsconfig: 1 / 2 / 2) | 0 / 2 / 2 |
| Apollo | 81 / 11 / 64 | 28 / 1 / 26 | 26 / 4 / 29 |

Apollo by category: db 22 (Opengrep) and 25 (Joern) of 48, http 0 and 0 of 11, log 7 and 5 of 33.

### Why flows were missed

- **TypeScript imports (Opengrep).** Grist imports first-party modules as bare `app/...` paths. The view held Grist's `tsconfig.json` and `app/tsconfig.json`, which get their `paths` mapping through `extends` from `buildtools/tsconfig-base.json`, and that file was not in the view. As found, Opengrep resolved no cross-file call: 0 db flows and 0 of 52 units. Opengrep reads `paths` from `tsconfig.json` or `tsconfig.build.json`; whether it follows `extends` was not tested.
  - With a flattened `tsconfig.json` (`baseUrl: "."`, `paths: {"*": ["*"]}`), depth 4 ran out the 20 minutes at 8.9 GiB.
  - Depth 2 finished in 38 seconds with 123 db flows and 3 units.
  - Depth 3 was killed for memory at the 10 GiB limit after 517 seconds.
- **Selector flows (both engines).** When the request only picks the row, for example `user = manager.findOne(User, {where: {id: userId}})` and then `manager.save(user)`, the saved object is not tainted. The lookup returns fresh data, and neither catalog has a rule saying that a lookup keyed by request data taints its result. Selector recall:
  - Flagsmith: 0 of 23 for both engines.
  - Grist: 0 of 26 for Opengrep, 2 for Joern.
  - Apollo: 1 of 11 for Opengrep, 4 for Joern.
- **Framework dispatch (both engines).** Neither engine follows the following:
  - DRF's generic views. The engines do not analyse DRF itself, so a flow from `request.data` into `serializer.save()` exists only where the catalog marks the `perform_*` serializer parameter as a source.
  - Spring `ApplicationEventPublisher` events, `@ApolloAuditLog` aspects and executor tasks.
  - Grist handlers passed as values to wrappers such as `withDocTriggersLock(removeWebhook)`.

  All 11 Apollo HTTP units go through one of three things:
  - Spring events (3 units);
  - the audit aspect (4 units);
  - the `RetryableRestTemplate` wrapper (4 units). Here the engines stop at the wrapper call, such as `restTemplate.put` in `AdminServiceAPI`, one level above the traced `exchange`. For `ServerConfigController.deleteConfigDBConfig`, both engines reach an HTTP call from the handler.
- **Depth.** Apollo's portal-to-admin-service calls are 5 or more calls deep.
  - At depth 8, Joern went from 30 to 42 units.
  - Opengrep stayed at 29. Its "interfile search stopped at depth 4" warnings went away, so its Apollo misses are not a depth limit.
- **`.get()` on request data (Opengrep).** In a toy, Opengrep did not carry taint from `request.data` through `.get("name")`. Flagsmith's views use `request.data.get(`, `request.query_params.get(` and similar 73 times. The effect on the Flagsmith numbers was not isolated.
- **Python keyword arguments (Joern).** In a two-file toy, `create_widget(project_id=project_pk, name=name)` reached the sink. A positional call that came after it in the same function, `create_widget_pos(project_pk, name)`, did not. With the two calls swapped, both reached it. Opengrep found both in either order.
- **Build-time code (both engines).** Apollo's OpenAPI controllers implement interfaces that openapi-generator writes at build time. The request annotations live only in that generated code, so neither engine sees those parameters as sources.

### What the false flows were

- **Joern, Flagsmith (17 of 20 false).** Calls were resolved by method name across unrelated classes. For example, `FeatureHealthProviderViewSet.get_object` was taken as `SegmentViewSet`'s `get_object`, and `serializer.save` was resolved to an unrelated serializer's `save`. Taint then moved through `self` and `self.kwargs` from one view into another.
- **Joern, Grist (14 of 20 false).** In TypeScript, a builtin was taken for a first-party method: `String.prototype.replace` for `HostedStorageManager.replace`, and `res.set` for `MapWithTTL.set`. Taint also moved through `this` from each handler registered in `_addEndpoints` into the next. One flow starts at `GET /api/workspaces/:wid` and passes through 12 sibling handlers to reach `manager.remove` in `deleteWorkspace`.
- **Opengrep (4 of 60 false).**
  - Three were in Apollo's config import code. A request value that only chooses a branch, or picks a constant file name suffix, was counted as reaching a log call. For example, `conflictAction` only becomes a boolean skip flag.
  - One was in Grist: taint moved through the shared `req` object into a logger's metadata.
- **Joern, Apollo (3 false).**
  - An `appId` that is only used in a `startsWith` guard.
  - A shared static logger field.
  - `localCache.put(cacheKey, result)` merging its two arguments.

## Base versus head

| Pull request | What changed in data flow terms | Opengrep | Joern |
| --- | --- | --- | --- |
| Flagsmith #8813 | The new view reads `request.user`, which is not a source. It calls the HubSpot SDK, which is not in the catalog. No change was expected under the catalog. | 0 added, 0 removed, 0 rerouted. Correct. | 0, 0, 0. Correct. |
| Grist #1775 | `POST /api/users/:userId/disable` and `/enable` read `req.params.userId`, which reaches `UsersManager.updateUser`. Inside a transaction callback, that value picks the row for `manager.findOne` and then `manager.save`. That is one added selector flow per endpoint. | 0, 0, 0 as found, and 0, 0, 0 with a flattened tsconfig at depth 2. Missed. | 0 added, 0 removed, and 41 rerouted: 40 in the pull request's files, 1 elsewhere. All 40 are existing false flows into `updateApiKeyWithRetry`'s `manager.save` whose path now also runs through the two new handlers. Missed the real change and reported 40 false ones. |
| Apollo #5665 | `batchCreateItems`, `batchUpdateItems` and `batchDeleteItems` go through `ServerItemOpenApiService` and `ItemService.updateItems` to the admin service over HTTP. Their parameters have no annotations in source (see build-time code above), so no change was expected under the catalog. A reviewer would want three added flows. | 0, 0, 0. | 0 added, 0 removed, and 33 rerouted, all in files the pull request did not touch. |

- **Joern is not deterministic.** Running Joern twice on the same Apollo base gave 2,019 and 2,018 flows, and 35 rerouted pairs between the two runs. Joern on Grist head gave 642 flows, and 633 on the same tree with only the tsconfig file changed. A diff of Joern paths therefore reports change where there is none, unless paths are made canonical first, for example by keeping the shortest path per pair.
- **Opengrep was stable.** Base and head gave identical keys in all three repositories, including Grist with tsconfig at depth 2 (241 keys on each side).

## Cross-file check in other languages

Each toy has two files: a handler reads a request value in one file and calls a function in another file, which builds SQL from it and executes it. A second function executes a constant (`SELECT 1`) as a negative control. Each engine had a one-line source rule and a one-line sink rule.

| Language | Opengrep | Joern |
| --- | --- | --- |
| Go (net/http, `database/sql`) | Yes: `main.go:10` to `store/store.go:9`, control not reported | Yes: same flow, control not reported (`gosrc2cpg`) |
| Ruby (Rails-style controller and service class) | Yes: `widgets_controller.rb:3` to `widget_store.rb:4` | No. `rubysrc2cpg` does not follow `WidgetStore.save(name)` into the class method, even with both in the same file; a flow inside one method is found. Its lowered code (`(<tmp-2> = ActiveRecord::Base.connection).execute`) also needed a different sink regex. |
| Rust | Yes: `main.rs:14` to `store.rs:13` | Yes, but only after adding `cargo` and `rustc` (1.86.0) to the image, because `rust2cpg` runs `cargo metadata`. A crate with dependencies would also need their sources available offline. Not measured. |

## Licence notes

- **Joern.** Apache-2.0, which allows use on customers' private code and in a hosted service. The release zip carries no licence file and bundles 94 third-party jars, which were not audited.
- **Opengrep.** LGPL-2.1. It would run as a separate, unmodified executable with no linking, which is ordinary LGPL use. Shipping the binary in an image that customers receive brings the usual obligations: the licence text, the source or an offer of it, and letting the binary be replaced. This needs a check by whoever owns licence review before P10-7 ships the binary.
- **Rules.** All Opengrep rules were written for this spike from the shared catalog. Semgrep registry rules were not used, since they may not be redistributed.
- **Repositories.** The three repositories are BSD-3-Clause, Apache-2.0 and Apache-2.0. Their code was only read and analysed in scratch space, and none of it is in this report beyond short identifiers.

## Maintenance cost

- **Catalog.** The catalog is small: 4 to 10 sink regexes and 5 to 29 source lines per framework. The misses, though, show where the work is: lookup-keyed taint for selector flows, framework dispatch (DRF generic views, Spring events and aspects), and wrapper functions.
  - In Opengrep, each of those is a `pattern-propagators` or extra source entry in YAML, written once per framework.
  - In Joern, each is Scala against the code property graph. It depends on how each frontend lowers code (pysrc2cpg rewrites decorators as assignments, rubysrc2cpg wraps receivers in temporaries), so the same intent needs a different query per language.
- **Cost of running.**
  - Opengrep: one 361 MB image, a static binary, and 0.3 to 0.6 GiB per run in these tests. The exception is resolved TypeScript imports at depth 3 or more, where it reached the 10 GiB limit at depth 3 and 8.9 GiB at depth 4.
  - Joern: a 4.8 GB image (5.5 GB with Rust), a JVM, and about 2 GiB per run. Before the query, every run pays for JVM start-up and script compilation (about 10 seconds) plus 4 to 23 seconds of CPG loading.

## Recommendation for P10-6

**Pick Opengrep. Re-scope P10-7 before building it.** The reasons:

- Its flows were real 93 % of the time, against 43 % for Joern. Joern was only comparable on Java.
- Its diff had no noise. Joern's paths change between identical runs.
- It uses about a sixth of the memory, and its image is about a thirteenth the size. It ran faster than Joern on Grist and Apollo, and slower on Flagsmith.
- It crossed files in Go, Ruby and Rust with no extra setup.

Joern has no advantage here that would justify running a second engine. Its one edge, more reach at depth 8 on Java, is not worth its false flows on Python and TypeScript.

Neither engine, as measured, would have shown a reviewer anything useful on these three pull requests. The one real change was missed by both. Before P10-7 is sized, its spec should:

1. **Report found flows, never absence.** Each edge says "found by Opengrep taint, depth N", and the lens never says "no new data flows".
2. **Generate a flattened tsconfig in the sandbox copy** for TypeScript repositories (one `tsconfig.json` with the `paths` mapping inlined), and set depth by language. TypeScript runs at depth 2 under a hard time and memory cap; Python and Java run at depth 4. With resolved imports, depth 3 ran out of 10 GiB and depth 4 ran out of 20 minutes.
3. **Add per-framework propagators before claiming selector flows**, for ORM lookups keyed by request data and for DRF generic views, Spring events and Express wrappers. Then re-measure recall on the 30 entry points here, with the ground truth already in hand.
4. **Key the diff on (source symbol, sink symbol)** and treat the function path as detail. Name anonymous handlers by route.

Measure again on a few pull requests that add flows the catalog covers. The three here had one such change between them, which is too few to judge the lens. Until the propagators exist, P10-7 stays XL, and most of its work is the catalog, not the engine.

## Not measured

- Precision of the depth 2, 3 and 8 configurations, and of either engine on head.
- Head runs at depth 3 and 8.
- A Joern run with the flattened tsconfig beyond flow counts, and whether Joern reads tsconfig `paths` at all.
- Repositories larger than 81k lines.
- Rust crates with dependencies.
- Opengrep's behaviour on generated sources.
- Python FastAPI.
