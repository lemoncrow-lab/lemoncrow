/**
 * Where a part's header sits in the diff, and which unchanged lines must be
 * shown so the part reads from its signature.
 *
 * A symbol part owns its whole scope, so its header belongs above its first
 * line (the `def`/`class` line), not wedged between context lines. Pierre
 * renders a header as an annotation row *after* a line, so "above line F"
 * means "after line F - 1" (line 0 is Pierre's file-top slot). That works only
 * when F - 1 is a rendered row: in a hunk the patch carries, or in context
 * revealed once the file's contents have loaded. The signature itself is often
 * unchanged context outside the hunk (git keeps three lines), so it is revealed
 * by expanding the gap above the hunk, or, when the gap is long, pinned in the
 * header as text.
 */
import { targetScrollLocation } from "./readerModel";
import type { ReviewTarget } from "./types";

/** New-side extent of one patch hunk (Pierre's parsed hunk fields). */
export interface PatchHunk {
  additionStart: number;
  additionCount: number;
}

/** A signature this far above its hunk is pinned in the header instead of expanding every line between. */
export const SIGNATURE_REVEAL_MAX_LINES = 40;
/**
 * About a screen of code at the reader's 21px rows: a part this tall gets a
 * second set of judgment buttons at its end.
 * lc-debt: line count stands in for rendered height (split rows, wrapped lines and comments differ); measure the rendered part if this misfires.
 */
export const TALL_PART_LINES = 40;

export type SignatureReveal = {
  targetId: string;
  hunkIndex: number;
  /** The signature line, new side. */
  line: number;
  /** "expand": show `lines` unchanged lines above the hunk; "pin": too far, show it in the header. */
  mode: "expand" | "pin";
  lines: number;
};

type HeaderLocation = { side: "additions" | "deletions"; lineNumber: number };

/**
 * The line a part reads from: a symbol's first line (its signature) when it
 * starts above the first change on the new side, else the first change.
 */
export function partFirstLine(target: ReviewTarget): HeaderLocation | null {
  const location = targetScrollLocation(target);
  if (!location) return null;
  const ownsScope = target.kind === "symbol" || target.kind === "document_section";
  if (ownsScope && location.side === "additions" && target.start_line > 0 && target.start_line < location.lineNumber) {
    return { side: "additions", lineNumber: target.start_line };
  }
  return location;
}

function hunkIndexAt(hunks: readonly PatchHunk[], line: number): number {
  return hunks.findIndex((hunk) => hunk.additionCount > 0
    && line >= hunk.additionStart
    && line <= hunk.additionStart + hunk.additionCount - 1);
}

/** First new-side line of the unchanged gap above hunk `index`. */
function gapStart(hunks: readonly PatchHunk[], index: number): number {
  const previous = hunks[index - 1];
  return previous ? previous.additionStart + previous.additionCount : 1;
}

/** Signatures the patch alone leaves folded, and how to show each. */
export function signatureReveals(
  targets: readonly ReviewTarget[],
  hunks: readonly PatchHunk[] | undefined,
  maxLines = SIGNATURE_REVEAL_MAX_LINES,
): SignatureReveal[] {
  if (!hunks?.length) return [];
  const reveals: SignatureReveal[] = [];
  for (const target of targets) {
    const first = partFirstLine(target);
    const change = targetScrollLocation(target);
    if (!first || !change || first.side !== "additions" || change.side !== "additions") continue;
    const hunkIndex = hunkIndexAt(hunks, change.lineNumber);
    if (hunkIndex < 0) continue;
    const hunkTop = hunks[hunkIndex].additionStart;
    const top = gapStart(hunks, hunkIndex);
    // Already a rendered row: inside this hunk or in an earlier one.
    if (first.lineNumber >= hunkTop || first.lineNumber < top) continue;
    const distance = hunkTop - first.lineNumber;
    reveals.push(distance <= maxLines
      // One more line than the signature, when the gap has it, gives the header a row to sit after.
      ? { targetId: target.target_id, hunkIndex, line: first.lineNumber, mode: "expand", lines: Math.min(distance + 1, hunkTop - top) }
      : { targetId: target.target_id, hunkIndex, line: first.lineNumber, mode: "pin", lines: 0 });
  }
  return reveals;
}

/** One expansion per hunk: the most lines any of its parts needs (Pierre's expansions accumulate). */
export function revealExpansions(reveals: readonly SignatureReveal[]): Map<number, number> {
  const byHunk = new Map<number, number>();
  for (const reveal of reveals) byHunk.set(reveal.hunkIndex, Math.max(byHunk.get(reveal.hunkIndex) ?? 0, reveal.lines));
  return byHunk;
}

function renderedByPatch(hunks: readonly PatchHunk[], line: number): boolean {
  return hunkIndexAt(hunks, line) >= 0;
}

/**
 * The annotation slot for a part's header. `contextShown` says the file's
 * contents have loaded, so the reveals from `signatureReveals` are on screen.
 * Without hunk data the header keeps its historical slot above the first change.
 */
export function partHeaderLocation(
  target: ReviewTarget,
  hunks: readonly PatchHunk[] | undefined,
  contextShown = false,
): HeaderLocation {
  const change = targetScrollLocation(target) ?? { side: "additions" as const, lineNumber: 0 };
  const fallback = { side: change.side, lineNumber: change.lineNumber > 1 ? change.lineNumber - 1 : 0 };
  const first = partFirstLine(target);
  if (!first || !hunks?.length || first.lineNumber === change.lineNumber) return fallback;
  const above = first.lineNumber - 1;
  // Line 0 is the file-top slot: right above line 1 only while line 1 shows.
  const anchorRow = above === 0 ? 1 : above;
  if (renderedByPatch(hunks, anchorRow)) return { side: first.side, lineNumber: above };
  if (contextShown) {
    const reveal = signatureReveals([target], hunks).find((item) => item.mode === "expand");
    if (reveal) {
      const hunkTop = hunks[reveal.hunkIndex].additionStart;
      if (anchorRow >= hunkTop - reveal.lines) return { side: first.side, lineNumber: above };
    }
  }
  return fallback;
}

/** Whether a part runs long enough to deserve judgment buttons at its end too. */
export function partIsTall(target: ReviewTarget): boolean {
  const first = partFirstLine(target);
  const start = first?.lineNumber ?? target.start_line;
  return start > 0 && target.end_line - start + 1 >= TALL_PART_LINES;
}
