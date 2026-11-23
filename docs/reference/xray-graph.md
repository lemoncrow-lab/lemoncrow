# X-Ray graph document (contract v1)

The X-Ray screen draws one **graph document** per Review revision. The backend
produces it, and the frontend renders it with LemonCrow's own deterministic
renderer. This file is the contract between them; change both sides together.

The idea is borrowed from PR Lens (components in lanes, labelled arrows,
sequence flows, a walkthrough), but every element here also says **where its
evidence comes from**:

- `recorded`: a before/after run on this revision actually observed it (a call,
  a request or response, a side effect). Drawn solid and animated.
- `static`: LemonCrow's code index or data-path analysis found it in the code.
  Drawn dashed.
- `inferred`: a model proposed it from the diff. Drawn dashed and labelled
  "AI". It is never shown as fact.

## Endpoint

`GET /api/reviews/{review_id}/xray-graph?revision_id=<id>` returns:

```ts
{ state: "ready" | "pending" | "unavailable"; graph?: XRayGraph; reason?: string }
```

- `ready` always carries `graph`. An index-only graph is still `ready`.
- `pending` means the AI enrichment is running, so poll again. When an
  index-only graph already exists, it is returned in `graph`.
- `unavailable` carries a one-sentence `reason`.

`POST /api/reviews/{review_id}/xray-graph/generate` with body
`{ revision_id, ai: boolean }` asks for (re)generation (`revision_id` may be
omitted for the latest revision). It answers with the same body as GET,
`{ state, graph?, reason? }`:

- `ai: false` rebuilds the index-only graph and answers `200` `ready`.
- `ai: true` starts one AI enrichment in the background and answers `202`
  `pending` with the index-only graph; poll GET until it is `ready`. A request
  while that enrichment runs answers `202` `pending` again and starts nothing.
  When an enrichment of exactly this graph, content-access policy and model
  already exists, it answers `200` `ready` with it and makes no model call.
- `ai: true` respects the Review AI budget and configuration. When AI is off,
  over budget, routed to an agent CLI, or the content-access policy lets no file
  of the change through, it answers `409` with `state: "ready"`, the unchanged
  index-only graph and a one-sentence `reason`.

AI runs only on this explicit request, never on its own, because it spends the
Review's AI budget. GET never runs a model. When the last enrichment failed, GET
answers `ready` with the index-only graph and a `reason` that says so.

## Types

```ts
type Delta = "added" | "modified" | "removed" | "unchanged";
type Evidence = "recorded" | "static" | "inferred";
type Side = "base" | "head";

interface Anchor { path: string; start_line?: number; end_line?: number; side: Side }

interface XRayGraph {
  version: 1;
  review_id: string;
  revision_id: string;
  digest: string;                 // sha256 hex of the canonical JSON without `digest` (see Rules)
  title: string;                  // <= 80 chars, what the change does
  summary: string;                // <= 240 chars
  provenance: {
    producer: "index" | "index+ai";
    model?: string;               // present only when AI contributed
    generated_at: string;         // ISO 8601
    recorded_runs: { ref: string; recipe: string; side: Side; at: string }[];
  };
  lanes: { id: string; label: string; platform?: string }[];       // 1..8, left to right
  nodes: XRayNode[];              // <= 60
  edges: XRayEdge[];              // <= 150
  flows: XRayFlow[];              // <= 6
  views: XRayView[];              // >= 1; views[0] is the overview, scope all
  walkthrough?: { steps: XRayTourStep[] };   // 2..12 steps when present
  degraded: string[];             // short plain notes, <= 5
}

interface XRayNode {
  id: string;
  label: string;                  // component name: "POST /api/plans", "PlansTable", "plans.csv", "Stripe"
  kind: "ui" | "route" | "handler" | "function" | "class" | "module" | "datastore" | "queue"
      | "external" | "job" | "config" | "test";
  lane: string;
  delta: Delta;
  subtitle?: string;              // <= 60, e.g. the file or the framework role
  badges: string[];               // <= 3 short tags, e.g. "+38 / -12", "new"
  anchors: Anchor[];              // required unless kind is "external"; every anchor must exist on its side
  source: Evidence;               // how we know the node exists
}

interface XRayEdge {
  id: string;
  from: string;
  to: string;
  kind: "http" | "call" | "data" | "event" | "dependency" | "render";
  label?: string;                 // <= 40, e.g. "GET /api/plans", "reads rows", "onWrite"
  delta: Delta;
  evidence: Evidence;
  hero?: boolean;                 // at most one per graph: the edge the change is about
  anchors: Anchor[];              // call sites; may be empty for recorded-only edges
  recorded?: { ref: string; count: number };
}

interface XRayFlow {
  id: string;
  title: string;                  // <= 60
  summary?: string;               // <= 140
  participants: { node: string; label?: string }[];   // 2..8, in column order
  steps: XRayFlowStep[];          // 1..24, in order
}

interface XRayFlowStep {
  id: string;                     // unique in the graph: "<flow id>.s<n>", e.g. "f_3ce0d1a24939.s1"
  from: string;                   // participant node ids
  to: string;
  label: string;                  // <= 48
  kind: "sync" | "async" | "return" | "self";
  delta: Delta;
  evidence: Evidence;
  repeat?: number;                // >= 2 when the step repeats
  note?: string;                  // <= 140
  anchors: Anchor[];
  payload?: { request?: XRayPayload; response?: XRayPayload };
  recorded?: { ref: string; count: number };   // when evidence is recorded: the run that observed this step
}

interface XRayPayload {
  type?: string;                  // <= 60, e.g. "PlansResponse"
  shape?: string;                 // <= 2048 bytes, a type sketch
  before?: unknown;               // JSON, <= 4096 bytes serialised, null when absent on that side
  after?: unknown;
  status?: { before?: number; after?: number };   // HTTP status when relevant
  evidence: "recorded" | "inferred";
  evidence_ref?: string;          // required when recorded: the ReviewEvidence/behavior target it came from
  behavior_key?: string;          // the behavior target a reviewer can judge
  redacted: boolean;              // true when secrets or personal data were masked
}

interface XRayView {
  id: string;
  title: string;                  // <= 60
  lens: "architecture" | "flow";
  summary?: string;               // <= 140
  scope: { kind: "all" } | { kind: "nodes"; nodes: string[] } | { kind: "flow"; flow: string };
  children: XRayView[];           // drill-downs, total views <= 32
}

interface XRayTourStep {
  id: string;
  heading: string;                // <= 48, sentence case: the thing and what happened to it
  body: string;                   // <= 140, what it means for behaviour
  stage: { kind: "view"; view: string } | { kind: "flow"; flow: string };
  focus: { kind: "all" } | { kind: "selection"; nodes?: string[]; edges?: string[]; messages?: string[] };
}
```

## Rules the producer enforces

1. Referential integrity: lanes, nodes, edges, participants, steps, views and
   tour steps only name ids that exist. Tour `messages` belong to the flow the
   step stages.
2. Grounding: every node except `external` has at least one anchor, and every
   anchor resolves to a file (and line range) on its side of the revision. Any
   AI-proposed element that fails this check is dropped and counted in
   `degraded`, never kept.
3. Evidence: `recorded` requires a run reference from `provenance.recorded_runs`.
   A recorded edge or flow step names that run in `recorded` (`ref` is the
   newest side's record of the request that observed it, `count` how many
   sides ran), so a step shows only its own request's runs, not every run of
   the revision. A recorded payload requires `evidence_ref`. AI may add labels,
   names, grouping, flows and the tour, but never marks anything `recorded`.
   Tour `messages` are whole step ids.
4. Safety: payload samples pass the existing secret/PII redaction before they are
   stored. Strings over 256 characters are cut, with `redacted` set when masking
   applied.
5. Determinism: for the same revision and inputs, the index-only graph is
   byte-identical (stable ids derived from paths and symbols, sorted arrays).
   `digest` is the sha256 hex of
   `json.dumps(graph_without_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
6. Caps as listed above. Past a cap, fold into a `module` node labelled
   `+N more` and add a `degraded` note.
7. Payload reachability: payloads live only on flow steps (edges carry no
   payload; an arrow's payload panel is built from the steps between the same
   two nodes). Every recorded `api_response` gets a flow (largest body change
   first, within the flow cap): when its endpoint is not in the changed code,
   the endpoint is an `external` node and the flow has two steps, the recorded
   request and the recorded return with the payload. A recorded return's
   `label` names the status and the biggest field change of the recorded
   bodies (`200 · Team monthly 49 → 59, +1 more`, at most 48 characters); its
   `note` keeps the body change percentage. A handler's own return names what
   it returns (`returns plans`), and a response flow shows what the handler
   reads or calls before it returns.
8. Static references: besides imports and calls, two literal references are
   drawn as `static` edges anchored at the literal's line. A request path
   literal in changed UI or script code (`fetch("/api/plans")`,
   `` `${API}/api/plans` ``, a URL constant) is an `http` edge to the route it
   names, labelled with the method and path. A file path literal in a
   function or handler (`"plans.csv"`, `"../data/plans.csv"`, or a module
   constant holding one that the function uses) that names a data or config
   file of the graph is a `data` edge labelled `reads <file>` (or
   `writes <file>`). Comments, test files, route declarations and navigation
   targets (`to=`, `href=`) never count.

## Rendering rules (frontend)

- Delta is shown in colour *and* shape: added = green solid outline with a
  `+` badge, modified = amber with `~`, removed = red dashed, ghosted and struck
  through, unchanged = neutral.
- Evidence is shown in line style: recorded = solid with a travelling pulse in
  step order (off under `prefers-reduced-motion`), static = dashed, inferred =
  dashed plus a small "AI" tag.
- Layout is deterministic: fixed-width lanes, rank = longest path within the
  lane, text measured from a width table (not the font engine), coordinates
  rounded. Adding a node to one lane never moves cards in another lane.
- Before shows `removed` plus `unchanged` plus `modified`; After shows `added`
  plus `unchanged` plus `modified`.
- The screen keeps its place in its address, `/r/<review>/xray?at=<side>~<snapshot>~<node>`:
  the side (`both`, the default, `before` or `after`) and the selected card's id.
  A link from the Map, the Walkthrough or Investigate names a Map node instead;
  the screen opens on the card in that node's file (at its line when it has one).
- A selected component lists the recorded behavior changes on its files, so a
  change whose request lands on no route still shows on the X-Ray.
