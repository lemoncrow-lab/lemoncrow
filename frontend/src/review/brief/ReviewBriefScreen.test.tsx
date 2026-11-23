import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

describe("ReviewBriefScreen: blockers and evidence", () => {
  const failing = { id: "check:required:tests", reason: "", title: "Required check failing: tests", detail: "pytest exited 1", item_ids: [], target_ids: [], actions: [], tier: 0 };
  const checks = [
    { name: "ruff", status: "PASS", detail: "", source: "none" },
    { name: "e2e", status: "NOT_RUN", detail: "", source: "agent" },
    { name: "pytest", status: "FAIL", detail: "2 failed", source: "session:abc" },
  ] as ReviewBriefScreenProps["checks"];
  const claims = [
    { id: "c1", statement: "Expired tokens are rejected", source: "author", target_ids: ["t-a"], limitations: [], evidence: [], summary: "contradictory", summary_text: "A check contradicts it", result_line: "pytest: FAIL" },
    { id: "c2", statement: "Guests keep their cart", source: "author", target_ids: [], limitations: [], evidence: [], summary: "none", summary_text: "Nothing shows it", result_line: "" },
  ];
  const rich = () => projection({
    actions: [failing, { ...failing, id: "decision:x", title: "Decide x" }],
    claims,
    evaluation_timeline: { snapshots: [{ revision_id: "rev-1", snapshot_number: 2, comparable_results: [{ scenario: "login", from: "PASS", to: "FAIL", change: "regression" }] }] },
    ai_review: { status: "ready", findings: 2, budget: { calls_used: 1, max_calls: 4 } },
  } as unknown as Partial<ReviewProjection>);
  // jsdom draws no layout: test/setup.ts stubs scrollIntoView, and this spies on it.
  const original = Element.prototype.scrollIntoView;
  let scroll = vi.fn();
  beforeEach(() => { scroll = vi.fn(); Element.prototype.scrollIntoView = scroll; });
  afterEach(() => { Element.prototype.scrollIntoView = original; });

  it("shows a failing required check first, and Inspect evidence lands on the Evidence card", async () => {
    show({ projection: rich(), checks });
    const rows = within(screen.getByTestId("brief-blockers")).getAllByTestId("brief-required-check");
    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row).toHaveTextContent("Required check failing: tests");
    expect(row).toHaveTextContent("pytest exited 1");
    await userEvent.click(within(row).getByRole("button", { name: "Inspect evidence" }));
    expect(scroll).toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByTestId("brief-evidence"));
  });

  it("lists each check with its result, detail and source, each claim with its evidence, the regressions and the AI status", async () => {
    show({ projection: rich(), checks });
    const card = screen.getByTestId("brief-evidence");
    expect(card).toHaveTextContent("1 failed");
    const rows = within(card).getAllByTestId("brief-evidence-check");
    expect(rows.map((row) => row.textContent)).toEqual(["Failpytest· the agent session2 failed", "Passruff", "Did not rune2e· the agent"]);
    const claimRows = within(card).getAllByTestId("brief-evidence-claim");
    expect(claimRows[0]).toHaveTextContent("Expired tokens are rejectedA check contradicts itpytest: FAIL");
    expect(claimRows[1]).toHaveTextContent("Guests keep their cartNothing shows it");
    await userEvent.click(within(claimRows[0]).getByRole("button", { name: "Expired tokens are rejected" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/one/alpha");
    cleanup();
    show({ projection: rich(), checks });
    expect(screen.getByTestId("brief-regressions")).toHaveTextContent("Regression · login · PASS → FAIL · Revision 3");
    expect(screen.getByTestId("brief-ai-review")).toHaveTextContent("AI review · complete · 2 findings · not a test result");
    expect(within(screen.getByTestId("brief-evidence")).queryByRole("button", { name: /Run|Cancel|Synthesis/ })).toBeNull();
  });

  it("opened at #evidence, scrolls to the Evidence card", () => {
    render(
      <MemoryRouter initialEntries={["/r/shop/brief#evidence"]}>
        <ReviewBriefScreen reviewRef="shop" apiReviewId="r1" overview={overview} targets={targets} projection={rich()} checks={checks} />
      </MemoryRouter>,
    );
    expect(scroll).toHaveBeenCalledWith({ block: "start" });
  });

  it("draws no blocker, evidence, priority, statement, dependency or architecture box without data", async () => {
    const loadArchitecture = vi.fn(async () => ({ summary: { material: false, changed_components: 0, important_concerns: [] } }) as never);
    show({ loadArchitecture, dependencies: { supported: false, changes: [], degraded: [], lockfiles: [] } as never, xrayAvailable: true });
    await waitFor(() => expect(loadArchitecture).toHaveBeenCalled());
    for (const id of ["brief-blockers", "brief-evidence", "brief-priority", "brief-statements", "brief-dependencies", "brief-architecture", "brief-check-command", "brief-ai-review"]) {
      expect(screen.queryByTestId(id), id).toBeNull();
    }
    // No architecture line: the plain X-Ray button stays.
    expect(screen.getByTestId("brief-open-xray")).toBeInTheDocument();
    cleanup();
    show({ projection: projection({ triage: { priority: "high", criticality: "critical", severity_summary: { p0: 1 }, explanation: [], computed: false } } as unknown as Partial<ReviewProjection>) });
    expect(screen.queryByTestId("brief-priority")).toBeNull();
  });
});

describe("ReviewBriefScreen: what changed", () => {
  it("leads with the priority line and each field's reason", async () => {
    const triage = { priority: "high", criticality: "critical", severity_summary: { p0: 2, p1: 1, p2: 0 }, computed: true, explanation: [{ field: "criticality", value: "critical", reason: "Changes the auth boundary", origin: "engine" }] };
    show({ projection: projection({ triage } as unknown as Partial<ReviewProjection>) });
    const line = screen.getByTestId("brief-priority");
    expect(line.querySelector("summary")).toHaveTextContent("Priority High · Critical · 2 P0 · 1 P1");
    expect(screen.getByTestId("brief-priority-reasons")).toHaveTextContent("Criticality · Changes the auth boundary");
    // The size line still sits right under the title.
    expect(screen.getByTestId("brief-title").nextElementSibling).toBe(screen.getByTestId("brief-size"));
  });

  it("says how to check what was not checked", () => {
    show({ projection: projection({ summary: { paragraphs: [], facts: [{ kind: "unchecked", text: "Not checked: sign-in tests", command: "uv run pytest tests/auth" }] } } as unknown as Partial<ReviewProjection>) });
    expect(screen.getByTestId("brief-change-facts")).toHaveTextContent("Not checked: sign-in tests · run uv run pytest tests/auth");
    expect(screen.getByTestId("brief-check-command")).toHaveTextContent("uv run pytest tests/auth");
  });

  it("lists the change statements with where they come from, and not the facts again", () => {
    const statements = [
      { text: "Sign-in rejects expired tokens", origin: "inferred", target_ids: [], claim_ids: [] },
      { text: "Tests were not run", origin: "observed", target_ids: [], claim_ids: [] },
    ];
    show({ projection: projection({ summary: { paragraphs: [], facts: [{ kind: "unchecked", text: "Tests were not run" }], statements } } as unknown as Partial<ReviewProjection>) });
    const block = screen.getByTestId("brief-statements");
    expect(screen.getByTestId("brief-statements-origin")).toHaveTextContent("AI summary · intent inferred");
    expect(block).toHaveTextContent("Sign-in rejects expired tokens");
    expect(block).not.toHaveTextContent("Tests were not run");
    cleanup();
    show({ projection: projection({ summary: { paragraphs: [], facts: [], statements: [{ text: "Guests keep their cart", origin: "author", target_ids: [], claim_ids: [] }] } } as unknown as Partial<ReviewProjection>) });
    expect(screen.getByTestId("brief-statements-origin")).toHaveTextContent("From author context");
  });

  it("shows the package changes the Reader loaded", () => {
    const dependencies = {
      supported: true, changes: [], degraded: [], lockfiles: [{ path: "uv.lock" }],
      summary: { semantic_changes: 3, direct_changes: 1, transitive_changes: 2 }, supply_chain: { sboms: [], provenance: [], vulnerability_scans: [] },
    };
    show({ dependencies: dependencies as never });
    const section = screen.getByTestId("brief-dependencies");
    expect(section).toHaveTextContent("Dependencies · 3");
    expect(within(section).getByTestId("dependency-review")).toHaveTextContent("3 package changes · 1 direct · 2 transitive");
  });

  it("says the architecture moved, with its top concerns, and opens the X-Ray from that line", async () => {
    const loadArchitecture = vi.fn(async () => ({ summary: { material: true, changed_components: 2, important_concerns: ["auth now calls billing", "new cycle: cart ↔ orders"] } }) as never);
    show({ loadArchitecture, xrayAvailable: true });
    const line = await screen.findByTestId("brief-architecture");
    expect(line).toHaveTextContent("Architecture moved");
    expect(line).toHaveTextContent("auth now calls billing · new cycle: cart ↔ orders");
    expect(screen.getAllByTestId("brief-open-xray")).toHaveLength(1);
    await userEvent.click(within(line).getByTestId("brief-open-xray"));
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/xray");
  });
});
