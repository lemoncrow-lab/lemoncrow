/** Unsent Focus note text is scoped to one review, frozen revision and part, not to the next part. */
const PREFIX = "lc.review.focus-note-draft";

export function focusDraftKey(reviewId: string, revisionId: string, unitKey: string): string {
  return `${PREFIX}.${encodeURIComponent(reviewId)}.${encodeURIComponent(revisionId)}.${encodeURIComponent(unitKey)}`;
}

export function loadFocusDraft(reviewId: string, revisionId: string, unitKey: string): string {
  if (!reviewId || !revisionId || !unitKey) return "";
  try {
    return window.localStorage.getItem(focusDraftKey(reviewId, revisionId, unitKey)) ?? "";
  } catch {
    return "";
  }
}

export function saveFocusDraft(reviewId: string, revisionId: string, unitKey: string, body: string): void {
  if (!reviewId || !revisionId || !unitKey) return;
  try {
    const key = focusDraftKey(reviewId, revisionId, unitKey);
    if (body) window.localStorage.setItem(key, body);
    else window.localStorage.removeItem(key);
  } catch {
    // A browser that blocks storage can still keep the draft in current component memory.
  }
}
