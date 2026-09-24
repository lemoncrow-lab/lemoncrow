import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReaderHeader from "./ReaderHeader";
import { fallbackReviewEnvironment } from "./reviewEnvironment";
import type { ReviewOverview, ReviewProgress } from "./types";

const overview = {
  session: { id: "abcdef12abcdef127abcdef12abcdef12", ref: "r/abcdef12abcdef127abcdef12abcdef12", status: "open", actor_type: "agent", repo_root: "/server/scratch" },
  revision: { revision_number: 4, provenance_model: "claude-opus-5", provenance_certainty: "heuristic" },
  title: "Hosted review",
  change_story: [],
} as unknown as ReviewOverview;

const progress = {
  target_count: 5,
  reviewed: 2,
  changed_since_review: 1,
  needs_changes: 0,
  unreviewed: 2,
  unknown: 0,
  mechanical: 0,
} as ReviewProgress;

function renderHeader(
  authorshipOverride?: { prefix: string; label: string },
  overrides: Partial<React.ComponentProps<typeof ReaderHeader>> = {},
) {
  render(
    <ReaderHeader
      overview={overview}
      progress={progress}
      diffStyle="split"
      diffOverflow="scroll"
      order="recommended"
      focusMode={false}
      contextOpen={false}
      busy={false}
      readOnly={false}
      revisionAdvanced={false}
      codeTheme="github-dark"
      commentCount={3}
      openCommentCount={1}
      primaryAction={{
        kind: "review_changed",
        label: "Re-review changed area",
        detail: "Changed since your last review",
        disabled: false,
        targetId: "target-1",
      }}
      authorshipOverride={authorshipOverride}
      historical={false}
      onCodeThemeChange={vi.fn()}
      onShowDirectory={vi.fn()}
      onShowComments={vi.fn()}
      onShowHistory={vi.fn()}
      onPrimaryAction={vi.fn()}
      onDownloadPatch={vi.fn()}
      onFinish={vi.fn()}
      onRefresh={vi.fn()}
      onOrderChange={vi.fn()}
      onDiffStyleChange={vi.fn()}
      onToggleDiffOverflow={vi.fn()}
      onToggleContext={vi.fn()}
      onToggleFocus={vi.fn()}
      onShowShortcuts={vi.fn()}
      onShowOverview={vi.fn()}
      {...overrides}
    />,
  );
}

describe("ReaderHeader keyboard behavior", () => {
  it("contains product-feedback focus and restores it to the trigger on Escape", async () => {
    renderHeader();
    await userEvent.click(screen.getByLabelText("Review actions"));
    const trigger = screen.getByRole("button", { name: "Send LemonCrow feedback" });
    await userEvent.click(trigger);

    expect(screen.getByRole("dialog", { name: "LemonCrow feedback" })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Feedback" }));
    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "LemonCrow feedback" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
});

describe("ReaderHeader diff layout preference", () => {
  it("offers Auto, Split, and Unified and sends the explicit user preference", async () => {
    const onDiffStyleChange = vi.fn();
    renderHeader(undefined, { diffStyle: "auto", onDiffStyleChange });

    await userEvent.click(screen.getByLabelText("Review actions"));
    const select = screen.getByLabelText("Diff view");
    expect((select as HTMLSelectElement).value).toBe("auto");
    expect(within(select).getByRole("option", { name: "Auto · responsive" })).toBeTruthy();
    expect(within(select).getByRole("option", { name: "Split" })).toBeTruthy();
    expect(within(select).getByRole("option", { name: "Unified" })).toBeTruthy();

    await userEvent.selectOptions(select, "unified");
    expect(onDiffStyleChange).toHaveBeenCalledWith("unified");
  });
});

describe("ReaderHeader primary action", () => {
  it("renders and executes the state-machine action descriptor", async () => {
    const onPrimaryAction = vi.fn();
    renderHeader(undefined, {
      primaryAction: {
        kind: "send_feedback",
        label: "Send 2 to Claude…",
        detail: "Preview and send all unsent review comments together to the exact Claude coding session.",
        disabled: false,
      },
      onPrimaryAction,
    });

    const action = screen.getByRole("button", { name: "Send 2 to Claude…" });
    expect(action.getAttribute("title")).toContain("exact Claude coding session");
    await userEvent.click(action);
    expect(onPrimaryAction).toHaveBeenCalledTimes(1);
  });

  it("shows the active mutation instead of a stale disabled action label", () => {
    renderHeader(undefined, {
      busy: true,
      busyLabel: "Updating review…",
      primaryAction: {
        kind: "review_target",
        label: "Review next area",
        detail: "Continue reviewing the current immutable revision.",
        disabled: false,
        targetId: "t-1",
      },
    });
    const updating = screen.getByRole("button", { name: "Updating review…" }) as HTMLButtonElement;
    expect(updating.disabled).toBe(true);
  });

  it("renders non-actionable workflow states as disabled", () => {
    renderHeader(undefined, {
      primaryAction: {
        kind: "delivery_pending",
        label: "Delivery pending",
        detail: "This handoff is not safe to retry automatically.",
        disabled: true,
      },
    });
    const pending = screen.getByRole("button", { name: "Delivery pending" }) as HTMLButtonElement;
    expect(pending.disabled).toBe(true);
  });

  it("puts an apply-ready review patch in the actions menu", async () => {
    const onDownloadPatch = vi.fn();
    renderHeader(undefined, { onDownloadPatch });

    await userEvent.click(screen.getByLabelText("Review actions"));
    const download = screen.getByRole("button", { name: /Download review patch/i });
    expect(download.textContent).toContain("git apply");
    await userEvent.click(download);
    expect(onDownloadPatch).toHaveBeenCalledTimes(1);
  });

  it("keeps finish as a secondary explicit action while primary work remains", async () => {
    const onFinish = vi.fn();
    renderHeader(undefined, {
      primaryAction: {
        kind: "review_target",
        label: "Review next area",
        detail: "Needs human judgment",
        disabled: false,
        targetId: "target-1",
      },
      onFinish,
    });

    expect(screen.getByRole("button", { name: "Review next area" })).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Finish review…" }));
    expect(onFinish).toHaveBeenCalledTimes(1);
  });
});

describe("ReaderHeader review context", () => {
  it("shows an explicit hosted repository context without replacing authorship", () => {
    renderHeader({ prefix: "by", label: "author-a" }, {
      contextOverride: { label: "repo-checkout", title: "Repository repo-checkout" },
      environment: fallbackReviewEnvironment("customer_hosted"),
    });
    expect(screen.getByLabelText("Review context: repo-checkout")).toBeTruthy();
    expect(screen.getByText("author-a")).toBeTruthy();
  });
});

describe("ReaderHeader extension authorship", () => {
  it("uses an explicit outer-composition authorship label", () => {
    renderHeader({ prefix: "by", label: "alice@example.com" });
    expect(screen.getByText("alice@example.com")).toBeTruthy();
  });
});


describe("ReaderHeader environment context", () => {
  it("names the local source and keeps local refresh available", async () => {
    const localOverview = {
      ...overview,
      session: { ...overview.session, actor_type: "human", range_mode: "working_tree", source_ref: "", repo_root: "/workspace/repo" },
    } as ReviewOverview;
    renderHeader(undefined, {
      overview: localOverview,
      environment: fallbackReviewEnvironment("local"),
      allowRefresh: true,
    });

    expect(screen.getByText("Working tree")).toBeTruthy();
    expect(screen.getByLabelText("Review environment: Local")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.getByRole("button", { name: "Refresh local revision" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy workspace path" }).getAttribute("title")).toBe("/workspace/repo");
  });

  it("does not expose local refresh in hosted mode", async () => {
    renderHeader(undefined, {
      environment: fallbackReviewEnvironment("customer_hosted"),
      allowRefresh: false,
    });

    expect(screen.getByLabelText("Review environment: Customer hosted")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.queryByRole("button", { name: "Refresh local revision" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy workspace path" })).toBeNull();
  });
});
