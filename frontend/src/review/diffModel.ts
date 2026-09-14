export type DiffStyle = "split" | "unified";
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
