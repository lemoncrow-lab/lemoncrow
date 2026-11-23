/**
 * Review time as a range (SPEC 4.1, 7.1 P5-1 C), never a single number. Pure.
 *
 * Counted from a review's real parts: a decision takes a person 3 to 6 minutes, a quick check half a minute to a
 * minute, and opening and wrapping up one minute. Nothing about the reviewer's own speed is stored yet (Q11 asks for
 * the median of their last 20 reviews), so every range here is the starting estimate and says "about" where a
 * sentence needs it.
 */

export interface PartCounts {
  decisions: number;
  quickChecks: number;
}

export interface MinuteRange {
  low: number;
  high: number;
}

const DECISION: MinuteRange = { low: 3, high: 6 };
const QUICK_CHECK: MinuteRange = { low: 0.5, high: 1 };
const OPEN_AND_WRAP = 1;

/** Whole minutes, at least a one minute spread: 1 decision is 4 to 7 min, 2 decisions and 1 quick check 8 to 14. */
export function timeRange(counts: PartCounts): MinuteRange {
  const low = Math.round(OPEN_AND_WRAP + counts.decisions * DECISION.low + counts.quickChecks * QUICK_CHECK.low);
  const high = Math.round(OPEN_AND_WRAP + counts.decisions * DECISION.high + counts.quickChecks * QUICK_CHECK.high);
  return { low, high: Math.max(high, low + 1) };
}

/** Adds ranges (a queue's budget). */
export function sumRanges(ranges: readonly MinuteRange[]): MinuteRange {
  return ranges.reduce((total, range) => ({ low: total.low + range.low, high: total.high + range.high }), { low: 0, high: 0 });
}

function clock(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

/** "4–7 min", or "1 h 10 min–2 h 5 min" once either end passes an hour. */
export function rangeText(range: MinuteRange): string {
  if (range.high < 60) return `${range.low}–${range.high} min`;
  return `${clock(range.low)}–${clock(range.high)}`;
}
