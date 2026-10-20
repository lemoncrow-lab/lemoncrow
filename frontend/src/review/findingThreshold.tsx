import type { ReactNode } from "react";

import type { Annotation, ReviewTriage } from "./types";

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

const SEVERITY_LEVELS = ["p0", "p1", "p2", "suggestion"] as const;

/**
 * Gives the AI findings their severity when the triage summary makes it exact.
 *
 * The server rates each AI finding (p0 | p1 | p2 | suggestion, from its validated category) but sends
 * only the counts per source. When every AI finding of this Review falls in one rated level and none is
 * unrated or stale, each of them has exactly that level, so the badge cannot lie. In any other mix the
 * findings stay unbadged rather than guessed. A `severity` already on the annotation always wins.
 */
export function markFindingSeverity(annotations: Annotation[], triage: ReviewTriage | null | undefined): Annotation[] {
  const row = triage?.severity_summary.by_source?.ai_review;
  if (!row || row.unrated > 0 || row.stale > 0) return annotations;
  const rated = SEVERITY_LEVELS.filter((level) => row[level] > 0);
  const roots = annotations.filter((item) => item.source === "ai_review" && !item.parent_id && item.state !== "obsolete");
  if (rated.length !== 1 || roots.length === 0 || roots.length !== row[rated[0]]) return annotations;
  const level = rated[0];
  let changed = false;
  const marked = annotations.map((item) => {
    if (item.source !== "ai_review" || item.parent_id || item.severity) return item;
    changed = true;
    return { ...item, severity: level };
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
        <details data-testid="below-threshold" className="group rounded-md border border-border py-1 text-meta text-muted-foreground">
          <summary className="flex min-h-7 cursor-pointer select-none items-center gap-2 px-3">{below.length} below threshold</summary>
          {below.map(render)}
        </details>
      )}
    </>
  );
}
