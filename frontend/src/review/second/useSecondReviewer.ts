import { useCallback, useEffect, useState } from "react";

import { fetchSecondReviewer, type SecondReviewerStatus } from "./secondApi";

/**
 * The wait before the n-th re-read while a signature is owed: 5 s, then doubling to a 30 s ceiling. A hosted server
 * holds each person to 120 requests a minute (the shipped policy); this is at most 12 a minute at the start and 2 a
 * minute at rest, per screen, and nothing at all while the tab is hidden.
 */
export function watchDelayMs(attempt: number): number {
  return Math.min(5_000 * 2 ** Math.max(0, attempt), 30_000);
}

/** The most re-reads one watcher makes in any 60 s window (for the request budget). */
export function worstCaseReadsPerMinute(): number {
  const times: number[] = [];
  for (let attempt = 0, at = 0; at < 600_000; attempt += 1) {
    at += watchDelayMs(attempt);
    times.push(at);
  }
  return Math.max(...times.map((start) => times.filter((time) => time >= start && time < start + 60_000).length));
}

export interface SecondReviewerView {
  /** `null` until read, and for good where this server has no second reviewer (a local install). */
  status: SecondReviewerStatus | null;
  /** The first read is over (answered, absent or failed): until then nobody knows which kind of server this is. */
  settled: boolean;
  /** The read failed on a server that has the feature: say so, never read it as absent. */
  failed: boolean;
  refresh: () => Promise<SecondReviewerStatus | null>;
}

/**
 * The second-signature status of one review. Local: always `status: null` after one 404, so nothing about a second
 * reviewer is drawn. `watch` re-reads on that interval while a signature is waited for.
 */
export function useSecondReviewer(apiReviewId: string, options: { enabled?: boolean; watchMs?: number } = {}): SecondReviewerView {
  const { enabled = true, watchMs = 0 } = options;
  const [status, setStatus] = useState<SecondReviewerStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [settled, setSettled] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const next = await fetchSecondReviewer(apiReviewId);
      setStatus(next);
      setFailed(false);
      return next;
    } catch {
      setFailed(true);
      return null;
    }
  }, [apiReviewId]);
  useEffect(() => {
    if (!enabled || !apiReviewId) return;
    let live = true;
    void fetchSecondReviewer(apiReviewId)
      .then((next) => { if (live) { setStatus(next); setFailed(false); setSettled(true); } })
      .catch(() => { if (live) { setFailed(true); setSettled(true); } });
    return () => { live = false; };
  }, [apiReviewId, enabled]);
  const waiting = status?.state === "waiting";
  useEffect(() => {
    if (!enabled || !watchMs || !waiting) return;
    let attempt = 0;
    let timer = 0;
    let onShow: (() => void) | null = null;
    const stopWaitingForShow = () => {
      if (onShow) document.removeEventListener("visibilitychange", onShow);
      onShow = null;
    };
    const next = () => {
      timer = window.setTimeout(() => {
        // A hidden tab asks nothing; it reads once when it is shown again.
        if (typeof document !== "undefined" && document.hidden) {
          onShow = () => { stopWaitingForShow(); void refresh(); next(); };
          document.addEventListener("visibilitychange", onShow);
          return;
        }
        attempt += 1;
        void refresh();
        next();
      }, watchDelayMs(attempt));
    };
    next();
    return () => {
      window.clearTimeout(timer);
      stopWaitingForShow();
    };
  }, [enabled, watchMs, waiting, refresh]);
  return { status, failed, settled, refresh };
}
