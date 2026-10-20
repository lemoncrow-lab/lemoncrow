import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Discussion } from "./ContextSections";
import { deriveReviewReadiness } from "./ReviewReadinessSheet";
import { BelowThresholdList, markBelowThreshold } from "./findingThreshold";
import type { Annotation } from "./types";

function annotation(id: string, overrides: Partial<Annotation> = {}): Annotation {
  return {
    id,
    review_id: "review-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: `body ${id}`,
    title: `title ${id}`,
    created_by: "local",
    created_by_actor: "agent",
    source: "ai_review",
    source_id: "run-1",
    created_at: "",
    updated_at: "",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path: "src/a.py",
    side: "new",
    start_line: 1,
    end_line: 1,
    unit_key: "sym:a",
    symbol: "a",
    origin_symbol: "",
    ...overrides,
  };
}

const ANNOTATIONS: Annotation[] = [
  annotation("human", { source: "human", created_by_actor: "human" }),
  annotation("p2-finding"),
  annotation("suggestion"),
  annotation("reply", { parent_id: "p2-finding", source: "human" }),
  annotation("resolved-finding", { state: "resolved" }),
];

function openRoots(items: Annotation[]): number {
  return items.filter((item) => !item.parent_id && item.state !== "resolved" && item.state !== "obsolete").length;
}

function readiness(items: Annotation[]) {
  return deriveReviewReadiness({
    progress: { target_count: 2, reviewed: 1, changed_since_review: 0, needs_changes: 0, unreviewed: 1, unknown: 0, mechanical: 0 },
    openComments: openRoots(items),
    verification: { pass: 0, fail: 0, not_run: 0, unknown: 0 },
    outcome: null,
  });
}

describe("finding threshold surfacing", () => {
  it("marks only hidden root AI findings and never removes, reorders or recounts anything", () => {
    // Medium+ hides the suggestion; Suggestions hides nothing; High+ also hides the P2 finding.
    const medium = markBelowThreshold(ANNOTATIONS, ["suggestion", "resolved-finding"]);
    const suggestions = markBelowThreshold(ANNOTATIONS, []);
    const high = markBelowThreshold(ANNOTATIONS, ["p2-finding", "suggestion", "resolved-finding", "human", "reply"]);

    expect(suggestions).toBe(ANNOTATIONS);
    expect(high.map((item) => item.id)).toEqual(ANNOTATIONS.map((item) => item.id));
    expect(high.filter((item) => item.below_threshold).map((item) => item.id)).toEqual([
      "p2-finding",
      "suggestion",
      "resolved-finding",
    ]);
    expect(markBelowThreshold(high, ["p2-finding", "suggestion", "resolved-finding"])).toBe(high);
    expect(markBelowThreshold(high, []).some((item) => item.below_threshold)).toBe(false);

    // The threshold changes surfacing only: what readiness reads is identical at every threshold.
    expect(openRoots(medium)).toBe(openRoots(ANNOTATIONS));
    expect(openRoots(high)).toBe(openRoots(ANNOTATIONS));
    expect(readiness(medium)).toEqual(readiness(suggestions));
    expect(readiness(high)).toEqual(readiness(suggestions));
  });

  it("folds below-threshold items behind one collapsed disclosure that expands in place", async () => {
    const items = markBelowThreshold(ANNOTATIONS.filter((item) => !item.parent_id), ["p2-finding", "suggestion"]);
    render(<div><BelowThresholdList items={items} render={(item) => <p key={item.id}>{item.title}</p>} /></div>);

    expect(screen.getByText("title human")).toBeVisible();
    expect(screen.getByText("title resolved-finding")).toBeVisible();
    const disclosure = screen.getByTestId("below-threshold") as HTMLDetailsElement;
    expect(screen.getAllByTestId("below-threshold")).toHaveLength(1);
    expect(screen.getByText("2 below threshold")).toBeVisible();
    expect(disclosure.open).toBe(false);
    expect(screen.getByText("title p2-finding")).not.toBeVisible();

    await userEvent.click(screen.getByText("2 below threshold"));
    expect(disclosure.open).toBe(true);
    expect(screen.getByText("title p2-finding")).toBeVisible();
    expect(screen.getByText("title suggestion")).toBeVisible();
  });

  it("renders no disclosure when nothing is below the threshold", () => {
    render(<div><BelowThresholdList items={ANNOTATIONS} render={(item) => <p key={item.id}>{item.id}</p>} /></div>);
    expect(screen.queryByTestId("below-threshold")).toBeNull();
  });

  it("folds them in the context drawer's file discussion too", () => {
    render(<Discussion annotations={markBelowThreshold(ANNOTATIONS, ["suggestion"])} path="src/a.py" />);
    expect(screen.getByText("title p2-finding")).toBeVisible();
    expect(screen.getByText("1 below threshold")).toBeVisible();
    expect(screen.getByText("title suggestion")).not.toBeVisible();
  });
});
