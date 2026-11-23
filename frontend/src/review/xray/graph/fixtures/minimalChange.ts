/** Fixture (c): the smallest valid document, one changed function drawn from the code index alone. */
import type { XRayGraph } from "../xrayGraphTypes";

export const MINIMAL_CHANGE: XRayGraph = {
  version: 1,
  review_id: "rev_minimal",
  revision_id: "rv_minimal_1",
  digest: "0d1c2b3a49586776a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f00f1e",
  title: "Greeting trims the name it is given",
  summary: "greeting() strips surrounding spaces before it builds the message.",
  provenance: { producer: "index", generated_at: "2026-11-18T10:02:00Z", recorded_runs: [] },
  lanes: [{ id: "api", label: "Backend", platform: "Python" }],
  nodes: [
    {
      id: "fn_greeting",
      label: "greeting()",
      kind: "function",
      lane: "api",
      delta: "modified",
      subtitle: "backend/app.py",
      badges: ["+1 / -1"],
      anchors: [{ path: "examples/demo/backend/app.py", start_line: 10, end_line: 12, side: "head" }],
      source: "static",
    },
  ],
  edges: [],
  flows: [],
  views: [{ id: "v_overview", title: "Whole change", lens: "architecture", scope: { kind: "all" }, children: [] }],
  degraded: [],
};
