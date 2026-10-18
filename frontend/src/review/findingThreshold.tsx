import type { ReactNode } from "react";

import type { Annotation } from "./types";

/**
 * Finding threshold surfacing (Phase 9 item 11).
 *
 * The server names the AI findings the effective threshold hides
 * (`projection.triage.hidden_finding_ids`; P0 and unrated findings are never
 * among them). The Reader only folds those behind one "N below threshold"
 * disclosure in each list. Nothing is removed: counts, readiness and every
 * other consumer read the same annotations, so the threshold changes what is
 * surfaced and nothing else.
 */

/** Marks hidden root AI findings; returns the input itself when nothing changes. */
export function markBelowThreshold(annotations: Annotation[], hiddenIds: readonly string[] | undefined): Annotation[] {
  const hidden = new Set(hiddenIds ?? []);
  let changed = false;
  const marked = annotations.map((item) => {
    const below = hidden.has(item.id) && !item.parent_id && item.source === "ai_review";
    if (below === Boolean(item.below_threshold)) return item;
    changed = true;
    return { ...item, below_threshold: below };
  });
  return changed ? marked : annotations;
}

export function isBelowThreshold(annotation: Annotation): boolean {
  return annotation.below_threshold === true;
}

/**
 * One list: surfaced items in order, then the below-threshold ones behind a
 * single collapsed disclosure that expands in place. Replies are rendered by
 * their root, so a hidden root's thread stays with it.
 */
export function BelowThresholdList<T extends Annotation>({
  items,
  render,
}: {
  items: readonly T[];
  render(item: T): ReactNode;
}) {
  const shown = items.filter((item) => !isBelowThreshold(item));
  const below = items.filter(isBelowThreshold);
  return (
    <>
      {shown.map(render)}
      {below.length > 0 && (
        <details data-testid="below-threshold" className="py-1 text-caption text-muted-foreground">
          <summary className="cursor-pointer select-none">{below.length} below threshold</summary>
          {below.map(render)}
        </details>
      )}
    </>
  );
}
