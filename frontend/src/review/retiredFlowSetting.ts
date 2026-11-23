/**
 * `review.flow` is retired (P8-3): the redesigned flow is the only Reader. A browser that stored a value under it keeps the
 * value, which nothing reads; this removes it once at startup so it does not linger. Storage that is blocked is left alone.
 */
export const RETIRED_REVIEW_FLOW_KEY = "lemoncrow.review.flow";

export function forgetRetiredReviewFlow(): void {
  try {
    window.localStorage.removeItem(RETIRED_REVIEW_FLOW_KEY);
  } catch {
    // Blocked storage: nothing reads the value, so there is nothing to do.
  }
}
