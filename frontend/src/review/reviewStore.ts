import type { Draft } from "./annotationModel";
import { outlineCount } from "./answerModel";
import { progressFromTargets } from "./readerModel";
import { retainTargetsOnSameRevision } from "./reviewTargetHydration";
import type {
  Annotation,
  AnnotationState,
  BulkMarkResult,
  BulkUndoResult,
  MarkResult,
  MarkState,
  RefreshInfo,
  ReviewOutlineItem,
  ReviewOverview,
  ReviewProgress,
  ReviewSessionStatus,
  ReviewTarget,
  ReviewTargetList,
  ReviewTargetPriority,
} from "./types";

/**
 * The review's state: the parts and what the reviewer has answered, the notes,
 * the unsent draft, the revision on screen and the part in focus. One reducer
 * owns it; effects, fetches and refs stay with whoever dispatches. The reducer
 * does no I/O and reads no clock, ref or storage.
 */

/** What a bulk "mark file reviewed" can take back. */
export interface ReviewStoreBulkUndo {
  path: string;
  marks: Array<{ unit_key: string; event_id: string }>;
}

export type ReviewStoreCommentsState = "loading" | "ready" | "failed";

export interface ReviewStoreState {
  overview: ReviewOverview | null;
  /** The server moved to a newer revision than the one on screen. */
  revisionAdvanced: boolean;
  revisionInfo: RefreshInfo | null;
  targetList: ReviewTargetList | null;
  /** Bumped when a mark commits. A mark shows at once, so the progress change that re-reads readiness can reach the server before the mark does. */
  committedMarks: number;
  bulkUndo: ReviewStoreBulkUndo | null;
  annotations: Annotation[];
  commentsState: ReviewStoreCommentsState;
  draft: Draft | null;
  activeTargetId: string;
}

export function createReviewStoreState(): ReviewStoreState {
  return {
    overview: null,
    revisionAdvanced: false,
    revisionInfo: null,
    targetList: null,
    committedMarks: 0,
    bulkUndo: null,
    annotations: [],
    commentsState: "loading",
    draft: null,
    activeTargetId: "",
  };
}

export interface ReviewStoreSwap {
  targetId: string;
  expected: MarkState;
  next: ReviewTarget;
}

export type ReviewStoreAction =
  | { type: "overview/set"; overview: ReviewOverview | null }
  | { type: "overview/sessionStatusSet"; status: ReviewSessionStatus }
  | { type: "revision/advancedSet"; advanced: boolean }
  | { type: "revision/infoSet"; info: RefreshInfo | null }
  | { type: "parts/set"; targetList: ReviewTargetList | null }
  | { type: "parts/startupRetained"; incoming: ReviewTargetList }
  | { type: "parts/refreshedIfUnchanged"; before: ReviewTargetList | null; next: ReviewTargetList }
  | { type: "parts/pathSliceMerged"; slice: ReviewTargetList; stableOrder: readonly string[] }
  | {
      type: "parts/commentTargetApplied";
      target: ReviewTarget;
      progress: ReviewProgress;
      priorities?: Record<string, ReviewTargetPriority> | null;
      outline?: ReviewOutlineItem[] | null;
      outlineUpdates?: ReviewOutlineItem[];
    }
  | { type: "marks/swapped"; swaps: ReadonlyArray<ReviewStoreSwap> }
  | { type: "marks/committed"; target: ReviewTarget; result: MarkResult; othersInFlight: boolean }
  | { type: "marks/bulkApplied"; path: string; result: BulkMarkResult }
  | { type: "marks/bulkUndone"; result: BulkUndoResult }
  | { type: "notes/loadStarted" }
  | { type: "notes/loaded"; annotations: Annotation[] }
  | { type: "notes/loadFailed" }
  | { type: "notes/set"; annotations: Annotation[] }
  | { type: "notes/merged"; annotations: Annotation[] }
  | { type: "notes/stateShown"; id: string; from: AnnotationState | undefined; to: AnnotationState }
  | { type: "notes/replaced"; id: string; annotation: Annotation }
  | { type: "draft/set"; draft: Draft | null }
  | { type: "draft/proposalTextFilled"; path: string; startLine: number; endLine: number; text: string }
  | { type: "draft/proposalLinked"; proposalId: string }
  | { type: "part/selected"; targetId: string };

/** Apply the server's fresh queue to every target; the client never recomputes it. */
export function withPriorities(
  targets: ReviewTarget[],
  priorities: Record<string, ReviewTargetPriority> | null | undefined,
): ReviewTarget[] {
  if (!priorities) return targets;
  return targets.map((target) => {
    const priority = priorities[target.target_id];
    return priority ? { ...target, priority } : target;
  });
}

export function mergeOutline(
  current: ReviewOutlineItem[],
  updates: ReviewOutlineItem[] | undefined,
): ReviewOutlineItem[] {
  if (!updates?.length) return current;
  const byPath = new Map(updates.map((item) => [item.path, item]));
  const seen = new Set<string>();
  const out = current.map((item) => {
    const replacement = byPath.get(item.path);
    if (replacement) seen.add(item.path);
    return replacement ?? item;
  });
  for (const update of updates) if (!seen.has(update.path)) out.push(update);
  return out;
}

// Moves one target between state buckets in its file's outline row, so a mark
// shown before the server answers keeps the outline counts in step with it.
export function shiftOutlineState(
  outline: ReviewOutlineItem[],
  path: string,
  from: MarkState,
  to: MarkState,
): ReviewOutlineItem[] {
  if (from === to) return outline;
  return outline.map((row) => row.path === path
    ? { ...row, [from]: Math.max(0, outlineCount(row, from) - 1), [to]: outlineCount(row, to) + 1 }
    : row);
}

/**
 * Put a list's targets in the order the reviewer has already seen: ids the
 * order knows come first, in its order; unseen ones follow in the list's own
 * order. Pure; the caller owns the order itself and appends the unseen ids.
 */
export function applyStableOrder(list: ReviewTargetList, order: readonly string[]): ReviewTargetList {
  const byId = new Map(list.targets.map((target) => [target.target_id, target]));
  const ordered: ReviewTarget[] = [];
  const seen = new Set<string>();
  for (const targetId of order) {
    const target = byId.get(targetId);
    if (!target) continue;
    ordered.push(target);
    seen.add(targetId);
  }
  for (const target of list.targets) {
    if (seen.has(target.target_id)) continue;
    ordered.push(target);
  }
  return { ...list, targets: ordered };
}

function withTargetList(state: ReviewStoreState, targetList: ReviewTargetList | null): ReviewStoreState {
  return state.targetList === targetList ? state : { ...state, targetList };
}

function withAnnotations(state: ReviewStoreState, annotations: Annotation[]): ReviewStoreState {
  return state.annotations === annotations ? state : { ...state, annotations };
}

export function reviewStoreReducer(state: ReviewStoreState, action: ReviewStoreAction): ReviewStoreState {
  switch (action.type) {
    case "overview/set":
      return state.overview === action.overview ? state : { ...state, overview: action.overview };
    case "overview/sessionStatusSet": {
      const current = state.overview;
      if (!current) return state;
      return { ...state, overview: { ...current, session: { ...current.session, status: action.status } } };
    }
    case "revision/advancedSet":
      return state.revisionAdvanced === action.advanced ? state : { ...state, revisionAdvanced: action.advanced };
    case "revision/infoSet":
      return state.revisionInfo === action.info ? state : { ...state, revisionInfo: action.info };

    case "parts/set":
      return withTargetList(state, action.targetList);
    case "parts/startupRetained":
      return withTargetList(state, retainTargetsOnSameRevision(state.targetList, action.incoming));
    case "parts/refreshedIfUnchanged":
      return state.targetList === action.before ? withTargetList(state, action.next) : state;
    case "parts/pathSliceMerged": {
      const current = state.targetList;
      const { slice, stableOrder } = action;
      if (!current || current.revision_id !== slice.revision_id) return state;
      const seen = new Set(current.targets.map((target) => target.target_id));
      const mergedTargets = [...current.targets, ...slice.targets.filter((target) => !seen.has(target.target_id))];
      const merged: ReviewTargetList = { ...current, targets: mergedTargets };
      // A bounded slice is not the authoritative queue: only a complete list takes the stable order.
      const stabilized = merged.complete !== false && stableOrder.length > 0
        ? applyStableOrder(merged, stableOrder)
        : merged;
      return {
        ...state,
        targetList: {
          ...stabilized,
          outline: current.outline,
          progress: current.progress,
          movement: current.movement,
          complete: current.complete,
        },
      };
    }
    case "parts/commentTargetApplied": {
      const current = state.targetList;
      if (!current) return state;
      return {
        ...state,
        targetList: {
          ...current,
          targets: withPriorities(
            current.targets.map((target) => target.unit_key === action.target.unit_key ? action.target : target),
            action.priorities,
          ),
          progress: action.progress,
          outline: action.outline ?? mergeOutline(current.outline, action.outlineUpdates),
        },
      };
    }

    case "marks/swapped": {
      const current = state.targetList;
      if (!current) return state;
      const byId = new Map(action.swaps.map((swap) => [swap.targetId, swap]));
      let outline = current.outline;
      let changed = false;
      const nextTargets = current.targets.map((item) => {
        const swap = byId.get(item.target_id);
        if (!swap || item.state !== swap.expected) return item;
        changed = true;
        outline = shiftOutlineState(outline, item.path, item.state, swap.next.state);
        return swap.next;
      });
      if (!changed) return state;
      return {
        ...state,
        targetList: { ...current, targets: nextTargets, progress: progressFromTargets(nextTargets), outline },
      };
    }
    case "marks/committed": {
      const { target, result, othersInFlight } = action;
      const committedMarks = state.committedMarks + 1;
      const current = state.targetList;
      if (!current) return { ...state, committedMarks };
      const replacement = result.target ?? { ...target, state: result.recorded, changed_since_mark: false };
      const shown = current.targets.find((item) => item.target_id === target.target_id)?.state ?? target.state;
      const nextTargets = current.targets.map((item) => item.target_id === target.target_id ? replacement : item);
      // With other marks still in flight, the server's totals predate them;
      // taking them would briefly undo marks the reviewer already sees.
      if (othersInFlight) {
        return {
          ...state,
          committedMarks,
          targetList: {
            ...current,
            targets: nextTargets,
            progress: progressFromTargets(nextTargets),
            outline: shiftOutlineState(current.outline, target.path, shown, replacement.state),
          },
        };
      }
      return {
        ...state,
        committedMarks,
        targetList: {
          ...current,
          targets: withPriorities(nextTargets, result.priorities),
          progress: result.progress ?? progressFromTargets(nextTargets),
          outline: result.outline ?? mergeOutline(current.outline, result.outline_updates),
        },
      };
    }
    case "marks/bulkApplied": {
      const { path, result } = action;
      const bulkUndo: ReviewStoreBulkUndo | null = result.marked.length > 0 ? {
        path,
        marks: result.marked.map((item) => ({ unit_key: item.unit_key, event_id: item.event_id })),
      } : null;
      const current = state.targetList;
      if (!current) return { ...state, bulkUndo };
      const marked = new Set(result.marked.map((item) => item.unit_key));
      const nextTargets = withPriorities(current.targets.map((target) => marked.has(target.unit_key)
        ? { ...target, state: "reviewed" as const, changed_since_mark: false, reviewed_revision_id: current.revision_id }
        : target), result.priorities);
      return {
        ...state,
        bulkUndo,
        targetList: {
          ...current,
          targets: nextTargets,
          progress: result.progress ?? progressFromTargets(nextTargets),
          outline: result.outline ?? mergeOutline(current.outline, result.outline_updates),
        },
      };
    }
    case "marks/bulkUndone": {
      const { result } = action;
      const current = state.targetList;
      if (!current) return state.bulkUndo === null ? state : { ...state, bulkUndo: null };
      const undone = new Set(result.undone.map((item) => item.unit_key));
      const nextTargets = withPriorities(current.targets.map((target) => undone.has(target.unit_key)
        ? { ...target, state: "unreviewed" as const, changed_since_mark: false, reviewed_revision_id: "" }
        : target), result.priorities);
      return {
        ...state,
        bulkUndo: null,
        targetList: {
          ...current,
          targets: nextTargets,
          progress: result.progress ?? progressFromTargets(nextTargets),
          outline: result.outline ?? mergeOutline(current.outline, result.outline_updates),
        },
      };
    }

    case "notes/loadStarted":
      return state.commentsState === "loading" ? state : { ...state, commentsState: "loading" };
    case "notes/loaded":
      return state.annotations === action.annotations && state.commentsState === "ready"
        ? state
        : { ...state, annotations: action.annotations, commentsState: "ready" };
    case "notes/loadFailed":
      return state.commentsState === "failed" ? state : { ...state, commentsState: "failed" };
    case "notes/set":
      return withAnnotations(state, action.annotations);
    case "notes/merged": {
      const currentById = new Map(state.annotations.map((item) => [item.id, item]));
      return {
        ...state,
        annotations: action.annotations.map((item) => {
          const previous = currentById.get(item.id);
          return previous && previous.updated_at > item.updated_at ? previous : item;
        }),
      };
    }
    case "notes/stateShown": {
      // Always a new array: the readiness effect depends on the list's identity.
      const { id, from, to } = action;
      return {
        ...state,
        annotations: state.annotations.map(
          (item) => item.id === id && item.state === from ? { ...item, state: to } : item,
        ),
      };
    }
    case "notes/replaced":
      return {
        ...state,
        annotations: state.annotations.map((item) => item.id === action.id ? action.annotation : item),
      };

    case "draft/set":
      return state.draft === action.draft ? state : { ...state, draft: action.draft };
    case "draft/proposalTextFilled": {
      const current = state.draft;
      const pending = Boolean(current
        && current.mode === "proposal"
        && current.path === action.path
        && current.startLine === action.startLine
        && current.endLine === action.endLine);
      if (!current || !pending) return state;
      return { ...state, draft: { ...current, replacementText: current.replacementText ?? action.text } };
    }
    case "draft/proposalLinked": {
      const current = state.draft;
      if (!current) return state;
      return { ...state, draft: { ...current, proposalId: action.proposalId } };
    }

    case "part/selected":
      return state.activeTargetId === action.targetId ? state : { ...state, activeTargetId: action.targetId };
  }
}
