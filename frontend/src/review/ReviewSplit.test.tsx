/**
 * Request split (Phase 12 item 13): the entry sits in the existing Fix &
 * corrections menu, the sheet previews the stacked layers and the exact
 * handoff, and collecting is offered only where the server can do it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ReviewSplitSheet from "./ReviewSplitSheet";
import type { ReviewActionListing, ReviewActionRoute } from "./reviewActionsApi";
import type { ReviewSplitListing, ReviewSplitPreview, ReviewSplitRecord } from "./reviewSplitApi";

const actionsApi = vi.hoisted(() => ({
  fetchReviewActions: vi.fn(),
  previewReviewAction: vi.fn(),
  startReviewAction: vi.fn(),
  cancelReviewAction: vi.fn(),
}));
vi.mock("./reviewActionsApi", () => actionsApi);

const splitApi = vi.hoisted(() => ({
  SPLIT_COLLECTABLE: new Set(["awaiting_branches", "uncertain"]),
  SPLIT_CANCELLABLE: new Set(["dispatching", "awaiting_branches", "uncertain"]),
  fetchReviewSplits: vi.fn(),
  previewReviewSplit: vi.fn(),
  startReviewSplit: vi.fn(),
  collectReviewSplit: vi.fn(),
  cancelReviewSplit: vi.fn(),
}));
vi.mock("./reviewSplitApi", () => splitApi);

const COPY: ReviewActionRoute = {
  tier: "prompt_fallback",
  mode: "copy",
  available: true,
  host: "",
  session_id: "",
  target_ref: "",
  label: "Copy a fix prompt",
  reason: "",
};
const PLAN = {
  base_sha: "a".repeat(40),
  head_sha: "b".repeat(40),
  digest: "plan-digest",
  layers: [
    { index: 1, cohort_id: "area:billing", title: "Billing", paths: ["src/billing.py"], branch: "lemoncrow/split/abcd/01" },
    { index: 2, cohort_id: "area:auth", title: "Auth", paths: ["src/auth.py", "src/session.py"], branch: "lemoncrow/split/abcd/02" },
  ],
};

function previewOf(overrides: Partial<ReviewSplitPreview> = {}): ReviewSplitPreview {
  return {
    revision_id: "rrv-1",
    operation_id: "spl_1",
    ready: true,
    reason: "",
    plan: PLAN,
    route: COPY,
    candidates: [
      { ...COPY, tier: "exact_session", mode: "resume", available: false, reason: "exact primary author session is unavailable" },
      COPY,
    ],
    handoff: "LemonCrow Review · correction handoff spl_1 · split request",
    handoff_digest: "digest-1",
    collect_available: true,
    collect_reason: "",
    ...overrides,
  };
}

function splitRecord(overrides: Partial<ReviewSplitRecord> = {}): ReviewSplitRecord {
  return {
    id: "spl_1",
    review_id: "review-1",
    source_revision_id: "rrv-1",
    source_revision_number: 1,
    status: "awaiting_branches",
    route: COPY,
    plan: PLAN,
    handoff_digest: "digest-1",
    directive: "",
    created_by: "local",
    created_at: "",
    updated_at: "",
    message: "copied for the agent; collect its branches when they are ready",
    events: [],
    children: [],
    migration: null,
    ...overrides,
  };
}

function listingOf(overrides: Partial<ReviewSplitListing> = {}): ReviewSplitListing {
  return { splits: [], split_from: [], collect_available: true, collect_reason: "", ...overrides };
}

const ACTIONS: ReviewActionListing = { revision_id: "rrv-1", actions: [], catalog: [], fix_all_limit: 25 };

beforeEach(() => {
  vi.clearAllMocks();
  actionsApi.fetchReviewActions.mockResolvedValue(ACTIONS);
  splitApi.fetchReviewSplits.mockResolvedValue(listingOf());
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("ReviewSplitSheet", () => {
  function mount() {
    const onClose = vi.fn();
    render(<ReviewSplitSheet reviewId="review-1" revisionId="rrv-1" onClose={onClose} />);
    return { onClose };
  }

  it("copies the split prompt first and only then records the handoff", async () => {
    const preview = previewOf();
    splitApi.previewReviewSplit.mockResolvedValue(preview);
    splitApi.startReviewSplit.mockResolvedValue({ split: splitRecord(), replayed: false });
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    mount();
    const routes = await screen.findByRole("radiogroup", { name: "Split route" });
    expect(within(routes).getByText("exact primary author session is unavailable")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Copy prompt and record handoff" }));
    expect(writeText).toHaveBeenCalledWith(preview.handoff);
    expect(splitApi.startReviewSplit).toHaveBeenCalledWith("review-1", { route: undefined, directive: "" }, preview, { copied: true });
  });

  it("never records a handoff when the copy fails", async () => {
    splitApi.previewReviewSplit.mockResolvedValue(previewOf());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "Copy prompt and record handoff" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(splitApi.startReviewSplit).not.toHaveBeenCalled();
  });

  it("says plainly when there is nothing to split and offers no start", async () => {
    splitApi.previewReviewSplit.mockResolvedValue(
      previewOf({ ready: false, reason: "nothing to split: every changed file belongs to one cohort", plan: null, route: null, candidates: [], handoff: "", handoff_digest: "" }),
    );
    mount();
    expect((await screen.findByTestId("review-split-refused")).textContent).toContain("nothing to split");
    expect((screen.getByRole("button", { name: "Request split" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("collects with the reviewed plan digest, and only where the server holds the checkout", async () => {
    splitApi.previewReviewSplit.mockResolvedValue(previewOf({ ready: false, reason: "split spl_1 is still awaiting_branches" }));
    splitApi.fetchReviewSplits.mockResolvedValue(listingOf({ splits: [splitRecord()] }));
    splitApi.collectReviewSplit.mockResolvedValue({ split: splitRecord({ status: "completed" }) });
    mount();
    const collect = await screen.findByRole("button", { name: "Collect branches" });
    await userEvent.click(collect);
    expect(splitApi.collectReviewSplit).toHaveBeenCalledWith("review-1", "spl_1", "plan-digest");
  });

  it("keeps Collect visible but disabled with the server's reason on hosted", async () => {
    const reason = "collecting a split's branches needs the trusted local checkout; run it from lc review there";
    splitApi.previewReviewSplit.mockResolvedValue(previewOf({ collect_available: false, collect_reason: reason }));
    splitApi.fetchReviewSplits.mockResolvedValue(listingOf({ splits: [splitRecord()], collect_available: false, collect_reason: reason }));
    mount();
    const collect = await screen.findByRole("button", { name: "Collect branches" });
    expect((collect as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("review-split-record").textContent).toContain("trusted local checkout");
  });

  it("links each produced Review and reports what carried", async () => {
    splitApi.previewReviewSplit.mockResolvedValue(previewOf());
    splitApi.fetchReviewSplits.mockResolvedValue(
      listingOf({
        splits: [
          splitRecord({
            status: "completed",
            message: "2 stacked Reviews linked to this one; 5 judgments carried on exactly matching parts",
            children: [
              { layer: 1, cohort_id: "area:billing", branch: "lemoncrow/split/abcd/01", base_sha: "", head_sha: "", review_id: "child-1", revision_id: "", title: "Split 1/2 · Billing", status: "open", extra_paths: ["src/auth.py"], missing_paths: [] },
              { layer: 2, cohort_id: "area:auth", branch: "lemoncrow/split/abcd/02", base_sha: "", head_sha: "", review_id: "child-2", revision_id: "", title: "Split 2/2 · Auth", status: "open", extra_paths: [], missing_paths: [] },
            ],
            migration: { carried: 5, per_child: {}, not_carried_total: 2, not_carried: [] },
          }),
        ],
      }),
    );
    mount();
    const record = await screen.findByTestId("review-split-record");
    expect(within(record).getByRole("link", { name: "Split 1/2 · Billing" })).toBeTruthy();
    expect(record.textContent).toContain("also changes 1 file of other layers");
    expect(record.textContent).toContain("5 judgments carried on identical parts · 2 not carried");
    expect(within(record).queryByRole("button", { name: "Collect branches" })).toBeNull();
  });
});
