import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const request = vi.fn();
vi.mock("../reviewApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("../reviewApi")>()), requestBrowserJson: (...args: unknown[]) => request(...args) }));

import type { ReviewOverview, ReviewTarget } from "../types";
import { noteRouterLocation, resetBootLocationForTest } from "../reviewBootLocation";
import { DEFAULT_SETTINGS } from "../settings/settingsModel";
import { resetUserSettingsForTest } from "../settings/userSettings";
import { ReviewOverviewScreen } from "./ReviewOverviewScreen";

function part(id: string, category: "decision" | "quick_check" = "decision"): ReviewTarget {
  return {
    target_id: id, unit_key: id, kind: "symbol", path: "app/auth.py", label: id, symbol: id, start_line: 6, end_line: 7, hunk_ordinals: [0], spans: [],
    state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "", attention_rank: 1, attention_level: "high", reasons: [], additions: 2, deletions: 1,
    fingerprint_method: "ast", verification: { pass: 0, fail: 0, unknown: 0 }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category, rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "review_first", outstanding: true, position: 0, reasons: [] },
  } as unknown as ReviewTarget;
}

const overview = { title: "Change can_view_order", session: { title: "Change can_view_order" }, revision: { revision_number: 1, dirty: true, provenance_host: "claude_code" }, journal: [] } as unknown as ReviewOverview;

function Where() {
  const location = useLocation();
  noteRouterLocation({ ...location });
  return <output data-testid="where">{location.pathname}</output>;
}

function open(targets: ReviewTarget[], from: string[] = ["/r/shop"]) {
  return render(
    <MemoryRouter initialEntries={from} initialIndex={from.length - 1}>
      <ReviewOverviewScreen reviewRef="shop" overview={overview} targets={targets} projection={null} ready progress={null} aiAvailable={false} />
      <Where />
    </MemoryRouter>,
  );
}

const served = (settings: Record<string, unknown>) => request.mockResolvedValue({ settings: { ...DEFAULT_SETTINGS, ...settings }, set: Object.keys(settings) });

beforeEach(() => {
  window.localStorage.clear();
  resetUserSettingsForTest();
  resetBootLocationForTest();
  request.mockReset();
});
afterEach(cleanup);

describe("Skip the Overview for reviews with 1 decision (SET-2)", () => {
  it("opens the first part when the review has exactly 1 decision", async () => {
    served({});
    open([part("can_view_order"), part("order_total", "quick_check")]);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/r/shop/one/can_view_order"));
  });

  it("stays on the Overview when the switch is off", async () => {
    served({ skip_overview_single_decision: false });
    open([part("can_view_order")]);
    await screen.findByRole("button", { name: /Start/ });
    expect(screen.getByTestId("where").textContent).toBe("/r/shop");
  });

  it("stays on the Overview when the review has 2 decisions", async () => {
    served({});
    open([part("a"), part("b")]);
    await screen.findByRole("button", { name: /Start/ });
    expect(screen.getByTestId("where").textContent).toBe("/r/shop");
  });

  it("shows the Overview when the reviewer comes back to it from inside the review", async () => {
    served({});
    noteRouterLocation({ key: "earlier", pathname: "/r/shop/one/can_view_order", search: "", hash: "" });
    open([part("can_view_order")]);
    await screen.findByRole("button", { name: /Start/ });
    expect(screen.getByTestId("where").textContent).toBe("/r/shop");
  });

  it("opens All files for a change over 30 parts", async () => {
    served({ skip_overview_single_decision: true });
    const many = [part("only_decision"), ...Array.from({ length: 31 }, (_, i) => part(`q${i}`, "quick_check"))];
    open(many);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/r/shop/all"));
  });
});
