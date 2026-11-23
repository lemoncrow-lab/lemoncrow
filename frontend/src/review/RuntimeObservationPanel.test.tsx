import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import RuntimeObservationPanel from "./RuntimeObservationPanel";
import type { RenderResult, RuntimeObservationProjection, SideEffectResult } from "./runtimeObservationApi";

const api = vi.hoisted(() => ({
  fetchRuntimeObservation: vi.fn(),
  postRuntimeObservationRun: vi.fn(),
  fetchRuntimeArtifact: vi.fn(),
}));

vi.mock("./runtimeObservationApi", () => api);

function sideEffects(options: Partial<SideEffectResult> = {}): SideEffectResult {
  const observation = { tier: "strace", truncated: false, query_count: 1, counts: {} };
  return {
    kind: "diff",
    recipe: { id: "unit", title: "Unit tests", digest: "recipe:sha256:x" },
    executor_class: "author_machine",
    environment: { base: "envfp:sha256:1", head: "envfp:sha256:1" },
    sides: {
      base: { status: "PASS", reason: "", evidence_id: "e1", evidence_ref: "check:test:runtime-observation:a", environment: "envfp:sha256:1", observation },
      head: { status: "PASS", reason: "", evidence_id: "e2", evidence_ref: "check:test:runtime-observation:b", environment: "envfp:sha256:1", observation },
    },
    diff: {
      comparable: true,
      reasons: [],
      differences: [],
      unchanged: 2,
      query_count: { base: 1, head: 5 },
      changes: [
        {
          category: "network",
          identity: "connect 203.0.113.7:443",
          change: "added",
          base: null,
          head: { category: "network", identity: "connect 203.0.113.7:443", detail: "", count: 1, outcome: "blocked", sources: ["connect"] },
        },
        {
          category: "db_query",
          identity: "SELECT ?",
          change: "changed",
          base: { category: "db_query", identity: "SELECT ?", detail: "", count: 1, outcome: "", sources: [] },
          head: { category: "db_query", identity: "SELECT ?", detail: "", count: 2, outcome: "", sources: [] },
        },
      ],
    },
    evidence_id: "e3",
    observed_at: "2026-10-04T00:00:00Z",
    ...options,
  };
}

function renderResult(options: Partial<RenderResult> = {}): RenderResult {
  return {
    kind: "render_diff",
    entry: "visual.matrix",
    executor_class: "author_machine",
    selection: { routes: ["/", "/about.html"], mapping: "unknown", note: "The component->route mapping is unknown for src/widget.tsx; the configured routes rendered (/, /about.html)", unmapped: ["src/widget.tsx"] },
    plan: { cells: [{ id: "/@desktop/light" }, { id: "/@desktop/dark" }], dropped: ["/@mobile/light"], limited_by_depth: false, limited_by_budget: true, estimate_seconds: 16 },
    browser: "Chrome 153",
    comparable: true,
    differences: [],
    reasons: ["Limited by budget: 1 cell(s) not rendered"],
    pages: [
      {
        cell: { id: "/@desktop/light", route: "/", theme: "light", flow: "", viewport: { id: "desktop", width: 1280, height: 800 } },
        changes: {
          a11y: { changes: [{ kind: "label" }] },
          screenshot: { base: { digest: "px:1", ref: "run-artifact:r:" + "a".repeat(64) }, head: { digest: "px:2", ref: "run-artifact:r:" + "b".repeat(64) } },
        },
      },
    ],
    unchanged: 1,
    evidence_id: "e4",
    observed_at: "2026-10-04T00:00:00Z",
    ...options,
  };
}

function projection(options: Partial<RuntimeObservationProjection> = {}): RuntimeObservationProjection {
  return {
    revision_id: "rev-1",
    historical: false,
    run_plan_digest: "plan:1",
    entries: [
      { id: "side_effects.runtime", status: "run", reason: "requested", sides: "base+head", executor: "local", required: false, estimated_minutes: 8 },
      { id: "visual.dom_a11y", status: "skipped", reason: "Auto escalates only on a risk signal", sides: "base+head", executor: "local", required: false, estimated_minutes: 8 },
      { id: "visual.matrix", status: "not_applicable", reason: "no web app", sides: "base+head", executor: "local", required: false, estimated_minutes: 40 },
    ],
    side_effects: [sideEffects()],
    render: [renderResult()],
    limitations: ["Absence is never a verified negative."],
    ...options,
  };
}

describe("RuntimeObservationPanel", () => {
  beforeEach(() => {
    api.fetchRuntimeObservation.mockReset();
    api.postRuntimeObservationRun.mockReset();
    api.fetchRuntimeArtifact.mockReset();
  });

  it("shows only side-effect differences, with blocked outcomes and the query-count change", async () => {
    api.fetchRuntimeObservation.mockResolvedValue(projection());
    render(<RuntimeObservationPanel reviewId="review-1" revisionId="rev-1" />);
    const card = await screen.findByTestId("side-effects-unit");
    const rows = within(card).getAllByTestId("side-effect-change");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Network");
    expect(rows[0]).toHaveTextContent("connect 203.0.113.7:443");
    expect(rows[0]).toHaveTextContent("added · blocked");
    expect(rows[1]).toHaveTextContent("1 → 2");
    expect(card).toHaveTextContent("1 → 5 queries");
    expect(card).toHaveTextContent("syscall tracer");
    expect(screen.getByTestId("observation-limitations")).toHaveTextContent("never a verified negative");
  });

  it("says not comparable instead of claiming a difference", async () => {
    api.fetchRuntimeObservation.mockResolvedValue(
      projection({
        side_effects: [sideEffects({ diff: { comparable: false, reasons: ["environment fingerprints differ"], differences: ["toolchain"], changes: [], unchanged: 0 } })],
        render: [renderResult({ comparable: false, differences: ["toolchain"], pages: [] })],
      }),
    );
    render(<RuntimeObservationPanel reviewId="review-1" />);
    expect(await screen.findByTestId("side-effects-not-comparable")).toHaveTextContent("Not comparable (toolchain)");
    expect(screen.queryAllByTestId("side-effect-change")).toHaveLength(0);
    expect(screen.getByTestId("render-not-comparable")).toHaveTextContent("never reported as a regression");
    expect(screen.queryAllByTestId("render-page")).toHaveLength(0);
  });

  it("shows the render selection note, Limited by budget, and the changed aspects per page", async () => {
    api.fetchRuntimeObservation.mockResolvedValue(projection());
    api.fetchRuntimeArtifact.mockResolvedValue(new Blob(["png"], { type: "image/png" }));
    const createObjectURL = vi.fn(() => "blob:capture");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    render(<RuntimeObservationPanel reviewId="review-1" revisionId="rev-1" />);
    const card = await screen.findByTestId("render-visual.matrix");
    expect(card).toHaveTextContent("Limited by budget: 1 cell(s) not rendered");
    expect(within(card).getByTestId("render-selection")).toHaveTextContent("mapping is unknown");
    const page = within(card).getByTestId("render-page");
    expect(page).toHaveTextContent("/@desktop/light");
    expect(page).toHaveTextContent("Accessibility · Screenshot");
    await userEvent.click(within(page).getByRole("button", { name: "head" }));
    await waitFor(() => expect(api.fetchRuntimeArtifact).toHaveBeenCalledWith("review-1", "run-artifact:r:" + "b".repeat(64), "rev-1"));
    expect(await screen.findByAltText("Masked render capture")).toHaveAttribute("src", "blob:capture");
  });

  it("runs a planned entry, picks an unplanned one, and offers nothing for an inapplicable entry", async () => {
    api.fetchRuntimeObservation.mockResolvedValue(projection());
    api.postRuntimeObservationRun.mockResolvedValue({ entry_id: "x", revision_id: "rev-1", executions: 2, reused: 0, notes: [], limited_by_budget: true, executor_class: "author_machine", run_on: "local" });
    render(<RuntimeObservationPanel reviewId="review-1" revisionId="rev-1" />);
    const planned = await screen.findByTestId("observation-entry-side_effects.runtime");
    await userEvent.click(within(planned).getByRole("button", { name: "Run" }));
    await waitFor(() => expect(api.postRuntimeObservationRun).toHaveBeenCalledWith("review-1", "rev-1", "side_effects.runtime", false));
    expect(await screen.findByRole("status")).toHaveTextContent("Runtime side effects: 2 run, 0 reused · Limited by budget");
    await userEvent.click(within(screen.getByTestId("observation-entry-visual.dom_a11y")).getByRole("button", { name: "Add & run" }));
    await waitFor(() => expect(api.postRuntimeObservationRun).toHaveBeenCalledWith("review-1", "rev-1", "visual.dom_a11y", true));
    expect(within(screen.getByTestId("observation-entry-visual.matrix")).queryByRole("button")).toBeNull();
  });

  it("is read-only on a historical revision", async () => {
    api.fetchRuntimeObservation.mockResolvedValue(projection({ historical: true }));
    render(<RuntimeObservationPanel reviewId="review-1" revisionId="rev-0" />);
    const planned = await screen.findByTestId("observation-entry-side_effects.runtime");
    expect(within(planned).queryByRole("button")).toBeNull();
  });
});
