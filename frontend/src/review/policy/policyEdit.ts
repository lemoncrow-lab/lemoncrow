import { useEffect, useState } from "react";

import { hostedJson } from "../hostedOnly";
import type { ReviewOverview } from "../types";

/** May this viewer approve a change that edits the review policy (`GET /reviews/<id>/policy-edit`, hosted only; SPEC 4.19, 7.1 P0-2)? */
export interface PolicyEditGate {
  allowed: boolean;
  reason: string;
  /** The CODEOWNERS who could approve it. `key` names one for the ask; `asked` is true once they have a review request. */
  needs: { name: string; handle: string; key?: string; asked?: boolean }[];
  /** Who an approval is signed as, when the viewer may approve it (SPEC 4.19: "signed by Maya Chen, CODEOWNER of ..."). */
  signer?: { name: string; handle: string; scope: string } | null;
}

export interface PolicyEditStatus {
  files: string[];
  gate: PolicyEditGate | null;
  /** Each edited file's own diff, asked as a question. */
  questions?: { file: string; text: string }[];
}

export const POLICY_EDIT_TITLE = "This change edits the review policy";
/** SPEC 4.19's banner without its commit hash: the server holds no base commit to show (SPEC 7.1 P7-3 C). */
export function policyEditBody(files: readonly string[]): string {
  return `It changes ${files.join(", ")}. This review is judged against the policy on the base branch, never against the change's own copy.`;
}
export const POLICY_GATE_NOTE = "The policy decides who may weaken it. You can still ask for a change.";

/** The policy files this change edits, from the Overview's own projection (local and hosted alike). */
export function policyEditFiles(overview: Pick<ReviewOverview, "policy">): string[] {
  return overview.policy?.edits_policy?.files ?? [];
}

export const POLICY_GATE_NOTE_ASKED = "Asked. They will see it as a review request.";
export const askOwnerLabel = (name: string): string => `Ask ${name} to review`;
export const askedOwnerLabel = (name: string): string => `Asked ${name}`;
export const signedByLine = (signer: NonNullable<PolicyEditGate["signer"]>): string => `Looks good · signed by ${signer.name}, CODEOWNER of ${signer.scope}`;

/** Why this part's own Looks good and Needs a change are off: the gate's reason, for a policy file the viewer may not approve. */
export function policyPartBlock(path: string, files: readonly string[], gate: PolicyEditGate | null): string {
  return files.includes(path) && gate !== null && !gate.allowed ? gate.reason : "";
}

export function fetchPolicyEdit(reviewId: string): Promise<PolicyEditStatus | null> {
  return hostedJson<PolicyEditStatus>(`/api/reviews/${encodeURIComponent(reviewId)}/policy-edit`);
}

/** The statuses already read, shared by every screen of one review (the banner on the Overview, Wrap up and the part). */
const statuses = new Map<string, PolicyEditStatus | null>();
const listeners = new Set<() => void>();
const reads = new Map<string, Promise<void>>();

function publish(reviewId: string, status: PolicyEditStatus | null): void {
  statuses.set(reviewId, status);
  listeners.forEach((listener) => listener());
}

/** Ask one CODEOWNER to review (`POST /reviews/<id>/policy-ask`); every screen then shows "Asked <name>". */
export async function askPolicyOwner(reviewId: string, key: string): Promise<void> {
  const answer = await hostedJson<{ gate: PolicyEditGate }>(`/api/reviews/${encodeURIComponent(reviewId)}/policy-ask`, {
    method: "POST",
    body: JSON.stringify({ key }),
  });
  const known = statuses.get(reviewId);
  if (answer && known) publish(reviewId, { ...known, gate: answer.gate });
}

/** For tests. */
export function resetPolicyStatusesForTests(): void { statuses.clear(); reads.clear(); listeners.forEach((listener) => listener()); }

/**
 * Who may approve, and each edited file's question, asked only when the change edits a policy file and only on a hosted
 * workspace (a local install has one reviewer and no identities, so it asks nothing and nothing blocks). `null` until
 * read or where there is no gate.
 */
export function usePolicyStatus(reviewId: string, files: readonly string[]): PolicyEditStatus | null {
  const [, force] = useState(0);
  const key = files.join("\u0000");
  useEffect(() => {
    const listener = () => force((n) => n + 1);
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  }, []);
  useEffect(() => {
    if (!reviewId || files.length === 0 || statuses.has(reviewId) || reads.has(reviewId)) return;
    const read = fetchPolicyEdit(reviewId)
      .then((status) => publish(reviewId, status))
      .catch(() => publish(reviewId, null))
      .finally(() => { reads.delete(reviewId); });
    reads.set(reviewId, read);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewId, key]);
  return !reviewId || files.length === 0 ? null : statuses.get(reviewId) ?? null;
}

/** The gate alone (Wrap up, the part). */
export function usePolicyGate(reviewId: string, files: readonly string[]): PolicyEditGate | null {
  return usePolicyStatus(reviewId, files)?.gate ?? null;
}

/** The question this policy file's diff asks, or "" (SPEC 4.19: "Remove `app/payments/**` from always a decision?"). */
export function policyQuestion(status: PolicyEditStatus | null, path: string): string {
  return status?.questions?.find((entry) => entry.file === path)?.text ?? "";
}

/**
 * The reviews among these rows that edit the review policy and that the viewer may approve. The gate is asked only for a
 * row that says it edits the policy, one request each (shared with the review's own screens), so the cost is bounded by
 * the policy changes on the page and a local install asks nothing.
 */
export function usePolicyApprovable(rows: readonly { id: string; edits_policy?: boolean }[] | null): { approvable: ReadonlySet<string> } {
  const [, force] = useState(0);
  useEffect(() => {
    const listener = () => force((n) => n + 1);
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  }, []);
  const wanted = (rows ?? []).filter((row) => row.edits_policy).map((row) => row.id);
  const key = wanted.join("\u0000");
  useEffect(() => {
    for (const id of wanted) {
      if (statuses.has(id) || reads.has(id)) continue;
      const read = fetchPolicyEdit(id)
        .then((status) => publish(id, status))
        .catch(() => publish(id, null))
        .finally(() => { reads.delete(id); });
      reads.set(id, read);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return {
    approvable: new Set(wanted.filter((id) => statuses.get(id)?.gate?.allowed === true)),
  };
}
