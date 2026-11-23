import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewDirectory from "./ReviewDirectory";
import { InboxRowSignals, inboxAxesText, nextActionText } from "./ReviewInboxViews";
import type { InboxSignals, SavedInboxFilter } from "./types";

const api = vi.hoisted(() => ({
  fetchReviewDirectory: vi.fn(),
  updateReviewTitle: vi.fn(),
  deleteReview: vi.fn(),
  finishReview: vi.fn(),
  fetchInboxFilters: vi.fn(),
  createInboxFilter: vi.fn(),
  deleteInboxFilter: vi.fn(),
}));
vi.mock("./reviewApi", () => ({
  adoptBootstrapFragment: () => ({ token: "secret", reviewId: "" }),
  fetchReviewDirectory: api.fetchReviewDirectory,
  updateReviewTitle: api.updateReviewTitle,
  deleteReview: api.deleteReview,
  finishReview: api.finishReview,
  fetchInboxFilters: api.fetchInboxFilters,
  createInboxFilter: api.createInboxFilter,
  deleteInboxFilter: api.deleteInboxFilter,
  reviewHref: (id: string) => `/reviews/${id}#token`,
}));

function signals(overrides: Partial<InboxSignals> = {}): InboxSignals {
  return {
    views: ["participated", "waiting_on_me"],
    workflow: {
      state: "needs_decision",
      label: "Needs decision",
      rule: "readiness_not_ready",
      explanation: "Review complete; 1 readiness condition(s) not satisfied",
    },
    priority: {
      level: "high", computed: true, computed_level: "high", criticality: "high",
      evidence_state: "complete", overridden: false, revision_id: "rr-3",
    },
    next_action: {
      owner_ids: ["local"],
      for_me: true,
      my_reasons: [{ principal_id: "local", kind: "decision_needed", fact: "Decide whether to merge", since: "" }],
      reasons: [{ principal_id: "local", kind: "decision_needed", fact: "Decide whether to merge", since: "" }],
    },
    readiness: {
      state: "not_ready", reason: "1 readiness condition(s) not satisfied", evaluated_at: "2026-09-15T20:00:00Z",
      valid_until: "", blocker_count: 1, scm_blockers: 1, pending_checks: 0, failed_checks: 1, scm_conditions: 1,
    },
    axes: {
      your_review: { state: "complete", target_count: 5, reviewed: 5, remaining: 0 },
      team_review: { state: "not_applicable", reason: "A local Review has one reviewer" },
      scm: { state: "not_ready", connected: true, provider: "github", failed_checks: 1, pending_checks: 0 },
    },
    attention: {
      score: 35,
      waiting_since: "",
      factors: [
        { key: "ownership", label: "You own the next action", points: 10, detail: "Decide whether to merge" },
        { key: "impact", label: "Impact", points: 13, detail: "priority high, criticality high" },
      ],
    },
    teams: [],
    team_requests: 0,
    ...overrides,
  };
}

const SAVED: SavedInboxFilter = {
  id: "rsf-1",
  name: "Needs a decision",
  definition: {
    view: "waiting_on_me", status: "open", repos: [], teams: [], workflow_states: ["needs_decision"],
    priorities: [], query: "",
  },
  created_at: "2026-09-15T20:00:00Z",
  updated_at: "2026-09-15T20:00:00Z",
};

function discardRefusal() {
  return Object.assign(new Error("Discarding this Review needs explicit confirmation"), {
    status: 409,
    detail: {
      code: "archive_confirmation_required",
      message: "Discarding this Review needs explicit confirmation naming its pending feedback and armed landing.",
      review_id: "rev-open",
      hazards: [
        {
          id: "pending_feedback",
          label: "Pending feedback",
          count: 2,
          detail: "2 comments not yet sent to the author or the provider.",
          outcome: "Nothing is sent. The comments stay inside the discarded Review.",
          subject_ids: ["a1", "a2"],
          provider: "",
          owed_cancellation: false,
        },
        {
          id: "armed_landing",
          label: "Armed landing",
          count: 1,
          detail: "Merge when ready is delegated.",
          outcome: "Merge when ready is withdrawn: LemonCrow asks github to cancel it. The cancellation stays owed until github confirms it.",
          subject_ids: ["rel-1"],
          provider: "github",
          owed_cancellation: false,
        },
      ],
      hazard_ids: ["pending_feedback", "armed_landing"],
      hazard_digest: "digest-1",
      confirmation_required: true,
      deletes_now: false,
      retention: "A discarded Review is kept (and restorable) until retention cleanup removes it later.",
    },
  });
}

describe("ReviewDirectory", () => {
  beforeEach(() => {
    api.fetchReviewDirectory.mockReset();
    api.updateReviewTitle.mockReset();
    api.deleteReview.mockReset();
    api.finishReview.mockReset();
    api.fetchInboxFilters.mockReset();
    api.createInboxFilter.mockReset();
    api.deleteInboxFilter.mockReset();
    api.fetchInboxFilters.mockResolvedValue({ filters: [], limit: 50, teams_supported: false });
    api.fetchReviewDirectory.mockResolvedValue({
      reviews: [
        {
          id: "rev-legacy",
          review_path: "/reviews/rev-legacy",
          repo_id: "repo_opaque_hash",
          title: "Legacy change",
          auto_title: "Revision 1: Update README.md",
          title_is_custom: false,
          source_ref: "client:working_tree",
          range_mode: "working_tree",
          status: "open",
          revision_number: 0,
          revision_id: "rr-legacy",
          progress: {
            target_count: 1, reviewed: 0, changed_since_review: 0, needs_changes: 0, unreviewed: 1, unknown: 0, mechanical: 0,
          },
          updated_at: "2026-09-15T20:05:00Z",
          created_at: "2026-09-15T19:05:00Z",
        },
        {
          id: "rev-open",
          review_path: "/reviews/rev-open",
          repo_id: "repo-1",
          title: "Open checkout change",
          auto_title: "Revision 4: frontend changes · 5 files",
          title_is_custom: false,
          source_ref: "client:working_tree",
          range_mode: "working_tree",
          directory: "/home/me/Projects/leanchain",
          project: "lemoncrow-dev",
          worktree: "agentic-review",
          worktree_root: "/home/me/Projects/leanchain/lemoncrow-dev/.lc-worktrees/agentic-review",
          branch: "feat/agentic-review",
          status: "open",
          revision_number: 3,
          revision_id: "rr-3",
          progress: {
            target_count: 5,
            reviewed: 3,
            changed_since_review: 0,
            needs_changes: 0,
            unreviewed: 2,
            unknown: 0,
            mechanical: 0,
          },
          updated_at: "2026-09-15T20:00:00Z",
          created_at: "2026-09-15T19:00:00Z",
        },
      ],
      status: "open",
      query: "",
      next_cursor: "",
    });
  });

  it("reads a review with only a part left for later as still to review, never as ready to finish", async () => {
    api.fetchReviewDirectory.mockResolvedValue({
      reviews: [
        {
          id: "rev-later",
          review_path: "/reviews/rev-later",
          repo_id: "repo-1",
          title: "Checkout change",
          auto_title: "Revision 1: checkout",
          title_is_custom: false,
          source_ref: "client:working_tree",
          range_mode: "working_tree",
          status: "open",
          revision_number: 0,
          revision_id: "rr-1",
          progress: {
            target_count: 3, reviewed: 2, changed_since_review: 0, needs_changes: 0, unreviewed: 0, unknown: 0, mechanical: 0, later: 1,
          },
          updated_at: "2026-09-15T20:00:00Z",
          created_at: "2026-09-15T19:00:00Z",
        },
      ],
      status: "open",
      query: "",
      next_cursor: "",
    });
    render(<ReviewDirectory />);

    expect(await screen.findByText("1 left")).toBeTruthy();
    expect(screen.queryByText("Ready to finish")).toBeNull();
  });

  it("loads the shared review directory and preserves the local fragment on review links", async () => {
    render(<ReviewDirectory />);

    await waitFor(() => expect(screen.getAllByText("Open checkout change").length).toBeGreaterThan(0));
    expect(api.fetchReviewDirectory).toHaveBeenCalledTimes(2);
    expect(api.fetchReviewDirectory).toHaveBeenNthCalledWith(1, expect.objectContaining({ view: undefined, summary: true }));
    expect(api.fetchReviewDirectory).toHaveBeenCalledWith({
      status: "open",
      query: "",
      cursor: "",
      limit: 50,
      view: "all",
      workflow: "",
      filter: "",
    });
    expect(screen.getAllByText("Working tree").length).toBe(2);
    expect(screen.queryByText("client:working_tree")).toBeNull();
    expect(screen.getByText("Workspace unavailable")).toBeTruthy();
    expect(screen.queryByText("repo_opaque_hash")).toBeNull();
    expect(screen.getByText("lemoncrow-dev")).toBeTruthy();
    expect(screen.getByText("agentic-review")).toBeTruthy();
    expect(screen.getByText("feat/agentic-review")).toBeTruthy();
    // The full path is on hover, not on the card.
    expect(screen.queryByText("/home/me/Projects/leanchain")).toBeNull();
    expect(screen.getByRole("link", { name: "Open Open checkout change" }).getAttribute("title")).toContain("/home/me/Projects/leanchain/lemoncrow-dev/.lc-worktrees/agentic-review");
    expect(screen.getByText("2 left")).toBeTruthy();
    expect(screen.getByText(/3\/5 reviewed/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Open checkout change" }).getAttribute("href")).toBe(
      "/reviews/rev-open#token",
    );
    expect(screen.getByTestId("review-directory")).toBeTruthy();
  });

  it("renders review links before expensive Inbox enrichment completes", async () => {
    const sample = await api.fetchReviewDirectory();
    api.fetchReviewDirectory.mockReset();
    let finishInbox: ((page: unknown) => void) | undefined;
    api.fetchReviewDirectory.mockImplementation((request: { view?: string }) => request.view
      ? new Promise((resolve) => { finishInbox = resolve; })
      : Promise.resolve(sample));

    render(<ReviewDirectory />);
    expect(await screen.findByText("Open checkout change")).toBeTruthy();
    expect(screen.queryByText("Loading reviews…")).toBeNull();
    expect(api.fetchReviewDirectory).toHaveBeenCalledTimes(2);
    expect(finishInbox).toBeDefined();

    finishInbox!({ ...sample, reviews: [{ ...sample.reviews[1], title: "Rich Inbox ready" }], total: 1 });
    expect(await screen.findByText("Rich Inbox ready")).toBeTruthy();
  });

  it("omits workspace facets that are unavailable instead of rendering placeholder dashes", async () => {
    api.fetchReviewDirectory.mockResolvedValue({
      reviews: [{
        id: "rev-hinted",
        review_path: "/reviews/rev-hinted",
        repo_id: "repo-hidden",
        title: "Hinted workspace",
        source_ref: "client:working_tree",
        range_mode: "working_tree",
        directory: "/tmp",
        project: "demo",
        worktree: "demo",
        worktree_root: "/tmp/demo",
        branch: "",
        status: "open",
        revision_number: 0,
        revision_id: "rr-hinted",
        progress: {
          target_count: 1, reviewed: 0, changed_since_review: 0, needs_changes: 0, unreviewed: 1, unknown: 0, mechanical: 0,
        },
        updated_at: "2026-09-15T20:05:00Z",
        created_at: "2026-09-15T19:05:00Z",
      }],
      status: "open",
      query: "",
      next_cursor: "",
    });

    render(<ReviewDirectory />);
    await waitFor(() => expect(screen.getByText("Hinted workspace")).toBeTruthy());

    expect(screen.getAllByText("demo").length).toBe(1);
    expect(screen.queryByText("branch")).toBeNull();
    expect(screen.queryByText("—")).toBeNull();
  });

  it("saves a custom title, shows the current automatic title beneath it, and can restore auto", async () => {
    api.updateReviewTitle.mockResolvedValueOnce({
      title: "My review",
      auto_title: "Revision 4: frontend changes · 5 files",
      title_is_custom: true,
    }).mockResolvedValueOnce({
      title: "Revision 4: frontend changes · 5 files",
      auto_title: "Revision 4: frontend changes · 5 files",
      title_is_custom: false,
    });
    render(<ReviewDirectory />);
    await screen.findByRole("button", { name: "Rename Open checkout change" });
    fireEvent.click(screen.getByRole("button", { name: "Rename Open checkout change" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Custom review title" }), { target: { value: "My review" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText("My review")).toBeTruthy());
    expect(api.updateReviewTitle).toHaveBeenCalledWith("repo-1", "rev-open", "My review");
    expect(screen.getByText("Auto: Revision 4: frontend changes · 5 files")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Rename My review" }));
    fireEvent.click(screen.getByRole("button", { name: "Use auto" }));
    await waitFor(() => expect(screen.getByText("Revision 4: frontend changes · 5 files")).toBeTruthy());
    expect(api.updateReviewTitle).toHaveBeenCalledWith("repo-1", "rev-open", "");
  });
  it("offers deletion only for archived reviews and requires confirmation", async () => {
    const page = await api.fetchReviewDirectory();
    page.reviews[0].status = "archived";
    page.reviews[1].status = "finished";
    api.fetchReviewDirectory.mockResolvedValue(page);
    api.deleteReview.mockResolvedValue({ deleted: true });
    render(<ReviewDirectory />);
    await userEvent.click(await screen.findByRole("button", { name: "More actions for Legacy change" }));
    const remove = await screen.findByRole("menuitem", { name: "Delete Legacy change" });
    expect(screen.queryByRole("menuitem", { name: "Delete Open checkout change" })).toBeNull();
    await userEvent.click(remove);
    expect(api.deleteReview).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("link", { name: "Open Legacy change" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "More actions for Legacy change" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete Legacy change" }));
    // Permanent delete names the Review exactly: a near miss keeps the button disabled.
    const confirmDelete = screen.getByRole("button", { name: "Delete review" });
    expect(confirmDelete).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Confirm the review to delete"), { target: { value: "Legacy" } });
    expect(confirmDelete).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Confirm the review to delete"), { target: { value: "Legacy change" } });
    expect(confirmDelete).not.toBeDisabled();
    fireEvent.click(confirmDelete);
    await waitFor(() => expect(screen.queryByRole("link", { name: "Open Legacy change" })).toBeNull());
    expect(api.deleteReview).toHaveBeenCalledWith("rev-legacy", "Legacy change");
    expect(screen.getByRole("link", { name: "Open Open checkout change" })).toHaveFocus();
  });

  it("keeps an archived review and allows retry when deletion fails", async () => {
    const page = await api.fetchReviewDirectory();
    page.reviews[0].status = "archived";
    page.reviews = [page.reviews[0]];
    api.fetchReviewDirectory.mockResolvedValue(page);
    api.deleteReview.mockRejectedValueOnce(new Error("Review was restored")).mockResolvedValueOnce({ deleted: true });
    render(<ReviewDirectory />);
    await userEvent.click(await screen.findByRole("button", { name: "More actions for Legacy change" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete Legacy change" }));
    fireEvent.change(screen.getByLabelText("Confirm the review to delete"), { target: { value: "rev-legacy" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete review" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Review was restored");
    expect(screen.getByLabelText("Open Legacy change")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete review" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.deleteReview).toHaveBeenCalledTimes(2);
    expect(api.deleteReview).toHaveBeenLastCalledWith("rev-legacy", "rev-legacy");
    expect(screen.getByRole("region", { name: "Reviews" })).toHaveFocus();
  });

  it("confirms a discard by naming pending feedback and the armed landing, then sends the acknowledgement", async () => {
    api.finishReview.mockRejectedValueOnce(discardRefusal()).mockResolvedValueOnce({});
    render(<ReviewDirectory />);
    await userEvent.click(await screen.findByRole("button", { name: "More actions for Open checkout change" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Archive Open checkout change" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("affects pending feedback and armed landing");
    expect(dialog).toHaveTextContent("Nothing is sent. The comments stay inside the discarded Review.");
    expect(dialog).toHaveTextContent("The cancellation stays owed until github confirms it.");
    expect(dialog).toHaveTextContent("until retention cleanup removes it later");
    expect(api.finishReview).toHaveBeenCalledTimes(1);
    expect(api.finishReview).toHaveBeenLastCalledWith("rev-open", "archived", "rr-3", undefined);

    const discard = screen.getByRole("button", { name: "Discard with pending feedback and armed landing" });
    expect(discard).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Confirm pending feedback"));
    expect(discard).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Confirm armed landing"));
    expect(discard).not.toBeDisabled();
    fireEvent.click(discard);

    await waitFor(() => expect(api.finishReview).toHaveBeenCalledTimes(2));
    expect(api.finishReview).toHaveBeenLastCalledWith("rev-open", "archived", "rr-3", {
      acknowledged_hazards: ["pending_feedback", "armed_landing"],
      hazard_digest: "digest-1",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("keeps the review when the discard confirmation is dismissed", async () => {
    api.finishReview.mockRejectedValueOnce(discardRefusal());
    render(<ReviewDirectory />);
    await userEvent.click(await screen.findByRole("button", { name: "More actions for Open checkout change" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Archive Open checkout change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Keep review" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.finishReview).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "Open Open checkout change" })).toBeTruthy();
  });

  it("selects Inbox views and workflow states and renders the server's separate signals honestly", async () => {
    const page = await api.fetchReviewDirectory();
    page.reviews[1] = { ...page.reviews[1], ...signals() };
    api.fetchReviewDirectory.mockResolvedValue({
      ...page,
      mode: "views",
      counts: { all: 2, to_review: 2, assigned_to_my_teams: 0, authored_by_me: 0, participated: 1, waiting_on_me: 1, ready: 0, blocked: 0 },
      total: 2,
      teams_supported: false,
    });
    render(<ReviewDirectory />);

    expect(await screen.findByText("Needs decision")).toHaveAttribute(
      "title",
      "Review complete; 1 readiness condition(s) not satisfied",
    );
    expect(screen.getByText("high priority")).toBeTruthy();
    expect(screen.getByText("Your turn: Decide whether to merge")).toBeTruthy();
    // Full readiness remains inspectable without turning every row into a state dashboard.
    expect(screen.getByTestId("inbox-readiness-detail")).toHaveTextContent("Your review complete");
    expect(screen.getByTestId("inbox-readiness-detail")).not.toHaveTextContent("Team review");
    expect(screen.getByTestId("inbox-readiness-detail")).toHaveTextContent("Merge not ready");
    expect(screen.getByTestId("inbox-attention").getAttribute("title")).toBe(
      "You own the next action +10: Decide whether to merge\nImpact +13: priority high, criticality high",
    );
    // The legacy row (no signals) still renders its own action.
    expect(screen.getByText("1 left")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review inbox"));
    expect(screen.queryByRole("option", { name: /Assigned to my teams/ })).toBeNull();
    await userEvent.click(screen.getByRole("option", { name: "To review · 2" }));
    await waitFor(() => expect(api.fetchReviewDirectory).toHaveBeenLastCalledWith(expect.objectContaining({ view: "to_review", cursor: "" })));

    await userEvent.click(screen.getByText("Filters"));
    await userEvent.click(screen.getByLabelText("Workflow state"));
    await userEvent.click(screen.getByRole("option", { name: "Ready to merge" }));
    await waitFor(() => expect(api.fetchReviewDirectory).toHaveBeenLastCalledWith(
      expect.objectContaining({ view: "to_review", workflow: "ready_to_merge" }),
    ));
  });

  it("saves the current view as a filter, applies it as saved and deletes it", async () => {
    api.fetchInboxFilters
      .mockResolvedValueOnce({ filters: [], limit: 50 })
      .mockResolvedValueOnce({ filters: [SAVED], limit: 50 })
      .mockResolvedValue({ filters: [], limit: 50 });
    api.createInboxFilter.mockResolvedValue({ filter: SAVED });
    api.deleteInboxFilter.mockResolvedValue({ deleted: true, id: SAVED.id });
    render(<ReviewDirectory />);
    await screen.findAllByText("Open checkout change");

    await userEvent.click(screen.getByText("Filters"));
    await userEvent.click(screen.getByLabelText("Workflow state"));
    await userEvent.click(screen.getByRole("option", { name: "Needs decision" }));
    await userEvent.click(screen.getByRole("button", { name: "Save view" }));
    await userEvent.type(screen.getByLabelText("Saved filter name"), "Needs a decision");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.createInboxFilter).toHaveBeenCalledWith("Needs a decision", {
      view: "all",
      status: "open",
      workflow_states: ["needs_decision"],
      query: "",
    }));
    // Applied as saved: the request names the filter and its own controls go neutral.
    await waitFor(() => expect(api.fetchReviewDirectory).toHaveBeenLastCalledWith(expect.objectContaining({
      filter: SAVED.id, view: "all", status: "all", workflow: "", query: "",
    })));

    await userEvent.click(screen.getByRole("button", { name: "Delete saved filter" }));
    await waitFor(() => expect(api.deleteInboxFilter).toHaveBeenCalledWith(SAVED.id));
    await waitFor(() => expect(api.fetchReviewDirectory).toHaveBeenLastCalledWith(expect.objectContaining({ filter: "" })));
    expect(screen.queryByRole("button", { name: "Delete saved filter" })).toBeNull();
  });

  it("shows a refused saved filter without losing the list", async () => {
    api.createInboxFilter.mockRejectedValue(new Error("a saved filter with this name already exists"));
    render(<ReviewDirectory />);
    await screen.findAllByText("Open checkout change");
    await userEvent.click(screen.getByText("Filters"));
    await userEvent.click(screen.getByRole("button", { name: "Save view" }));
    await userEvent.type(screen.getByLabelText("Saved filter name"), "Mine");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("a saved filter with this name already exists");
    expect(screen.getAllByText("Open checkout change").length).toBeGreaterThan(0);
  });

  it("shows an owed provider cancellation on an archived review until the provider confirms", async () => {
    const page = await api.fetchReviewDirectory();
    page.reviews[0].status = "archived";
    page.reviews[0].owed_cancellation = {
      provider: "github",
      landing_status: "delegated",
      reason: "discard",
      detail: "Cancellation FAILED: GitHub is unavailable",
      error: "",
    };
    api.fetchReviewDirectory.mockResolvedValue(page);
    render(<ReviewDirectory />);
    expect(await screen.findByText(/github cancellation owed/)).toHaveAttribute(
      "title",
      "Cancellation FAILED: GitHub is unavailable",
    );
  });
});

describe("InboxRowSignals blocking actions", () => {
  const blocked = {
    state: "not_ready" as const, reason: "Blocked: Merged result fails checks · Waiting for checks", evaluated_at: "2026-10-05T09:00:00Z",
    valid_until: "", blocker_count: 2, scm_blockers: 0, pending_checks: 1, failed_checks: 0, scm_conditions: 0,
    blocking: [
      { id: "merge-result", owner: "lemoncrow", state: "fail", action: "Merged result fails checks" },
      { id: "org-policy:check:ci", owner: "lemoncrow", state: "pending", action: "Waiting for checks" },
    ],
  };
  const idle = { owner_ids: [], for_me: false, my_reasons: [], reasons: [] };

  it("names the exact blocking actions when nobody owns a next action", () => {
    render(<InboxRowSignals signals={signals({ readiness: blocked, next_action: idle })} />);
    expect(screen.getByTestId("inbox-blocking").textContent).toBe("Blocked: Merged result fails checks · Waiting for checks");
  });

  it("keeps one line: a next action wins, and an older record without actions shows none", () => {
    const { rerender } = render(<InboxRowSignals signals={signals({ readiness: blocked })} />);
    expect(screen.queryByTestId("inbox-blocking")).toBeNull();
    rerender(<InboxRowSignals signals={signals({ next_action: idle })} />);
    expect(screen.queryByTestId("inbox-blocking")).toBeNull();
  });
});

describe("inboxAxesText", () => {
  it("shows only the states that apply: no team or merge line on a solo local review", () => {
    const axes = {
      your_review: { state: "not_started" },
      team_review: { state: "not_applicable" },
      scm: { state: "not_connected" },
    } as InboxSignals["axes"];
    expect(inboxAxesText(axes)).toEqual(["Your review not started"]);
    const hosted = { ...axes, team_review: { state: "waiting" }, scm: { state: "ready" } } as InboxSignals["axes"];
    expect(inboxAxesText(hosted)).toEqual(["Your review not started", "Team review waiting", "Merge ready"]);
  });

  it("names who a team review waits on by stored display name, else by the id", () => {
    const waiting = {
      your_review: { state: "complete" },
      team_review: { state: "waiting", waiting_on: ["agent_review", "user_sam"] },
      scm: { state: "not_connected" },
    } as InboxSignals["axes"];
    expect(inboxAxesText(waiting)).toEqual(["Your review complete", "Team review waiting on agent review, sam"]);
    expect(inboxAxesText(waiting, { agent_review: "Review agent" })).toEqual([
      "Your review complete",
      "Team review waiting on Review agent, sam",
    ]);
  });
});

describe("InboxRowSignals names", () => {
  const waiting = {
    owner_ids: ["agent_billing", "user_sam"],
    for_me: false,
    my_reasons: [],
    reasons: [],
  };

  it("reads the row's stored display names for the next-action owners, falling back to the id", () => {
    const row = signals({ next_action: waiting, principal_names: { agent_billing: "Billing agent" } });
    expect(nextActionText(row)).toBe("Waiting on Billing agent, sam");
    render(<InboxRowSignals signals={row} />);
    expect(screen.getByText("Waiting on Billing agent, sam")).toBeTruthy();
  });

  it("is unchanged for a row without names", () => {
    expect(nextActionText(signals({ next_action: waiting }))).toBe("Waiting on agent billing, sam");
  });
});
