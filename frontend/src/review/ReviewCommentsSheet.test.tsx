import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewCommentsSheet from "./ReviewCommentsSheet";
import type { Annotation } from "./types";

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-1",
    review_id: "rev-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: "Please explain this branch",
    created_by: "alice",
    created_by_actor: "human",
    created_at: "2026-09-15T20:00:00Z",
    updated_at: "2026-09-15T20:00:00Z",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path: "src/app.py",
    side: "new",
    start_line: 12,
    end_line: 12,
    unit_key: "sym:app",
    symbol: "run",
    origin_symbol: "run",
    ...overrides,
  };
}

describe("ReviewCommentsSheet", () => {
  it("shows role and producer metadata in the existing filter, without orphaning Change discussion", async () => {
    const user = userEvent.setup();
    render(<ReviewCommentsSheet
      annotations={[
        annotation({ id: "plan", source: "author", created_by_actor: "agent", agent_role: "plan",
          agent_host: "claude", agent_model: "opus", source_id: "planner-session",
          source_message_id: "original-message", parent_session_id: "parent",
          comment_scope: "change", revision_id: "", anchored: false, path: "", start_line: 0, end_line: 0,
          body: "Plan before implementation" }),
        annotation({ id: "code", source: "author", agent_role: "code", agent_host: "codex", body: "Implementation complete" }),
        annotation({ id: "review", source: "ai_review", agent_role: "review", body: "Review finding" }),
      ]}
      onClose={vi.fn()} onJump={vi.fn()}
    />);
    expect(screen.getByText("Agent(plan)")).toBeVisible();
    expect(screen.getByText("Claude Code · opus")).toHaveAttribute("title", expect.stringContaining("planner-session"));
    expect(screen.getByText("Claude Code · opus")).toHaveAttribute("title", expect.stringContaining("original-message"));
    expect(screen.getByText("Draft discussion")).toBeVisible();
    expect(screen.queryByText("Not anchored in the current Snapshot")).toBeNull();
    expect(screen.getAllByText("Show in review")).toHaveLength(2);
    await user.click(screen.getByLabelText("Comment source: All sources"));
    await user.click(screen.getByRole("menuitemradio", { name: /^Agent\(plan\)/ }));
    expect(screen.getByText("Plan before implementation")).toBeVisible();
    expect(screen.queryByText("Implementation complete")).toBeNull();
    expect(screen.queryByText("Review finding")).toBeNull();
    await user.click(screen.getByRole("tab", { name: "Orphaned" }));
    expect(screen.queryByText("Plan before implementation")).toBeNull();
  });

  it("filters Human, AI, and LemonCrow independently from comment status", async () => {
    const user = userEvent.setup();
    render(
      <ReviewCommentsSheet
        annotations={[
          annotation({ id: "human", source: "human", body: "Human note" }),
          annotation({ id: "ai-review", source: "ai_review", created_by: "review-agent", body: "AI review note" }),
          annotation({ id: "author", source: "author", created_by: "coding-agent", body: "AI author reply" }),
          annotation({ id: "lc", source: "lemoncrow", created_by: "lemoncrow", body: "LemonCrow note" }),
          annotation({ id: "resolved-ai", source: "ai_review", state: "resolved", body: "Resolved AI note" }),
        ]}
        onClose={vi.fn()}
        onJump={vi.fn()}
      />,
    );

    expect(screen.getByText("Human note")).toBeTruthy();
    expect(screen.getByText("AI review note")).toBeTruthy();
    expect(screen.getByText("AI author reply")).toBeTruthy();
    expect(screen.getByText("LemonCrow note")).toBeTruthy();
    expect(screen.queryByText("Resolved AI note")).toBeNull();

    await user.click(screen.getByLabelText("Comment source: All sources"));
    await user.click(screen.getByRole("menuitemradio", { name: /Human/ }));
    expect(screen.getByText("Human note")).toBeTruthy();
    expect(screen.queryByText("AI review note")).toBeNull();
    expect(screen.queryByText("LemonCrow note")).toBeNull();

    await user.click(screen.getByLabelText("Comment source: Human"));
    await user.click(screen.getByRole("menuitemradio", { name: /^AI/ }));
    expect(screen.getByText("AI review note")).toBeTruthy();
    expect(screen.getByText("AI author reply")).toBeTruthy();
    expect(screen.queryByText("Human note")).toBeNull();
    expect(screen.queryByText("Resolved AI note")).toBeNull();

    await user.click(screen.getByRole("tab", { name: "Resolved" }));
    expect(screen.getByText("Resolved AI note")).toBeTruthy();
    expect(screen.queryByText("AI review note")).toBeNull();

    await user.click(screen.getByLabelText("Comment source: AI"));
    await user.click(screen.getByRole("menuitemradio", { name: /LemonCrow/ }));
    expect(screen.getByText("No matching comments.")).toBeTruthy();
  });

  it("moves keyboard focus and the visible comments together across filters", async () => {
    const user = userEvent.setup();
    render(<ReviewCommentsSheet
      annotations={[annotation(), annotation({ id: "resolved", state: "resolved", body: "Verified fix" })]}
      onClose={vi.fn()}
      onJump={vi.fn()}
    />);
    screen.getByRole("tab", { name: "Open" }).focus();
    await user.keyboard("{ArrowRight}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Resolved" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Resolved" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Verified fix")).toBeVisible();
    expect(screen.queryByText("Please explain this branch")).toBeNull();
    await user.keyboard("{End}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Open" })).toHaveFocus();
    expect(screen.getByText("Please explain this branch")).toBeVisible();
  });

  it("shows the coding-agent handoff lifecycle and jumps only current anchored comments", async () => {
    const user = userEvent.setup();
    const onJump = vi.fn();
    render(
      <ReviewCommentsSheet
        annotations={[
          annotation(),
          annotation({ id: "ann-2", body: "old concern", state: "orphaned", anchored: false, path: "src/old.py" }),
          annotation({ id: "reply-1", parent_id: "ann-1", body: "author reply", created_by: "bob" }),
        ]}
        feedbackStatus={{ open_total: 2, unpublished: 1, published: 0, in_flight: 0, addressed: 1 }}
        feedbackDelivery={{
          supported: true,
          host: "claude",
          session_id: "session-1",
          target_ref: "claude:session-1",
          label: "Claude",
          reason: "",
        }}
        onClose={vi.fn()}
        onJump={onJump}
      />,
    );

    expect(screen.getByText("Please explain this branch")).toBeTruthy();
    expect(screen.getByText("1 not sent · 1 ready for re-review")).toBeTruthy();
    expect(screen.getByText(/send to Claude → agent fixes and verifies → marks addressed/)).toBeTruthy();
    expect(screen.getByText(/1 reply/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Show in review" }));
    expect(onJump).toHaveBeenCalledWith(expect.objectContaining({ id: "ann-1" }));

    await user.click(screen.getByRole("tab", { name: "Orphaned" }));
    expect(screen.getByText("old concern")).toBeTruthy();
    expect(screen.getByText("Not anchored in the current Snapshot")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Show in review" })).toBeNull();
  });

  it("resolves and reopens an orphaned comment without anchoring it", async () => {
    const user = userEvent.setup();
    const onSet = vi.fn();
    const orphan = annotation({ id: "ann-2", body: "old concern", state: "orphaned", anchored: false, path: "src/old.py", source: "ai_review", created_by: "review-agent" });
    const { rerender } = render(
      <ReviewCommentsSheet annotations={[orphan]} onClose={vi.fn()} onJump={vi.fn()} onSetAnnotationState={onSet} />,
    );
    await user.click(screen.getByRole("tab", { name: "Orphaned" }));
    await user.click(screen.getByRole("button", { name: "Resolve unanchored comment" }));
    expect(onSet).toHaveBeenCalledWith("ann-2", "resolved");

    rerender(
      <ReviewCommentsSheet
        annotations={[{ ...orphan, state: "resolved" }]}
        onClose={vi.fn()}
        onJump={vi.fn()}
        onSetAnnotationState={onSet}
      />,
    );
    await user.click(screen.getByRole("tab", { name: "Orphaned" }));
    expect(screen.getByText("Not anchored in the current Snapshot")).toBeTruthy();
    expect(screen.getByText("review-agent")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Reopen unanchored comment" }));
    expect(onSet).toHaveBeenLastCalledWith("ann-2", "open");
    expect(screen.queryByRole("button", { name: "Show in review" })).toBeNull();
  });

  it("counts each status tab and each source within the selected status", async () => {
    const user = userEvent.setup();
    render(
      <ReviewCommentsSheet
        annotations={[
          annotation({ id: "h-open", source: "human", body: "Human open" }),
          annotation({ id: "ai-open", source: "ai_review", created_by: "review-agent", body: "AI open" }),
          annotation({ id: "h-addressed", source: "human", author_response: "addressed", body: "Addressed by the agent" }),
          annotation({ id: "lc-resolved", source: "lemoncrow", state: "resolved", body: "LemonCrow resolved" }),
          annotation({ id: "author-orphan", source: "author", state: "orphaned", anchored: false, body: "Author orphan" }),
          // A reply is part of its thread, never its own row or count.
          annotation({ id: "reply", parent_id: "h-open", source: "human", body: "A reply" }),
        ]}
        onClose={vi.fn()}
        onJump={vi.fn()}
      />,
    );

    const count = (name: string) => screen.getByRole("tab", { name }).textContent?.replace(name, "");
    expect([count("Open"), count("Re-review"), count("Resolved"), count("Orphaned"), count("All")]).toEqual(["3", "1", "1", "1", "5"]);
    expect(screen.getByText("3 open · 1 need re-review")).toBeTruthy();
    expect(screen.getByText(/1 reply/)).toBeTruthy();

    // Source counts follow the selected status: Open holds 2 human, 1 AI, 0 LemonCrow.
    const sourceCounts = async () => {
      await user.click(screen.getByLabelText(/^Comment source:/));
      const menu = await screen.findByRole("menu");
      const counts = ["All sources", "Human", "AI", "LemonCrow"].map((label) =>
        within(menu).getByRole("menuitemradio", { name: new RegExp(`^${label}`) }).textContent?.replace(label, ""));
      await user.keyboard("{Escape}");
      return counts;
    };
    expect(await sourceCounts()).toEqual(["3", "2", "1", "0"]);
    expect(screen.getByLabelText("Comment source: All sources").textContent).toContain("3");

    await user.click(screen.getByRole("tab", { name: "All" }));
    expect(await sourceCounts()).toEqual(["5", "2", "2", "1"]);
  });

  it("the Re-review tab holds only addressed comments that still await the human", async () => {
    const user = userEvent.setup();
    render(
      <ReviewCommentsSheet
        annotations={[
          annotation({ id: "open", body: "Still open" }),
          annotation({ id: "addressed", author_response: "addressed", author_addressed_revision_number: 2, body: "Agent fixed this" }),
          annotation({ id: "addressed-resolved", author_response: "addressed", state: "resolved", body: "Already verified" }),
        ]}
        feedbackDelivery={{ supported: true, host: "claude", session_id: "s", target_ref: "claude:s", label: "Claude", reason: "" }}
        onClose={vi.fn()}
        onJump={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("tab", { name: "Re-review" }));
    expect(screen.getByText("Agent fixed this")).toBeTruthy();
    expect(screen.queryByText("Still open")).toBeNull();
    expect(screen.queryByText("Already verified")).toBeNull();
    expect(screen.getByText("re-review")).toBeTruthy();
    expect(screen.getByText(/Coding agent marked this addressed\. Addressed in Snapshot 2\./)).toBeTruthy();
    expect(screen.getByText(/stays open until you re-review and resolve it/)).toBeTruthy();
  });

  it("opens on Re-review when the only comments awaiting the reviewer are addressed ones whose lines were rewritten", () => {
    const orphanedAddressed = (id: string, body: string) => annotation({
      id, body, state: "orphaned", anchored: false, author_response: "addressed", author_response_source_id: "codex-session",
    });
    const { unmount } = render(
      <ReviewCommentsSheet
        annotations={[orphanedAddressed("a1", "Rename this"), orphanedAddressed("a2", "Guard the null"), annotation({ id: "done", state: "resolved", body: "Verified" })]}
        onClose={vi.fn()}
        onJump={vi.fn()}
        onSetAnnotationState={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: "Re-review" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Rename this")).toBeTruthy();
    expect(screen.getByText("Guard the null")).toBeTruthy();
    // Actionable without a faked anchor: resolvable here, never "Show in review".
    expect(screen.getAllByRole("button", { name: "Resolve unanchored comment" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Show in review" })).toBeNull();
    unmount();

    // A caller that names the tab wins; plain open comments keep Open first.
    render(
      <ReviewCommentsSheet
        annotations={[annotation({ id: "open", body: "Still open" }), orphanedAddressed("a1", "Rename this")]}
        initialFilter="rereview"
        onClose={vi.fn()}
        onJump={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: "Re-review" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Rename this")).toBeTruthy();
    expect(screen.queryByText("Still open")).toBeNull();
  });

  it("attributes an addressed claim to the agent session that made it, whatever the current delivery capability", () => {
    render(
      <ReviewCommentsSheet
        annotations={[
          annotation({ id: "agent", author_response: "addressed", author_response_source_id: "codex-session", body: "Agent fixed this" }),
          annotation({ id: "legacy", author_response: "addressed", body: "Claimed without a session" }),
        ]}
        // Resolving another comment re-derives capability for the new Snapshot,
        // which can no longer be sent to an exact session.
        feedbackDelivery={{ supported: false, host: "", session_id: "", target_ref: "", label: "", reason: "exact primary author session is unavailable" }}
        initialFilter="rereview"
        onClose={vi.fn()}
        onJump={vi.fn()}
      />,
    );
    const card = (text: string) => screen.getByText(text).closest("article") as HTMLElement;
    expect(within(card("Agent fixed this")).getByText(/Coding agent marked this addressed\./)).toBeTruthy();
    expect(within(card("Claimed without a session")).getByText(/Author says addressed\./)).toBeTruthy();
  });

  it("wraps the status tabs with the arrow keys and jumps with Home and End", async () => {
    const user = userEvent.setup();
    render(<ReviewCommentsSheet annotations={[annotation()]} onClose={vi.fn()} onJump={vi.fn()} />);
    screen.getByRole("tab", { name: "Open" }).focus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "All" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "All" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Open" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Re-review" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "All" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Please explain this branch")).toBeVisible();
  });

  it("offers no resolve action when judgments are read-only", async () => {
    const user = userEvent.setup();
    render(
      <ReviewCommentsSheet
        annotations={[annotation({ id: "ann-2", state: "orphaned", anchored: false })]}
        onClose={vi.fn()}
        onJump={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("tab", { name: "Orphaned" }));
    expect(screen.queryByRole("button", { name: /unanchored comment/ })).toBeNull();
  });
});
