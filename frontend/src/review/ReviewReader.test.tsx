import { describe, expect, it, beforeEach, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ReviewReader from "./ReviewReader";
import type { FileDetail, RevisionTargetRef, ReviewOutlineItem, ReviewOverview, ReviewTarget, ReviewTargetList, SourceState } from "./types";

const stream = vi.hoisted(() => ({
  scrollToTarget: vi.fn(),
  scrollToFile: vi.fn(),
  lastProps: null as any,
}));

vi.mock("./ReviewStream", async () => {
  const React = await import("react");
  return {
    default: React.forwardRef((props: any, ref: any) => {
      stream.lastProps = props;
      React.useImperativeHandle(ref, () => ({
        scrollToTarget: stream.scrollToTarget,
        scrollToFile: stream.scrollToFile,
      }));
      return <div data-testid="reader-stream-mock">{props.targets.length} streamed targets</div>;
    }),
  };
});

const drawer = vi.hoisted(() => ({ lastProps: null as any }));
vi.mock("./ContextDrawer", () => ({
  default: (props: any) => {
    drawer.lastProps = props;
    return (
      <div data-testid="context-drawer-mock">
        <span>{props.tab} · {props.pinned ? "pinned" : "overlay"}</span>
        <button type="button" onClick={props.onClose}>Close context mock</button>
      </div>
    );
  },
}));

const api = vi.hoisted(() => ({
  deliverFeedbackToClaude: vi.fn(),
  fetchOverview: vi.fn(),
  fetchTargets: vi.fn(),
  fetchAnnotations: vi.fn(),
  fetchReviewEvidence: vi.fn(),
  fetchFile: vi.fn(),
  fetchFinishReview: vi.fn(),
  fetchSourceState: vi.fn(),
  postBulkReviewed: vi.fn(),
  postMark: vi.fn(),
  postRefresh: vi.fn(),
  postAnnotation: vi.fn(),
  patchAnnotation: vi.fn(),
  fetchRelatedSource: vi.fn(),
  finishReview: vi.fn(),
  exportFeedback: vi.fn(),
  uploadReviewEvidence: vi.fn(),
  linkReviewEvidence: vi.fn(),
}));

vi.mock("./reviewApi", () => ({
  adoptBootstrapFragment: () => ({ token: "t", reviewId: "review-1" }),
  deliverFeedbackToClaude: api.deliverFeedbackToClaude,
  fetchOverview: api.fetchOverview,
  fetchTargets: api.fetchTargets,
  fetchAnnotations: api.fetchAnnotations,
  fetchReviewEvidence: api.fetchReviewEvidence,
  fetchFile: api.fetchFile,
  fetchFinishReview: api.fetchFinishReview,
  fetchSourceState: api.fetchSourceState,
  postBulkReviewed: api.postBulkReviewed,
  postMark: api.postMark,
  postRefresh: api.postRefresh,
  postAnnotation: api.postAnnotation,
  patchAnnotation: api.patchAnnotation,
  fetchRelatedSource: api.fetchRelatedSource,
  finishReview: api.finishReview,
  exportFeedback: api.exportFeedback,
  uploadReviewEvidence: api.uploadReviewEvidence,
  linkReviewEvidence: api.linkReviewEvidence,
}));

function makeTarget(index: number): ReviewTarget {
  const fileIndex = Math.floor(index / 10);
  const path = `src/file-${fileIndex}.py`;
  return {
    target_id: `target-${index}`,
    unit_key: `sym-${index}`,
    kind: "symbol",
    path,
    label: `${path}::fn_${index}`,
    symbol: `fn_${index}`,
    start_line: index + 1,
    end_line: index + 2,
    hunk_ordinals: [index],
    spans: [{ side: "new", start_line: index + 1, end_line: index + 1, hunk_ordinal: index }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: index + 1,
    attention_level: index < 3 ? "high" : "normal",
    reasons: index < 3 ? ["public contract changed"] : ["+1 -0"],
    additions: 1,
    deletions: 0,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

const TARGETS = Array.from({ length: 30 }, (_, index) => makeTarget(index));

function deltaRef(target: ReviewTarget): RevisionTargetRef {
  const { target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts } = target;
  return { target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts };
}

function outline(): ReviewOutlineItem[] {
  return [0, 1, 2].map((index) => ({
    path: `src/file-${index}.py`,
    target_count: 10,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 10,
    unknown: 0,
    mechanical: 0,
    attention_rank: index * 10 + 1,
    reasons: index === 0 ? ["public contract changed"] : ["+10 -0"],
  }));
}

const OVERVIEW: ReviewOverview = {
  session: {
    id: "review-1",
    title: "working tree",
    subject_type: "local_change",
    range_mode: "working_tree",
    source_ref: "",
    status: "open",
    actor_type: "agent",
    reviewer_id: "local",
    repo_root: "/repo",
    updated_at: "",
  },
  revision: {
    id: "rrv-1",
    revision_number: 1,
    range_mode: "working_tree",
    base_sha: "abc",
    head_sha: "",
    dirty: true,
    degraded: [],
    provenance_host: "claude",
    provenance_model: "",
    provenance_session_id: "",
    provenance_certainty: "none",
    created_at: "",
  },
  revision_count: 1,
  packet_available: true,
  stats: {},
  title: "working tree",
  change_story: ["Review reader", "target progress", "continuous diff"],
  provenance: {
    status: "unknown",
    host: "",
    model: "",
    session_id: "",
    task: "",
    certainty: "none",
    match_confidence: 0,
    match_reason: "",
    commands_run: [],
    subagents: [],
    reads_recorded: false,
    inspected: null,
    uninspected_impacted: [],
  },
  evidence: [],
  degraded: [],
  groups: [],
  group_counts: {
    needs_attention: 0,
    changed_since_my_review: 0,
    unreviewed: 3,
    reviewed: 0,
    mechanical: 0,
  },
  unit_count: 63,
  target_count: 30,
  progress: {
    target_count: 30,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 30,
    unknown: 0,
    mechanical: 0,
  },
  outline: outline(),
  mark_count: 0,
  discarded: [],
};
const TARGET_LIST: ReviewTargetList = {
  revision_id: "rrv-1",
  order: "recommended",
  targets: TARGETS,
  progress: OVERVIEW.progress!,
  outline: outline(),
};

function largeReviewFixture(): { overview: ReviewOverview; targets: ReviewTargetList } {
  const targets = Array.from({ length: 1500 }, (_, index) => {
    const fileIndex = Math.floor(index / 3);
    const path = `src/large-${fileIndex}.py`;
    const base = makeTarget(index);
    return {
      ...base,
      target_id: `large-target-${index}`,
      unit_key: `large-sym-${index}`,
      path,
      label: `${path}::fn_${index}`,
      symbol: `fn_${index}`,
      start_line: (index % 3) + 1,
      end_line: (index % 3) + 2,
      spans: [{ side: "new" as const, start_line: (index % 3) + 1, end_line: (index % 3) + 1, hunk_ordinal: 0 }],
      attention_rank: index + 1,
    };
  });
  const rows: ReviewOutlineItem[] = Array.from({ length: 500 }, (_, index) => ({
    path: `src/large-${index}.py`,
    target_count: 3,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 3,
    unknown: 0,
    mechanical: 0,
    attention_rank: index + 1,
    reasons: index < 10 ? ["public contract changed"] : ["+3 -0"],
  }));
  const progress = {
    target_count: 1500,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 1500,
    unknown: 0,
    mechanical: 0,
  };
  return {
    overview: { ...OVERVIEW, unit_count: 3200, target_count: 1500, progress, outline: rows },
    targets: { revision_id: "rrv-large", order: "recommended", targets, progress, outline: rows },
  };
}

const ADVANCED_OVERVIEW: ReviewOverview = {
  ...OVERVIEW,
  revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
  revision_count: 2,
};

/** Refresh onto a new revision whose dependent target read dies, leaving the reader stranded. */
async function strandReaderOnAdvancedRevision() {
  api.postRefresh.mockResolvedValueOnce(ADVANCED_OVERVIEW);
  api.fetchTargets
    .mockResolvedValueOnce(TARGET_LIST)
    .mockRejectedValueOnce(new Error("gateway restarted"));

  render(<ReviewReader />);
  expect(await screen.findByText("30 streamed targets")).toBeTruthy();

  await userEvent.click(screen.getByLabelText("Review actions"));
  await userEvent.click(screen.getByRole("button", { name: "Refresh revision" }));
  expect(await screen.findByText("Revision advanced — reload required")).toBeTruthy();
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  stream.lastProps = null;
  drawer.lastProps = null;
  api.fetchOverview.mockResolvedValue(OVERVIEW);
  api.fetchTargets.mockResolvedValue(TARGET_LIST);
  api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-1", annotations: [], counts: {} });
  api.fetchReviewEvidence.mockResolvedValue({ revision_id: "rrv-1", evidence: [] });
  api.fetchFile.mockImplementation(async (_id: string, path: string) => ({
    path,
    status: "modified",
    additions: 10,
    deletions: 0,
    patch: "patch",
    renderable: true,
    refusal: "",
    detail: "",
    degraded: [],
  }));
  api.fetchSourceState.mockResolvedValue({ supported: true, changed: false, fingerprint: "same", path_count: 3, paths: [], reason: "" });
  api.postAnnotation.mockResolvedValue({ annotation: {} });
  api.postBulkReviewed.mockResolvedValue({
    marked: [],
    skipped: [],
    progress: TARGET_LIST.progress,
    outline_updates: [],
    group_counts: OVERVIEW.group_counts,
  });
  api.exportFeedback.mockResolvedValue({ markdown: "## Review feedback\n", open: 1, orphaned: 0, resolved: 0 });
  api.deliverFeedbackToClaude.mockResolvedValue({
    state: "sent",
    target_ref: "claude:session-1",
    remote_ref: "message-1",
    message: "sent",
    annotation_count: 1,
  });
  api.postMark.mockImplementation(async (_id: string, unitKey: string, state: string) => {
    const old = TARGETS.find((target) => target.unit_key === unitKey)!;
    const target = { ...old, state };
    return {
      unit_key: unitKey,
      requested: state,
      recorded: state,
      downgraded: false,
      note: "",
      target,
      progress: { ...TARGET_LIST.progress, reviewed: state === "reviewed" ? 1 : 0, unreviewed: state === "reviewed" ? 29 : 30 },
      outline_updates: [],
      groups: [],
      group_counts: OVERVIEW.group_counts,
    };
  });
  const finishSummary = {
    status: "open" as const,
    target_count: 30,
    reviewed_targets: 0,
    unreviewed_targets: 30,
    changed_since_review: 0,
    needs_changes: 0,
    unknown_targets: 0,
    open_comments: 0,
    orphaned_comments: 0,
    failed_verification: 0,
    unresolved_verification: 0,
    previous_revision_evidence: 0,
    discarded_verdicts: 0,
  };
  api.fetchFinishReview.mockResolvedValue(finishSummary);
  api.finishReview.mockResolvedValue({ ...finishSummary, status: "finished" });
});

describe("ReviewReader R22 shell", () => {
  it("opens a 30-target review as one stream instead of one selected file", async () => {
    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.getByText((_text, element) => element?.textContent === "30 targets")).toBeTruthy();
    expect(screen.getByText((_text, element) => element?.textContent === "0 reviewed")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(3));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(
      new Set(["src/file-0.py", "src/file-1.py", "src/file-2.py"]),
    );
  });

  it("keeps a 500-file / 1,500-target review bounded and prefetches only a small navigation window", async () => {
    const large = largeReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(5));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(new Set([
      "src/large-0.py",
      "src/large-1.py",
      "src/large-2.py",
      "src/large-3.py",
      "src/large-4.py",
    ]));

    await userEvent.click(screen.getByText("large-5.py"));
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(8));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(new Set([
      "src/large-0.py",
      "src/large-1.py",
      "src/large-2.py",
      "src/large-3.py",
      "src/large-4.py",
      "src/large-5.py",
      "src/large-6.py",
      "src/large-7.py",
    ]));
  });

  it("r judges the current target and advances to the next target", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("r");

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-0", "reviewed"));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");
    expect(await screen.findByText((_text, element) => element?.textContent === "1 reviewed")).toBeTruthy();
  });

  it("J moves between files while j remains target navigation", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("j");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");

    stream.scrollToTarget.mockClear();
    await userEvent.keyboard("J");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/file-1.py");
  });

  it("switches to file order without changing target identity or refetching loaded patches", async () => {
    const fileOrdered: ReviewTargetList = { ...TARGET_LIST, order: "file", targets: [...TARGETS] };
    api.fetchTargets.mockResolvedValueOnce(TARGET_LIST).mockResolvedValueOnce(fileOrdered);

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(3));

    await userEvent.keyboard("j");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1"));
    stream.scrollToTarget.mockClear();

    await userEvent.click(screen.getByLabelText("Review actions"));
    const order = screen.getByRole("combobox", { name: "Review order" });
    await userEvent.selectOptions(order, "file");

    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));
    await waitFor(() => expect((screen.getByRole("combobox", { name: "Review order" }) as HTMLSelectElement).value).toBe("file"));
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1"));
    expect(api.postMark).not.toHaveBeenCalled();
    expect(api.fetchFile).toHaveBeenCalledTimes(3);
  });

  it("keeps file order when refreshing to a new revision", async () => {
    const fileOrdered: ReviewTargetList = { ...TARGET_LIST, order: "file", targets: [...TARGETS] };
    const refreshedOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
      revision_count: 2,
    };
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce(fileOrdered)
      .mockResolvedValueOnce({ ...fileOrdered, revision_id: "rrv-2" });
    api.postRefresh.mockResolvedValueOnce(refreshedOverview);

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Review order" }), "file");
    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));

    await userEvent.click(screen.getByRole("button", { name: "Refresh revision" }));

    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledWith("review-1"));
    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));
    expect((screen.getByRole("combobox", { name: "Review order" }) as HTMLSelectElement).value).toBe("file");
  });

  it("focus mode collapses the outline and / reopens search with keyboard focus", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.getByLabelText("Collapse review outline")).toBeTruthy();

    await userEvent.keyboard("f");
    expect(await screen.findByLabelText("Open review outline")).toBeTruthy();
    expect(screen.getByTestId("reader-stream-mock")).toBeTruthy();

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await waitFor(() => expect(document.activeElement).toBe(search));
    expect(screen.getByLabelText("Collapse review outline")).toBeTruthy();
  });

  it("p opens searchable review actions and executes the existing feedback flow", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("p");
    const dialog = await screen.findByRole("dialog", { name: "Review actions" });
    const filter = within(dialog).getByRole("textbox", { name: "Filter review actions" });
    await waitFor(() => expect(document.activeElement).toBe(filter));
    await userEvent.type(filter, "prepare feedback");
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(api.exportFeedback).toHaveBeenCalledWith("review-1"));
    expect(screen.queryByRole("dialog", { name: "Review actions" })).toBeNull();
    expect(await screen.findByText(/Feedback · 1 open/)).toBeTruthy();
  });

  it("opens the compact change overview and jumps from a major change into the stream", async () => {
    api.fetchOverview.mockResolvedValueOnce({
      ...OVERVIEW,
      brief: {
        summary: "3 files across Review reader and target progress; 1 currently deserves focused attention.",
        themes: ["Review reader", "Target progress"],
        major_changes: [{
          key: "commit:abc",
          label: "feat(review): target-aware continuous reader",
          file_count: 2,
          target_count: 15,
          attention_count: 1,
          first_path: "src/file-1.py",
        }],
        review_first: [],
        verification: { pass: 3, fail: 1, not_run: 0, unknown: 1 },
        annotations: { human: 1, author: 0, lemoncrow: 2, ai_review: 0 },
        artifacts: { current: 2, stale: 1 },
      },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Open change overview" }));

    const dialog = await screen.findByRole("dialog", { name: "Change overview" });
    expect(within(dialog).getByText("feat(review): target-aware continuous reader")).toBeTruthy();
    expect(within(dialog).getByText("2 files · 15 targets")).toBeTruthy();
    expect(within(dialog).getByText("1 elevated")).toBeTruthy();
    expect(within(dialog).getByText("failed").previousElementSibling?.textContent).toBe("1");
    expect(within(dialog).getByText("stale").previousElementSibling?.textContent).toBe("1");

    await userEvent.click(within(dialog).getByRole("button", { name: /target-aware continuous reader/i }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Change overview" })).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/file-1.py"));
  });

  it("? opens shortcut help and Escape restores the reviewer's focus", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const finish = screen.getByRole("button", { name: "Finish review" });
    finish.focus();

    await userEvent.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(dialog).getByText("Mark reviewed and advance")).toBeTruthy();
    expect(within(dialog).getByText("Review actions")).toBeTruthy();

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(finish));
  });

  it("search jumps to a distant target without loading the intervening large-review corpus", async () => {
    const large = largeReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);

    render(<ReviewReader />);
    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(5));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "large-499 fn_1499");
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
    expect(screen.getByText("1 streamed targets")).toBeTruthy();

    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-499.py"));
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("large-target-1499"));
    expect(api.fetchFile).toHaveBeenCalledTimes(6);

    await userEvent.keyboard("{Escape}");
    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
  });

  it("search includes already-loaded comment text without another request", async () => {
    api.fetchAnnotations.mockResolvedValueOnce({
      revision_id: "rrv-1",
      annotations: [{
        id: "ann-search",
        review_id: "review-1",
        revision_id: "rrv-1",
        parent_id: "",
        kind: "comment",
        state: "open",
        body: "Retry hazard in this state transition",
        title: "Database retry",
        unit_key: "sym-11",
        path: "src/file-1.py",
        side: "new",
        start_line: 12,
        end_line: 12,
      }],
      counts: {},
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "database hazard");
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
    expect(screen.getByText("1 streamed targets")).toBeTruthy();

    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-11"));
    expect(api.fetchAnnotations).toHaveBeenCalledTimes(1);
  });

  it("bulk-reviews the full file remainder even when search shows only one target", async () => {
    const marked = TARGETS.slice(3, 10).map((target) => ({ unit_key: target.unit_key, path: target.path }));
    const skipped = TARGETS.slice(0, 3).map((target) => ({
      unit_key: target.unit_key,
      path: target.path,
      reason: "still needs individual attention",
    }));
    api.postBulkReviewed.mockResolvedValueOnce({
      marked,
      skipped,
      progress: { ...TARGET_LIST.progress, reviewed: 7, unreviewed: 23 },
      outline_updates: [],
      group_counts: OVERVIEW.group_counts,
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_3");
    expect(await screen.findByText("1 streamed targets")).toBeTruthy();

    await act(async () => stream.lastProps.onBulkReview("src/file-0.py"));

    await waitFor(() => expect(api.postBulkReviewed).toHaveBeenCalledWith(
      "review-1",
      TARGETS.slice(0, 10).map((target) => target.unit_key),
    ));
    await waitFor(() => expect(stream.lastProps.targets[0].state).toBe("reviewed"));
    expect(screen.getByText((_text, element) => element?.textContent === "7 reviewed")).toBeTruthy();
    expect(screen.getByText(/7 marked reviewed · 3 kept for individual review/)).toBeTruthy();
  });

  it("after refresh shows only the target delta and keeps preserved reviewed work out of the queue", async () => {
    const nextTargets = TARGETS.map((target, index) => ({
      ...target,
      state: index < 24 ? "reviewed" as const : "changed_since_review" as const,
      changed_since_mark: index >= 24,
      reviewed_revision_id: "rrv-1",
    }));
    const active = nextTargets.slice(24).map(deltaRef);
    const preserved = nextTargets.slice(0, 24).map(deltaRef);
    const nextOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
      revision_count: 2,
      progress: { ...OVERVIEW.progress!, reviewed: 24, changed_since_review: 6, unreviewed: 0 },
      refreshed: {
        created: true,
        revision_number: 2,
        previous_revision_number: 1,
        reopened: 6,
        carried: 24,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        target_delta: { preserved, reopened: active, added: [], removed: [], active },
      },
    };
    const nextList: ReviewTargetList = {
      ...TARGET_LIST,
      revision_id: "rrv-2",
      targets: nextTargets,
      progress: nextOverview.progress!,
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets.mockResolvedValueOnce(TARGET_LIST).mockResolvedValueOnce(nextList);

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Refresh revision" }));

    expect(await screen.findByText("6 streamed targets")).toBeTruthy();
    expect(screen.getByText("6 need your eyes")).toBeTruthy();
    expect(screen.getByText("24 preserved")).toBeTruthy();
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-24"));

    await userEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
  });

  it("ignores a file response from the previous revision after refresh", async () => {
    let releaseStale: (detail: FileDetail) => void = () => {};
    const stale = new Promise<FileDetail>((resolve) => { releaseStale = resolve; });
    const calls = new Map<string, number>();
    api.fetchFile.mockImplementation(async (_id: string, path: string) => {
      const count = (calls.get(path) ?? 0) + 1;
      calls.set(path, count);
      if (path === "src/file-2.py" && count === 1) return stale;
      return {
        path,
        status: "modified",
        additions: 1,
        deletions: 0,
        patch: path === "src/file-2.py" ? "NEW-PATCH-rrv2" : "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      };
    });
    const nextOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
      revision_count: 2,
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2" });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(calls.get("src/file-2.py")).toBe(1));

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Refresh revision" }));
    await waitFor(() => expect(stream.lastProps.details["src/file-2.py"]?.patch).toBe("NEW-PATCH-rrv2"));

    await act(async () => {
      releaseStale({
        path: "src/file-2.py",
        status: "modified",
        additions: 1,
        deletions: 0,
        patch: "OLD-PATCH-rrv1",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      });
      await Promise.resolve();
    });
    expect(stream.lastProps.details["src/file-2.py"]?.patch).toBe("NEW-PATCH-rrv2");
  });

  it("previews the target-based finish sheet before mutating review status", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Finish review" }));

    await waitFor(() => expect(api.fetchFinishReview).toHaveBeenCalledWith("review-1"));
    const finishDialog = await screen.findByRole("dialog", { name: "Finish review" });
    expect(finishDialog).toBeTruthy();
    expect(api.finishReview).not.toHaveBeenCalled();
    expect(within(finishDialog).getByText("0/30")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Finish with outstanding work" }));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull());
    expect(screen.getByRole("button", { name: "Reopen review" })).toBeTruthy();
  });

  it("returns focus to Finish review when the finish sheet is dismissed", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const finishButton = screen.getByRole("button", { name: "Finish review" });

    await userEvent.click(finishButton);
    const close = await screen.findByRole("button", { name: "Close finish review" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.click(screen.getByRole("button", { name: "Continue reviewing" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(finishButton));
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  it("restores focus to the Context trigger after closing the drawer", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    const trigger = screen.getByRole("button", { name: "Open context" });
    await userEvent.click(trigger);
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Close context mock" }));
    await waitFor(() => expect(screen.queryByTestId("context-drawer-mock")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("opens Context on demand, accepts an exact tab, and Escape closes it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.queryByTestId("context-drawer-mock")).toBeNull();

    await act(async () => stream.lastProps.onContext(TARGETS[0], "checks"));
    expect(await screen.findByText("checks · overlay")).toBeTruthy();
    expect(drawer.lastProps.target.target_id).toBe("target-0");

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("context-drawer-mock")).toBeNull());
  });

  it("restores keyboard Context focus to the active review row instead of body", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const activeRow = screen.getByRole("button", { name: /file-0.py/i });

    await userEvent.keyboard("e");
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("context-drawer-mock")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(activeRow));
  });

  it("pins Context only by explicit choice and remembers that choice for the tab", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "impact"));
    expect(await screen.findByText("impact · overlay")).toBeTruthy();

    act(() => drawer.lastProps.onTogglePin());
    await waitFor(() => expect(screen.getByTestId("context-drawer-mock").textContent).toContain("impact · pinned"));
    expect(window.sessionStorage.getItem("lemoncrow.review.reader.contextPinned")).toBe("1");
  });

  it("applies a request-change response to target state without a second mark request", async () => {
    const blocked = { ...TARGETS[0], state: "needs_changes" as const, annotation_counts: { ...TARGETS[0].annotation_counts, open: 1 } };
    api.postAnnotation.mockResolvedValueOnce({
      annotation: { id: "ann-1" },
      target: blocked,
      progress: { ...TARGET_LIST.progress, needs_changes: 1, unreviewed: 29 },
      outline_updates: [],
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    act(() => stream.lastProps.onCreate({
      path: blocked.path,
      start_line: blocked.start_line,
      end_line: blocked.start_line,
      side: "additions",
      body: "Guard this path.",
      kind: "request_change",
      target_unit_key: blocked.unit_key,
      mark_target: true,
    }));

    await waitFor(() => expect(api.postAnnotation).toHaveBeenCalledTimes(1));
    expect(api.postMark).not.toHaveBeenCalled();
    expect(await screen.findByText((_text, element) => element?.textContent === "1 needs changes")).toBeTruthy();
  });

  it("sends prepared feedback only through the explicit exact-Claude action", async () => {
    api.fetchOverview.mockResolvedValueOnce({
      ...OVERVIEW,
      revision: {
        ...OVERVIEW.revision,
        provenance_host: "claude",
        provenance_certainty: "exact",
        provenance_session_id: "session-1",
      },
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Prepare feedback" }));
    expect(await screen.findByRole("button", { name: "Send to Claude" })).toBeTruthy();
    expect(api.deliverFeedbackToClaude).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Send to Claude" }));
    await waitFor(() => expect(api.deliverFeedbackToClaude).toHaveBeenCalledWith("review-1"));
    expect(await screen.findByText(/Feedback sent · 1 comment · claude:session-1/)).toBeTruthy();
  });

  it("blocks marking when a refresh advances the revision but its targets never load", async () => {
    await strandReaderOnAdvancedRevision();

    expect(screen.getByText("Review · rev 2")).toBeTruthy();
    expect(stream.lastProps.details).toEqual({});

    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();
  });

  it("says nothing about the revision while a refresh that will succeed is still loading", async () => {
    let releaseTargets: (value: ReviewTargetList) => void = () => {};
    api.postRefresh.mockResolvedValueOnce(ADVANCED_OVERVIEW);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockImplementationOnce(() => new Promise<ReviewTargetList>((resolve) => { releaseTargets = resolve; }));

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Refresh revision" }));

    // The overview is committed the moment postRefresh resolves, but the
    // reviewer hears nothing while the dependent reads are still out: `busy`
    // is already holding every judgment, and the refresh may yet succeed.
    expect(await screen.findByText("Review · rev 2")).toBeTruthy();
    expect(screen.queryByText("Revision advanced — reload required")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => { releaseTargets(TARGET_LIST); });
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
    expect(screen.queryByText("Revision advanced — reload required")).toBeNull();
  });

  it("disables the header controls a stranded reader would refuse", async () => {
    await strandReaderOnAdvancedRevision();
    expect(api.postRefresh).toHaveBeenCalledTimes(1);

    const refresh = screen.getByRole("button", { name: "Refresh revision" });
    expect(refresh).toBeDisabled();
    const finish = screen.getByRole("button", { name: "Finish review" });
    expect(finish).toBeDisabled();

    await userEvent.click(refresh);
    await userEvent.click(finish);
    expect(api.postRefresh).toHaveBeenCalledTimes(1);
    expect(api.fetchFinishReview).not.toHaveBeenCalled();

    // The palette carries the same two actions and must refuse them too.
    await userEvent.keyboard("p");
    const palette = await screen.findByRole("dialog", { name: "Review actions" });
    await userEvent.type(within(palette).getByRole("textbox", { name: "Filter review actions" }), "finish review");
    await userEvent.keyboard("{Enter}");
    expect(api.fetchFinishReview).not.toHaveBeenCalled();
  });

  it("hands judgments back when Reload review catches the reader up", async () => {
    await strandReaderOnAdvancedRevision();

    await userEvent.click(screen.getByRole("button", { name: "Reload review" }));
    await waitFor(() => expect(screen.queryByText("Revision advanced — reload required")).toBeNull());

    await userEvent.keyboard("r");
    await waitFor(() => expect(api.postMark).toHaveBeenCalledTimes(1));
  });

  it("keeps judgments blocked when Reload review fails too", async () => {
    api.fetchOverview
      .mockResolvedValueOnce(OVERVIEW)
      .mockRejectedValueOnce(new Error("gateway still down"));
    await strandReaderOnAdvancedRevision();

    await userEvent.click(screen.getByRole("button", { name: "Reload review" }));
    await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledTimes(2));

    expect(screen.getByText("Revision advanced — reload required")).toBeTruthy();
    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();
  });

  it("keeps a drafted file in the stream when the search stops matching it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_25");

    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
    await waitFor(() => expect(
      stream.lastProps.targets.some((target: ReviewTarget) => target.path === "src/file-0.py"),
    ).toBe(true));
    expect(stream.lastProps.draft?.path).toBe("src/file-0.py");
  });

  it("steps to the file the search matched, not the file a draft pinned into the stream", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_25");
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
    expect(stream.lastProps.activeTargetId).toBe("target-0");

    // The one match lives in src/file-2.py; src/file-0.py is only in the stream
    // because it holds the draft, and the counter does not count it.
    await userEvent.click(screen.getByRole("button", { name: "Next search match" }));
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-25"));
  });

  it("lists only the searched files in the outline, not the file a draft pinned into the stream", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_25");
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();

    // src/file-0.py is in the stream only because it holds the draft. An
    // outline row for it under a counter that says one file would read
    // "file-0.py · 0 matches" and contradict the line right above it.
    const outlinePanel = screen.getByRole("complementary", { name: "Review outline" });
    expect(within(outlinePanel).getByText("file-2.py")).toBeTruthy();
    expect(within(outlinePanel).queryByText("file-0.py")).toBeNull();
    expect(within(outlinePanel).queryByText("0 matches")).toBeNull();
  });

  it("discards the open composer when a refresh replaces the diff it was anchored to", async () => {
    const nextTargets = TARGETS.map((target, index) => ({
      ...target,
      state: index < 20 ? "reviewed" as const : "changed_since_review" as const,
      changed_since_mark: index >= 20,
      reviewed_revision_id: "rrv-1",
    }));
    // The whole delta lives in src/file-2.py; the draft sits on src/file-0.py.
    const active = nextTargets.slice(20).map(deltaRef);
    const nextOverview: ReviewOverview = {
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 2,
        previous_revision_number: 1,
        reopened: 10,
        carried: 20,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        target_delta: { preserved: nextTargets.slice(0, 20).map(deltaRef), reopened: active, added: [], removed: [], active },
      },
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2", targets: nextTargets });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Refresh revision" }));

    expect(await screen.findByText("10 streamed targets")).toBeTruthy();
    expect(stream.lastProps.draft).toBeNull();
    expect(new Set(stream.lastProps.targets.map((target: ReviewTarget) => target.path))).toEqual(new Set(["src/file-2.py"]));
    expect(screen.getByText(/Unsaved comment on src\/file-0\.py was discarded/)).toBeTruthy();
  });

  it("refuses both evidence writes while another action still holds the busy lock", async () => {
    let releaseBulk: () => void = () => {};
    api.postBulkReviewed.mockImplementationOnce(() => new Promise((resolve) => {
      releaseBulk = () => resolve({
        marked: [],
        skipped: [],
        progress: TARGET_LIST.progress,
        outline_updates: [],
        group_counts: OVERVIEW.group_counts,
      });
    }));

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "evidence"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();

    act(() => { void stream.lastProps.onBulkReview("src/file-0.py"); });
    await waitFor(() => expect(api.postBulkReviewed).toHaveBeenCalledTimes(1));

    // Both controls sit side by side in the evidence tab and are gated only by
    // readOnly there, so both are reachable mid-bulk.
    await act(async () => drawer.lastProps.onUploadEvidence([new File(["x"], "shot.png", { type: "image/png" })]));
    expect(api.uploadReviewEvidence).not.toHaveBeenCalled();

    await act(async () => drawer.lastProps.onAddPreview("https://example.test/shot.png", "Shot"));
    expect(api.linkReviewEvidence).not.toHaveBeenCalled();

    // The bulk POST still owns the lock, so marking is still refused.
    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();

    await act(async () => { releaseBulk(); });
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
  });

  it("leaves focus in the dialog a palette action opens instead of the button behind it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const finish = screen.getByRole("button", { name: "Finish review" });
    finish.focus();

    await userEvent.keyboard("p");
    const palette = await screen.findByRole("dialog", { name: "Review actions" });
    await userEvent.type(within(palette).getByRole("textbox", { name: "Filter review actions" }), "change overview");
    await userEvent.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog", { name: "Change overview" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 80)); });
    expect(document.activeElement).not.toBe(finish);
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("closes the topmost dialog on Escape after focus has escaped its overlay", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Open change overview" }));
    expect(await screen.findByRole("dialog", { name: "Change overview" })).toBeTruthy();

    const finish = screen.getByRole("button", { name: "Finish review" });
    act(() => finish.focus());
    await userEvent.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Change overview" })).toBeNull());
  });

  it("never offers a revision update on an archived review", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, session: { ...OVERVIEW.session, status: "archived" } });
    api.fetchSourceState.mockResolvedValue({ supported: true, changed: true, fingerprint: "wt-1", path_count: 2, paths: [], reason: "" });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Restore review" })).toBeTruthy();

    expect(api.fetchSourceState).not.toHaveBeenCalled();
    expect(screen.queryByText("New revision available")).toBeNull();
  });

  it("waits for the same working-tree change twice before offering a new revision", async () => {
    // Each probe hangs until the test releases it, so "has the banner appeared
    // yet" is answered at an exact number of observations, not after a sleep.
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const changed: SourceState = { supported: true, changed: true, fingerprint: "wt-1", path_count: 2, paths: [], reason: "" };

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(probes).toHaveLength(1));

    await act(async () => { probes[0](changed); });
    expect(screen.queryByText("New revision available")).toBeNull();

    await waitFor(() => expect(probes).toHaveLength(2));
    await act(async () => { probes[1](changed); });
    expect(screen.getByText("New revision available")).toBeTruthy();
  });

  it("reports evidence upload progress without erasing an unrelated banner", async () => {
    let release: () => void = () => {};
    api.uploadReviewEvidence.mockImplementation(() => new Promise<void>((resolve) => { release = () => resolve(); }));
    api.postBulkReviewed.mockResolvedValueOnce({
      marked: [],
      skipped: [{ unit_key: "sym-0", path: "src/file-0.py", reason: "still needs individual attention" }],
      progress: TARGET_LIST.progress,
      outline_updates: [],
      group_counts: OVERVIEW.group_counts,
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onBulkReview("src/file-0.py"));
    expect(await screen.findByText(/1 kept for individual review/)).toBeTruthy();

    await act(async () => stream.lastProps.onContext(TARGETS[0], "evidence"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();

    act(() => drawer.lastProps.onUploadEvidence([new File(["x"], "shot.png", { type: "image/png" })]));
    await waitFor(() => expect(drawer.lastProps.uploading).toBe(true));

    await act(async () => { release(); await Promise.resolve(); });
    await waitFor(() => expect(drawer.lastProps.uploading).toBe(false));
    expect(screen.getByText(/1 kept for individual review/)).toBeTruthy();
  });

  it("clears a zero-match search with Escape after focus has left the search box", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "zzz-no-such-target");
    expect(await screen.findByText("0 targets · 0 files")).toBeTruthy();
    expect(screen.getByText("No review targets match this search.")).toBeTruthy();

    act(() => search.blur());
    await userEvent.keyboard("{Escape}");

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
  });
});
