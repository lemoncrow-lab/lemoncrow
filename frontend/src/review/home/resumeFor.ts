import { partSlug } from "../overview/overviewModel";
import type { ReviewResume, ReviewTarget } from "../types";

/** Where One at a time is: "decision n of N" on a decision, the Quick check on a quick check, nothing on anything else. */
export function focusResume(
  targets: readonly ReviewTarget[],
  current: Pick<ReviewTarget, "target_id" | "symbol"> | null,
  decisions: readonly Pick<ReviewTarget, "target_id">[],
): ReviewResume | null {
  if (!current) return null;
  const index = decisions.findIndex((target) => target.target_id === current.target_id);
  const part = partSlug(current as ReviewTarget, targets);
  if (index >= 0) return { screen: "one", part, position: index + 1, total: decisions.length };
  return { screen: "quick_check", part, position: 1, total: 1 };
}

export const QUICK_CHECK_RESUME = (part: string): ReviewResume => ({ screen: "quick_check", part, position: 1, total: 1 });
export const WRAP_RESUME: ReviewResume = { screen: "wrap", part: "", position: 0, total: 0 };
