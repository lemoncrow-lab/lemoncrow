/**
 * The large-change tiers (SPEC 4.15, 7.1 P5-3 A), one pure rule. A tier starts AT its threshold: 29 parts is small,
 * 30 is medium, 229 is medium, 230 is large, 999 is large and 1,000 is huge. The count is every part of the review
 * (decisions, quick checks and parts accepted by rule), never a startup slice of them.
 */
export const SCALE_THRESHOLDS = { medium: 30, large: 230, huge: 1000 } as const;

export type ScaleTier = "small" | "medium" | "large" | "huge";

export function scaleTier(parts: number): ScaleTier {
  if (parts >= SCALE_THRESHOLDS.huge) return "huge";
  if (parts >= SCALE_THRESHOLDS.large) return "large";
  if (parts >= SCALE_THRESHOLDS.medium) return "medium";
  return "small";
}

/** The Map lists modules, not files, from the first tier above small. */
export function mapByModule(parts: number): boolean {
  return scaleTier(parts) !== "small";
}
