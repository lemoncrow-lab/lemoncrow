/**
 * The Reader's friction counts that leave the browser (status index `review-friction-metrics-deferred`).
 *
 * Each is one fixed metric name sent to this machine's own server, which adds it to the anonymous feature counts
 * (`lemoncrow_client/usage.py`): counts and duration buckets only, no path, title, code or Review id in the count, and
 * nothing kept or sent while telemetry is off. A hosted workspace has no such route and is never asked. Every function
 * returns at once and never throws: a count must not change what the reviewer did.
 */
import { isHostedWorkspace } from "./hostedOnly";
import { requestBrowserJson } from "./reviewApi";
import { REVIEW_FIRST_JUDGMENT_KEY } from "./reviewFriction";

export const REVIEW_FRICTION_PATH = "/api/review-friction";

function send(body: { metric: string; review_id?: string }): void {
  void isHostedWorkspace()
    .then((hosted) => hosted ? null : requestBrowserJson<unknown>(REVIEW_FRICTION_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }))
    .catch(() => null);
}

/** The Map or X-Ray was opened, on a change with or without an architecture delta to show. */
export function countMapOpen(hasDelta: boolean): void {
  send({ metric: hasDelta ? "map_open_with_delta" : "map_open_without_delta" });
}

/** The reviewer asked why a blocked action is blocked. */
export function countBlockedReason(): void {
  send({ metric: "blocked_reason" });
}

/** Once per open of a Review: the server reads who it waits on and for how long; the id is not counted. */
export function countWaiting(reviewId: string): void {
  send({ metric: "waiting", review_id: reviewId });
}

/** A setting was changed before this browser's first human judgment; later changes are not counted. */
export function countSettingsChange(): void {
  try {
    if (window.localStorage.getItem(REVIEW_FIRST_JUDGMENT_KEY)) return;
  } catch {
    return; // Without storage the "before" cannot be known, so nothing is claimed.
  }
  send({ metric: "settings_before_first_review" });
}
