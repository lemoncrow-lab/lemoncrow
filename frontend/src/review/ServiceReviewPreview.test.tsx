import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ServiceReviewPreview from "./ServiceReviewPreview";
import type { ReviewSurfaceInfo, ReviewSurfaceRunResult } from "./types";

const surface: ReviewSurfaceInfo = {
  id: "app-stack:api",
  provider: "service",
  kind: "service",
  title: "App stack",
  locator: "docker-compose.yml",
  runtime: "compose-runtime",
  affected_paths: ["docker-compose.yml"],
  capabilities: ["source", "execute", "results", "compare"],
  metadata: { surface_id: "app-stack", service: "api", runner: "docker-compose" },
};

function result(side: "old" | "new", overrides: Partial<ReviewSurfaceRunResult> = {}): ReviewSurfaceRunResult {
  return {
    surface_id: surface.id,
    provider: surface.provider,
    revision_id: "rrv-9",
    side,
    status: "passed",
    summary: `${side} stack healthy`,
    runner: "docker-compose",
    runtime: surface.runtime,
    exit_code: 0,
    duration_ms: side === "old" ? 1200 : 1350,
    output: "",
    data: {},
    ...overrides,
  };
}

function pane(label: string): HTMLElement {
  const node = screen.getByText(label).parentElement?.parentElement;
  if (!node) throw new Error(`no ${label} pane`);
  return node;
}

describe("ServiceReviewPreview", () => {
  it("renders one Result pane in preview mode with status, duration, services and output", async () => {
    const onRun = vi.fn();
    render(
      <ServiceReviewPreview
        surface={surface}
        mode="preview"
        run={{ new: result("new", { data: { services: ["api", "db"] }, output: "api | listening on :8000" }) }}
        onRun={onRun}
      />,
    );

    expect(screen.getByText("api")).toBeTruthy();
    expect(screen.getByText("runtime compose-runtime · docker-compose")).toBeTruthy();
    const resultPane = pane("Result");
    expect(within(resultPane).getByText("passed")).toBeTruthy();
    expect(within(resultPane).getByText("1350 ms")).toBeTruthy();
    expect(within(resultPane).getByText("new stack healthy")).toBeTruthy();
    expect(within(resultPane).getByText("api · db")).toBeTruthy();
    expect(within(resultPane).getByText("api | listening on :8000")).toBeTruthy();
    expect(screen.queryByText("Before")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Run check" }));
    expect(onRun).toHaveBeenCalledWith(false);
  });

  it("pairs Before and After in compare mode and marks an unrun side", async () => {
    const onRun = vi.fn();
    render(
      <ServiceReviewPreview
        surface={surface}
        mode="compare"
        run={{ new: result("new", { status: "failed", summary: "api exited 1", exit_code: 1 }) }}
        onRun={onRun}
      />,
    );

    const before = screen.getByText("Not run yet");
    const after = pane("After");
    // Before stays in the first column even though only the After side ran.
    expect(before.parentElement?.firstElementChild).toBe(before);
    expect(before.parentElement?.lastElementChild).toBe(after);
    expect(within(after).getByText("failed").className).toContain("text-rose-300");
    expect(within(after).getByText("api exited 1")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Run compare" }));
    expect(onRun).toHaveBeenCalledWith(true);
  });

  it("shows a run failure and disables the run while one is in flight", () => {
    render(
      <ServiceReviewPreview
        surface={surface}
        mode="preview"
        run={{ error: "Runtime compose-runtime is not pinned to Snapshot rrv-9" }}
        running
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText("Runtime compose-runtime is not pinned to Snapshot rrv-9")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Running…" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Not run yet")).toBeTruthy();
  });

  it("presents an unavailable hosted runner as an environment limitation, not a failed check", () => {
    render(
      <ServiceReviewPreview
        surface={surface}
        mode="preview"
        run={{ error: "review runtime 'app-stack' uses unavailable runner 'docker-compose'" }}
        onRun={vi.fn()}
      />,
    );

    const message = screen.getByText("This check cannot run in this review environment. Its definition remains reviewable.");
    expect(message.className).toContain("text-amber-300");
    expect(screen.queryByText(/uses unavailable runner/)).toBeNull();
    expect(screen.queryByText("Not run yet")).toBeNull();
    expect((screen.getByRole("button", { name: "Run check" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps service metadata reviewable without a Snapshot-pinned runtime", () => {
    render(
      <ServiceReviewPreview
        surface={{ ...surface, runtime: "", metadata: {}, title: "" }}
        mode="preview"
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText("app-stack:api")).toBeTruthy();
    expect(screen.getByText("no runtime bound")).toBeTruthy();
    expect(screen.getByText(/Execution needs a Snapshot-pinned runtime/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
