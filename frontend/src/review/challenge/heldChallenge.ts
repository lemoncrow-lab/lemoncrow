// The one challenge a Review tab holds: its claim and, after consent, the run's
// answer. Per-tab view state (sessionStorage), not a record: the durable result
// is the executor's own evidence. A held challenge read on another Snapshot is
// shown as stale, never as a fact about the Snapshot shown now. Dismiss clears it;
// the next challenge replaces it; it ends with the tab.
import { useCallback, useEffect, useState } from "react";

import type { ChallengeClaim, ChallengeRun } from "./challengeModel";

export interface HeldChallenge {
  claim: ChallengeClaim;
  run: ChallengeRun | null;
}

const PREFIX = "lemoncrow.review.challenge.";

function valid(value: unknown): value is HeldChallenge {
  if (!value || typeof value !== "object") return false;
  const { claim, run } = value as { claim?: Partial<ChallengeClaim>; run?: unknown };
  return Boolean(
    claim
      && typeof claim.text === "string"
      && typeof claim.ref === "string"
      && typeof claim.snapshotIdentity === "string"
      && Array.isArray(claim.sources)
      && Array.isArray(claim.targetIds)
      && Array.isArray(claim.namedChecks)
      && (run === null || (typeof run === "object" && typeof (run as ChallengeRun).claimDigest === "string")),
  );
}

function read(reviewId: string): HeldChallenge | null {
  if (!reviewId) return null;
  try {
    const raw = window.sessionStorage.getItem(PREFIX + reviewId);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    return valid(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function write(reviewId: string, value: HeldChallenge | null): void {
  if (!reviewId) return;
  try {
    if (value) window.sessionStorage.setItem(PREFIX + reviewId, JSON.stringify(value));
    else window.sessionStorage.removeItem(PREFIX + reviewId);
  } catch {
    // Storage blocked: the challenge still works for this mount.
  }
}

// Every mounted holder of one Review (the Brief, the Context drawer's Investigate
// tab) shows the same claim: a write in one is announced to the others, since
// sessionStorage raises no event in the tab that wrote it.
const holders = new Set<(reviewId: string, value: HeldChallenge | null) => void>();

export function useHeldChallenge(reviewId: string): [HeldChallenge | null, (value: HeldChallenge | null) => void] {
  const [held, setHeld] = useState<HeldChallenge | null>(() => read(reviewId));
  useEffect(() => setHeld(read(reviewId)), [reviewId]);
  useEffect(() => {
    const holder = (id: string, value: HeldChallenge | null) => { if (id === reviewId) setHeld(value); };
    holders.add(holder);
    return () => { holders.delete(holder); };
  }, [reviewId]);
  const update = useCallback((value: HeldChallenge | null) => {
    setHeld(value);
    write(reviewId, value);
    for (const holder of holders) holder(reviewId, value);
  }, [reviewId]);
  return [held, update];
}
