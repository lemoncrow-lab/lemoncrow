export type ReviewView = "brief" | "tour" | "implementation" | "map";

/**
 * The in-Review backtrail: where an in-Review jump (Code Peek, Map node, Ask
 * citation, Brief/Guided target, Affected code) left from, so Back returns to
 * that exact place — view, target, line, scroll and Guided step — without
 * leaving the Review. It lives in the URL, so a reload keeps it.
 */

export const REVIEW_BACKTRAIL_PARAM = "trail";
/** Oldest entries fall off past this many jumps. */
export const REVIEW_BACKTRAIL_LIMIT = 12;
/** A departure whose own query is longer than this is not recorded. */
const MAX_ENTRY_QUERY = 1024;
const MAX_LABEL = 120;
const VIEWS: ReadonlySet<string> = new Set(["brief", "tour", "implementation", "map"]);

export interface ReviewBacktrailEntry {
  /** Departure URL query minus the trail: view, Map location, Guided step, page. */
  query: string;
  view: ReviewView;
  /** Code target active at departure; "" when none. */
  target: string;
  /** Scroll offset of the departure view's scroll root. */
  scroll: number;
  /** Snapshot source panel open at departure. */
  related: { path: string; line: number } | null;
  /** Where Back returns, said plainly ("Brief", "src/a.py:L12"). */
  label: string;
}

type EncodedEntry = [string, string, string, number, string, number, string];

function base64UrlEncode(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlDecode(value: string): string {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  const binary = atob(padded);
  return new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
}

/** The departure query without the trail, so an entry never carries its own history. */
export function backtrailQuery(params: URLSearchParams): string {
  const copy = new URLSearchParams(params);
  copy.delete(REVIEW_BACKTRAIL_PARAM);
  return copy.toString();
}

function validEntry(raw: unknown): ReviewBacktrailEntry | null {
  if (!Array.isArray(raw) || raw.length !== 7) return null;
  const [query, view, target, scroll, relatedPath, relatedLine, label] = raw as unknown[];
  if (typeof query !== "string" || query.length > MAX_ENTRY_QUERY) return null;
  if (typeof view !== "string" || !VIEWS.has(view)) return null;
  if (typeof target !== "string" || typeof label !== "string" || label.length > MAX_LABEL) return null;
  if (typeof scroll !== "number" || !Number.isFinite(scroll) || scroll < 0) return null;
  if (typeof relatedPath !== "string" || typeof relatedLine !== "number" || !Number.isInteger(relatedLine) || relatedLine < 0) return null;
  if (new URLSearchParams(query).has(REVIEW_BACKTRAIL_PARAM)) return null;
  return {
    query,
    view: view as ReviewView,
    target,
    scroll: Math.round(scroll),
    related: relatedPath ? { path: relatedPath, line: Math.max(1, relatedLine) } : null,
    label,
  };
}

/**
 * Reads the trail from the URL, oldest first. A trail that does not decode
 * whole is dropped whole: offering no Back beats returning somewhere wrong.
 */
export function readReviewBacktrail(params: URLSearchParams): ReviewBacktrailEntry[] {
  const raw = params.get(REVIEW_BACKTRAIL_PARAM);
  if (!raw) return [];
  try {
    const decoded: unknown = JSON.parse(base64UrlDecode(raw));
    if (!Array.isArray(decoded) || decoded.length > REVIEW_BACKTRAIL_LIMIT) return [];
    const entries = decoded.map(validEntry);
    return entries.every((entry): entry is ReviewBacktrailEntry => entry !== null) ? entries : [];
  } catch {
    return [];
  }
}

export function writeReviewBacktrail(params: URLSearchParams, trail: readonly ReviewBacktrailEntry[]): URLSearchParams {
  if (trail.length === 0) {
    params.delete(REVIEW_BACKTRAIL_PARAM);
    return params;
  }
  const encoded: EncodedEntry[] = trail.map((entry) => [
    entry.query,
    entry.view,
    entry.target,
    Math.max(0, Math.round(entry.scroll)),
    entry.related?.path ?? "",
    entry.related?.line ?? 0,
    entry.label.slice(0, MAX_LABEL),
  ]);
  params.set(REVIEW_BACKTRAIL_PARAM, base64UrlEncode(JSON.stringify(encoded)));
  return params;
}

/** Pushes a departure, keeping only the newest REVIEW_BACKTRAIL_LIMIT entries. */
export function pushReviewBacktrail(
  trail: readonly ReviewBacktrailEntry[],
  entry: ReviewBacktrailEntry,
): ReviewBacktrailEntry[] {
  if (entry.query.length > MAX_ENTRY_QUERY) return [...trail];
  const top = trail[trail.length - 1];
  // Re-jumping from the very place just recorded adds nothing to go back to.
  if (top && top.query === entry.query && top.view === entry.view && top.target === entry.target
    && top.related?.path === entry.related?.path && top.related?.line === entry.related?.line) {
    return [...trail.slice(0, -1), entry];
  }
  return [...trail, entry].slice(-REVIEW_BACKTRAIL_LIMIT);
}
