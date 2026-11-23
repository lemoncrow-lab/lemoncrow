/**
 * Time controls for the new Review flow (SPEC §4.9 undo window, §4.6 seen rule D7). Pure: a source object in, timings out.
 *
 * A test lowers a timing by setting a window global before the page loads (the prototype's `window.LC_FLOW_UNDO_MS`
 * pattern). Only positive finite numbers count; anything else keeps the SPEC default.
 *
 * The server-held undo window (P0-5) is not built yet; P2-5 adds the server-side override.
 */

export const DEFAULT_UNDO_MS = 10_000;
export const DEFAULT_SEEN_MIN_MS = 600;
export const DEFAULT_SEEN_PER_LINE_MS = 40;
export const DEFAULT_SEEN_CAP_MS = 3_000;
/** P6-1 O: asking the server whether the signed record has landed. First gap, doubling up to the max gap, for this many asks. */
export const DEFAULT_SIGNING_FIRST_GAP_MS = 500;
export const DEFAULT_SIGNING_MAX_GAP_MS = 5_000;
export const DEFAULT_SIGNING_LOOKS = 15;
/** Largest delay setTimeout accepts; a bigger override would fire at once. */
export const MAX_TIMING_MS = 2_147_483_647;

export interface FlowTimings {
  /** Undo window after Send. */
  undoMs: number;
  /** Seen rule floor: a changed-line block needs at least this long on screen. */
  seenMinMs: number;
  /** Seen rule per changed line. */
  seenPerLineMs: number;
  /** Seen rule cap. */
  seenCapMs: number;
  /** Signed record: gap before the second ask; each later gap doubles. */
  signingFirstGapMs: number;
  /** Signed record: the largest gap between asks. */
  signingMaxGapMs: number;
  /** Signed record: asks before the line says "Still signing · check again". */
  signingLooks: number;
}

export const DEFAULT_FLOW_TIMINGS: Readonly<FlowTimings> = Object.freeze({
  undoMs: DEFAULT_UNDO_MS,
  seenMinMs: DEFAULT_SEEN_MIN_MS,
  seenPerLineMs: DEFAULT_SEEN_PER_LINE_MS,
  seenCapMs: DEFAULT_SEEN_CAP_MS,
  signingFirstGapMs: DEFAULT_SIGNING_FIRST_GAP_MS,
  signingMaxGapMs: DEFAULT_SIGNING_MAX_GAP_MS,
  signingLooks: DEFAULT_SIGNING_LOOKS,
});

/** Window global name for each override. */
export const FLOW_TIMING_GLOBALS: Readonly<Record<keyof FlowTimings, string>> = Object.freeze({
  undoMs: "LC_FLOW_UNDO_MS",
  seenMinMs: "LC_FLOW_SEEN_MIN_MS",
  seenPerLineMs: "LC_FLOW_SEEN_PER_LINE_MS",
  seenCapMs: "LC_FLOW_SEEN_CAP_MS",
  signingFirstGapMs: "LC_FLOW_SIGNING_FIRST_GAP_MS",
  signingMaxGapMs: "LC_FLOW_SIGNING_MAX_GAP_MS",
  signingLooks: "LC_FLOW_SIGNING_LOOKS",
});

/**
 * A positive finite number no larger than MAX_TIMING_MS, or the fallback. Numeric strings are accepted;
 * anything else is ignored.
 */
export function parseTimingOverride(value: unknown, fallback: number): number {
  let n = Number.NaN;
  if (typeof value === "number") n = value;
  else if (typeof value === "string" && value.trim() !== "") n = Number(value);
  return Number.isFinite(n) && n > 0 && n <= MAX_TIMING_MS ? n : fallback;
}

/** Reads the window-global overrides from a source (pass `window` in the browser). */
export function readFlowTimings(source: object): FlowTimings {
  const globals = source as Record<string, unknown>;
  const out = { ...DEFAULT_FLOW_TIMINGS } as FlowTimings;
  for (const key of Object.keys(FLOW_TIMING_GLOBALS) as (keyof FlowTimings)[]) {
    out[key] = parseTimingOverride(globals[FLOW_TIMING_GLOBALS[key]], DEFAULT_FLOW_TIMINGS[key]);
  }
  return out;
}

/**
 * D7: a changed-line block counts as seen after max(0.6 s, 40 ms × changed lines), capped at 3 s.
 * A negative or NaN line count counts as zero; Infinity reaches the cap.
 */
export function seenThresholdMs(changedLines: number, timings: FlowTimings = DEFAULT_FLOW_TIMINGS): number {
  const lines = Number.isNaN(changedLines) ? 0 : Math.max(0, changedLines);
  return Math.min(Math.max(timings.seenMinMs, timings.seenPerLineMs * lines), timings.seenCapMs);
}

/** P6-1 O: the wait after ask number `look` (1-based) before the next one: first gap, doubling, never above the max gap. */
export function signingGapMs(look: number, timings: FlowTimings = DEFAULT_FLOW_TIMINGS): number {
  const doublings = Math.max(0, Math.min(look - 1, 30));
  return Math.min(timings.signingFirstGapMs * 2 ** doublings, timings.signingMaxGapMs);
}
