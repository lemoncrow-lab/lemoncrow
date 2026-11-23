/**
 * The flow's one state line, pure: which of the Review's standing conditions the reviewer reads under the top bar, in
 * words, and which one action changes it. Every condition that makes the Reader read-only or degraded is named here;
 * when several hold, exactly one row shows.
 *
 * The order is the Classic Reader's notice policy (reviewNoticeArbitration, retired with P8-3): a failed operation
 * needs recovery first, then the conditions that change what the revision on screen is (lost access, superseded,
 * running draft, historical, finished), then recoverable degradation, and a routine source delta last. One addition:
 * an agent request waiting for approval comes right after a failure, because it is the only condition with a deadline
 * (the agent is denied when its wait runs out) and it can be decided even while this revision is read-only.
 */
import { revisionName } from "./revisionLabel";

export type StateLineId =
  | "failed"
  | "access-lost"
  | "agent-approvals"
  | "revision-advanced"
  | "running-draft"
  | "historical"
  | "finished"
  | "archived"
  | "context-failures"
  | "source-moved";

export type StateLineTone = "p0" | "p1" | "p2" | "neutral";

export const STATE_LINE_PRIORITY: Readonly<Record<StateLineId, number>> = {
  failed: 0,
  "access-lost": 5,
  "agent-approvals": 8,
  "revision-advanced": 10,
  "running-draft": 20,
  historical: 30,
  finished: 35,
  archived: 35,
  "context-failures": 50,
  "source-moved": 90,
};

export interface StateLineFacts {
  /** The line's own last action failed (Make it a Snapshot, Reopen, Snapshot now). */
  failure?: { label: string; message: string } | null;
  accessLost: "" | "forbidden" | "gone";
  pendingApprovals: number;
  revisionAdvanced: boolean;
  /** The newer revision when it is known; the live feed names only its id. */
  newerRevisionNumber?: number | null;
  runningDraft: boolean;
  historical: boolean;
  revisionNumber: number;
  latestRevisionNumber: number;
  status: string;
  /** One plain sentence per context read that failed. */
  contextFailures: readonly string[];
  /** The working tree moved past this Snapshot (and a Snapshot can be taken here); `files` may be 0 when the count is unknown. */
  sourceMoved: { files: number } | null;
}

export interface StateLineSignal {
  id: StateLineId;
  tone: StateLineTone;
  text: string;
  /** Muted text after the reason (what failed, in words). */
  detail?: string;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** Every condition that holds, in the order above. */
export function stateLineSignals(facts: StateLineFacts): StateLineSignal[] {
  const out: StateLineSignal[] = [];
  if (facts.failure) {
    out.push({ id: "failed", tone: "p0", text: `Couldn't ${facts.failure.label}`, detail: facts.failure.message });
  }
  if (facts.accessLost) {
    out.push({
      id: "access-lost",
      tone: "p0",
      text: facts.accessLost === "gone"
        ? "This review was deleted or discarded elsewhere · read-only"
        : "Your access to this review was removed · read-only",
    });
  }
  if (facts.pendingApprovals > 0) {
    out.push({
      id: "agent-approvals",
      tone: "p1",
      text: `${plural(facts.pendingApprovals, "agent request waits", "agent requests wait")} for you`,
    });
  }
  if (facts.revisionAdvanced) {
    const newer = facts.newerRevisionNumber != null && facts.newerRevisionNumber > facts.revisionNumber
      ? revisionName(facts.newerRevisionNumber)
      : "A newer Snapshot";
    out.push({ id: "revision-advanced", tone: "p2", text: `${newer} replaced this one elsewhere · read-only` });
  }
  if (facts.runningDraft) {
    out.push({ id: "running-draft", tone: "neutral", text: "Running draft · read-only until it is a Snapshot" });
  }
  if (facts.historical && !facts.runningDraft) {
    out.push({
      id: "historical",
      tone: "neutral",
      text: `${revisionName(facts.revisionNumber)} · read-only · latest is ${revisionName(facts.latestRevisionNumber)}`,
    });
  }
  if (facts.status === "finished") out.push({ id: "finished", tone: "neutral", text: "Finished · read-only until reopened" });
  if (facts.status === "archived") out.push({ id: "archived", tone: "neutral", text: "Archived · read-only until restored" });
  if (facts.contextFailures.length > 0) {
    out.push({
      id: "context-failures",
      tone: "p1",
      text: "Some review context couldn't load",
      detail: facts.contextFailures.join(" "),
    });
  }
  if (facts.sourceMoved) {
    out.push({
      id: "source-moved",
      tone: "p2",
      text: `${facts.sourceMoved.files > 0 ? plural(facts.sourceMoved.files, "file", "files") : "The source"} changed since this Snapshot`,
    });
  }
  return out.sort((left, right) => STATE_LINE_PRIORITY[left.id] - STATE_LINE_PRIORITY[right.id]);
}

/** The one row to show, or null when the Review is writable and whole. */
export function stateLine(facts: StateLineFacts): StateLineSignal | null {
  return stateLineSignals(facts)[0] ?? null;
}
