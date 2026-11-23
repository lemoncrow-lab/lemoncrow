import { requestBrowserJson } from "../reviewApi";
import { hostedJson } from "../hostedOnly";
import type { TeamPerson, TeamState } from "../types";

/** A person a review can be handed to, and where they are away (SPEC 4.16). */
export interface Teammate extends TeamPerson {
  /** `reassign_to` is null when the stand-in is not on this review. */
  away: { until: string; reassign_to: TeamPerson | null } | null;
}

function base(reviewId: string): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/team`;
}

/**
 * I am in this review now. `null` when this server has no team routes: a local install is known to have none from its
 * own discovery document and an older hosted server answers a bare 404 (`hostedJson`, SPEC 7.1 P0-2). A discovery
 * that failed, and any other failure, throw: they never read as "no team here".
 */
export function sendPresence(reviewId: string): Promise<TeamState | null> {
  return hostedJson<TeamState>(`${base(reviewId)}/presence`, { method: "POST", body: "{}" });
}

/** The team of one review as it is now (who is in it, what they have sent); the caller has already seen a hosted row. */
export function fetchTeamStatus(reviewId: string): Promise<TeamState> {
  return requestBrowserJson<TeamState>(base(reviewId));
}

export function takeReview(reviewId: string): Promise<TeamState> {
  return requestBrowserJson<TeamState>(`${base(reviewId)}/claim`, { method: "POST", body: "{}" });
}

/** "Let go": give the claim back. */
export function releaseReview(reviewId: string): Promise<TeamState> {
  return requestBrowserJson<TeamState>(`${base(reviewId)}/release`, { method: "POST", body: "{}" });
}

export function handOffReview(reviewId: string, to: string, note: string): Promise<TeamState> {
  return requestBrowserJson<TeamState>(`${base(reviewId)}/handoff`, { method: "POST", body: JSON.stringify({ to, note }) });
}

export async function fetchTeammates(reviewId: string): Promise<Teammate[]> {
  const body = await requestBrowserJson<{ teammates: Teammate[] }>(`${base(reviewId)}/teammates`);
  return body.teammates;
}
