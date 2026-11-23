/** P2-3: Quick check grouping and batch guard. No AI verdicts and no live-checkout evidence. */
import { isJudgedState, targetAnswer } from "../answerModel";
import { aiFindingsForPart } from "../focus/focusAnnotations";
import { partName, partsOf } from "../overview/overviewModel";
import { readOrder, routineSinceReview, type HasProof } from "../sinceReview";
import type { Annotation, BatchGuard, FileDetail, ReviewTarget } from "../types";
import { ROOT_MODULE, moduleOf } from "../scale/mapModules";

/** A kind ("Small logic changes", "Tests", "Config and docs") or, in a large change (SPEC 4.15), a directory. */
export type QuickGroup = string;
export type QuickGuard = { guarded: true; reason: string } | { guarded: false; reason: "" };
export type QuickPatch = Pick<FileDetail, "path" | "revision_id" | "renderable" | "patch">;
/** P11-2: parts whose only change since the reviewer's own review is routine, confirmed in one step. */
export const SINCE_REVIEW_GROUP = "Routine changes since your review";
export const GROUP_ORDER: readonly string[] = [SINCE_REVIEW_GROUP, "Small logic changes", "Tests", "Config and docs"];

/**
 * An AI hint that says a part changes a result. The hint is AI text, not a measurement, so this stays a client check
 * on the hint only. Arithmetic and loop-bound detection is the server's (`batch_guard`, `batch_guard.py`).
 */
const RESULT_HINT = /\b(result|return|output|total|sum|count|skips?|cap(?:ped)?|limit|off.by.one|values?|discount|permission|authorization|changes? the behavior)\b/i;
const NON_BATCH_PATH = /(?:^|\/)(?:package-lock\.json|npm-shrinkwrap\.json|poetry\.lock|uv\.lock|pdm\.lock|Cargo\.lock|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|go\.sum|Gemfile\.lock|composer\.lock|Pipfile\.lock|Podfile\.lock|mix\.lock|gradle\.lockfile|packages\.lock\.json)$/i;

/**
 * Keep the first guard the server reported for each part of this revision. A mark replaces the target and drops the
 * field, and a Run test must never move a guarded card into a batch (the SPEC card stays its own after the run).
 */
export function latchGuards(previous: { revision: string; guards: ReadonlyMap<string, BatchGuard> }, targets: readonly ReviewTarget[], revisionId: string): { revision: string; guards: ReadonlyMap<string, BatchGuard> } {
  const base = previous.revision === revisionId ? previous.guards : new Map<string, BatchGuard>();
  let next: Map<string, BatchGuard> | null = null;
  for (const target of targets) {
    if (!target.batch_guard || base.has(target.target_id)) continue;
    next ??= new Map(base);
    next.set(target.target_id, target.batch_guard);
  }
  if (!next && previous.revision === revisionId) return previous;
  return { revision: revisionId, guards: next ?? base };
}

const sessionLatch = new Map<string, { revision: string; guards: ReadonlyMap<string, BatchGuard> }>();

/** The page session's latch for one review: it outlives the screen, so leaving Quick check and returning keeps it. */
export function latchSession(reviewId: string, targets: readonly ReviewTarget[], revisionId: string) {
  const next = latchGuards(sessionLatch.get(reviewId) ?? { revision: "", guards: new Map() }, targets, revisionId);
  sessionLatch.set(reviewId, next);
  return next;
}

/**
 * P11-2: the parts first seen routine since the reviewer's review. A mark replaces the target and drops `since_review`
 * (and clears `changed_since_mark`), so without this a part just confirmed in its batch would fall into the base-diff
 * guard and leave its group. The latch only keeps a group steady; it never admits a part that was not proved routine.
 */
export function latchRoutine(previous: { revision: string; ids: ReadonlySet<string> }, targets: readonly ReviewTarget[], revisionId: string): { revision: string; ids: ReadonlySet<string> } {
  const base = previous.revision === revisionId ? previous.ids : new Set<string>();
  let next: Set<string> | null = null;
  for (const target of targets) {
    if (base.has(target.target_id) || !routineSinceReview(target)) continue;
    next ??= new Set(base);
    next.add(target.target_id);
  }
  if (!next && previous.revision === revisionId) return previous;
  return { revision: revisionId, ids: next ?? base };
}

const routineLatch = new Map<string, { revision: string; ids: ReadonlySet<string> }>();

export function latchRoutineSession(reviewId: string, targets: readonly ReviewTarget[], revisionId: string): ReadonlySet<string> {
  const next = latchRoutine(routineLatch.get(reviewId) ?? { revision: "", ids: new Set() }, targets, revisionId);
  routineLatch.set(reviewId, next);
  return next.ids;
}

/** For tests only. */
export function resetGuardLatch() { sessionLatch.clear(); routineLatch.clear(); }

/**
 * The reason line once the reviewer's own run has finished. "Test didn't run" would be false by then; the card stays
 * out of the batch either way, because a pass does not show the changed lines are checked.
 */
export function reasonAfterRun(reason: string, run: { state: string; verdict: string } | undefined): string {
  if (!run || run.state !== "done" || !reason.startsWith("Test didn't run")) return reason;
  if (run.verdict === "PASS") return "Test passed, but nothing shows it checks the changed lines. Answer for this part.";
  if (run.verdict === "FAIL") return "Test failed. Read the result before you answer for this part.";
  return "Test gave no result, so nothing checks the changed lines. Answer for this part.";
}

/**
 * What the part and the server's answers alone decide (no patch, no AI hint). A part reopened since the reviewer's review is judged by what changed since then
 * (P11-2): a proved-routine change (`since_review`) is compared with the content the reviewer reviewed, so the base
 * diff's `batch_guard` does not apply to it; every other reopened part, and every part the server did not classify,
 * is guarded.
 */
function structuralGuard(target: ReviewTarget, serverGuard: BatchGuard | undefined = target.batch_guard, routine: boolean = routineSinceReview(target)): QuickGuard {
  if (targetAnswer(target).category !== "quick_check") return { guarded: true, reason: "This part needs a decision, not a batch confirmation." };
  if (NON_BATCH_PATH.test(target.path) || target.kind === "dependency") return { guarded: true, reason: "Dependency or lockfile changes need an individual decision." };
  if (routine) return { guarded: false, reason: "" };
  if (target.changed_since_mark || target.state === "changed_since_review") return { guarded: true, reason: "This part changed after a previous answer. Check the new code individually." };
  if (!serverGuard) return { guarded: true, reason: "Could not be checked: the server did not classify this part. This part stays out of the batch." };
  if (serverGuard.guarded) return { guarded: true, reason: serverGuard.reason || "Could not be checked: the server gave no reason. This part stays out of the batch." };
  return { guarded: false, reason: "" };
}

/**
 * The one answer to "does this quick check stay out of the batch?", for the Overview's count and the Quick check screen.
 * Membership never depends on what code has loaded: a part the server proved safe stays in the batch whether or not its
 * code has been read here (a large change reads only the files around the part in hand), and the code is read on demand
 * when the batch is confirmed. A patch that was read and is unusable (`null`: failed, another revision's, not renderable
 * or empty) takes the part out. `undefined` means not read. A quick-check part with no server guard fails closed.
 */
export function quickGuard(target: ReviewTarget, revisionId: string, patch?: QuickPatch | null, findings: readonly Annotation[] = [], serverGuard: BatchGuard | undefined = target.batch_guard, routine: boolean = routineSinceReview(target)): QuickGuard {
  const structural = structuralGuard(target, serverGuard, routine);
  if (structural.guarded) return structural;
  if (findings.some((finding) => RESULT_HINT.test(finding.body))) return { guarded: true, reason: "An anchored AI hint raises a possible change to the result. Read the code and answer yourself." };
  if (patch !== undefined && (!patch || patch.revision_id !== revisionId || patch.path !== target.path || !patch.renderable || !patch.patch)) {
    return { guarded: true, reason: "The frozen code evidence is unavailable or belongs to another revision; batch confirmation is unavailable." };
  }
  return { guarded: false, reason: "" };
}

/** Everything `guardOfPart` reads besides the part itself, so every screen asks the same question over the same inputs. */
export interface GuardContext {
  reviewId: string;
  /** The id annotations carry for this review (the session id). */
  annotationReviewId: string;
  revisionId: string;
  /** Open AI findings only (source "ai_review"); `aiFindingsForPart` narrows them per part. */
  aiNotes: readonly Annotation[];
  serverGuards: ReadonlyMap<string, BatchGuard>;
  routineIds: ReadonlySet<string>;
  /** Paths whose frozen code was read and is unusable (see patchEvidence). */
  failedPaths: ReadonlySet<string>;
}

/** The context for `quickParts` of one revision: latches the server's first answers for them. */
export function guardContext(input: { reviewId: string; annotationReviewId: string; revisionId: string; parts: readonly ReviewTarget[]; annotations: readonly Annotation[]; failedPaths: ReadonlySet<string> }): GuardContext {
  return {
    reviewId: input.reviewId,
    annotationReviewId: input.annotationReviewId,
    revisionId: input.revisionId,
    aiNotes: input.annotations.filter((note) => note.source === "ai_review"),
    serverGuards: latchSession(input.reviewId, input.parts, input.revisionId).guards,
    routineIds: latchRoutineSession(input.reviewId, input.parts, input.revisionId),
    failedPaths: input.failedPaths,
  };
}

const NO_PATHS: ReadonlySet<string> = new Set();
/** A context with only what the part itself carries: no latch, no AI hint, nothing read. */
export function plainGuardContext(revisionId: string): GuardContext {
  return { reviewId: "", annotationReviewId: "", revisionId, aiNotes: [], serverGuards: new Map(), routineIds: NO_PATHS, failedPaths: NO_PATHS };
}

export function guardOfPart(target: ReviewTarget, context: GuardContext): QuickGuard {
  const findings = context.aiNotes.length > 0 ? aiFindingsForPart(context.aiNotes, context.annotationReviewId, context.revisionId, target, true) : [];
  return quickGuard(
    target, context.revisionId, context.failedPaths.has(target.path) ? null : undefined, findings,
    context.serverGuards.get(target.target_id), context.routineIds.has(target.target_id) || routineSinceReview(target),
  );
}

export function quickGroup(target: ReviewTarget, routine: boolean = routineSinceReview(target)): QuickGroup {
  if (routine) return SINCE_REVIEW_GROUP;
  if (/(^|\/)(?:tests?|__tests__)(\/|$)|(?:^|\.)test\.|\.spec\./i.test(target.path)) return "Tests";
  if (/\.(?:md|mdx|rst|txt|yaml|yml|toml|json|ini|cfg|css|scss)$/i.test(target.path)) return "Config and docs";
  return "Small logic changes";
}

/** The quick checks in the order they are asked: changed since the reviewer's review first (no proof before proof), then the rest. */
export function quickParts(targets: readonly ReviewTarget[], hasProof?: HasProof) {
  return readOrder(partsOf(targets, "quick_check"), hasProof);
}

/** The directory a part is grouped under in a large change: the part's parent directory ("Repository root" at the top). */
export function quickDirectory(target: ReviewTarget): string {
  return moduleOf(target.path);
}

/**
 * Groups of parts that may be confirmed together: only parts `quickGuard` clears, never a guarded one (SCALE-2). By kind,
 * or by directory when `byDirectory` is set (a large change, SPEC 4.15), "Repository root" first, then directories in name order.
 */
export function groupedQuick(targets: readonly ReviewTarget[], guards: ReadonlyMap<string, QuickGuard>, byDirectory = false, routineIds: ReadonlySet<string> = new Set()): Array<{ name: QuickGroup; parts: ReviewTarget[] }> {
  if (byDirectory) {
    const groups = new Map<string, ReviewTarget[]>();
    // What the server proved routine since the review stays one group at any size: it is the Overview's "one step".
    const routine: ReviewTarget[] = [];
    for (const part of targets) {
      if (guards.get(part.target_id)?.guarded !== false) continue;
      if (routineIds.has(part.target_id) || routineSinceReview(part)) { routine.push(part); continue; }
      const name = quickDirectory(part);
      const list = groups.get(name);
      if (list) list.push(part); else groups.set(name, [part]);
    }
    const directories = [...groups].sort(([a], [b]) => (a === ROOT_MODULE ? -1 : b === ROOT_MODULE ? 1 : a.localeCompare(b))).map(([name, parts]) => ({ name, parts }));
    return routine.length > 0 ? [{ name: SINCE_REVIEW_GROUP, parts: routine }, ...directories] : directories;
  }
  return GROUP_ORDER.map((name) => ({
    name,
    parts: targets.filter((part) => guards.get(part.target_id)?.guarded === false && quickGroup(part, routineIds.has(part.target_id) || routineSinceReview(part)) === name),
  })).filter((group) => group.parts.length > 0);
}

/** A change of at most this many files has every patch read, as before. */
export const QUICK_PATCH_ALL_FILES = 200;
/** In a larger change, patches are read for this many files around the part in hand (the cards and rows drawn)... */
export const QUICK_PATCH_WINDOW = 20;
/** ...plus this many more after it, so answering onward finds the next patches already read. */
export const QUICK_PATCH_LOOKAHEAD = 10;

/**
 * The files whose patches Quick check should have read, the one in hand first. A small change reads every file. A larger one
 * reads a window around `anchorPath` (in the parts' own order) and a look-ahead after it, so the requests follow what is drawn
 * instead of growing with the change. A part whose patch is not read stays on its own card, which is the safe side.
 */
export function quickPatchPaths(parts: readonly { path: string }[], anchorPath: string): string[] {
  const unique = [...new Set(parts.map((part) => part.path))];
  const inHandFirst = (list: readonly string[]) => (anchorPath && unique.includes(anchorPath) ? [anchorPath, ...list.filter((path) => path !== anchorPath)] : [...list]);
  if (unique.length <= QUICK_PATCH_ALL_FILES) return inHandFirst(unique);
  const centre = Math.max(0, unique.indexOf(anchorPath));
  const start = Math.max(0, Math.min(centre - Math.floor(QUICK_PATCH_WINDOW / 2), unique.length - QUICK_PATCH_WINDOW));
  return inHandFirst(unique.slice(start, start + QUICK_PATCH_WINDOW + QUICK_PATCH_LOOKAHEAD));
}

export function quickRemaining(targets: readonly ReviewTarget[]): number {
  return targets.filter((part) => !isJudgedState(part.state)).length;
}

export function quickConfirmation(parts: readonly ReviewTarget[]): string {
  return `${parts.length} ${parts.length === 1 ? "part" : "parts"} saved as reviewed`;
}

export function quickPartLabel(target: ReviewTarget): string {
  return `${partName(target)} · ${target.path}`;
}
