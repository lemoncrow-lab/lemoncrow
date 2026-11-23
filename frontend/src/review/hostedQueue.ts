import type { HostedAction, HostedAttachmentStatus, HostedQueuedWrite } from "./types";

/** A Reader write the server queued for hosted (HTTP 202) instead of applying locally. */
export function isQueuedWrite(value: unknown): value is HostedQueuedWrite {
  return typeof value === "object" && value !== null && (value as { queued?: unknown }).queued === true;
}

/** Why hosted answered `conflict`; the reviewer re-applies explicitly or discards. */
export const HOSTED_CONFLICT_TEXT: Record<string, string> = {
  review_closed: "The hosted Review is closed.",
  thread_deleted: "The thread was deleted on hosted.",
  thread_changed: "Someone changed this thread on hosted after you replied.",
  request_changed: "Your request changed on hosted.",
  snapshot_advanced: "Hosted has a newer revision than the one you judged.",
  target_changed: "Hosted's content for this part differs from what you saw.",
  judgment_changed: "Your judgment on hosted changed after you queued this.",
  scope_unresolved: "The request still has unresolved parts on hosted.",
  rejected: "Hosted refused it.",
};

const MARK_TEXT: Record<string, string> = {
  reviewed: "Mark reviewed",
  needs_changes: "Mark needs changes",
  unreviewed: "Clear judgment",
};

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function clip(value: string, limit = 60): string {
  const flat = value.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
}

/** One line naming what a queued action does, from its own payload. */
export function hostedActionSummary(action: HostedAction): string {
  const payload = action.payload;
  if (action.kind === "mark") {
    const state = text(payload.state);
    return `${MARK_TEXT[state] ?? `Mark ${state}`} · ${text(payload.unit_key)}`;
  }
  if (action.kind === "comment") {
    const line = typeof payload.start_line === "number" && payload.start_line > 0 ? `:L${payload.start_line}` : "";
    return `Comment on ${text(payload.path)}${line}: “${clip(text(payload.body))}”`;
  }
  if (action.kind === "reply") return `Reply: “${clip(text(payload.body))}”`;
  if (action.kind === "outcome") return `Verdict ${text(payload.outcome).replace(/_/g, " ")}`;
  return "Complete request";
}

/** The acknowledgement after a queued Reader write. */
export function queuedWriteNotice(result: HostedQueuedWrite): string {
  const what = result.actions.map(hostedActionSummary).join("; ");
  return `Queued for hosted ${result.reference}: ${what}. It applies when you run \`lc review hosted sync\`.`;
}

/** Unresolved actions, conflicts first (they need the reviewer), each in queue order. */
export function unresolvedHostedActions(status: HostedAttachmentStatus | null): HostedAction[] {
  const actions = status?.actions ?? [];
  const order = (action: HostedAction) => (action.state === "conflict" ? 0 : 1);
  return actions
    .filter((action) => action.state === "conflict" || action.state === "pending")
    .sort((left, right) => order(left) - order(right) || left.sequence - right.sequence);
}
