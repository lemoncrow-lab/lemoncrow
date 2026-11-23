import { partName, partRoute, partSlug } from "../overview/overviewModel";
import { buildReviewFlowPath } from "../reviewRoutes";
import type { ReviewTarget } from "../types";
import { partStateLabel, type PalettePart } from "./paletteModel";

/**
 * The palette's "Parts in this review" rows: every part by name and file with the answer it carries, and the address
 * that opens it (the part in All files when the person reads there, else One at a time).
 */
const ENTRIES = new WeakMap<readonly ReviewTarget[], Map<string, PalettePart[]>>();

export function partEntries(reviewRef: string, targets: readonly ReviewTarget[], view: "one" | "all"): PalettePart[] {
  // One list per parts array and view: every screen asks on every render, and a large review has thousands of parts.
  const key = `${view}|${reviewRef}`;
  const cached = ENTRIES.get(targets)?.get(key);
  if (cached) return cached;
  const entries = targets.map((target) => ({
    id: target.target_id,
    name: partName(target),
    file: target.path,
    state: partStateLabel(target.state, target.changed_since_mark),
    path: buildReviewFlowPath(reviewRef, view === "all" ? { kind: "all", part: partSlug(target, targets) } : partRoute(target, targets)),
  }));
  const forTargets = ENTRIES.get(targets) ?? new Map<string, PalettePart[]>();
  forTargets.set(key, entries);
  ENTRIES.set(targets, forTargets);
  return entries;
}

/** Where a review screen is, for the Recent list: "shop · Fix discount" and the screen or part. */
export function reviewPlace(repo: string, title: string, sub: string): { label: string; sub: string } {
  return { label: [repo, title].filter(Boolean).join(" · "), sub };
}
