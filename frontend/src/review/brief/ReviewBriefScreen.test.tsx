import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchReviewProjectContext } from "../reviewApi";
import type { ReviewBehaviorSummary, ReviewOverview, ReviewProjection, ReviewProjectionDecision, ReviewTarget } from "../types";
import ReviewBriefScreen, { briefDecisions, reviewFirstParts, type ReviewBriefScreenProps } from "./ReviewBriefScreen";

vi.mock("../reviewApi", async (original) => ({
  ...(await original<typeof import("../reviewApi")>()),
  fetchReviewProjectContext: vi.fn(async () => ({ project_context: { available: false, reason: "not_configured", entries: [], narration: [] } })),
}));

function target(id: string, symbol: string, position: number, attention = "review_first"): ReviewTarget {
  return {
    target_id: id, unit_key: id, kind: "symbol", path: `src/${symbol}.py`, label: symbol, symbol, start_line: 1, end_line: 2,
    hunk_ordinals: [0], spans: [], state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "", attention_rank: 1,
    attention_level: "high", reasons: [], additions: 2, deletions: 1, fingerprint_method: "ast", verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 }, answer: { category: "decision", rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 1, attention, outstanding: true, position, reasons: [{ code: "auth", label: "Auth", detail: `Touches ${symbol}` }] },
  } as unknown as ReviewTarget;
}

const targets = [target("t-b", "beta", 1), target("t-a", "alpha", 0), target("t-c", "gamma", 2, "routine")];
const overview = { title: "Fix sign-in", session: { id: "r1", title: "Fix sign-in" }, revision: { id: "rev-1", revision_number: 2 } } as unknown as ReviewOverview;

function decision(key: string, state: Partial<NonNullable<ReviewProjectionDecision["state"]>> | null): ReviewProjectionDecision {
  return {
    key, question: `Decide ${key}?`, options: [{ key: "keep", label: "Keep it" }, { key: "drop", label: "Drop it" }], selected_option: "keep",
    reason_human_required: "A person owns this.", target_ids: ["t-a"], relied_checks: [], stale: false, conflict: false, actions: ["accept", "defer"],
    state: state ? { disposition: "open", outstanding: true, withdrawn: false, navigable: true, changed_inputs: [], limitations: [], accepted_option: "", latest_event_id: "", semantic_fingerprint: "", ...state } : null,
  } as unknown as ReviewProjectionDecision;
}

function projection(options: Partial<ReviewProjection> = {}): ReviewProjection {
  return {
    projection_version: 1, review_id: "r1", revision_id: "rev-1", snapshot_number: 2, historical: false,
    summary: { paragraphs: ["Sign-in rejects expired tokens."], facts: [{ kind: "unchecked", text: "Tests were not run" }] },
    plan: { schema_version: 1, input_digest: "x", intents: [], limitations: [] }, actions: [], questions: [], items: [], claims: [], decisions: [],
    coverage: { target_count: 3, author_explained: 0, fallback_grouped: 0, other: 0, routine: 0, unresolved_refs: 0, unexplained_targets: 0, file_count: 3 },
    counts: { review_first: 2, understand: 0, routine: 1, done: 0, decisions: 0 }, queue: [],
    ...options,
  } as unknown as ReviewProjection;
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function show(props: Partial<ReviewBriefScreenProps> = {}) {
  return render(
    <MemoryRouter initialEntries={["/r/shop/brief"]}>
      <ReviewBriefScreen reviewRef="shop" apiReviewId="r1" overview={overview} targets={targets} projection={projection()} {...props} />
      <Where />
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("ReviewBriefScreen", () => {
  it("leads with the review's title and the count line under it, and lists the read-first parts in the server's order", async () => {
    show({ projection: projection({ summary: { headline: { title: "Fix sign-in for expired tokens", counts: "3 files · 3 parts · +6 −3" }, paragraphs: ["handler (src/a.py) changed · called from jobs.py"], facts: [{ kind: "unchecked", text: "Tests were not run" }] } as unknown as ReviewProjection["summary"] }) });
    const title = screen.getByTestId("brief-title");
    expect(title).toHaveTextContent("Fix sign-in for expired tokens");
    expect(title).not.toHaveTextContent("called from");
    expect(screen.getByTestId("brief-size")).toHaveTextContent("3 files · 3 parts · +6 −3");
    // The count line sits right under the title.
    expect(title.nextElementSibling).toBe(screen.getByTestId("brief-size"));
    expect(screen.getByTestId("brief-change-facts")).toHaveTextContent("Tests were not run");
    const rows = screen.getAllByTestId("brief-attention-part");
    expect(rows.map((row) => row.textContent)).toEqual([expect.stringContaining("alpha"), expect.stringContaining("beta")]);
    await userEvent.click(rows[0]);
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/one/alpha");
  });

  it("from a server without a headline, leads with the name the top bar shows and counts the parts itself", () => {
    show();
    expect(screen.getByTestId("brief-title")).toHaveTextContent("Fix sign-in");
    expect(screen.getByTestId("brief-size")).toHaveTextContent("3 files · 3 parts · +6 −3");
  });

  it("says no check ran once: the Risk line, not again as a fact", () => {
    const risk = { level: "high", label: "High", reason: "alpha is called from jobs.py and no check ran on this revision", text: "", facts: [], not_measured: [], risky_target_ids: [], understanding: { reviewed: 0, total: 2, risky: true, text: "0 of 2" } };
    show({ projection: projection({ summary: { paragraphs: ["Sign-in rejects expired tokens."], facts: [{ kind: "unchecked", text: "Not checked: no test or check ran on this revision" }, { kind: "unchecked", text: "Whether guests can sign in" }], risk } as unknown as ReviewProjection["summary"] }) });
    const facts = screen.getByTestId("brief-change-facts");
    expect(facts).not.toHaveTextContent("no test or check ran");
    expect(facts).toHaveTextContent("Whether guests can sign in");
  });

  it("with no open decision question, counts the parts that need a decision as Home and the Overview do", async () => {
    show();
    const line = screen.getByTestId("brief-no-decisions");
    expect(line).toHaveTextContent("3 parts need your decision.");
    expect(line).not.toHaveTextContent("No decision is open");
    await userEvent.click(within(line).getByRole("button", { name: "Open the first" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/one/");
    cleanup();
    show({ targets: targets.map((row) => ({ ...row, answer: { category: "quick_check", rule: "", rule_label: "", reason: "" } })) });
    expect(screen.getByTestId("brief-no-decisions")).toHaveTextContent("No part needs your decision on this revision.");
  });

  it("names the other repositories a change reaches, with evidence and no source line", async () => {
    const edge = (key: string) => ({
      key, confidence: "high", provider: { path: "api/routes.py", line: 4 }, consumer: { path: "web/client.ts", line: 9, detail: "secret source line" },
    });
    const reached = Array.from({ length: 6 }, (_, index) => ({
      repo: `repo-${index}`, hops: index === 0 ? 2 : 1, confidence: "medium", direct: index === 0, path: ["core", "mid", `repo-${index}`],
      edges: [{ kind: "http", confidence: "medium", direct: true, links: [edge("/a"), edge("/b"), edge("/c")], more: 1 }],
    }));
    show({ projection: projection({ workspace_impact: { repo: "core", root: "~/work", changed: [], reached, incomplete: ["big"] } as never }) });
    const section = screen.getByTestId("brief-workspace-impact");
    expect(section).toHaveTextContent("6 other repositories can be affected (~/work, this repository is core)");
    expect(section).toHaveTextContent("via core → mid → repo-0");
    expect(section).toHaveTextContent("web/client.ts:9 uses api/routes.py:4");
    expect(section).toHaveTextContent("2 more http evidence");
    expect(section).toHaveTextContent("big was not fully scanned");
    expect(section).not.toHaveTextContent("secret source line");
    expect(screen.getAllByTestId("brief-workspace-repo")).toHaveLength(5);
    await userEvent.click(screen.getByTestId("brief-workspace-show-all"));
    expect(screen.getAllByTestId("brief-workspace-repo")).toHaveLength(6);
    expect(section).toHaveTextContent("1 more http evidence");
  });

  it("shows no workspace section for a Review with no workspace", () => {
    show({ projection: projection({ workspace_impact: {} }) });
    expect(screen.queryByTestId("brief-workspace-impact")).toBeNull();
  });

  it("says behavior was not observed instead of showing an empty section", () => {
    show();
    expect(screen.getByTestId("brief-no-behavior")).toHaveTextContent("not proof that behavior is unchanged");
  });

  it("draws behavior changes from the plan, read-only on a historical revision", () => {
    const behavior = {
      total: 1, unchanged: 0, degraded: [], targets: [{ behavior_key: "b1", kind_label: "API", title: "POST /x", summary: "200 to 401", state: "unreviewed", critical: true, executor_class: "", fingerprint: "f", environment_fingerprint: "", paths: [], unverified_integrations: [] }],
      progress: { reviewed: 0, critical: 1 }, narrative: null,
    } as unknown as ReviewBehaviorSummary;
    const onJudge = vi.fn();
    const { unmount } = show({ projection: projection({ behavior }), onJudge, onLoadTarget: vi.fn() });
    const section = screen.getByTestId("brief-behavior");
    expect(within(section).getByText("API · POST /x")).toBeInTheDocument();
    expect(within(section).getByText("1 critical")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Mark reviewed" })).toBeDisabled();
    unmount();
    show({ projection: projection({ behavior, historical: true }), onJudge, onLoadTarget: vi.fn() });
    expect(within(screen.getByTestId("brief-behavior")).queryByRole("button", { name: "Mark reviewed" })).toBeNull();
    expect(within(screen.getByTestId("brief-behavior")).queryByRole("button", { name: "Show evidence" })).toBeNull();
  });

  it("records a decision through the host and offers no action without one", async () => {
    const onDecision = vi.fn(async () => undefined);
    const open = projection({ decisions: [decision("d1", {})] });
    const { unmount } = show({ projection: open, onDecision });
    const card = screen.getByTestId("brief-decision-d1");
    await userEvent.click(within(card).getByRole("radio", { name: /Drop it/ }));
    await userEvent.click(within(card).getByRole("button", { name: "Accept decision" }));
    expect(onDecision).toHaveBeenCalledWith("d1", { action: "accept", option_key: "drop" });
    unmount();
    show({ projection: open });
    expect(within(screen.getByTestId("brief-decision-d1")).queryByRole("button", { name: "Accept decision" })).toBeNull();
  });

  it("keeps deferred and withdrawn decisions out of the open list", () => {
    const groups = briefDecisions(projection({ decisions: [decision("open", {}), decision("later", { disposition: "deferred", navigable: false, outstanding: false }), decision("gone", { withdrawn: true })] }));
    expect(groups.active.map((row) => row.key)).toEqual(["open"]);
    expect(groups.deferred.map((row) => row.key)).toEqual(["later"]);
    expect(groups.withdrawn.map((row) => row.key)).toEqual(["gone"]);
  });

  it("shows the Review's blind spots and names what was not measured", () => {
    show();
    const section = screen.getByTestId("brief-blind-spots");
    expect(within(section).getByTestId("blind-spots")).toBeInTheDocument();
    expect(within(section).getByTestId("blind-spot-measurement")).toBeInTheDocument();
  });

  it("reads Project history for this revision and adds nothing when the repository has none", async () => {
    show();
    await waitFor(() => expect(fetchReviewProjectContext).toHaveBeenCalledWith("r1", 2, "brief", {}));
    expect(screen.queryByTestId("project-history")).toBeNull();
    cleanup();
    vi.mocked(fetchReviewProjectContext).mockResolvedValueOnce({
      review_id: "r1", revision_id: "rev-1", revision_number: 2,
      project_context: {
        purpose: "brief", available: true, reason: "", context_class: "project_knowledge", authority: "advisory", authority_note: "Landed project history.",
        current_generation: "g", omitted: 0, truncated: false, filtered_by_content_access: 0, narration: [],
        entries: [{
          entry_id: "e1", context_class: "project_knowledge", subject: { kind: "path", key: "src/alpha.py" }, knowledge_kind: "documentation", summary: "Alpha owns sign-in.",
          summary_truncated: false, state: "current", state_label: "Landed on main", freshness_label: "Landed on main", stale_for_snapshot: false, support: "source_document",
          canonical_ref: "refs/heads/main", relevance: "changed_path", source_paths: [], observed_at: "", history: { origin: null, superseded_by: null }, citations: [],
        }],
      },
    });
    show();
    expect(await screen.findByTestId("project-history")).toHaveTextContent("Alpha owns sign-in.");
  });

  it("offers the X-Ray only where the change has a Map, and says when no Brief exists", async () => {
    const { unmount } = show({ xrayAvailable: true });
    await userEvent.click(screen.getByTestId("brief-open-xray"));
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/xray");
    unmount();
    show({ projection: null });
    expect(screen.getByTestId("brief-unavailable")).toHaveTextContent("No Brief is available");
    expect(screen.queryByTestId("brief-open-xray")).toBeNull();
    expect(reviewFirstParts(targets).map((row) => row.target_id)).toEqual(["t-a", "t-b"]);
  });
});
