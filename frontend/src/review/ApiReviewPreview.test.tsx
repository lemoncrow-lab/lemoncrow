import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ApiReviewPreview from "./ApiReviewPreview";
import type { ReviewSurfaceInfo, ReviewSurfaceRunResult } from "./types";

const surface: ReviewSurfaceInfo = {
  id: "api/status.bru",
  provider: "bruno",
  kind: "api.request",
  title: "Status",
  locator: "GET http://localhost:8000/status",
  runtime: "api-runtime",
  affected_paths: ["api/status.bru", "src/status.py"],
  capabilities: ["source", "execute", "results", "compare"],
  metadata: { method: "GET", url: "http://localhost:8000/status" },
};

function result(side: "old" | "new", statusCode: number, body: string): ReviewSurfaceRunResult {
  return {
    surface_id: surface.id,
    provider: surface.provider,
    revision_id: "rrv-4",
    side,
    status: "passed",
    summary: `HTTP ${statusCode}`,
    runner: "service",
    runtime: surface.runtime,
    exit_code: 0,
    duration_ms: side === "old" ? 8 : 9,
    output: "",
    data: { status_code: statusCode, body },
  };
}

describe("ApiReviewPreview", () => {
  it("shows before/after behavior as two sides of the exact Snapshot", () => {
    render(
      <ApiReviewPreview
        surface={surface}
        mode="compare"
        run={{ old: result("old", 200, '{"state":"old"}'), new: result("new", 201, '{"state":"new"}') }}
        onModeChange={vi.fn()}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText("Before")).toBeTruthy();
    expect(screen.getByText("After")).toBeTruthy();
    expect(screen.getByText("Snapshot rrv-4 · old")).toBeTruthy();
    expect(screen.getByText("Snapshot rrv-4 · new")).toBeTruthy();
    expect(screen.getByText(/"state": "old"/)).toBeTruthy();
    expect(screen.getByText(/"state": "new"/)).toBeTruthy();
  });
});
