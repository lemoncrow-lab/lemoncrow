/** Captured Old/New output kept for this browser session, so a Focus card can show the evidence the reviewer already
 * produced in Preview. Nothing is stored on the server and nothing runs from here: no capture, no evidence.
 * Closed by `clearEvidence` (a new revision or leaving the review) and by a page reload.
 */
import type { PreviewResult } from "./previewFlowModel";

export interface EvidenceEntry {
  surfaceKey: string;
  title: string;
  affectedPaths: readonly string[];
  old: PreviewResult;
  next: PreviewResult;
}
const store = new Map<string, Map<string, EvidenceEntry>>();
const listeners = new Set<() => void>();
let version = 0;
const bump = () => { version++; listeners.forEach((l) => l()); };
const TEXT = new Set(["text", "json", "html"]);

export const evidenceScope = (apiReviewId: string, revisionId: string) => `${apiReviewId}\u0000${revisionId}`;
/** Only text-like results are kept: an image or download holds a blob address that Preview revokes. */
export function recordEvidence(scope: string, entry: EvidenceEntry) {
  if (!TEXT.has(entry.old.display) || !TEXT.has(entry.next.display)) return;
  const rows = store.get(scope) ?? new Map<string, EvidenceEntry>();
  rows.set(entry.surfaceKey, entry);
  store.set(scope, rows);
  bump();
}
export function dropEvidence(scope: string, surfaceKey: string) {
  if (store.get(scope)?.delete(surfaceKey)) bump();
}
export function clearEvidence(scope?: string) {
  if (scope === undefined) store.clear(); else store.delete(scope);
  bump();
}
export function allEvidence(scope: string): EvidenceEntry[] {
  return [...(store.get(scope)?.values() ?? [])];
}
/** Evidence of an older revision of the same review is closed when a newer one is opened. */
export function pruneEvidence(apiReviewId: string, keepScope: string) {
  let changed = false;
  for (const scope of [...store.keys()]) {
    if (scope.startsWith(`${apiReviewId}\u0000`) && scope !== keepScope) { store.delete(scope); changed = true; }
  }
  if (changed) bump();
}
export function evidenceForPath(scope: string, path: string): EvidenceEntry[] {
  return [...(store.get(scope)?.values() ?? [])].filter((e) => e.affectedPaths.includes(path));
}
export function subscribeEvidence(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export const evidenceVersion = () => version;
