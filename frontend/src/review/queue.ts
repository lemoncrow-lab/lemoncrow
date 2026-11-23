/**
 * P11-2: what a reviewer must read on this revision: one queue, three counts. Pure.
 *
 * - read: every open decision and quick check that is not safe to confirm in one step, in the order they are asked:
 *   changed since the review with no proof, then with proof, then the rest, decisions before quick checks inside a tier.
 * - confirm: the open quick checks `guardOfPart` clears: exactly the parts Quick check offers in one batch (the same
 *   function over the same inputs; what code has loaded does not decide membership). A part
 *   reopened since the reviewer's review is here only when the server proved the change routine against the content
 *   the reviewer reviewed; a part the server did not answer for (hosted) is never here.
 * - unchanged: parts the reviewer already judged on an earlier revision and that have not changed since.
 *
 * The headline counts are the lengths of these three lists, so the headline and the queue cannot disagree.
 */
import { isJudgedState, targetAnswer } from "./answerModel";
import { decisionQueue, needsAnswer } from "./focus/focusModel";
import { guardOfPart, plainGuardContext, quickParts, type GuardContext } from "./quickCheck/quickCheckModel";
import { readOrder, type HasProof } from "./sinceReview";
import type { ReviewTarget } from "./types";

export interface ReviewQueue {
  read: ReviewTarget[];
  confirm: ReviewTarget[];
  unchanged: ReviewTarget[];
  /** The reviewer judged parts on an earlier revision of this change: the line is about their own review. */
  followsReview: boolean;
}

/** `targets` need not be ordered; the lists come back in the order Focus and Quick check ask them. */
export function reviewQueue(targets: readonly ReviewTarget[], revisionId: string, hasProof?: HasProof, context: GuardContext = plainGuardContext(revisionId)): ReviewQueue {
  const open: ReviewTarget[] = [];
  const unchanged: ReviewTarget[] = [];
  let followsReview = false;
  for (const target of targets) {
    if (targetAnswer(target).category === "accepted_by_rule") continue;
    const earlier = Boolean(target.reviewed_revision_id) && target.reviewed_revision_id !== revisionId;
    if (earlier) followsReview = true;
    if (needsAnswer(target)) open.push(target);
    else if (isJudgedState(target.state) && !target.changed_since_mark && earlier) unchanged.push(target);
  }
  const confirm: ReviewTarget[] = [];
  const guardedQuick: ReviewTarget[] = [];
  for (const target of quickParts(open, hasProof)) (guardOfPart(target, context).guarded ? guardedQuick : confirm).push(target);
  // One order across both kinds: changed since the review without proof, with proof, then the rest. Inside a tier the
  // decisions come before the quick checks (the stable sort keeps decisionQueue's order, then Quick check's).
  const read = readOrder([...decisionQueue(open, hasProof), ...guardedQuick], hasProof);
  return { read, confirm, unchanged, followsReview };
}

/** "3 to read · 2 to confirm in one step · 14 unchanged since your review": the three lengths, nothing else. */
export function queueLine(queue: Pick<ReviewQueue, "read" | "confirm" | "unchanged">): string {
  return `${queue.read.length} to read · ${queue.confirm.length} to confirm in one step · ${queue.unchanged.length} unchanged since your review`;
}
