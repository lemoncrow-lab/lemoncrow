import { useEffect } from "react";
import { useLocation } from "react-router-dom";

import { showFlowToast } from "../flowToast";
import { noticeText } from "./teamCopy";
import { sendPresence } from "./teamApi";

/** One beat a minute while this review is on screen and the tab is shown; the server forgets a person after three missed beats. */
export const BEAT_MS = 60_000;
/** The first beat waits until the page has been on screen this long. */
export const FIRST_BEAT_MS = 10_000;
/** After a failed beat the wait doubles to this ceiling, so a struggling server is asked less, never more. */
export const BEAT_CEILING_MS = 5 * 60_000;

export function beatDelayMs(failures: number): number {
  return Math.min(BEAT_MS * 2 ** Math.max(0, failures), BEAT_CEILING_MS);
}

/**
 * A line to show once the review I am about to open is on screen ("Reviewing together with Maya"). A route change
 * clears the toast (R12), and the hosted list opens a review with a full page load, so the line waits in the tab's
 * session storage (memory where that is unavailable) for the review screen's first beat to show it.
 */
const entryToasts = new Map<string, string>();
const ENTRY_KEY = "lemoncrow.team.entryToast.";
export function queueEntryToast(apiReviewId: string, text: string): void {
  entryToasts.set(apiReviewId, text);
  try { window.sessionStorage.setItem(ENTRY_KEY + apiReviewId, text); } catch { /* storage can be unavailable */ }
}
export function takeEntryToast(apiReviewId: string): string {
  let text = entryToasts.get(apiReviewId) ?? "";
  entryToasts.delete(apiReviewId);
  try {
    text = window.sessionStorage.getItem(ENTRY_KEY + apiReviewId) ?? text;
    window.sessionStorage.removeItem(ENTRY_KEY + apiReviewId);
  } catch { /* storage can be unavailable */ }
  return text;
}

/**
 * A route change clears the toast (R12), and a review opens, then lands on its own screen (Overview, the saved part),
 * which can be a route change a moment later. So a notice is shown only once the path has held for `SETTLE_MS`, and it
 * stays pending until it has been on screen for `SEEN_MS` without a route change: one that was cleared by a late
 * redirect shows again on the next settled path. `TeamNoticeAnchor` is rendered inside the flow screen, so a page
 * without a flow screen shows nothing.
 */
export const SETTLE_MS = 800;
export const SEEN_MS = 3_000;
let anchored = false;
let pending = "";
let seenTimer = 0;
function present(): void {
  if (!pending) return;
  showFlowToast({ text: pending });
  window.clearTimeout(seenTimer);
  seenTimer = window.setTimeout(() => { pending = ""; }, SEEN_MS);
}
function deliver(text: string): void {
  pending = text;
  if (anchored) present();
}
export function TeamNoticeAnchor(): null {
  const { pathname } = useLocation();
  useEffect(() => {
    const timer = window.setTimeout(() => { anchored = true; present(); }, SETTLE_MS);
    return () => { window.clearTimeout(timer); window.clearTimeout(seenTimer); anchored = false; };
  }, [pathname]);
  return null;
}

/** Notices already shown on this page, so a notice the server keeps for a while shows once. */
const shown = new Set<string>();

/**
 * Tell the server I am in this review (SPEC 4.16, hosted only), and show what it holds for me: that someone took the
 * review, or handed it to me with a note. Local: nothing is asked and nothing is drawn. A hidden tab sends nothing and
 * beats once when it is shown again, at most 1 request a minute, far under the 120 a minute the server allows.
 */
export function useReviewPresence(apiReviewId: string, enabled: boolean): void {
  useEffect(() => {
    if (!enabled || !apiReviewId) return;
    let live = true;
    let timer = 0;
    let failures = 0;
    let onShow: (() => void) | null = null;
    const stopWaitingForShow = () => {
      if (onShow) document.removeEventListener("visibilitychange", onShow);
      onShow = null;
    };
    const beat = async (): Promise<boolean> => {
      try {
        const team = await sendPresence(apiReviewId);
        if (!live) return false;
        failures = 0;
        if (team === null) return false; // this server has no team: stop for good
        for (const notice of team.notices ?? []) {
          const key = `${apiReviewId}:${notice.kind}:${notice.at}`;
          if (shown.has(key)) continue;
          shown.add(key);
          deliver(noticeText(notice));
        }
      } catch {
        failures += 1;
      }
      return live;
    };
    // A hidden tab sends nothing, the first beat included: the first one waits until the page has been shown for
    // FIRST_BEAT_MS (a page that is only passed through asks nothing), and a later one is sent when the tab is shown.
    const next = (delay: number, first: boolean) => {
      timer = window.setTimeout(() => {
        if (document.hidden) {
          onShow = () => {
            stopWaitingForShow();
            if (first) next(FIRST_BEAT_MS, true);
            else void beat().then((go) => { if (go) next(beatDelayMs(failures), false); });
          };
          document.addEventListener("visibilitychange", onShow);
          return;
        }
        void beat().then((go) => { if (go) next(beatDelayMs(failures), false); });
      }, delay);
    };
    // The line the list queued ("Reviewing together with Maya") does not wait for a beat.
    const entry = takeEntryToast(apiReviewId);
    if (entry) deliver(entry);
    next(FIRST_BEAT_MS, true);
    return () => {
      live = false;
      pending = ""; // a notice about this review is not for the next one
      window.clearTimeout(timer);
      stopWaitingForShow();
    };
  }, [apiReviewId, enabled]);
}
