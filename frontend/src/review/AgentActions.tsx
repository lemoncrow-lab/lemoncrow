import { Button as UiButton } from "../components/ui/button";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  decideAgentApproval,
  fetchAgentApprovals,
  type AgentActionApproval,
  type AgentApproval,
} from "./agentApprovalsApi";
import { hostLabel, shortTime } from "./developmentLabels";

/**
 * Consequential actions an authoring agent took outside the repository
 * (Phase 9R item 17): package installs, network/cloud/database commands,
 * secret access, pushes and writes outside the workspace. Rendered inside
 * existing homes only (the Brief's Behavior changes section and Activity).
 *
 * P12-AGENT: a row says whether a human pre-approved exactly that action, and
 * the approval requests themselves are listed (and decided) beside it.
 */
export interface AgentActionRow {
  id?: string;
  category: string;
  summary: string;
  host: string;
  at: string;
  detail?: string;
  approval?: AgentActionApproval;
}

function ApprovalBadge({ approval }: { approval: AgentActionApproval | undefined }) {
  if (!approval) return null;
  if (approval.status === "pre_approved") {
    const scope = approval.scope === "pattern" ? ` · pattern ${approval.pattern}` : "";
    return (
      <span className="shrink-0 text-emerald-300" title={`Approved by ${approval.decided_by}${scope}`}>
        {`Pre-approved by ${approval.decided_by || "a reviewer"}`}
      </span>
    );
  }
  return (
    <span className="shrink-0 text-amber-300" title="No human approved exactly this action">
      Not approved
    </span>
  );
}

export function AgentActionList({ rows, limit = 8 }: { rows: AgentActionRow[]; limit?: number }) {
  if (rows.length === 0) return null;
  return (
    <ul className="space-y-1" data-testid="agent-actions">
      {rows.slice(0, limit).map((row, index) => (
        <li key={row.id || `${row.at}-${index}`} className="flex min-w-0 items-baseline gap-2 text-caption">
          <span className="min-w-0 flex-1 truncate text-neutral-300" title={row.detail || row.summary}>{row.summary}</span>
          <ApprovalBadge approval={row.approval} />
          <span className="shrink-0 text-muted-foreground">
            {hostLabel(row.host)}
            {row.at ? ` · ${shortTime(row.at)}` : ""}
          </span>
        </li>
      ))}
      {rows.length > limit && <li className="text-caption text-muted-foreground">{`${rows.length - limit} more`}</li>}
    </ul>
  );
}

const STATUS_TEXT: Record<string, string> = {
  pending: "Waiting for you",
  approved: "Approved",
  denied: "Denied",
  expired: "Expired",
};

const STATUS_TONE: Record<string, string> = {
  pending: "text-amber-300",
  approved: "text-emerald-300",
  denied: "text-rose-300",
  expired: "text-muted-foreground",
};

function decisionLine(approval: AgentApproval): string {
  if (approval.status === "pending") return approval.expires_at ? `asks until ${shortTime(approval.expires_at)}` : "";
  if (approval.status === "approved") {
    const scope = approval.scope === "pattern" ? `pattern ${approval.pattern}` : approval.used ? "once · used" : "once";
    return `by ${approval.decided_by} · ${scope}${approval.valid_until ? ` · until ${shortTime(approval.valid_until)}` : ""}`;
  }
  if (approval.status === "denied") return `by ${approval.decided_by}${approval.note ? ` · ${approval.note}` : ""}`;
  return "";
}

/**
 * Approval requests from the agent host's hook, newest first. A pending one
 * can be approved (exactly this action, once) or denied when `onDecide` is
 * given; the server decides who may.
 */
export function AgentApprovalList({
  approvals,
  onDecide,
  limit = 8,
}: {
  approvals: AgentApproval[];
  onDecide?: (approval: AgentApproval, decision: "approve" | "deny") => Promise<void>;
  limit?: number;
}) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  if (approvals.length === 0) return null;
  const decide = async (approval: AgentApproval, decision: "approve" | "deny") => {
    if (!onDecide) return;
    setBusy(approval.id);
    setError("");
    try {
      await onDecide(approval, decision);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "the decision was not recorded");
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="flex flex-col gap-1" data-testid="agent-approvals">
      <ul className="space-y-1.5">
        {approvals.slice(0, limit).map((approval) => (
          <li key={approval.id} className="text-caption" data-testid="agent-approval">
            <div className="flex min-w-0 items-baseline gap-2">
              <span className={`shrink-0 font-medium ${STATUS_TONE[approval.status] ?? "text-muted-foreground"}`}>
                {STATUS_TEXT[approval.status] ?? approval.status}
              </span>
              <span className="min-w-0 flex-1 truncate text-neutral-300">{approval.risk_label || approval.risk_class}</span>
              <span className="shrink-0 text-muted-foreground">
                {hostLabel(approval.host)}
                {approval.requested_at ? ` · ${shortTime(approval.requested_at)}` : ""}
              </span>
            </div>
            <pre className="mt-0.5 max-h-24 overflow-auto whitespace-pre-wrap break-all rounded border border-border/70 px-1.5 py-1 font-mono text-micro text-neutral-300">
              {approval.action}
            </pre>
            <div className="mt-0.5 flex items-center gap-2 text-muted-foreground">
              <span className="min-w-0 flex-1 truncate">{decisionLine(approval)}</span>
              {approval.status === "pending" && onDecide && (
                <>
                  <UiButton
                    variant="outline"
                    size="compact"
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => void decide(approval, "approve")}
                  >
                    Approve once
                  </UiButton>
                  <UiButton
                    variant="ghost"
                    size="compact"
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() => void decide(approval, "deny")}
                  >
                    Deny
                  </UiButton>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>
      {approvals.length > limit && (
        <p className="text-caption text-muted-foreground">{`${approvals.length - limit} more`}</p>
      )}
      {error && <p role="alert" className="text-caption text-rose-300">{error}</p>}
    </div>
  );
}

const POLL_MS = 5_000;

/**
 * Live approval requests for the current Snapshot, decided in place. Renders
 * nothing until the agent asks. Read-only (no buttons) when `readOnly`.
 */
export function AgentApprovalPanel({
  reviewId,
  initial = [],
  readOnly = false,
  title = "Agent actions waiting for approval",
}: {
  reviewId: string;
  initial?: AgentApproval[];
  readOnly?: boolean;
  title?: string;
}) {
  const [approvals, setApprovals] = useState<AgentApproval[]>(initial);
  const sequence = useRef(0);
  const load = useCallback(async () => {
    const current = ++sequence.current;
    try {
      const listing = await fetchAgentApprovals(reviewId);
      if (current === sequence.current && Array.isArray(listing?.approvals)) setApprovals(listing.approvals);
    } catch {
      // Keep what is shown; a failed poll never invents or drops a decision.
    }
  }, [reviewId]);

  useEffect(() => {
    void load();
    // lc-debt: polls the approval listing every 5s while mounted (a pending
    // request has no live-state cursor yet); upgrade path: an approvals cursor
    // in /live-state so the Reader refetches only on change.
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "hidden") void load();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const decide = useCallback(async (approval: AgentApproval, decision: "approve" | "deny") => {
    const result = await decideAgentApproval(reviewId, approval, { decision });
    setApprovals((rows) => rows.map((row) => (row.id === result.approval.id ? result.approval : row)));
    void load();
  }, [reviewId, load]);

  if (approvals.length === 0) return null;
  const pending = approvals.filter((item) => item.status === "pending").length;
  return (
    <section className="rounded-md border border-amber-900/50 px-2.5 py-2" aria-label={title} data-testid="agent-approval-panel">
      <div className="mb-1 text-caption font-semibold text-neutral-300">
        {pending > 0 ? `${title} · ${pending}` : "Agent action approvals"}
      </div>
      <AgentApprovalList approvals={approvals} onDecide={readOnly ? undefined : decide} />
    </section>
  );
}
