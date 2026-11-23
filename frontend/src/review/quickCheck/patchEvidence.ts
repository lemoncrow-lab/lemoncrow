/**
 * P11-2: which files' frozen code Quick check has read for a revision, and which of those could not be used.
 *
 * Batch membership never waits on a read (a part the server proved routine is in the batch whether or not its code has
 * loaded). Only a read that fails, or returns code that is not this revision's or cannot be shown, takes a part out. That
 * answer lives here, outside either screen, so the Overview's "to confirm in one step" and the Quick check batch read the
 * same state and move together.
 */
import { useSyncExternalStore } from "react";
import { fetchFile } from "../reviewApi";

export interface PatchEvidence {
  /** Paths whose frozen code was read and is usable. */
  readonly loaded: ReadonlySet<string>;
  /** Paths whose frozen code failed to load, belongs to another revision, or cannot be rendered. */
  readonly failed: ReadonlySet<string>;
}

const NONE: PatchEvidence = { loaded: new Set(), failed: new Set() };
const entries = new Map<string, PatchEvidence>();
const listeners = new Set<() => void>();

const keyOf = (reviewId: string, revisionId: string) => `${reviewId}\u0000${revisionId}`;

function publish(key: string, next: PatchEvidence) {
  entries.set(key, next);
  for (const listener of listeners) listener();
}

/** Record what reading each path's code came to. A path is in one set only; the latest read wins. */
export function recordPatchResults(reviewId: string, revisionId: string, results: ReadonlyArray<readonly [path: string, usable: boolean]>) {
  const key = keyOf(reviewId, revisionId);
  const before = entries.get(key) ?? NONE;
  let loaded: Set<string> | null = null;
  let failed: Set<string> | null = null;
  for (const [path, usable] of results) {
    if (usable ? before.loaded.has(path) && !before.failed.has(path) : before.failed.has(path) && !before.loaded.has(path)) continue;
    loaded ??= new Set(before.loaded);
    failed ??= new Set(before.failed);
    if (usable) { loaded.add(path); failed.delete(path); } else { failed.add(path); loaded.delete(path); }
  }
  if (loaded && failed) publish(key, { loaded, failed });
}

/** A read that failed is tried again when Quick check opens, so a network blip does not hold a part out for the session. */
export function forgetPatchFailures(reviewId: string, revisionId: string) {
  const key = keyOf(reviewId, revisionId);
  const before = entries.get(key);
  if (before && before.failed.size > 0) publish(key, { loaded: before.loaded, failed: new Set() });
}

export function patchEvidence(reviewId: string, revisionId: string): PatchEvidence {
  return entries.get(keyOf(reviewId, revisionId)) ?? NONE;
}

export function usePatchEvidence(reviewId: string, revisionId: string): PatchEvidence {
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => patchEvidence(reviewId, revisionId),
    () => NONE,
  );
}

/** For tests only. */
export function resetPatchEvidence() {
  entries.clear();
  for (const listener of listeners) listener();
}

/** Paths read per round when a batch loads the code it has not read yet (a round is a burst of requests, not a queue of thousands). */
const READ_ROUND = 20;

/**
 * Read the frozen code of `paths` (requested with `fetchId`) and record whether each is usable for this revision. Never throws: a read that fails
 * is recorded as unusable, which is what takes the part out of the batch.
 *
 * `inHand` is the path on screen: when it is among `paths` it is read alone first and the rest follow in rounds, so the code the reviewer is
 * waiting for is not queued behind a burst of its neighbours (the server answers a burst a few at a time).
 */
export async function readPatches(
  fetchId: string,
  reviewId: string,
  revisionId: string,
  paths: readonly string[],
  options: { readonly inHand?: string } = {},
): Promise<void> {
  const readRound = async (round: readonly string[]) => {
    const results = await Promise.all(round.map(async (path): Promise<readonly [string, boolean]> => {
      try {
        const file = await fetchFile(fetchId, path, revisionId);
        return [path, file.revision_id === revisionId && file.path === path && file.renderable && Boolean(file.patch)];
      } catch {
        return [path, false];
      }
    }));
    recordPatchResults(reviewId, revisionId, results);
  };
  let rest = paths;
  if (options.inHand && paths.includes(options.inHand)) {
    await readRound([options.inHand]);
    rest = paths.filter((path) => path !== options.inHand);
  }
  // lc-debt: rounds of READ_ROUND reads in order; a batch of thousands of distinct files takes many rounds on one click; a server-side "these files are frozen for this revision" answer would replace the reads.
  for (let at = 0; at < rest.length; at += READ_ROUND) {
    await readRound(rest.slice(at, at + READ_ROUND));
  }
}
