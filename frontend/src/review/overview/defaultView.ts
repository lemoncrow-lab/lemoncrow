/**
 * What the Overview remembers in this browser (SPEC 3.4, 4.2): the reading view Start opens, whether Evidence is
 * open, and whether the reviewer approved the plan. Per browser until P5-4 moves per-user settings to the server
 * (SPEC 7.1 P2-1 A, C; the reading view moved to the server in P5-4, see P5-4 A). Every read tolerates blocked or junk storage and answers the default.
 */

import { currentSettings, patchUserSettings, DEFAULT_VIEW_KEY } from "../settings/userSettings";

export type ReadingView = "one" | "all";

export { DEFAULT_VIEW_KEY };
export const EVIDENCE_OPEN_KEY = "lemoncrow.review.overview.evidence";
const PLAN_APPROVED_PREFIX = "lemoncrow.review.planApproved.";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}

/** One at a time unless the reviewer chose All files: the server's choice once it has answered, this browser's before. */
export function getDefaultView(): ReadingView {
  return currentSettings().default_view;
}

/** Remembers the reading view. This browser keeps it at once; the server is told, and a miss is sent on the next load. */
export function setDefaultView(view: ReadingView): void {
  write(DEFAULT_VIEW_KEY, view === "all" ? "all" : null);
  void patchUserSettings({ default_view: view });
}

export function otherView(view: ReadingView): ReadingView {
  return view === "one" ? "all" : "one";
}

export function viewLabel(view: ReadingView): string {
  return view === "all" ? "All files" : "One at a time";
}

/** Collapsed unless the reviewer left it open (OV-1). */
export function getEvidenceOpen(): boolean {
  return read(EVIDENCE_OPEN_KEY) === "open";
}

export function setEvidenceOpen(open: boolean): void {
  write(EVIDENCE_OPEN_KEY, open ? "open" : null);
}

/** A stable 32-bit hash (FNV-1a) of the plan's lines, so a changed plan has a different key. */
function planHash(planned: readonly string[]): string {
  let hash = 0x811c9dc5;
  for (const char of planned.join("\u0000")) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16);
}

/** Plan approval belongs to one review, one revision and one plan: a new revision or a changed plan starts at "Not yet". */
export function planKey(reviewRef: string, revisionId: string, planned: readonly string[]): string {
  return `${reviewRef}.${revisionId}.${planHash(planned)}`;
}

export function getPlanApproved(key: string): boolean {
  return read(PLAN_APPROVED_PREFIX + key) === "yes";
}

export function setPlanApproved(key: string, approved: boolean): void {
  write(PLAN_APPROVED_PREFIX + key, approved ? "yes" : null);
}
