export type DiffStyle = "split" | "unified";
export type DiffOverflow = "scroll" | "wrap";
export type DiffStylePreference = "auto" | DiffStyle;

export const REVIEW_DIFF_STYLE_KEY = "lemoncrow.review.diffStyle";
export const AUTO_SPLIT_MIN_WIDTH = 1000;

/** Below this width long code lines soft-wrap by default instead of running off a phone screen. */
export const WRAP_DEFAULT_MAX_WIDTH = 640;

/**
 * The reader's starting overflow: the reviewer's saved choice, else wrap on a
 * narrow screen (Pierre wraps within each row, so line numbers and syntax
 * highlighting stay aligned) and one line per row elsewhere.
 */
export function initialDiffOverflow(stored: string | null, viewportWidth: number): DiffOverflow {
  if (stored === "wrap" || stored === "scroll") return stored;
  return viewportWidth > 0 && viewportWidth < WRAP_DEFAULT_MAX_WIDTH ? "wrap" : "scroll";
}

export function getInitialDiffStylePreference(): DiffStylePreference {
  if (typeof window === "undefined") return "auto";
  const stored = window.localStorage.getItem(REVIEW_DIFF_STYLE_KEY);
  return stored === "split" || stored === "unified" || stored === "auto" ? stored : "auto";
}

export function persistDiffStylePreference(preference: DiffStylePreference): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(REVIEW_DIFF_STYLE_KEY, preference);
}

export function resolveDiffStyle(preference: DiffStylePreference, availableWidth: number): DiffStyle {
  if (preference !== "auto") return preference;
  return availableWidth >= AUTO_SPLIT_MIN_WIDTH ? "split" : "unified";
}

export function nextDiffStylePreference(preference: DiffStylePreference): DiffStylePreference {
  if (preference === "auto") return "split";
  if (preference === "split") return "unified";
  return "auto";
}
export type ReviewScrollBehavior = "instant" | "smooth";

/** Keep @pierre/diffs on the same light/dark surface as the surrounding reader. */
export function appThemeType(): "light" | "dark" {
  if (typeof document === "undefined") return "light";
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

/** Respect reduced-motion without forcing every caller to duplicate the media query. */
export function motionSafeScrollBehavior(behavior: ReviewScrollBehavior): ReviewScrollBehavior {
  if (behavior !== "smooth" || typeof window === "undefined" || typeof window.matchMedia !== "function") return behavior;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
}
