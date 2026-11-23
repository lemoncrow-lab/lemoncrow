/** Done's "Next up": which other open review comes next and why, from the same step rules as the Reader's primary action. */
import { nextReviewGuideTarget, progressFromTargets } from "../readerModel";
import { partsOf, plural } from "../overview/overviewModel";
import { targetStep, type ReviewPrimaryAction } from "../reviewPrimaryAction";
import type { FeedbackStatus, ReviewSessionInfo, ReviewTarget } from "../types";

export interface NextUpCandidate {
  review: ReviewSessionInfo;
  targets: readonly ReviewTarget[];
  feedback?: FeedbackStatus;
}

export interface NextUp {
  review: ReviewSessionInfo;
  /** The step the Reader would offer there; null when nothing is left to judge. */
  action: ReviewPrimaryAction | null;
  /** Something is left to judge and no sent note is still with the agent. */
  yourTurn: boolean;
  /** Decisions still to answer there. */
  decisions: number;
  /** The reason line after "Next up · "; only claims what was checked. */
  reason: string;
}

/** The viewer snoozed this review and the snooze still holds: it is not offered (the server ends it on a newer Snapshot). */
export function isSnoozedReview(review: Pick<ReviewSessionInfo, "queue">, now: number): boolean {
  const until = Date.parse(review.queue?.snoozed_until ?? "");
  return Number.isFinite(until) && until > now;
}

const ANSWERED = new Set(["reviewed", "needs_changes"]);

export function describeNextUp(candidate: NextUpCandidate): Omit<NextUp, "reason"> {
  const { review, targets, feedback } = candidate;
  const handoff = { unsent: feedback?.unpublished ?? 0, published: feedback?.published ?? 0, inFlight: feedback?.in_flight ?? 0 };
  const action = targetStep({
    progress: progressFromTargets(targets), nextTarget: nextReviewGuideTarget(targets), handoff, started: false,
  });
  const withAgent = handoff.published + handoff.inFlight > 0 && handoff.unsent === 0;
  return {
    review, action,
    yourTurn: action !== null && !withAgent,
    decisions: partsOf(targets, "decision").filter((target) => !ANSWERED.has(target.state)).length,
  };
}

/**
 * Fewest decisions first among reviews that are waiting on the reviewer, then the directory order. "Quickest to finish"
 * is only said when every open review was compared (otherwise "one of the quickest") and two or more are waiting on the reviewer and this one has fewer decisions than the next.
 */
export function pickNextUp(allCandidates: readonly NextUpCandidate[], comparedAll = true, now = Date.now()): NextUp | null {
  const candidates = allCandidates.filter((candidate) => !isSnoozedReview(candidate.review, now));
  if (candidates.length === 0) return null;
  const described = candidates.map(describeNextUp);
  const turn = described.filter((item) => item.yourTurn);
  const pool = turn.length > 0 ? turn : described;
  const best = pool.reduce((a, b) => (b.decisions < a.decisions ? b : a));
  const decisions = best.decisions > 0 ? ` (${plural(best.decisions, "decision")})` : "";
  const strictlyQuickest = turn.length > 1 && turn.some((item) => item !== best && item.decisions > best.decisions);
  const reason = !best.yourTurn
    ? best.action === null ? "nothing left to judge" : "waiting on the coding agent"
    : strictlyQuickest ? `your turn · ${comparedAll ? "quickest to finish" : "one of the quickest to finish"}${decisions}` : `your turn${decisions}`;
  return { ...best, reason };
}
