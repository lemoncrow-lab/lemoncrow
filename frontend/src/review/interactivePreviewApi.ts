/**
 * Interactive side-by-side preview of one exact Review Snapshot (Phase 9R item 16).
 *
 * Base and head run sandboxed for a limited time, only when the run plan admits
 * the Deep manual entry, against the one visible budget. What the reviewer sees
 * is a human observation recorded as evidence, never a check verdict. The
 * one-time session token lives only in the opener's memory; each entry URL
 * carries a one-time ticket in its fragment, which no server or log receives.
 */
import { requestBrowserJson } from "./reviewApi";

export type InteractivePreviewState =
  | "not_offered"
  | "not_available"
  | "limited_by_budget"
  | "available";
export type InteractivePreviewSide = "old" | "new";
export type InteractivePreviewSessionState =
  | "starting"
  | "running"
  | "expired"
  | "stopped"
  | "superseded"
  | "closed"
  | "failed";

export interface InteractivePreviewSession {
  id: string;
  review_id: string;
  revision_id: string;
  created_at: number;
  expires_at: number;
  ttl_seconds: number;
  state: InteractivePreviewSessionState;
  run_on: string;
  executor_class: string;
  mode: string;
  comparable: boolean;
  comparability: string;
  stop_reason: string;
  limited_by_budget: boolean;
  compute_seconds: number;
  events: number;
  recording_evidence_id: string;
  /** The environment.toml preview it runs ("": review.yaml or a static build). */
  preview?: string;
  /** Live sessions only: each side's own origin. */
  origins?: Partial<Record<InteractivePreviewSide, string>>;
  /** Live sessions only, by the server's clock. */
  remaining_seconds?: number;
}

export interface InteractivePreviewOffer {
  state: InteractivePreviewState;
  reason: string;
  revision_id: string;
  run_on: string | null;
  executor_class: string | null;
  recipe: { mode: string; source: string; framework: string } | null;
  /** The trusted base's environment.toml previews, in declaration order; the first is the default. */
  previews: string[];
  /** The preview this offer describes; null without environment.toml previews. */
  preview: string | null;
  budget: { remaining_minutes: number | null; minutes_per_minute: number };
  ttl: { default_seconds: number; max_seconds: number; min_seconds: number };
  opens_from: string[];
  observation: "human";
  sessions: InteractivePreviewSession[];
}

export interface InteractivePreviewStart {
  session: InteractivePreviewSession;
  token: string;
}

export const PREVIEW_TOKEN_HEADER = "X-LemonCrow-Preview-Token";

function base(reviewId: string): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/interactive-preview`;
}

export function fetchInteractivePreview(
  reviewId: string
): Promise<InteractivePreviewOffer> {
  return requestBrowserJson<InteractivePreviewOffer>(base(reviewId));
}

/**
 * Start base and head (a manual run-plan amendment); refused (409) when the plan would not run it.
 * `preview` names one of the offer's `previews`; without it the server runs the first declared.
 */
export function startInteractivePreview(
  reviewId: string,
  expectedRevisionId: string,
  ttlSeconds?: number,
  preview?: string
): Promise<InteractivePreviewStart> {
  return requestBrowserJson<InteractivePreviewStart>(
    `${base(reviewId)}/sessions`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_revision_id: expectedRevisionId,
        ...(ttlSeconds === undefined ? {} : { ttl_seconds: ttlSeconds }),
        ...(preview === undefined ? {} : { preview }),
      }),
    }
  );
}

/** A one-time entry URL into one side, for this session's own viewer only. */
export function enterInteractivePreview(
  reviewId: string,
  sessionId: string,
  token: string,
  side: InteractivePreviewSide,
  path = "/"
): Promise<{ url: string }> {
  return requestBrowserJson<{ url: string }>(
    `${base(reviewId)}/sessions/${encodeURIComponent(sessionId)}/enter`,
    {
      method: "POST",
      headers: { [PREVIEW_TOKEN_HEADER]: token },
      body: JSON.stringify({ side, path }),
    }
  );
}

export function stopInteractivePreview(
  reviewId: string,
  sessionId: string,
  expectedRevisionId: string
): Promise<{ session: InteractivePreviewSession }> {
  return requestBrowserJson<{ session: InteractivePreviewSession }>(
    `${base(reviewId)}/sessions/${encodeURIComponent(sessionId)}/stop`,
    {
      method: "POST",
      body: JSON.stringify({ expected_revision_id: expectedRevisionId }),
    }
  );
}
