import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CORE_ONLY_PRODUCT_CAPABILITIES,
  createProductCapabilities,
  ProductCapabilitiesProvider,
} from "../../lib/RuntimeCapabilities";
import { TimeRangeProvider } from "../../lib/TimeRangeContext";

const api = vi.hoisted(() => ({ requestBrowserJson: vi.fn() }));
vi.mock("../reviewApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../reviewApi")>()),
  requestBrowserJson: api.requestBrowserJson,
}));

import ReviewAnalytics from "./ReviewAnalytics";
import type { AnalyticsRow, ReviewAnalyticsDrill, ReviewAnalyticsReport } from "./reviewAnalyticsApi";
import { reviewAnalyticsParams, reviewAnalyticsPath } from "./reviewAnalyticsApi";

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

function row(drill: string, label: string, count = 0, extra: Partial<AnalyticsRow> = {}): AnalyticsRow {
  return { key: drill, label, count, reviews: count, drill, ...extra };
}

function lane(name: string) {
  return {
    reviews: row(`escalation.${name}`, name, 1),
    useful_ai_findings: row(`escalation.${name}.useful_ai`, "Useful AI findings"),
    ai_model_calls: row(`escalation.${name}.ai_calls`, "AI review model calls", 0, { value: 0 }),
    useful_per_review: null,
    model_calls_per_useful: null,
  };
}

const SCOPE = {
  org: "",
  repos: [],
  teams: [],
  paths: [],
  since: "2026-09-28T10:00:00.000000Z",
  until: "2026-10-05T10:00:00.000000Z",
};

function report(overrides: Partial<ReviewAnalyticsReport> = {}): ReviewAnalyticsReport {
  return {
    version: 1,
    gating: "never",
    individuals: "not_reported",
    generated_at: "2026-10-05T10:00:00Z",
    scope: SCOPE,
    teams_supported: false,
    bounds: {
      max_reviews: 300,
      candidates: 2,
      analyzed: 2,
      in_scope: 2,
      truncated: false,
      scan_truncated: false,
      escapes_truncated: false,
    },
    notes: [],
    volume: {
      reviews: row("volume.reviews", "Reviews", 2),
      by_status: [row("volume.status.open", "Open", 2)],
      by_author_kind: [],
      by_repository: [row("volume.repo.repo-a", "widgets", 2, { key: "repo-a" })],
    },
    time_to_first_review: {
      measured: row("first_review.measured", "First human review", 1),
      awaiting: row("first_review.awaiting", "Waiting for a first review", 1),
      median_hours: 3.5,
      p90_hours: 3.5,
    },
    merge_time: { merged: row("merge.merged", "Merged", 0), median_hours: null, p90_hours: null },
    findings: {
      current: row("findings.current", "Current findings", 3),
      by_source: [],
      by_category: [],
      by_severity: [],
      by_outcome: [row("findings.outcome.acted_on", "Acted on", 1, { key: "acted_on" })],
      invalidated: [row("findings.invalidated.stale", "Stale", 4, { key: "stale" })],
      basis: "Stale and retracted findings are counted only as invalidated, never as current findings.",
    },
    catch_contribution: {
      useful: row("catch.useful", "Useful findings", 1),
      deterministic: row("catch.deterministic", "Deterministic", 1),
      ai: row("catch.ai", "AI", 0),
      acted_on_rate: 0.33,
    },
    rules: [
      {
        rule: "semgrep:py.sqli",
        kind: "deterministic_rule",
        tool: "semgrep",
        check: "semgrep",
        asked: 0,
        hits: 2,
        acted_on: 1,
        dismissed: 1,
        false_positive: 0,
        hit_rate: null,
        dismissal_rate: 0.5,
        acted_on_rate: 0.5,
        signal: "insufficient_data",
        drill: {
          asked: "rule.semgrep:py.sqli.asked",
          hits: "rule.semgrep:py.sqli.hits",
          acted_on: "rule.semgrep:py.sqli.acted_on",
          dismissed: "rule.semgrep:py.sqli.dismissed",
          false_positive: "rule.semgrep:py.sqli.false_positive",
        },
      },
    ],
    checks: [],
    policy_effectiveness: {
      min_judged: 5,
      signal_rate: 0.5,
      signal: [],
      noise: [],
      checks_signal: [],
      checks_noise: [],
      basis: "advisory and gates nothing",
    },
    auto_approval: {
      evaluated: row("auto.evaluated", "Auto-approval evaluated"),
      eligible: row("auto.eligible", "Eligible for auto-approval"),
      not_eligible: row("auto.not_eligible", "Not eligible"),
      failed_gates: [],
    },
    overrides: { break_glass: row("override.break_glass", "Break-glass landings"), bypassed: [] },
    depth: { distribution: [row("depth.standard", "Standard", 2, { key: "standard" })] },
    escalation: { escalated: lane("escalated"), standard: lane("standard"), triggers: [] },
    cost: {
      model_calls: [
        row("cost.calls.customer.local", "Your compute and provider accounts (local)", 2, {
          value: 2,
          cost_class: "customer",
          locus: "local",
        }),
        row("cost.calls.managed.managed", "LemonCrow-managed (managed)", 1, {
          value: 1,
          cost_class: "managed",
          locus: "managed",
        }),
        row("cost.calls.customer.review_host", "Your compute and provider accounts (review_host)", 1, {
          value: 1,
          cost_class: "customer",
          locus: "review_host",
        }),
      ],
      provider_spend: [],
      calls_without_cost: [],
      compute_minutes: [],
      job_minutes: [
        row("cost.jobs.managed.lemoncrow_managed", "LemonCrow-managed (lemoncrow_managed)", 1, {
          value: 1.5,
          cost_class: "managed",
          executor: "lemoncrow_managed",
        }),
      ],
      basis: "separately from LemonCrow-managed spend",
    },
    cache_reuse: [],
    cost_per_useful_finding: [],
    correction_loops: {
      started: row("loops.started", "Fix requests started"),
      snapshot_recorded: row("loops.snapshot_recorded", "New revision recorded"),
      re_reviewed: row("loops.re_reviewed", "Re-reviewed after the fix"),
      in_flight: row("loops.in_flight", "Waiting for a new revision"),
      failed: row("loops.failed", "Failed"),
      cancelled: row("loops.cancelled", "Cancelled"),
      completion_rate: null,
    },
    escaped_defects: {
      total: row("escapes.total", "Escaped defects"),
      by_depth: [],
      by_check: [],
      by_kind: [],
      basis: "incident links are never counted",
    },
    drift: {
      granularity: "week",
      changes: row("drift.changes", "Side-effect register changes", 1),
      by_judgment: [row("drift.judgment.accepted", "Accepted by a reviewer", 1, { key: "accepted" })],
      by_category: [],
      by_change: [],
      series: [],
      items: [
        {
          review_id: "rev-7",
          repo_id: "repo-a",
          repo_name: "widgets",
          review_title: "Add cache",
          review_path: "/r/rev-7",
          revision_id: "rrv-1",
          merged: false,
          at: "2026-10-01T00:00:00Z",
          category: "network",
          change: "added",
          title: "Calls the billing API",
          critical: false,
          judgment: { state: "accepted", label: "Accepted by a reviewer", by: "alice", at: "", event_id: "e1" },
        },
      ],
      items_total: 1,
    },
    waiting_by_owner: [row("waiting.reviewers", "Reviewers", 1, { key: "reviewers", median_hours: 5, max_hours: 5 })],
    reviewer_load: {
      waiting_on_reviewers: row("load.waiting_on_reviewers", "Open Reviews waiting on reviewers", 1),
      by_team: [],
      teams_shown: "yours",
      mine: {
        to_review: row("load.mine.to_review", "To review (you)"),
        waiting_on_me: row("load.mine.waiting_on_me", "Waiting on you"),
      },
      individuals: "not_reported",
      basis: "No figure about any other person is computed.",
    },
    ...overrides,
  };
}

const DRILL: ReviewAnalyticsDrill = {
  key: "findings.invalidated.stale",
  label: "Stale",
  known: true,
  count: 1,
  value: 0,
  reviews: 1,
  records: [
    {
      review_id: "rev-9",
      repo_id: "repo-a",
      record_kind: "deterministic_finding",
      record_id: "f-1",
      revision_id: "rrv-2",
      at: "2026-10-02T00:00:00Z",
      label: "bandit B101",
      value: 0,
      review_title: "Harden login",
      repo_name: "widgets",
      review_path: "/r/rev-9",
    },
  ],
  limit: 500,
  truncated: false,
  scope: SCOPE,
};

function show(mode: "local" | "hosted", review = true) {
  const capabilities = review
    ? createProductCapabilities({ review: true, runtime: false, mode })
    : CORE_ONLY_PRODUCT_CAPABILITIES;
  return render(
    <MemoryRouter>
      <TimeRangeProvider>
        <ProductCapabilitiesProvider value={capabilities}>
          <ReviewAnalytics />
        </ProductCapabilitiesProvider>
      </TimeRangeProvider>
    </MemoryRouter>,
  );
}

function requested(): URL[] {
  return api.requestBrowserJson.mock.calls.map(([path]) => new URL(String(path), "http://x"));
}

describe("Review analytics on the Usage page", () => {
  it("uses the hosted route beside the Inbox and the local server route otherwise", () => {
    expect(reviewAnalyticsPath("hosted")).toBe("/v1/reviews/analytics");
    expect(reviewAnalyticsPath("local")).toBe("/api/review-analytics");
    const params = reviewAnalyticsParams({ days: 7, repos: ["a", "b"], paths: ["src/"], teams: ["web"] });
    expect(params.getAll("repo")).toEqual(["a", "b"]);
    expect(params.get("days")).toBe("7");
    // An explicit window wins over a relative one; the drill repeats the report's exact window.
    const exact = reviewAnalyticsParams({ days: 7, since: SCOPE.since, until: SCOPE.until });
    expect(exact.get("days")).toBeNull();
    expect(exact.get("since")).toBe(SCOPE.since);
  });

  it("stays hidden and asks nothing where Review is not available", () => {
    const { container } = show("local", false);
    expect(container.textContent).toBe("");
    expect(api.requestBrowserJson).not.toHaveBeenCalled();
  });

  it("renders advisory figures with the compute split and opens the records behind a count", async () => {
    api.requestBrowserJson.mockImplementation((path: string) =>
      Promise.resolve(path.includes("/drill?") ? DRILL : report()),
    );
    show("local");

    expect(await screen.findByText("Review analytics")).toBeTruthy();
    expect(screen.getByText(/never blocks Merge when ready/)).toBeTruthy();
    expect(screen.getByText("semgrep:py.sqli")).toBeTruthy();
    expect(screen.getByText("Your compute & keys · local")).toBeTruthy();
    // The internal review_host locus is not shown: the cost class alone names it.
    expect(screen.getByText("Your compute & keys")).toBeTruthy();
    expect(screen.queryByText(/review_host/)).toBeNull();
    expect(screen.getByText("LemonCrow-managed · managed")).toBeTruthy();
    expect(screen.getByText("LemonCrow-managed · job time on lemoncrow_managed")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Review" }).getAttribute("href")).toBe("/r/rev-7");
    // Local reviews have no teams: the team filter is not offered.
    expect(screen.queryByLabelText("Team")).toBeNull();
    const [first] = requested();
    expect(first.pathname).toBe("/api/review-analytics");
    expect(first.searchParams.get("days")).toBe("7");

    fireEvent.click(screen.getByRole("button", { name: "Show the Reviews behind Stale" }));
    const drill = await screen.findByTestId("review-analytics-drill");
    expect(within(drill).getByText("Harden login")).toBeTruthy();
    expect(within(drill).getByRole("link", { name: "Open Review" }).getAttribute("href")).toBe("/r/rev-9");
    const drillUrl = requested().find((url) => url.pathname.endsWith("/drill"));
    expect(drillUrl?.searchParams.get("key")).toBe("findings.invalidated.stale");
    expect(drillUrl?.searchParams.get("since")).toBe(SCOPE.since);
    expect(drillUrl?.searchParams.get("until")).toBe(SCOPE.until);
    expect(drillUrl?.searchParams.get("days")).toBeNull();
    // Internal vocabulary stays out of the page.
    expect(document.body.textContent).not.toMatch(/intelligence|session|landing gate/i);
  });

  it("offers the team filter where teams exist and sends the filters it applies", async () => {
    api.requestBrowserJson.mockResolvedValue(report({ teams_supported: true }));
    show("hosted");

    const team = await screen.findByLabelText("Team");
    fireEvent.change(team, { target: { value: "web" } });
    fireEvent.change(screen.getByLabelText("Path"), { target: { value: "src/api" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(api.requestBrowserJson).toHaveBeenCalledTimes(2));
    const last = requested()[1];
    expect(last.pathname).toBe("/v1/reviews/analytics");
    expect(last.searchParams.getAll("team")).toEqual(["web"]);
    expect(last.searchParams.getAll("path")).toEqual(["src/api"]);
  });

  it("says when the figures cover only part of the period", async () => {
    api.requestBrowserJson.mockResolvedValue(
      report({
        bounds: {
          max_reviews: 300,
          candidates: 301,
          analyzed: 300,
          in_scope: 300,
          truncated: true,
          scan_truncated: true,
          escapes_truncated: false,
        },
      }),
    );
    show("local");
    expect(await screen.findByText("Showing the newest 300 of 301 Reviews created in this period.")).toBeTruthy();
    expect(screen.getByText(/Older Reviews were not reached/)).toBeTruthy();
  });

  it("marks what this engine does not keep instead of showing zeros", async () => {
    api.requestBrowserJson.mockResolvedValue(
      report({
        unavailable: [
          { source: "fix_requests", label: "Fix requests", sections: ["correction_loops", "findings"] },
          { source: "check_findings", label: "Check findings", sections: ["findings", "rules", "checks", "catch_contribution"] },
        ],
      }),
    );
    show("local");
    const notes = await screen.findAllByTestId("review-analytics-unavailable");
    expect(notes.map((note) => note.textContent)).toEqual([
      "Not available in this engine: Fix requests, Check findings.",
      "Not available in this engine: Check findings.",
      "Not available in this engine: Fix requests.",
    ]);
    // A section fed only by the absent source shows no measured zeros.
    expect(screen.queryByText("Fix requests started")).toBeNull();
    expect(screen.getByText("semgrep:py.sqli")).toBeTruthy();
  });

  it("stays out of the way without Review read access", async () => {
    api.requestBrowserJson.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
    const { container } = show("hosted");
    await waitFor(() => expect(api.requestBrowserJson).toHaveBeenCalled());
    await waitFor(() => expect(container.textContent).toBe(""));
  });
});
