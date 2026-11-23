/**
 * "Confirm all N quick checks": which quick checks one click may confirm, and how the click is split into the server's
 * bulk-mark rounds. Pure. The guard is the one Quick check batches with (`guardOfPart`), so a part that needs its own answer
 * there needs it here too, and a part Quick check would batch is offered here. A part still unanswered on this revision goes
 * through the server's bulk mark; the few others (a change since the reviewer's review the server proved routine) through
 * the single mark, as Quick check's batch records them. A part without a content identity is never offered.
 */
import { needsAnswer } from "../focus/focusModel";
import type { ReviewTarget } from "../types";
import { guardOfPart, type GuardContext } from "./quickCheckModel";

/** The server's own limit per bulk mark (POST /marks/bulk refuses more than 500 parts at once). */
export const BULK_MARK_LIMIT = 500;

export interface QuickConfirmSplit {
  /** Open quick checks one click may confirm, in the order given. */
  confirm: ReviewTarget[];
  /** Open quick checks that need their own answer (guarded, or no content identity). */
  ownAnswer: number;
}

export function quickConfirmSplit(quick: readonly ReviewTarget[], context: GuardContext): QuickConfirmSplit {
  const confirm: ReviewTarget[] = [];
  let ownAnswer = 0;
  for (const part of quick) {
    if (!needsAnswer(part)) continue;
    const batchable = part.state !== "unknown"
      && part.fingerprint_method !== "unknown"
      && part.fingerprint_method !== "revision_compare"
      && !guardOfPart(part, context).guarded;
    if (batchable) confirm.push(part); else ownAnswer += 1;
  }
  return { confirm, ownAnswer };
}

/** `list` in rounds of at most `size`, in order; every item is in exactly one round. */
export function rounds<T>(list: readonly T[], size = BULK_MARK_LIMIT): T[][] {
  const out: T[][] = [];
  for (let at = 0; at < list.length; at += size) out.push(list.slice(at, at + size));
  return out;
}

/** What one confirm-all recorded: the server's mark events and single marks (what Undo takes back), and the parts it would not mark. */
export interface QuickConfirmResult {
  /** Recorded by the bulk mark: Undo sends these event ids back. */
  marked: { unit_key: string; event_id: string }[];
  /** Recorded one by one (not unanswered on this revision before): Undo answers each back to unreviewed. */
  single: ReviewTarget[];
  skipped: { unit_key: string; path: string; reason: string }[];
  /** A round failed after earlier rounds were recorded; what was recorded is still listed and can be undone. */
  failed?: boolean;
}

export function confirmAllLabel(count: number): string {
  return count === 1 ? "Confirm 1 quick check" : `Confirm all ${count} quick checks`;
}

export function confirmedCount(result: Pick<QuickConfirmResult, "marked" | "single">): number {
  return result.marked.length + result.single.length;
}

export function confirmedLine(result: QuickConfirmResult): string {
  const count = confirmedCount(result);
  const head = `${count} ${count === 1 ? "quick check" : "quick checks"} confirmed as Looks good`;
  const skipped = result.skipped.length > 0 ? ` · ${result.skipped.length} left for their own answer (${result.skipped[0].reason})` : "";
  return `${head}${skipped}${result.failed ? " · the rest could not be saved" : ""}`;
}
