import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ContextDrawer from "./ContextDrawer";
import type { Annotation, FileDetail, ReviewTarget } from "./types";

const reviewApi = vi.hoisted(() => ({
  fetchTargetHistory: vi.fn(),
}));
vi.mock("./reviewApi", () => ({
  fetchReviewEvidenceContent: vi.fn(async () => new Blob(["artifact"])),
  fetchTargetHistory: reviewApi.fetchTargetHistory,
}));

const TARGET: ReviewTarget = {
  target_id: "target:checkout",
  unit_key: "sym:checkout",
  kind: "symbol",
  path: "src/checkout.tsx",
  label: "src/checkout.tsx::Checkout",
  symbol: "Checkout",
  start_line: 10,
  end_line: 80,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 20, end_line: 24, hunk_ordinal: 0 }],
  state: "unreviewed",
  changed_since_mark: false,
  reviewed_revision_id: "",
  attention_rank: 2,
  attention_level: "high",
  reasons: ["7 known callers", "1 untouched impacted site"],
  additions: 5,
  deletions: 1,
  fingerprint_method: "symbol_body_sha256",
  verification: { pass: 1, fail: 1, unknown: 1 },
  annotation_counts: { open: 1, orphaned: 0, addressed_needs_rereview: 0 },
};

const DETAIL: FileDetail = {
  path: "src/checkout.tsx",
  status: "modified",
  language: "tsx",
  additions: 42,
  deletions: 8,
  patch: "",
  renderable: true,
  refusal: "",
  detail: "",
  degraded: [],
  impact: [
    {
      kind: "untouched_caller",
      path: "src/cart.ts:L19",
      old: "checkout",
      new: null,
      snippet: "checkout(cart)",
      in_patch: false,
      inspected_by_agent: false,
      source_path: "src/checkout.tsx",
      uncertainty: "",
    },
  ],
  symbols: [
    {
      symbol_name: "Checkout",
      qualified_name: "Checkout",
      kind: "function",
      change: "modified",
      start_line: 10,
      end_line: 80,
      caller_count: 7,
      centrality_rank: 4,
      source: "tree_sitter",
    },
  ],
  evidence: [
    { name: "Full suite", status: "PASS", detail: "624 passed", source: "test", scope: "review" },
    { name: "Lint", status: "FAIL", detail: "1 failure", source: "ruff", scope: "file" },
    { name: "Focused tests", status: "UNKNOWN", detail: "not captured", source: "none", scope: "file" },
  ],
  provenance: {
    status: "matched",
    host: "claude",
    model: "claude-opus",
    session_id: "session-1",
    task: "checkout refactor",
    certainty: "exact",
    match_confidence: 1,
    match_reason: "recorded at authoring time",
    commands_run: ["pytest tests/checkout"],
    subagents: [],
    reads_recorded: true,
    inspected: true,
    uninspected_impacted: [],
  },
};

const AUTHOR: Annotation = {
  id: "author-1",
  review_id: "review-1",
  revision_id: "rrv-1",
  parent_id: "",
  kind: "comment",
  state: "open",
  body: "Validation moved before payment creation.",
  created_by: "claude",
  created_by_actor: "agent",
  source: "author",
  source_id: "session-1",
  title: "Why validation moved",
  evidence: [],
  confidence: null,
  author_response: "none",
  created_at: "",
  updated_at: "",
  anchor_method: "identical_blob",
  anchor_method_label: "file unchanged since the comment",
  anchor_exact: true,
  anchor_detail: "",
  anchored: true,
  path: "src/checkout.tsx",
  side: "new",
  start_line: 24,
  end_line: 24,
  unit_key: "sym:checkout",
  symbol: "Checkout",
  origin_symbol: "Checkout",
};

/** A comment whose anchor could not be re-found: listed, never drawn on a line. */
const ORPHAN: Annotation = {
  ...AUTHOR,
  id: "orphan-1",
  source: "human",
  created_by: "reviewer",
  created_by_actor: "human",
  title: "",
  body: "Does this still handle the empty cart?",
  state: "orphaned",
  anchor_method: "unique_text",
  anchor_method_label: "matched by its text alone",
  anchor_exact: false,
  anchor_detail: "more than one place fit",
  anchored: false,
  start_line: 41,
  end_line: 41,
};

const OTHER_TARGET: ReviewTarget = {
  ...TARGET,
  target_id: "target:cart",
  unit_key: "sym:cart",
  path: "src/cart.tsx",
  label: "src/cart.tsx::Cart",
  symbol: "Cart",
};

function drawerFor(
  target: ReviewTarget,
  tab: "impact" | "checks" | "evidence" | "author" | "discussion" | "history",
  onAddPreview = vi.fn(),
) {
  return (
    <ContextDrawer
      reviewId="review-1"
      target={target}
      detail={DETAIL}
      degraded={[]}
      annotations={[AUTHOR]}
      evidence={[]}
      tab={tab}
      pinned
      onTab={vi.fn()}
      onClose={vi.fn()}
      onTogglePin={vi.fn()}
      onOpenImpact={vi.fn()}
      onUploadEvidence={vi.fn()}
      onAddPreview={onAddPreview}
    />
  );
}

function renderDrawer(
  tab: "impact" | "checks" | "evidence" | "author" | "discussion" | "history" = "impact",
  detail: FileDetail = DETAIL,
  annotations: Annotation[] = [AUTHOR],
) {
  const onTab = vi.fn();
  const onClose = vi.fn();
  const onTogglePin = vi.fn();
  const onOpenImpact = vi.fn();
  render(
    <ContextDrawer
      reviewId="review-1"
      target={TARGET}
      detail={detail}
      degraded={[]}
      annotations={annotations}
      evidence={[]}
      tab={tab}
      pinned={false}
      onTab={onTab}
      onClose={onClose}
      onTogglePin={onTogglePin}
      onOpenImpact={onOpenImpact}
      onUploadEvidence={vi.fn()}
      onAddPreview={vi.fn()}
    />,
  );
  return { onTab, onClose, onTogglePin, onOpenImpact };
}

describe("ContextDrawer", () => {
  beforeEach(() => {
    reviewApi.fetchTargetHistory.mockReset();
    reviewApi.fetchTargetHistory.mockResolvedValue({ review_id: "review-1", reviewer_id: "local", unit_key: TARGET.unit_key, events: [] });
  });

  it("uses task-oriented tabs and starts from the caller-selected tab", async () => {
    const { onTab } = renderDrawer("impact");
    expect(screen.getByText("Affected code")).toBeTruthy();
    expect(screen.queryByText("Summary")).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: /checks/i }));
    expect(onTab).toHaveBeenCalledWith("checks");
  });

  it("keeps review-work tabs primary and exposes expert detail through Details", async () => {
    const { onTab } = renderDrawer("impact");
    expect(screen.getByRole("tab", { name: /impact/i })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /comments/i })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /checks/i })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /evidence/i })).toBeNull();

    await userEvent.click(screen.getByText("Details"));
    await userEvent.click(screen.getByRole("button", { name: /Evidence/i }));
    expect(onTab).toHaveBeenCalledWith("evidence");
  });

  it("loads append-only judgment transitions for the active semantic target", async () => {
    reviewApi.fetchTargetHistory.mockResolvedValue({
      review_id: "review-1",
      reviewer_id: "local",
      unit_key: TARGET.unit_key,
      events: [
        {
          id: "mark-1",
          review_id: "review-1",
          reviewer_id: "local",
          unit_key: TARGET.unit_key,
          revision_id: "rrv-2",
          reviewed_revision_id: "rrv-1",
          event_kind: "reconciled",
          from_state: "reviewed",
          to_state: "changed_since_review",
          content_fingerprint: "fp",
          previous_unit_key: "",
          actor_type: "human",
          note: "",
          reason: "content changed",
          created_at: "2026-09-16T07:00:00Z",
        },
      ],
    });
    renderDrawer("history");
    await waitFor(() => expect(screen.getByText("reviewed → changed_since_review")).toBeTruthy());
    expect(reviewApi.fetchTargetHistory).toHaveBeenCalledWith("review-1", TARGET.unit_key);
    expect(screen.getByText("content changed")).toBeTruthy();
    expect(screen.queryByText(/synthetic event/i)).toBeNull();
  });

  it("orders failed and unresolved checks ahead of review-wide passes", () => {
    renderDrawer("checks");
    const text = screen.getByText("Lint").closest("div")?.parentElement?.parentElement?.textContent ?? "";
    expect(text).toContain("Failed");
    expect(screen.getByText("Focused tests")).toBeTruthy();
    expect(screen.getByText("Full suite")).toBeTruthy();
    expect(screen.getByText(/review-wide passes are context, not proof/i)).toBeTruthy();
  });

  it("describes the single ordered list of checks it actually renders", () => {
    // Outcome is the primary sort, so a review-wide failure sits above a
    // file-scoped pass. Promising two separate sections would send the reviewer
    // hunting for a file-scoped block that is not on the panel.
    renderDrawer("checks", {
      ...DETAIL,
      evidence: [
        { name: "File lint", status: "PASS", detail: "", source: "ruff", scope: "file" },
        { name: "Packet suite", status: "FAIL", detail: "", source: "test", scope: "review" },
      ],
    });
    const panel = screen.getByRole("tabpanel");
    const order = Array.from(panel.querySelectorAll("span"))
      .map((node) => node.textContent)
      .filter((text) => text === "File lint" || text === "Packet suite");
    expect(order).toEqual(["Packet suite", "File lint"]);
    expect(panel.textContent).not.toMatch(/shown separately/i);
    expect(panel.textContent).toMatch(/review-wide passes are context, not proof/i);
  });

  it("puts a file-scoped check ahead of a review-wide check that ended the same way", () => {
    // Spec §14.4: outcome first, then "File-scoped checks before review-wide
    // checks." The names are deliberately anti-alphabetical, so a comparator
    // that falls straight from outcome to name orders these the wrong way.
    renderDrawer("checks", {
      ...DETAIL,
      evidence: [
        { name: "Alpha suite", status: "FAIL", detail: "", source: "test", scope: "review" },
        { name: "Zed lint", status: "FAIL", detail: "", source: "ruff", scope: "file" },
      ],
    });
    const panel = screen.getByRole("tabpanel");
    const order = Array.from(panel.querySelectorAll("span"))
      .map((node) => node.textContent)
      .filter((text) => text === "Alpha suite" || text === "Zed lint");
    expect(order).toEqual(["Zed lint", "Alpha suite"]);
    expect(panel.textContent).toContain("this file");
    expect(panel.textContent).toContain("review-wide");
  });

  it("opens the evidence picker from the keyboard, not only with a pointer", async () => {
    renderDrawer("evidence");
    const picker = document.querySelector('input[type="file"]') as HTMLInputElement;
    const opened = vi.fn();
    picker.addEventListener("click", opened);
    screen.getByRole("button", { name: "Choose files" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("lists an orphaned comment in its own unresolved area, with the rung that lost it", () => {
    // §15.6: orphans belong in a dedicated unresolved area and are never drawn
    // on a guessed line. `Discussion` has no anchor filter, so an orphan handed
    // to it renders in the anchored list under a stale "L41" and the bare word
    // "orphaned", with nothing saying why the anchor was lost.
    renderDrawer("discussion", DETAIL, [AUTHOR, ORPHAN]);
    const panel = screen.getByRole("tabpanel");
    const body = screen.getByText(/Does this still handle the empty cart\?/);
    const area = screen.getByText(/1 comment lost its anchor/i).parentElement as HTMLElement;

    expect(area.contains(body)).toBe(true);
    expect(area.textContent).toContain("matched by its text alone");
    expect(area.textContent).toContain("more than one place fit");
    // Once on the panel, so it is listed in the unresolved area rather than
    // also being mixed into the anchored discussion above it.
    expect(panel.textContent?.match(/Does this still handle the empty cart\?/g)).toHaveLength(1);
    // The anchored comment still reaches the discussion list.
    expect(screen.getByText("Validation moved before payment creation.")).toBeTruthy();
  });

  it("counts the Comments card by what is inside it, not by the orphans beside it", () => {
    // The badge hangs off the card and the card holds only anchored comments,
    // so counting the orphans too printed "Discussion 2" over one comment, with
    // the other one in a sibling block outside the card entirely.
    renderDrawer("discussion", DETAIL, [AUTHOR, ORPHAN]);
    const header = screen.getByRole("heading", { name: "Comments" }).parentElement as HTMLElement;
    expect(header.textContent).toBe("Comments1");
    // The tab still counts both halves: both of them are read on this tab.
    expect(screen.getByRole("tab", { name: /comments/i }).textContent).toBe("Comments2");
  });

  it("drops a preview URL typed for one target when the drawer moves to the next", async () => {
    // The drawer is mounted without a key and survives j/k, so an uncommitted
    // URL left in the box would be filed against the file that happens to be on
    // screen when Add is clicked -- evidence on the wrong file, silently.
    const onAddPreview = vi.fn();
    const view = render(drawerFor(TARGET, "evidence", onAddPreview));
    const field = () => screen.getByPlaceholderText("http://localhost:3000/...") as HTMLInputElement;
    await userEvent.type(field(), "http://localhost:3000/checkout");
    expect(field().value).toBe("http://localhost:3000/checkout");

    view.rerender(drawerFor(OTHER_TARGET, "evidence", onAddPreview));
    expect(field().value).toBe("");
    expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
    expect(onAddPreview).not.toHaveBeenCalled();
  });

  it("keeps exact author provenance separate from discussion", () => {
    renderDrawer("author");
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("Authored by")).toBeTruthy();
    expect(within(panel).getByText("Why validation moved")).toBeTruthy();
    expect(within(panel).queryByRole("heading", { name: "Comments" })).toBeNull();
  });

  it("exposes explicit pin and close controls", async () => {
    const { onClose, onTogglePin } = renderDrawer("impact");
    await userEvent.click(screen.getByRole("button", { name: "Pin" }));
    expect(onTogglePin).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Close review context" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps a selected expert section in the tab semantics", async () => {
    renderDrawer("history");
    expect(screen.getByRole("tab", { name: /history/i }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("review-context-tab-history");
    await waitFor(() => expect(reviewApi.fetchTargetHistory).toHaveBeenCalledWith("review-1", TARGET.unit_key));
  });

  it("uses tab semantics and moves focus into Context when it opens", async () => {
    renderDrawer("checks");
    const close = screen.getByRole("button", { name: "Close review context" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    expect(screen.getByRole("tab", { name: /checks/i }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("review-context-tab-checks");
  });
});
