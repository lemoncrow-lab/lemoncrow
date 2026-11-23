/**
 * P11-2: what a reviewer must read again, from what changed since their own review. Pure.
 *
 * A part the reviewer judged and that has since changed is "reopened". The server compares the reopened part with the
 * content the reviewer reviewed (not with the diff base) and says, in `since_review`, whether the change is only
 * formatting, a comment or a lone rename. This module reads that one answer and nothing else: a part with no answer, an
 * answer it does not recognise, or a hosted server that never sends one is "not routine" and stays in the read queue.
 * A rename hint (`renamed_from`) is never read here: a rename alone is not proof the content is the same.
 */
import { partOutcome, type PartProofProjection } from "./partProof";
import type { ReviewTarget } from "./types";

/** The rules the server proves for a reopened part. Anything else is not recognised and reads as not routine. */
const SINCE_REVIEW_RULES: ReadonlySet<string> = new Set(["comment_only", "formatting_only", "rename_only"]);

/**
 * A hunk nobody has answered on this revision, whose file was re-hunked since the reviewer's review (every hunk mark of the
 * file resets), and which is byte for byte a hunk the reviewer answered Looks good. It is not "reopened" (its state is
 * unreviewed); the server says so with this one rule, and a person still confirms it in the batch.
 */
const REHUNKED_RULE = "rehunked_identical";

/** The part was judged and its content has changed since. */
export function isReopened(target: Pick<ReviewTarget, "state" | "changed_since_mark">): boolean {
  return target.state === "changed_since_review" && target.changed_since_mark;
}

/** An unanswered hunk the server proved is only a reviewed hunk renumbered by a re-hunking of its file. Fails closed. */
export function isRehunkedIdentical(target: Pick<ReviewTarget, "kind" | "state" | "since_review">): boolean {
  const proof = target.since_review;
  return target.kind === "hunk" && target.state === "unreviewed" && proof?.routine === true && proof.rule === REHUNKED_RULE;
}

/** A part whose change since the reviewer's review the server proved routine. Fails closed. */
export function routineSinceReview(target: Pick<ReviewTarget, "kind" | "state" | "changed_since_mark" | "since_review">): boolean {
  const proof = target.since_review;
  if (isRehunkedIdentical(target)) return true;
  return isReopened(target) && proof?.routine === true && SINCE_REVIEW_RULES.has(proof.rule);
}

/** The one line a row shows for a part changed since the review: what changed, or why it must be read again. "" for any other part. */
export function sinceReviewLine(target: Pick<ReviewTarget, "kind" | "state" | "changed_since_mark" | "since_review">): string {
  if (isRehunkedIdentical(target)) return `Re-hunked since your review · ${target.since_review?.detail?.trim() ?? ""}`.replace(/ · $/, "");
  if (!isReopened(target)) return "";
  const detail = target.since_review?.detail?.trim() ?? "";
  return detail ? `Changed since your review · ${detail}` : "Changed since your review";
}

/** Test outcomes that count as a proof for the order (P11-1): a passing test reaches the file. A failure is no comfort. */
export function countsAsProof(outcome: string): boolean {
  return outcome === "proven" || outcome === "exercised";
}

/** Whether a part's file has a passing test that reaches it, read from the shared P11-1 record. */
export type HasProof = (path: string) => boolean;

/** `HasProof` over the revision's shared record. No record yet reads as no proof (the part is read first, the safe side). */
export function hasProofFrom(proof: PartProofProjection | null | undefined): HasProof {
  return (path) => !proof?.load_failed && countsAsProof(partOutcome(proof, path).outcome);
}

/**
 * Where a part waits in the queue: 0 changed since the review with no proof, 1 changed with proof, 2 everything else
 * (new parts, and parts the reviewer has not answered). Stable: inside a tier the server's own order stands.
 */
export function readTier(target: Pick<ReviewTarget, "state" | "changed_since_mark" | "path">, hasProof?: HasProof): 0 | 1 | 2 {
  if (!isReopened(target)) return 2;
  return hasProof?.(target.path) ? 1 : 0;
}

/** `parts` (already in the server's order) with the changed-since-review parts first, no-proof before proof. */
export function readOrder(parts: readonly ReviewTarget[], hasProof?: HasProof): ReviewTarget[] {
  const tiers = new Map<ReviewTarget, number>(parts.map((part) => [part, readTier(part, hasProof)]));
  // Array.prototype.sort is stable, so the server's order is kept inside each tier.
  return [...parts].sort((a, b) => (tiers.get(a) ?? 2) - (tiers.get(b) ?? 2));
}
