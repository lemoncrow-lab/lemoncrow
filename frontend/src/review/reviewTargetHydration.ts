import type { ReviewTargetList } from "./types";

/**
 * A same-Snapshot startup refresh is a smaller projection, never a reason to
 * forget a full target model or a distant file's explicitly fetched targets.
 * A full projection already holds authoritative progress and review marks;
 * a bounded startup slice must never overwrite those with stale values.
 */
export function retainTargetsOnSameRevision(
  previous: ReviewTargetList | null,
  incoming: ReviewTargetList
): ReviewTargetList {
  if (
    !previous ||
    previous.revision_id !== incoming.revision_id ||
    previous.order !== incoming.order ||
    incoming.complete !== false
  ) {
    return incoming;
  }
  if (previous.complete === true) return previous;
  const incomingById = new Map(
    incoming.targets.map((target) => [target.target_id, target])
  );
  const hasAdditionalTargets = previous.targets.some(
    (target) => !incomingById.has(target.target_id)
  );
  if (!hasAdditionalTargets) return incoming;

  const retained = previous.targets.map(
    (target) => incomingById.get(target.target_id) ?? target
  );
  const previousIds = new Set(
    previous.targets.map((target) => target.target_id)
  );
  for (const target of incoming.targets) {
    if (!previousIds.has(target.target_id)) retained.push(target);
  }
  return { ...incoming, targets: retained };
}
