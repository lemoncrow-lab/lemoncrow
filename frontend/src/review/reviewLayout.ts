import { useSyncExternalStore } from "react";

/**
 * The phone composition (Phase 14 item 3): row 1 Back | title | More, row 2
 * the persistent views, content, then one sticky bottom dock with the primary
 * action and the Changes / Comments / Status / Ask entry points. Must match the
 * `max-width: 639px` block in reviewUi.css.
 */
export const REVIEW_COMPACT_QUERY = "(max-width: 639px)";

function compactQuery(): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  return window.matchMedia(REVIEW_COMPACT_QUERY);
}

export function isCompactReviewViewport(): boolean {
  return Boolean(compactQuery()?.matches);
}

function subscribe(onChange: () => void): () => void {
  const query = compactQuery();
  if (!query) return () => undefined;
  query.addEventListener?.("change", onChange);
  return () => query.removeEventListener?.("change", onChange);
}

/** Live phone-composition flag; false wherever media queries are unavailable. */
export function useCompactReviewLayout(): boolean {
  return useSyncExternalStore(subscribe, isCompactReviewViewport, () => false);
}

/**
 * Below the md breakpoint the Reader opens with the file drawer closed (the
 * Map does the same with its inspector). Wider than the phone composition
 * above, so a tablet keeps its collapsed rail with a visible toggle.
 */
export const REVIEW_NARROW_QUERY = "(max-width: 767px)";

function narrowQuery(): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  return window.matchMedia(REVIEW_NARROW_QUERY);
}

export function isNarrowReviewViewport(): boolean {
  return Boolean(narrowQuery()?.matches);
}

function subscribeNarrow(onChange: () => void): () => void {
  const query = narrowQuery();
  if (!query) return () => undefined;
  query.addEventListener?.("change", onChange);
  return () => query.removeEventListener?.("change", onChange);
}

/** Live below-md flag; false wherever media queries are unavailable. */
export function useNarrowReviewLayout(): boolean {
  return useSyncExternalStore(subscribeNarrow, isNarrowReviewViewport, () => false);
}
