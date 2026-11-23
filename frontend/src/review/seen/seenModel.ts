/** D7 read exposure, never a human approval, and never independently verified. */
import { DEFAULT_FLOW_TIMINGS, seenThresholdMs, type FlowTimings } from "../flowTimings";
import { partName, partsOf } from "../overview/overviewModel";
import type { ReviewTarget, ReviewTargetSpan } from "../types";

export const SEEN_STORAGE_PREFIX = "lemoncrow.review.seen.v1";
export const MIN_VISIBLE_FRACTION = 0.6;

export interface SeenBlock {
  key: string;
  unitKey: string;
  path: string;
  side: "old" | "new";
  start: number;
  end: number;
  hunk: number;
  changedLines: number;
  thresholdMs: number;
}

export interface SeenObservation {
  blockKey: string;
  unitKey: string;
  path: string;
  side: "old" | "new";
  start: number;
  end: number;
  hunk: number;
  elapsedMs: number;
  source: "dwell" | "focus_answer";
  /** Confirmation from the review server, not a claim of independent verification. */
  serverRecorded: boolean;
}

export interface SeenLedger {
  version: 1;
  reviewId: string;
  revisionId: string;
  reviewerId: string;
  rows: Record<string, SeenObservation>;
  omission?: { blockKeys: string[]; serverRecorded: boolean; recordedAt: string; recordedCount?: number };
}

function blocksFromSpan(target: ReviewTarget, span: ReviewTargetSpan, timings: FlowTimings): SeenBlock | null {
  if (!Number.isInteger(span.start_line) || !Number.isInteger(span.end_line) || span.start_line <= 0 || span.end_line < span.start_line) return null;
  const changedLines = span.end_line - span.start_line + 1;
  if (changedLines > 100_000) return null;
  const side = span.side;
  const key = [target.unit_key, target.path, target.content_fingerprint || "fingerprint-unavailable", side, span.hunk_ordinal, span.start_line, span.end_line].join(":");
  return { key, unitKey: target.unit_key, path: target.path, side, hunk: span.hunk_ordinal, start: span.start_line, end: span.end_line, changedLines, thresholdMs: seenThresholdMs(changedLines, timings) };
}

export function blocksForTarget(target: ReviewTarget, timings: FlowTimings = DEFAULT_FLOW_TIMINGS): SeenBlock[] {
  const blocks = (target.spans ?? []).map((span) => blocksFromSpan(target, span, timings)).filter((block): block is SeenBlock => block !== null);
  const unique = new Map(blocks.map((block) => [block.key, block]));
  return [...unique.values()];
}

/** The key a part with no measurable changed lines is disclosed under; the server builds the same string. */
const UNMEASURED_KEY_TOKEN = "unmeasured"; // wire token shared with the server's block keys, not copy
export function unmeasuredKey(target: ReviewTarget): string {
  return `${target.unit_key}:${target.path}:${target.content_fingerprint || "fingerprint-unavailable"}:${UNMEASURED_KEY_TOKEN}`;
}

export function seenStorageKey(reviewId: string, reviewerId: string, revisionId: string): string {
  return [SEEN_STORAGE_PREFIX, encodeURIComponent(reviewId), encodeURIComponent(reviewerId), encodeURIComponent(revisionId)].join(".");
}

export function emptyLedger(reviewId: string, reviewerId: string, revisionId: string): SeenLedger {
  return { version: 1, reviewId, revisionId, reviewerId, rows: {} };
}

export function parseLedger(raw: string | null, reviewId: string, reviewerId: string, revisionId: string): SeenLedger {
  const empty = emptyLedger(reviewId, reviewerId, revisionId);
  if (!raw || !reviewId || !reviewerId || !revisionId) return empty;
  try {
    const parsed = JSON.parse(raw) as SeenLedger;
    if (parsed.version !== 1 || parsed.reviewId !== reviewId || parsed.revisionId !== revisionId || parsed.reviewerId !== reviewerId
        || !parsed.rows || typeof parsed.rows !== "object" || Array.isArray(parsed.rows)) return empty;
    // Bound untrusted browser storage. The server never trusts the local record as evidence.
    const rows: Record<string, SeenObservation> = {};
    for (const [key, row] of Object.entries(parsed.rows).slice(0, 5000)) {
      if (key.length > 400 || !row || row.blockKey !== key || !Number.isFinite(row.elapsedMs) || row.elapsedMs < 0
          || (row.source !== "dwell" && row.source !== "focus_answer")) continue;
      rows[key] = row;
    }
    const omission = parsed.omission?.blockKeys && Array.isArray(parsed.omission.blockKeys) ? {
      blockKeys: parsed.omission.blockKeys.filter((key): key is string => typeof key === "string").slice(0, 5000),
      serverRecorded: parsed.omission.serverRecorded === true,
      recordedAt: String(parsed.omission.recordedAt || ""),
      recordedCount: Number.isInteger(parsed.omission.recordedCount) ? parsed.omission.recordedCount : undefined,
    } : undefined;
    return { ...empty, rows, omission };
  } catch { return empty; }
}

export function withSeen(ledger: SeenLedger, block: SeenBlock, elapsedMs: number, source: SeenObservation["source"], serverRecorded = false): SeenLedger {
  if (!Number.isFinite(elapsedMs) || elapsedMs < block.thresholdMs && source === "dwell") return ledger;
  const existing = ledger.rows[block.key];
  if (existing && existing.serverRecorded && !serverRecorded) return ledger;
  return { ...ledger, rows: { ...ledger.rows, [block.key]: {
    blockKey: block.key, unitKey: block.unitKey, path: block.path, side: block.side,
    start: block.start, end: block.end, hunk: block.hunk,
    elapsedMs: Math.max(elapsedMs, existing?.elapsedMs ?? 0), source, serverRecorded: existing?.serverRecorded === true || serverRecorded,
  } } };
}

export function seenCount(target: ReviewTarget, ledger: SeenLedger): { seen: number; total: number; linesSeen: number; linesTotal: number } {
  const blocks = blocksForTarget(target);
  return {
    seen: blocks.filter((block) => Boolean(ledger.rows[block.key])).length,
    total: blocks.length,
    linesSeen: blocks.filter((block) => Boolean(ledger.rows[block.key])).reduce((n, block) => n + block.changedLines, 0),
    linesTotal: blocks.reduce((n, block) => n + block.changedLines, 0),
  };
}

/**
 * Risky means the part the server asks as a decision (the answer category of P1-2), not the attention level:
 * a symlink, submodule, executable bit or lockfile decision has no "high" attention but is still a decision.
 */
export function missingRisky(targets: readonly ReviewTarget[], ledger: SeenLedger): { target: ReviewTarget; blocks: SeenBlock[] }[] {
  return partsOf(targets, "decision")
    .map((target) => ({ target, blocks: blocksForTarget(target).filter((block) => !ledger.rows[block.key]) }))
    .filter(({ target, blocks }) => blocks.length > 0 || blocksForTarget(target).length === 0);
}

export function fractionVisible(rects: readonly { top: number; bottom: number; height: number }[], viewportTop: number, viewportBottom: number): number {
  if (rects.length === 0 || viewportBottom <= viewportTop) return 0;
  let visible = 0;
  for (const rect of rects) {
    if (rect.height > 0 && Math.max(0, Math.min(rect.bottom, viewportBottom) - Math.max(rect.top, viewportTop)) / rect.height >= MIN_VISIBLE_FRACTION) visible++;
  }
  return visible / rects.length;
}

/** "9–11", or "9–11, 20" for several blocks; removed-line blocks say so, because their numbers are old-side numbers. */
export function lineRangesLabel(blocks: readonly Pick<SeenBlock, "side" | "start" | "end">[]): string {
  if (!blocks.length) return "";
  const parts = blocks.map((block) => `${block.start === block.end ? block.start : `${block.start}–${block.end}`}${block.side === "old" ? " (removed)" : ""}`);
  return `${blocks.length === 1 && blocks[0].start === blocks[0].end ? "line" : "lines"} ${parts.join(", ")}`;
}

/** "seen 0 of 3 lines" / "seen 0 of 1 line" (SPEC 4.6 meter text). */
export function meterLabel(seen: number, total: number): string {
  return `seen ${seen} of ${total} ${total === 1 ? "line" : "lines"}`;
}

/** Parts with at least one changed block, and how many of those have every block seen (SPEC 4.6 footer). */
export function partsFullySeen(targets: readonly ReviewTarget[], ledger: SeenLedger): { seen: number; total: number } {
  const measured = targets.filter((target) => blocksForTarget(target).length > 0);
  return { seen: measured.filter((target) => { const c = seenCount(target, ledger); return c.seen === c.total; }).length, total: measured.length };
}

/** The record's Coverage rows (SPEC 4.18, REC-6): seen lines per risky part, and what the reviewer wrapped up without seeing. */
export function coverageRecordRows(targets: readonly ReviewTarget[], ledger: SeenLedger): { coverage: string[]; skipped: string[] } {
  const coverage = partsOf(targets, "decision").map((target) => {
    const counts = seenCount(target, ledger);
    return counts.linesTotal
      ? `${partName(target)}: seen ${counts.linesSeen} of ${counts.linesTotal} changed ${counts.linesTotal === 1 ? "line" : "lines"}`
      : `${partName(target)}: changed lines unavailable`;
  });
  const keys = new Set(ledger.omission?.blockKeys ?? []);
  const skipped: string[] = [];
  for (const target of targets) {
    const blocks = blocksForTarget(target);
    const omitted = blocks.filter((block) => keys.has(block.key));
    if (omitted.length) skipped.push(`Wrapped up without seeing ${partName(target)} ${lineRangesLabel(omitted)}`);
    else if (blocks.length === 0 && keys.has(unmeasuredKey(target))) skipped.push(`Wrapped up without seeing ${partName(target)} (changed lines unavailable)`);
  }
  return { coverage, skipped };
}
