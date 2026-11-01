import { describe, expect, it } from "vitest";

import type { Annotation, ReviewArchitectureFlowStep, ReviewMapEdge, ReviewMapGraph, ReviewMapNode, ReviewTarget } from "../types";
import { EXACTNESS_LABEL, buildChangeStory, checkText, storyStage, type ChangeStoryInput } from "./changeStoryModel";

function node(id: string, status: ReviewMapNode["status"], extra: Partial<ReviewMapNode> = {}): ReviewMapNode {
  const path = `src/${id}.ts`;
  return {
    id,
    label: `${id}.ts`,
    kind: "file",
    path,
    line: 0,
    end_line: 0,
    status,
    exactness: status === "context" ? "current_index" : "snapshot",
    target_ids: [],
    important_public_contract: false,
    boundaries: [],
    boundary_basis: "",
    selection: { kind: "review_map_node", review_id: "r", revision_id: "v", node_id: id, path, line: 1, side: "new", exactness: "snapshot" },
    ...extra,
  };
}

function edge(source: string, target: string, exactness: ReviewMapEdge["exactness"], extra: Partial<ReviewMapEdge> = {}): ReviewMapEdge {
  const id = `${source}->${target}`;
  return {
    id,
    source,
    target,
    kind: "imports",
    exactness,
    weight: 1,
    uncertain: false,
    selection: { kind: "review_map_edge", review_id: "r", revision_id: "v", edge_id: id, exactness },
    ...extra,
  };
}

function graph(nodes: ReviewMapNode[], edges: ReviewMapEdge[]): ReviewMapGraph {
  return { level: "architecture", focus: null, nodes, edges, truncated: false };
}

/** api -> service -> store -> util (an added import of an unchanged file); caller uses api; other stands alone. */
function fixture(): ReviewMapGraph {
  return graph(
    [
      node("api", "changed", { target_ids: ["t-api"] }),
      node("service", "changed", { target_ids: ["t-service"] }),
      node("store", "changed", { target_ids: ["t-store"] }),
      node("util", "context"),
      node("other", "new"),
      node("caller", "context"),
    ],
    [
      edge("api", "service", "snapshot"),
      edge("service", "store", "current_index", { evidence: [{ kind: "import", path: "src/service.ts", line: 12, uncertainty: "" }] }),
      edge("store", "util", "inferred", { delta_status: "added" }),
      edge("caller", "api", "snapshot"),
    ],
  );
}

const ids = (input: ChangeStoryInput) => buildChangeStory(input).steps.map((step) => `${step.nodeId}:${step.role}`);

function annotation(id: string, path: string, extra: Partial<Annotation> = {}): Annotation {
  return {
    id,
    path,
    severity: "p1",
    state: "open",
    anchored: true,
    side: "new",
    start_line: 10,
    end_line: 14,
    title: id,
    body: "",
    ...extra,
  } as unknown as Annotation;
}

function target(id: string, verification: { pass: number; fail: number; unknown: number }): ReviewTarget {
  return { target_id: id, path: "", start_line: 0, end_line: 0, verification } as unknown as ReviewTarget;
}

describe("buildChangeStory", () => {
  it("walks entry point, changed dependencies, then affected callers", () => {
    expect(ids({ graph: fixture() })).toEqual([
      "api:entry",
      "service:changed",
      "store:changed",
      "util:dependency",
      "other:entry",
      "caller:affected",
    ]);
    const story = buildChangeStory({ graph: fixture() });
    expect(story.ordering).toBe("dependencies");
    expect(story.orderNote).toContain("This is not a runtime order.");
  });

  it("does not start the story at a changed test", () => {
    const withTest = graph(
      [node("spec", "changed", { category: "test" }), node("auth", "changed")],
      [edge("spec", "auth", "snapshot")],
    );
    expect(ids({ graph: withTest })).toEqual(["auth:entry", "spec:entry"]);
  });

  it("follows the architecture flow first and names an observed step as observed", () => {
    const flow: ReviewArchitectureFlowStep[] = [{
      edge_id: "service->store",
      from: "src/service.ts",
      to: "src/store.ts",
      relationship: "imports",
      delta_status: "changed",
      base_weight: 1,
      snapshot_weight: 2,
      grounding: "observed",
      trace: { side: "head", caller_fn: "save", callee_fn: "write", recipe: "unit", evidence_ref: "ev-1", record_id: "rec-1" },
    }];
    const story = buildChangeStory({ graph: fixture(), flow: { applicable: true, steps: flow } });
    expect(story.steps.map((step) => step.nodeId)).toEqual(["service", "store", "util", "api", "other", "caller"]);
    expect(story.ordering).toBe("flow");
    const store = story.steps[1];
    expect(store.via?.exactness).toBe("observed");
    expect(store.via?.animate).toBe(true);
    expect(store.via?.runtime).toBe("Observed on head in unit: save → write");
    expect(store.evidence.receipts).toEqual([{ label: "unit: save → write (head)", ref: "ev-1", recordId: "rec-1" }]);
    expect(story.orderNote).toContain("1 step matched a recorded run");
    // An inferred flow step is never presented as a runtime order.
    const inferred = buildChangeStory({ graph: fixture(), flow: { applicable: true, steps: [{ ...flow[0], grounding: "inferred", trace: null }] } });
    expect(inferred.steps[1].via?.runtime).toBe("Static relationship; no recorded run observed this order.");
    expect(inferred.steps[1].evidence.receipts).toEqual([]);
    expect(inferred.orderNote).toContain("This is not a runtime order.");
  });

  it("labels exactness on every step and animates only Snapshot-exact or observed links", () => {
    const story = buildChangeStory({ graph: fixture() });
    for (const step of story.steps) {
      expect(step.exactnessLabel).toBe(EXACTNESS_LABEL[step.exactness]);
      expect(step.locationLabel).toMatch(/^src\/\w+\.ts.* · (head|base)$/);
      if (step.via) {
        expect(step.via.exactnessLabel).toBe(EXACTNESS_LABEL[step.via.exactness]);
        expect(step.via.animate).toBe(step.via.exactness === "snapshot" || step.via.exactness === "observed");
      }
    }
    const byId = new Map(story.steps.map((step) => [step.nodeId, step]));
    expect(byId.get("service")?.via?.animate).toBe(true);
    expect(byId.get("store")?.via?.exactnessLabel).toBe("from the current index, not this Snapshot");
    expect(byId.get("store")?.via?.animate).toBe(false);
    expect(byId.get("util")?.via?.exactnessLabel).toBe("inferred");
    expect(byId.get("util")?.via?.animate).toBe(false);
    // Current-index context does not open as Snapshot source.
    expect(byId.get("util")?.canOpenSource).toBe(false);
    expect(byId.get("api")?.canOpenSource).toBe(true);
  });

  it("never invents a pass", () => {
    expect(checkText("NOT_RUN").label).toBe("did not run");
    expect(checkText("UNKNOWN").label).toBe("no result recorded");
    expect(checkText("something new").label).toBe("no result recorded");

    const without = buildChangeStory({ graph: fixture() });
    expect(without.targetsKnown).toBe(false);
    expect(without.steps.every((step) => step.evidence.checks === null)).toBe(true);

    const story = buildChangeStory({
      graph: fixture(),
      targets: [
        target("t-api", { pass: 0, fail: 0, unknown: 0 }),
        target("t-service", { pass: 2, fail: 0, unknown: 1 }),
        target("t-store", { pass: 3, fail: 1, unknown: 0 }),
      ],
    });
    const checks = new Map(story.steps.map((step) => [step.nodeId, step.evidence.checks]));
    expect(checks.get("api")?.words).toEqual(["no check recorded"]);
    expect(checks.get("api")?.tone).not.toBe(checkText("PASS").tone);
    expect(checks.get("service")?.words).toEqual(["2 passed", "1 no result recorded"]);
    expect(checks.get("service")?.tone).not.toBe(checkText("PASS").tone);
    expect(checks.get("store")?.words).toEqual(["1 failed", "3 passed"]);
    expect(checks.get("store")?.tone).toBe(checkText("FAIL").tone);
    // A file with no linked target has no check line at all, not a pass.
    expect(checks.get("other")).toBeNull();
  });

  it("attaches only open, anchored P0/P1 findings the path or relationship site proves", () => {
    const story = buildChangeStory({
      graph: fixture(),
      findings: [
        annotation("on-store", "src/store.ts"),
        annotation("at-import", "src/service.ts", { severity: "p0" }),
        annotation("minor", "src/api.ts", { severity: "p2" }),
        annotation("dismissed", "src/api.ts", { finding_disposition: "dismissed" }),
        annotation("resolved", "src/api.ts", { state: "resolved" }),
        annotation("unanchored", "src/api.ts", { anchored: false }),
        annotation("elsewhere", "src/unrelated.ts"),
      ],
    });
    const findings = new Map(story.steps.map((step) => [step.nodeId, step.evidence.findings.map((item) => `${item.id}@${item.on}`)]));
    expect(findings.get("api")).toEqual([]);
    expect(findings.get("service")).toEqual(["at-import@node"]);
    // The service -> store import is recorded at src/service.ts:12, inside the P0 span.
    expect(findings.get("store")).toEqual(["at-import@relationship", "on-store@node"]);
    expect(story.findingsKnown).toBe(true);
    expect(buildChangeStory({ graph: fixture() }).findingsKnown).toBe(false);
  });

  it("keeps a 400+ file Snapshot to a bounded walk and a bounded stage", () => {
    const nodes: ReviewMapNode[] = [];
    const edges: ReviewMapEdge[] = [];
    for (let at = 0; at < 420; at += 1) {
      nodes.push(node(`f${at}`, "changed"));
      if (at > 0) edges.push(edge(`f${at - 1}`, `f${at}`, "snapshot"));
    }
    for (let at = 0; at < 30; at += 1) {
      nodes.push(node(`c${at}`, "context"));
      edges.push(edge(`c${at}`, "f0", "snapshot"));
      edges.push(edge(`c${at}`, "f1", "current_index"));
    }
    const big = graph(nodes, edges);
    const story = buildChangeStory({ graph: big });
    expect(story.changedTotal).toBe(420);
    expect(story.steps.filter((step) => step.role !== "affected")).toHaveLength(20);
    expect(story.omittedChanged).toBe(400);
    expect(story.steps.filter((step) => step.role === "affected")).toHaveLength(6);
    expect(story.omittedAffected).toBe(24);
    expect(story.steps[0].nodeId).toBe("f0");
    expect(story.steps[19].nodeId).toBe("f19");

    const path = storyStage(story, big, 1, "path");
    expect(path.graph.nodes).toHaveLength(26);
    expect(path.hiddenNodes).toBe(450 - 26);
    const near = storyStage(story, big, 1, "neighbors");
    expect(near.graph.nodes.length).toBeLessThanOrEqual(26 + 12);
    expect(near.graph.nodes.length).toBeGreaterThan(26);
    expect(near.graph.truncated).toBe(true);
    expect(near.graph.edges.every((item) => near.graph.nodes.some((n) => n.id === item.source) && near.graph.nodes.some((n) => n.id === item.target))).toBe(true);
    expect(storyStage(story, big, 1, "all").hiddenNodes).toBe(0);
    // Walking everything is the reader's explicit choice.
    expect(buildChangeStory({ graph: big, maxChanged: Number.POSITIVE_INFINITY }).omittedChanged).toBe(0);
  });
});
