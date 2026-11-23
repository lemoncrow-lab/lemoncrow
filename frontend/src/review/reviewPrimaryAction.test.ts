import { describe, expect, it } from "vitest";

import type { ReviewGuideTarget } from "./readerModel";
import {
  deriveReviewPrimaryAction,
  finishAvailableInMenu,
  headerLeadsWithSend,
  primaryActionDestination,
  sendCommentsLabel,
  type ReviewPrimaryActionKind,
  type ReviewPrimaryDestination,
} from "./reviewPrimaryAction";
import type { ReviewReadinessModel } from "./ReviewReadinessSheet";
import type { FeedbackDeliveryCapability, FeedbackStatus, ReviewProgress } from "./types";

const progress: ReviewProgress = {
  target_count: 4,
  reviewed: 4,
  changed_since_review: 0,
  needs_changes: 0,
  unreviewed: 0,
  unknown: 0,
  mechanical: 0,
};
const feedback: FeedbackStatus = { open_total: 0, unpublished: 0, published: 0, in_flight: 0, addressed: 0 };
const readiness: ReviewReadinessModel = {
  ready: true,
  label: "Ready for verdict",
  detail: "Ready",
  blockers: [],
  evidenceFacts: [],
  targetCount: 4,
  reviewedTargets: 4,
  changedSinceReview: 0,
  needsChanges: 0,
  targetPending: 0,
  openComments: 0,
  failedChecks: 0,
  unresolvedChecks: 0,
  outcome: null,
};
const claude: FeedbackDeliveryCapability = {
  supported: true,
  host: "claude",
  session_id: "s-1",
  target_ref: "claude:s-1",
  label: "Claude",
  reason: "",
};
const noAgent: FeedbackDeliveryCapability = {
  supported: false,
  host: "",
  session_id: "",
  target_ref: "",
  label: "",
  reason: "No exact author session",
};

type Input = Parameters<typeof deriveReviewPrimaryAction>[0];

function input(overrides: Partial<Input> = {}): Input {
  return {
    historical: false,
    sessionStatus: "open",
    revisionAdvanced: false,
    sourceChanged: false,
    commentsState: "ready",
    feedbackStatus: feedback,
    progress,
    readiness,
    nextTarget: null,
    ...overrides,
  };
}

function action(overrides: Partial<Input> = {}) {
  return deriveReviewPrimaryAction(input(overrides));
}

/** The queue head as `nextReviewGuideTarget` returns it. */
function head(targetId: string, reasonCode: string, reason: string): ReviewGuideTarget {
  return {
    targetId,
    path: "src/a.py",
    label: "authorize",
    reason,
    reasonCode,
    attention: ["request_change_open", "changed_since_judgment", "decision_scope_changed"].includes(reasonCode)
      ? "review_first"
      : "understand",
  };
}

interface Expected {
  kind: ReviewPrimaryActionKind;
  label: string;
  disabled: boolean;
  destination: ReviewPrimaryDestination;
  detail: string;
  /** "Finish review…" stays in the menu. */
  finishInMenu: boolean;
  opensReadiness: boolean;
  targetId?: string;
}

const github = (counts: Partial<NonNullable<FeedbackStatus["github"]>>, open = 2): FeedbackStatus => ({
  ...feedback,
  open_total: open,
  unpublished: counts.unpublished ?? 0,
  github: { unpublished: 0, published: 0, in_flight: 0, ...counts },
});

const FINISH_DETAIL = "Human judgments, discussions, and required verification have no current blockers.";

const MATRIX: Array<[string, Partial<Input>, Expected]> = [
  [
    "nothing judged yet",
    { progress: { ...progress, reviewed: 0, unreviewed: 4 }, nextTarget: head("t-first", "unreviewed", "Not reviewed yet") },
    { kind: "review_target", label: "Start review", disabled: false, destination: "target", detail: "Not reviewed yet", finishInMenu: true, opensReadiness: false, targetId: "t-first" },
  ],
  [
    "discussions still loading never hold back the next area",
    { commentsState: "loading", progress: { ...progress, reviewed: 0, unreviewed: 4 }, nextTarget: head("t-first", "unreviewed", "Not reviewed yet") },
    { kind: "review_target", label: "Start review", disabled: false, destination: "target", detail: "Not reviewed yet", finishInMenu: true, opensReadiness: false, targetId: "t-first" },
  ],
  [
    "unreviewed work remains",
    { progress: { ...progress, reviewed: 2, unreviewed: 2 }, nextTarget: head("t-next", "unreviewed", "Not reviewed yet") },
    { kind: "review_target", label: "Review next: authorize", disabled: false, destination: "target", detail: "Not reviewed yet", finishInMenu: true, opensReadiness: false, targetId: "t-next" },
  ],
  [
    "unreviewed work remains before the queue has loaded",
    { progress: { ...progress, reviewed: 2, unreviewed: 2 } },
    { kind: "review_target", label: "Review next part", disabled: true, destination: "none", detail: "2 parts still need human judgment.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "requested changes remain",
    { progress: { ...progress, reviewed: 3, needs_changes: 1 }, nextTarget: head("t-asked", "request_change_open", "Requested change is still open") },
    { kind: "review_requested_changes", label: "Review requested changes", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-asked" },
  ],
  [
    "requested changes head the queue ahead of changed and unreviewed work",
    {
      progress: { ...progress, reviewed: 0, changed_since_review: 1, needs_changes: 2, unreviewed: 1 },
      nextTarget: head("t-asked", "request_change_open", "Requested change is still open"),
    },
    { kind: "review_requested_changes", label: "Review 2 requested changes", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-asked" },
  ],
  [
    "a file-level requested change heads the queue without a needs-changes mark",
    {
      progress: { ...progress, reviewed: 2, unreviewed: 2 },
      nextTarget: head("t-file", "request_change_open", "Requested change is still open"),
    },
    { kind: "review_target", label: "Review next: authorize", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-file" },
  ],
  [
    "content changed since review",
    { progress: { ...progress, reviewed: 3, changed_since_review: 1 }, nextTarget: head("t-changed", "changed_since_judgment", "Changed since your review") },
    { kind: "review_changed", label: "Re-review changed part", disabled: false, destination: "target", detail: "Changed since your review", finishInMenu: true, opensReadiness: false, targetId: "t-changed" },
  ],
  [
    "a reconsidered decision heads the queue ahead of changed areas",
    {
      progress: { ...progress, reviewed: 1, changed_since_review: 2, unreviewed: 1 },
      nextTarget: head("t-decision", "decision_scope_changed", "Decision: scope changed"),
    },
    { kind: "review_target", label: "Review next: authorize", disabled: false, destination: "target", detail: "Decision: scope changed", finishInMenu: true, opensReadiness: false, targetId: "t-decision" },
  ],
  [
    "addressed comments need re-review",
    { feedbackStatus: { ...feedback, open_total: 2, addressed: 2 }, feedbackDelivery: claude },
    { kind: "rereview_comments", label: "Re-review 2", disabled: false, destination: "comments", detail: "The coding agent marked these comments addressed; only your re-review can resolve them.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "feedback addressed by an author without an agent session",
    { feedbackStatus: { ...feedback, open_total: 1, addressed: 1 }, feedbackDelivery: noAgent },
    { kind: "rereview_comments", label: "Re-review 1", disabled: false, destination: "comments", detail: "The author marked these comments addressed; only your re-review can resolve them.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "verification fails",
    { readiness: { ...readiness, ready: false, failedChecks: 2, detail: "2 failed checks" } },
    { kind: "review_readiness", label: "Review 2 failed checks", disabled: false, destination: "readiness", detail: "Current verification explicitly reports failure.", finishInMenu: true, opensReadiness: true },
  ],
  [
    "one check fails",
    { readiness: { ...readiness, ready: false, failedChecks: 1, detail: "1 failed check" } },
    { kind: "review_readiness", label: "Review failed check", disabled: false, destination: "readiness", detail: "Current verification explicitly reports failure.", finishInMenu: true, opensReadiness: true },
  ],
  [
    "checks have not run but every part is reviewed",
    { readiness: { ...readiness, ready: false, unresolvedChecks: 1, detail: "1 unresolved check" } },
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: "Every part is reviewed. Tests or checks have not run; Finish lists what is still open.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "open comments remain",
    { feedbackStatus: { ...feedback, open_total: 2 }, feedbackDelivery: claude },
    { kind: "review_comments", label: "Review comments", disabled: false, destination: "comments", detail: "Open human review discussions still need attention.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "unsent comments never take over the next area to review",
    { feedbackStatus: { ...feedback, open_total: 2, unpublished: 2 }, feedbackDelivery: claude, progress: { ...progress, reviewed: 3, unreviewed: 1 }, nextTarget: head("t-next", "unreviewed", "Not reviewed yet") },
    { kind: "review_target", label: "Review next: authorize", disabled: false, destination: "target", detail: "Not reviewed yet", finishInMenu: true, opensReadiness: false, targetId: "t-next" },
  ],
  [
    "unsent comments with everything reviewed go to the verdict",
    { feedbackStatus: { ...feedback, open_total: 1, unpublished: 1 }, feedbackDelivery: noAgent },
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: true, opensReadiness: false },
  ],
  [
    "feedback delivery in flight",
    { feedbackStatus: { ...feedback, open_total: 1, in_flight: 1 }, feedbackDelivery: claude },
    { kind: "waiting_agent", label: "Waiting on Claude", disabled: false, destination: "handoff", detail: "Feedback is with Claude; the next revision or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "feedback with the agent, working",
    { feedbackStatus: { ...feedback, open_total: 2, published: 2 }, feedbackDelivery: claude },
    { kind: "waiting_agent", label: "Waiting on Claude", disabled: false, destination: "handoff", detail: "Feedback is with Claude; the next revision or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "feedback copied for the author",
    { feedbackStatus: { ...feedback, open_total: 2, published: 2 }, feedbackDelivery: { ...noAgent, supported: true, kind: "export", label: "author", target_ref: "clipboard:author" } },
    { kind: "waiting_agent", label: "Waiting on author", disabled: false, destination: "handoff", detail: "Feedback is with author; the next revision or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "requested changes handed to the agent with nothing else to judge",
    { feedbackStatus: { ...feedback, open_total: 2, published: 2 }, feedbackDelivery: claude, progress: { ...progress, reviewed: 3, needs_changes: 1 }, nextTarget: head("t-asked", "request_change_open", "Requested change is still open") },
    { kind: "waiting_agent", label: "Waiting on Claude", disabled: false, destination: "handoff", detail: "Feedback is with Claude; the next revision or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "requested changes copied for the author while other parts are unreviewed: review goes on",
    {
      feedbackStatus: { ...feedback, open_total: 1, published: 1 },
      feedbackDelivery: { ...noAgent, supported: true, kind: "export", label: "author", target_ref: "clipboard:author" },
      progress: { ...progress, reviewed: 1, needs_changes: 1, unreviewed: 2 },
      nextTarget: head("t-asked", "request_change_open", "Requested change is still open"),
      afterHandoff: { ...head("t-next", "unreviewed", "Not reviewed yet"), label: "settle" },
    },
    { kind: "review_target", label: "Review next: settle", disabled: false, destination: "target", detail: "Not reviewed yet", finishInMenu: true, opensReadiness: false, targetId: "t-next" },
  ],
  [
    "requested changes not yet sent are still the reviewer's",
    { feedbackStatus: { ...feedback, open_total: 2, unpublished: 2 }, feedbackDelivery: claude, progress: { ...progress, reviewed: 3, needs_changes: 1 }, nextTarget: head("t-asked", "request_change_open", "Requested change is still open") },
    { kind: "review_requested_changes", label: "Review requested changes", disabled: false, destination: "target", detail: "Requested change is still open", finishInMenu: true, opensReadiness: false, targetId: "t-asked" },
  ],
  [
    "all work reviewed",
    {},
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: true, opensReadiness: false },
  ],
  [
    "source drift alone is not a Review step",
    { sourceChanged: true },
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: true, opensReadiness: false },
  ],
  [
    "GitHub PR review with comments not on the PR goes to the verdict",
    { feedbackStatus: github({ unpublished: 2 }), feedbackDelivery: noAgent },
    { kind: "finish", label: "Finish review", disabled: false, destination: "finish_sheet", detail: FINISH_DETAIL, finishInMenu: true, opensReadiness: false },
  ],
  [
    "GitHub PR review with a publish in doubt",
    { feedbackStatus: github({ unpublished: 1, in_flight: 1 }) },
    { kind: "delivery_pending", label: "Publish pending", disabled: true, destination: "none", detail: "A GitHub publish is in progress or its result is unknown; LemonCrow checks the pull request before posting again.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "GitHub PR review with everything published waits on the pull request",
    { feedbackStatus: github({ published: 2 }), feedbackDelivery: { ...noAgent, kind: "github", supported: true, label: "PR #7" } },
    { kind: "waiting_agent", label: "Waiting on PR #7", disabled: false, destination: "handoff", detail: "Feedback is with PR #7; the next revision or addressed comments return this Review to you.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "hosted policy blocker",
    {
      readiness: {
        ...readiness,
        ready: false,
        label: "Not ready · 1",
        detail: "1 more approval required",
        blockers: [{ id: "policy:reviewers", label: "1 more approval required", detail: "Hosted policy", tone: "amber" }],
      },
    },
    { kind: "review_readiness", label: "Review blockers", disabled: false, destination: "readiness", detail: "1 more approval required", finishInMenu: true, opensReadiness: true },
  ],
  [
    "historical revision",
    { historical: true, feedbackStatus: { ...feedback, open_total: 1, unpublished: 1 }, feedbackDelivery: claude },
    { kind: "go_latest", label: "Back to latest", disabled: false, destination: "latest_snapshot", detail: "Historical revisions are read-only. Return to the latest revision.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished Review",
    { sessionStatus: "finished" },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished review with comments never sent reopens",
    { sessionStatus: "finished", feedbackStatus: { ...feedback, open_total: 2, unpublished: 2 }, feedbackDelivery: claude },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished local review with a handoff in doubt",
    { sessionStatus: "finished", feedbackStatus: { ...feedback, open_total: 1, unpublished: 1, in_flight: 1 }, feedbackDelivery: claude },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished GitHub PR review whose comments are not on the PR reopens",
    { sessionStatus: "finished", feedbackStatus: github({ unpublished: 2 }) },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "finished GitHub PR review with a publish in doubt",
    { sessionStatus: "finished", feedbackStatus: github({ unpublished: 1, in_flight: 1 }) },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "archived Review",
    { sessionStatus: "archived", feedbackStatus: github({ unpublished: 2 }) },
    { kind: "restore", label: "Restore review", disabled: false, destination: "finish_sheet", detail: "Restore this archived review before making changes.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "degraded: content identity unavailable",
    { progress: { ...progress, reviewed: 3, unknown: 1 }, nextTarget: head("t-unknown", "fingerprint_unavailable", "Content identity unavailable; review individually") },
    { kind: "review_target", label: "Review next: authorize", disabled: false, destination: "target", detail: "Content identity unavailable; review individually", finishInMenu: true, opensReadiness: false, targetId: "t-unknown" },
  ],
  [
    "degraded: landing conditions unavailable fail closed",
    {
      readiness: {
        ...readiness,
        ready: false,
        label: "Landing readiness unavailable",
        detail: "The server-authoritative landing conditions could not be loaded; readiness fails closed until they are available.",
      },
    },
    { kind: "review_readiness", label: "Review blockers", disabled: false, destination: "readiness", detail: "The server-authoritative landing conditions could not be loaded; readiness fails closed until they are available.", finishInMenu: true, opensReadiness: true },
  ],
  [
    "degraded: discussions could not load",
    { commentsState: "failed" },
    { kind: "reload_state", label: "Retry review state", disabled: false, destination: "reload", detail: "Review discussions could not be loaded. Reload before deciding the review is complete.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "discussions still loading",
    { commentsState: "loading" },
    { kind: "checking_comments", label: "Checking comments…", disabled: true, destination: "none", detail: "Existing review discussions are still loading; readiness cannot assume there are none.", finishInMenu: true, opensReadiness: false },
  ],
  [
    "newer revision exists",
    { revisionAdvanced: true, progress: { ...progress, reviewed: 3, unreviewed: 1 }, nextTarget: head("t-next", "unreviewed", "Not reviewed yet") },
    { kind: "reload_state", label: "Reload review", disabled: false, destination: "reload", detail: "A newer revision exists and this one no longer accepts changes. Reload before judging the new revision.", finishInMenu: false, opensReadiness: false },
  ],
  [
    "newer revision on a finished GitHub PR review never publishes from the old one",
    { sessionStatus: "finished", revisionAdvanced: true, feedbackStatus: github({ unpublished: 2 }) },
    { kind: "reopen", label: "Reopen review", disabled: false, destination: "finish_sheet", detail: "Reopen this finished review before making new judgments. Finishing does not commit or merge.", finishInMenu: false, opensReadiness: false },
  ],
];

describe("deriveReviewPrimaryAction state matrix (plan §8.10)", () => {
  it.each(MATRIX)("%s", (_name, overrides, expected) => {
    const state = input(overrides);
    const result = deriveReviewPrimaryAction(state);
    const destination = primaryActionDestination(result);
    expect({
      kind: result.kind,
      label: result.label,
      disabled: result.disabled,
      destination,
      detail: result.detail,
      finishInMenu: finishAvailableInMenu({
        sessionStatus: state.sessionStatus,
        historical: state.historical,
        revisionAdvanced: state.revisionAdvanced,
        action: result,
      }),
      opensReadiness: destination === "readiness",
      ...(expected.targetId !== undefined ? { targetId: result.targetId } : {}),
    }).toEqual(expected);
  });

  it("gives every enabled action somewhere to go and every disabled one nowhere", () => {
    for (const [, overrides] of MATRIX) {
      const result = action(overrides);
      expect(primaryActionDestination(result) === "none").toBe(result.disabled);
    }
  });
});

describe("header primary selection", () => {
  const open = { locked: false, sessionStatus: "open" as const, commentsState: "ready" as const };
  const states: Array<[string, Partial<Input>, string]> = [
    ["nothing reviewed", { progress: { ...progress, reviewed: 0, unreviewed: 4 }, nextTarget: head("t-1", "unreviewed", "Not reviewed yet") }, "Start review"],
    ["in progress", { progress: { ...progress, reviewed: 2, unreviewed: 2 }, nextTarget: head("t-1", "unreviewed", "Not reviewed yet") }, "Review next: authorize"],
    ["all reviewed", {}, "Finish review"],
  ];

  it("stops saying Start review once the reviewer is in the code", () => {
    const untouched = { progress: { ...progress, reviewed: 0, unreviewed: 4 }, nextTarget: head("t-1", "unreviewed", "Not reviewed yet") };
    expect(deriveReviewPrimaryAction(input({ ...untouched, started: false })).label).toBe("Start review");
    const inCode = deriveReviewPrimaryAction(input({ ...untouched, started: true }));
    expect(inCode.label).toBe("Review next: authorize");
    expect(inCode.targetId).toBe("t-1");
  });

  it.each(states)("with nothing unsent, the review step leads (%s)", (_name, overrides, label) => {
    const state = input({ ...overrides, feedbackStatus: { ...feedback, open_total: 0, unpublished: 0 } });
    expect(headerLeadsWithSend({ ...open, unsent: 0 })).toBe(false);
    expect(deriveReviewPrimaryAction(state).label).toBe(label);
  });

  it.each(states)("with unsent comments, Send leads and the review step moves to the menu (%s)", (_name, overrides, label) => {
    const state = input({ ...overrides, feedbackStatus: { ...feedback, open_total: 2, unpublished: 2 }, feedbackDelivery: claude });
    expect(headerLeadsWithSend({ ...open, unsent: 2 })).toBe(true);
    expect(sendCommentsLabel(2)).toBe("Send 2 comments");
    expect(deriveReviewPrimaryAction(state).label).toBe(label);
  });

  it("never leads with Send while comments load, on a locked or finished review", () => {
    expect(sendCommentsLabel(1)).toBe("Send 1 comment");
    expect(headerLeadsWithSend({ ...open, unsent: 1, commentsState: "loading" })).toBe(false);
    expect(headerLeadsWithSend({ ...open, unsent: 1, locked: true })).toBe(false);
    expect(headerLeadsWithSend({ ...open, unsent: 1, sessionStatus: "finished" })).toBe(false);
  });
});

describe("deriveReviewPrimaryAction", () => {
  it("counts a pull request Review's handoff on the PR, and an agent route's with the agent", () => {
    const github = { ...feedback, open_total: 2, unpublished: 2, github: { unpublished: 0, published: 2, in_flight: 0 } };
    expect(action({ feedbackStatus: github })).toMatchObject({ kind: "waiting_agent", label: "Waiting on author" });
    expect(action({ feedbackStatus: github, feedbackDelivery: { ...claude, kind: "github", label: "PR #3" } })).toMatchObject({ label: "Waiting on PR #3" });
    expect(action({ feedbackStatus: github, feedbackDelivery: claude })).toMatchObject({ kind: "finish" });
  });

  it("never offers finish while current part judgment remains", () => {
    expect(action({
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: head("t-1", "unreviewed", "Not reviewed yet"),
    })).toMatchObject({ kind: "review_target", label: "Review next: authorize", targetId: "t-1", disabled: false });
  });

  it("puts changed work ahead of ordinary pending review", () => {
    expect(action({
      progress: { ...progress, reviewed: 2, changed_since_review: 1, unreviewed: 1 },
      nextTarget: head("changed", "changed_since_judgment", "Changed since your review"),
    })).toMatchObject({ kind: "review_changed", label: "Re-review changed part", targetId: "changed" });
  });

  it("keeps remaining review ahead of a handoff that is out, and names the real destination once it waits", () => {
    const out = { ...feedback, open_total: 1, published: 1 };
    expect(action({
      feedbackStatus: out,
      feedbackDelivery: claude,
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: head("t-1", "unreviewed", "Not reviewed yet"),
    })).toMatchObject({ kind: "review_target", targetId: "t-1" });
    expect(action({
      feedbackStatus: out,
      feedbackDelivery: { ...claude, kind: "agent", label: "Claude a1b2" },
    })).toMatchObject({ kind: "waiting_agent", label: "Waiting on Claude a1b2" });
  });

  it("keeps source drift separate from review workflow while revision/loading failures remain recovery actions", () => {
    expect(action({ revisionAdvanced: true })).toMatchObject({ kind: "reload_state", label: "Reload review" });
    expect(action({
      sourceChanged: true,
      progress: { ...progress, reviewed: 3, unreviewed: 1 },
      nextTarget: head("t-1", "unreviewed", "Not reviewed yet"),
    })).toMatchObject({ kind: "review_target", label: "Review next: authorize", targetId: "t-1" });
    expect(action({ commentsState: "failed" })).toMatchObject({ kind: "reload_state", label: "Retry review state" });
  });

  it("leads an open, fully judged review with an armable provider with Finish review, never Merge when ready", () => {
    const landing = {
      armable: true,
      landing: { supported: true, armed: false, provider: "github", state: "not_armed" },
    };
    const open = action({ landing });
    expect(open).toMatchObject({ kind: "finish", label: "Finish review" });
    expect(primaryActionDestination(open)).toBe("finish_sheet");
    expect(action({
      readiness: { ...readiness, ready: false, label: "1 landing condition unresolved", detail: "Required checks" },
      landing,
    }).label).not.toMatch(/Merge when ready/);
  });

  it("offers provider-native Merge when ready only after Finish, and keeps an armed merge visible", () => {
    const landing = {
      armable: true,
      landing: { supported: true, armed: false, provider: "github", state: "not_armed" },
    };
    const armed = { ...landing, landing: { ...landing.landing, armed: true, state: "delegated" } };
    const finished = action({ sessionStatus: "finished", landing });
    expect(finished).toMatchObject({ kind: "review_readiness", label: "Merge when ready" });
    expect(primaryActionDestination(finished)).toBe("readiness");
    expect(action({ sessionStatus: "finished", landing: armed })).toMatchObject({ kind: "review_readiness", label: "Merge when ready enabled" });
    // Where no provider can merge, a finished review offers Reopen, not a merge.
    expect(action({ sessionStatus: "finished", landing: { ...landing, landing: { ...landing.landing, supported: false } } }))
      .toMatchObject({ kind: "reopen", label: "Reopen review" });
    // A merge still armed on an open review stays visible so it can be inspected or cancelled.
    expect(action({ landing: armed })).toMatchObject({ kind: "review_readiness", label: "Merge when ready enabled" });
  });

  it("treats a merged landing as done rather than offering to arm it again", () => {
    expect(action({
      landing: { armable: true, landing: { supported: true, armed: false, provider: "github", state: "merged" } },
    })).toMatchObject({ kind: "review_readiness", label: "Merged" });
  });

  it("lets server landing conditions outrank client verification placeholders", () => {
    const landing = {
      armable: false,
      landing: { supported: false, armed: false, provider: "", state: "" },
      conditions: [
        {
          id: "latest-snapshot-reviewed",
          label: "Latest revision not reviewed yet",
          state: "pending" as const,
          detail: "1 part still needs human judgment or re-review.",
          owner: "lemoncrow" as const,
          category: "human_review",
          url: "",
        },
        {
          id: "verification",
          label: "Verification requirements",
          state: "not_applicable" as const,
          detail: "No required verification result is configured or recorded for this revision.",
          owner: "lemoncrow" as const,
          category: "verification",
          url: "",
        },
      ],
    };
    expect(action({
      readiness: {
        ...readiness,
        ready: false,
        unresolvedChecks: 5,
        detail: "5 unresolved checks",
      },
      landing,
    })).toMatchObject({
      kind: "review_readiness",
      label: "Review coverage",
      detail: "1 part still needs human judgment or re-review.",
    });

    expect(action({
      readiness: {
        ...readiness,
        ready: false,
        unresolvedChecks: 5,
        detail: "5 unresolved checks",
      },
      landing: {
        ...landing,
        conditions: [
          { ...landing.conditions[0], state: "pass" as const },
          landing.conditions[1],
        ],
      },
    })).toMatchObject({
      kind: "review_readiness",
      label: "Review blockers",
    });
  });

  it("routes a failed check to evidence, but finishes once only unrun checks are left", () => {
    expect(action({
      readiness: { ...readiness, ready: false, failedChecks: 2, blockers: [{ id: "failed-checks", label: "2 failed checks", detail: "fail", tone: "rose" }] },
    })).toMatchObject({ kind: "review_readiness", label: "Review 2 failed checks" });
    expect(action({
      readiness: { ...readiness, ready: false, unresolvedChecks: 1, blockers: [{ id: "unresolved-checks", label: "1 unresolved check", detail: "unknown", tone: "amber" }] },
    })).toMatchObject({ kind: "finish", label: "Finish review" });
    // Unrun checks plus another unmet requirement are not "only unrun checks": evidence still leads.
    expect(action({
      readiness: {
        ...readiness,
        ready: false,
        unresolvedChecks: 1,
        blockers: [
          { id: "unresolved-checks", label: "1 unresolved check", detail: "unknown", tone: "amber" },
          { id: "policy:reviewers", label: "1 more approval required", detail: "Hosted policy", tone: "amber" },
        ],
      },
    })).toMatchObject({ kind: "review_readiness", label: "Review verification" });
  });

  it("finishes when the server's only open conditions are checks that have not run", () => {
    const condition = (state: "pending" | "fail") => ({
      id: "verification",
      label: "Verification requirements",
      state,
      detail: "No test ran.",
      owner: "lemoncrow" as const,
      category: "verification",
      url: "",
    });
    const landing = (state: "pending" | "fail") => ({
      armable: false,
      landing: { supported: false, armed: false, provider: "", state: "" },
      conditions: [{ ...condition("pending"), id: "latest-snapshot-reviewed", category: "human_review", state: "pass" as const }, condition(state)],
    });
    const notReady = { ...readiness, ready: false, unresolvedChecks: 2, detail: "2 unresolved checks" };
    expect(action({ readiness: notReady, landing: landing("pending") })).toMatchObject({ kind: "finish", label: "Finish review" });
    expect(action({ readiness: notReady, landing: landing("fail") })).toMatchObject({ kind: "review_readiness", label: "Review failed check" });
  });

  it("keeps Finish review… in the menu while Finish review is the primary action", () => {
    const primary = action();
    expect(primary.kind).toBe("finish");
    expect(finishAvailableInMenu({ sessionStatus: "open", historical: false, revisionAdvanced: false, action: primary })).toBe(true);
  });

  it("only offers finish after durable state and readiness are clear", () => {
    expect(action()).toMatchObject({ kind: "finish", label: "Finish review", disabled: false });
  });
});
