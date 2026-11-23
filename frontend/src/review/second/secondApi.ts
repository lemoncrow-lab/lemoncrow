import { requestBrowserJson } from "../reviewApi";
import { fetchReviewEnvironment } from "../reviewEnvironment";

/** The server's status of a review's second signature (`GET /reviews/<id>/second-reviewer`). Hosted only (SPEC 7.1 P0-2). */
export type SecondState =
  | "not_required"
  | "unavailable"
  | "not_needed_yet"
  | "will_be_asked"
  | "waiting"
  | "agrees"
  | "disagrees";

export interface SecondPerson {
  id: string;
  name: string;
  handle: string;
}

export interface SecondSignatureFact {
  outcome_id: string;
  outcome: "lgtm" | "changes_requested" | "comment";
  signed: boolean;
  signed_at: string;
  record_id: string;
  note?: string;
  needs_change?: string[];
  /** Her note is in the author's note set (a published must-fix note); `null`: only her verdict holds it. */
  note_sent?: { count: number; to: string } | null;
}

export interface SecondPart {
  unit_key: string;
  path: string;
  label: string;
  start_line: number;
  end_line: number;
  state: string;
}

export interface SecondFirstAnswer {
  unit_key: string;
  state: string;
  notes: string[];
}

export interface SecondReviewerStatus {
  state: SecondState;
  role: "first" | "second" | "other";
  required: boolean;
  unavailable: string;
  area: string;
  decision_count: number;
  revision_id: string;
  reviewer: SecondPerson | null;
  first_reviewer: SecondPerson | null;
  first: SecondSignatureFact | null;
  second: SecondSignatureFact | null;
  /** `null` for a second reviewer who has not answered yet: she is told no count (P7-1 F). */
  signatures: { have: number; needed: number } | null;
  verdict: "approved" | "changes_requested" | "waiting";
  /** Only for the second reviewer. */
  parts?: SecondPart[];
  answered?: boolean;
  agrees?: boolean;
  first_answers_hidden?: boolean;
  /** Present only after the second reviewer answered: the server withholds it until then. */
  first_answers?: SecondFirstAnswer[];
}

const absent = new Map<string, boolean>();

/**
 * The status, or `null` when this server has no second reviewer. A local install is known to have none from its
 * own discovery document (SPEC 7.1 P0-2), so it is never even asked; an older hosted server answers 404, and that is
 * absent too, not unavailable. Any other failure throws, so a hosted server that cannot answer never reads as absent.
 */
export async function fetchSecondReviewer(reviewId: string): Promise<SecondReviewerStatus | null> {
  if (absent.get(origin(reviewId))) return null;
  if ((await fetchReviewEnvironment("local")).mode === "local") return null;
  try {
    return await requestBrowserJson<SecondReviewerStatus>(`/api/reviews/${encodeURIComponent(reviewId)}/second-reviewer`);
  } catch (reason) {
    const status = (reason as { status?: number }).status;
    // A missing route is FastAPI's bare "Not Found"; a missing review says more, and is not "no second reviewer here".
    if ((status === 404 || status === 405) && reason instanceof Error && /^(Not Found|Method Not Allowed|404|405)$/.test(reason.message)) {
      absent.set(origin(reviewId), true);
      return null;
    }
    throw reason;
  }
}

function origin(_reviewId: string): string {
  // One answer per server: a 404 on any review means the route does not exist here.
  return "server";
}

/** Forget "this server has no second reviewer" (tests). */
export function resetSecondReviewerSupport(): void {
  absent.clear();
}

export function signSecond(reviewId: string, expectedRevisionId: string, note: string): Promise<SecondReviewerStatus> {
  return requestBrowserJson<SecondReviewerStatus>(`/api/reviews/${encodeURIComponent(reviewId)}/second-reviewer/sign`, {
    method: "POST",
    body: JSON.stringify({ expected_revision_id: expectedRevisionId, note }),
  });
}
