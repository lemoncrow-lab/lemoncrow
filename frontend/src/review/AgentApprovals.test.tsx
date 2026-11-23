/**
 * Risky agent actions in the Brief and Activity (Phase 12 item 14): observed
 * rows say whether a human pre-approved exactly them, and a pending request is
 * approved once or denied in place.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentActionList, AgentApprovalPanel } from "./AgentActions";
import type { AgentApproval } from "./agentApprovalsApi";
import { ReviewStateLine, type ReviewStateLineProps } from "./ReviewStateLine";

const approvalsApi = vi.hoisted(() => ({
  fetchAgentApprovals: vi.fn(),
  decideAgentApproval: vi.fn(),
}));
vi.mock("./agentApprovalsApi", () => approvalsApi);

function approval(overrides: Partial<AgentApproval> = {}): AgentApproval {
  return {
    id: "apr_1",
    status: "pending",
    used: false,
    risk_class: "git_push",
    risk_label: "Git push",
    classes: ["git_push"],
    kind: "command",
    action: "git push origin main",
    action_digest: "d".repeat(64),
    cwd: "/work/repo",
    host: "claude",
    session_id: "author",
    requested_by: "agent:claude:author",
    requested_at: "2026-10-04T10:00:00+00:00",
    expires_at: "2026-10-04T10:30:00+00:00",
    decided_by: "",
    decided_at: "",
    scope: "",
    pattern: "",
    valid_until: "",
    claimed_at: "",
    note: "",
    revision_id: "rrv-1",
    ...overrides,
  };
}

function listing(approvals: AgentApproval[]) {
  return {
    approvals,
    total: approvals.length,
    policy: null,
    labels: {},
    limits: { min_ttl_seconds: 60, max_ttl_seconds: 86400, default_ttl_seconds: 900 },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("observed agent actions", () => {
  it("say pre-approved only when the server matched the exact action", () => {
    render(
      <AgentActionList
        rows={[
          { category: "git_push", summary: "Git push: git push origin main", host: "claude", at: "", approval: { status: "pre_approved", approval_id: "apr_1", decided_by: "alice", decided_at: "", scope: "once", pattern: "" } },
          { category: "git_push", summary: "Git push: git push --force origin main", host: "claude", at: "", approval: { status: "unapproved" } },
          { category: "network", summary: "Network: curl", host: "claude", at: "", approval: null },
        ]}
      />,
    );
    const rows = within(screen.getByTestId("agent-actions")).getAllByRole("listitem");
    expect(rows[0].textContent).toContain("Pre-approved by alice");
    expect(rows[1].textContent).toContain("Not approved");
    expect(rows[2].textContent).not.toMatch(/approved/i);
  });
});

describe("AgentApprovalPanel", () => {
  it("renders nothing until an agent asks", async () => {
    approvalsApi.fetchAgentApprovals.mockResolvedValue(listing([]));
    const { container } = render(<AgentApprovalPanel reviewId="review-1" />);
    await waitFor(() => expect(approvalsApi.fetchAgentApprovals).toHaveBeenCalledWith("review-1"));
    expect(container.textContent).toBe("");
  });

  it("approves exactly the shown action once and shows the decision", async () => {
    const pending = approval();
    approvalsApi.fetchAgentApprovals.mockResolvedValue(listing([pending]));
    const approved = approval({ status: "approved", decided_by: "local", scope: "once", valid_until: "2026-10-04T10:15:00+00:00" });
    approvalsApi.decideAgentApproval.mockImplementation(async () => {
      approvalsApi.fetchAgentApprovals.mockResolvedValue(listing([approved]));
      return { approval: approved, development_recorded: true };
    });
    render(<AgentApprovalPanel reviewId="review-1" />);
    const row = await screen.findByTestId("agent-approval");
    expect(row.textContent).toContain("Waiting for you");
    expect(row.textContent).toContain("git push origin main");
    expect(screen.getByTestId("agent-approval-panel").textContent).toContain("Agent actions waiting for approval · 1");
    await userEvent.click(within(row).getByRole("button", { name: "Approve once" }));
    expect(approvalsApi.decideAgentApproval).toHaveBeenCalledWith("review-1", pending, { decision: "approve" });
    await waitFor(() => expect(screen.getByTestId("agent-approval").textContent).toContain("Approved"));
    expect(screen.getByTestId("agent-approval").textContent).toContain("by local · once");
    expect(screen.queryByRole("button", { name: "Approve once" })).toBeNull();
  });

  it("shows the server's refusal and keeps the request pending", async () => {
    approvalsApi.fetchAgentApprovals.mockResolvedValue(listing([approval()]));
    approvalsApi.decideAgentApproval.mockRejectedValue(
      new Error("only a signed-in human can approve or deny an agent action; agent and service credentials are refused"),
    );
    render(<AgentApprovalPanel reviewId="review-1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Deny" }));
    expect((await screen.findByRole("alert")).textContent).toContain("only a signed-in human");
    expect(screen.getByTestId("agent-approval").textContent).toContain("Waiting for you");
  });

  it("offers no decision on a read-only Review", async () => {
    approvalsApi.fetchAgentApprovals.mockResolvedValue(listing([approval()]));
    render(<AgentApprovalPanel reviewId="review-1" readOnly />);
    await screen.findByTestId("agent-approval");
    expect(screen.queryByRole("button", { name: "Approve once" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
  });

  it("lists denied and expired requests without actions", async () => {
    approvalsApi.fetchAgentApprovals.mockResolvedValue(
      listing([approval({ id: "apr_2", status: "denied", decided_by: "alice", note: "not on Friday" }), approval({ status: "expired" })]),
    );
    render(<AgentApprovalPanel reviewId="review-1" />);
    const rows = await screen.findAllByTestId("agent-approval");
    expect(rows[0].textContent).toContain("Denied");
    expect(rows[0].textContent).toContain("by alice · not on Friday");
    expect(rows[1].textContent).toContain("Expired");
    expect(screen.queryByRole("button", { name: "Approve once" })).toBeNull();
    expect(screen.getByTestId("agent-approval-panel").textContent).toContain("Agent action approvals");
  });
});

describe("the Reader's state line names waiting agent requests", () => {
  function lineProps(overrides: Partial<ReviewStateLineProps> = {}): ReviewStateLineProps {
    return {
      reviewId: "review-1", reviewRef: "review-1", forcedReadOnly: false, busy: false, accessLost: "", revisionAdvanced: false,
      runningDraft: false, historical: false, revisionNumber: 1, latestRevisionNumber: 1, status: "open", contextFailures: [],
      sourceMoved: null, onWrite: vi.fn(async () => {}), onOpenNewer: vi.fn(), onRetryContext: vi.fn(),
      onSnapshotNow: vi.fn(async () => ""), onDismissSource: vi.fn(),
      ...overrides,
    };
  }

  it("counts the pending requests and opens the approval panel on them", async () => {
    approvalsApi.fetchAgentApprovals.mockResolvedValue(
      listing([approval(), approval({ id: "apr_2", action: "rm -rf build" }), approval({ id: "apr_3", status: "approved" })]),
    );
    render(<ReviewStateLine {...lineProps()} />);
    const line = await screen.findByTestId("review-state-line");
    expect(line).toHaveTextContent("2 agent requests wait for you");
    expect(approvalsApi.fetchAgentApprovals).toHaveBeenCalledWith("review-1");
    await userEvent.click(within(line).getByRole("button", { name: "Approve or deny" }));
    const sheet = await screen.findByRole("dialog", { name: "Agent requests" });
    expect(within(sheet).getAllByRole("button", { name: "Approve once" })).toHaveLength(2);
    expect(within(sheet).getByText("rm -rf build")).toBeTruthy();
  });

  it("shows no row when nothing waits", async () => {
    approvalsApi.fetchAgentApprovals.mockResolvedValue(listing([approval({ status: "denied" })]));
    render(<ReviewStateLine {...lineProps()} />);
    await waitFor(() => expect(approvalsApi.fetchAgentApprovals).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByTestId("review-state-line")).toBeNull();
  });

  it("lets a host that made the Review read-only see the requests but not decide them", async () => {
    approvalsApi.fetchAgentApprovals.mockResolvedValue(listing([approval()]));
    render(<ReviewStateLine {...lineProps({ forcedReadOnly: true })} />);
    const line = await screen.findByTestId("review-state-line");
    expect(line).toHaveTextContent("1 agent request waits for you");
    await userEvent.click(within(line).getByRole("button", { name: "Approve or deny" }));
    const sheet = await screen.findByRole("dialog", { name: "Agent requests" });
    expect(within(sheet).getByTestId("agent-approval")).toBeTruthy();
    expect(within(sheet).queryByRole("button", { name: "Approve once" })).toBeNull();
  });
});
