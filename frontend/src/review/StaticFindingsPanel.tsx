import { useEffect, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { PanelCard } from "./ContextSections";
import {
  fetchStaticFindings,
  postStaticDisposition,
  type StaticDismissalReason,
  type StaticDisposition,
  type StaticFinding,
  type StaticFindingChain,
  type StaticFindingsProjection,
} from "./staticFindingsApi";

const SEVERITY_TONE: Record<string, string> = {
  p0: "text-rose-300",
  p1: "text-amber-300",
  p2: "text-sky-300",
  suggestion: "text-muted-foreground",
};

const AUTHORITY_LABEL: Record<string, string> = {
  attested: "typed result",
  human: "human upload",
  reported: "caller-reported",
};

const DISMISS_REASONS: Array<{ value: StaticDismissalReason; label: string }> = [
  { value: "incorrect", label: "Incorrect" },
  { value: "accepted_risk", label: "Accepted risk" },
  { value: "duplicate", label: "Duplicate" },
  { value: "not_applicable", label: "Not applicable" },
];

function lines(finding: StaticFinding): string {
  if (!finding.start_line) return "";
  return finding.end_line > finding.start_line ? `L${finding.start_line}-${finding.end_line}` : `L${finding.start_line}`;
}

function FindingRow({
  finding,
  actionable,
  busy,
  onJudge,
}: {
  finding: StaticFinding;
  actionable: boolean;
  busy: boolean;
  onJudge(finding: StaticFinding, disposition: StaticDisposition, reason?: StaticDismissalReason): void;
}) {
  const [dismissing, setDismissing] = useState(false);
  const judged = finding.disposition?.disposition ?? "";
  return (
    <div className="border-t border-border/60 py-1.5 first:border-t-0" data-testid={`static-finding-${finding.id}`}>
      <div className="flex items-baseline gap-2">
        <span className={`text-caption ${SEVERITY_TONE[finding.severity] ?? "text-muted-foreground"}`}>
          {finding.severity_label}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-micro text-neutral-200" title={finding.rule_id}>
          {finding.tool} · {finding.rule_id || "(no rule id)"}
        </span>
        <span className="font-mono text-micro text-muted-foreground">{lines(finding)}</span>
      </div>
      {/* Producer text is data: rendered as text, never as markup. */}
      {finding.message && (
        <div className="whitespace-pre-wrap break-words text-micro leading-relaxed text-neutral-300">
          {finding.message}
          {finding.message_truncated && <span className="text-muted-foreground"> … (cut)</span>}
        </div>
      )}
      <div className="mt-0.5 flex flex-wrap gap-x-2 text-micro text-muted-foreground">
        <span>{finding.check}</span>
        <span>{AUTHORITY_LABEL[finding.authority] ?? finding.authority}</span>
        {finding.state === "stale" && (
          <span className="text-amber-300/80">
            Earlier Snapshot{finding.snapshot_number != null ? ` ${finding.snapshot_number}` : ""} · not current evidence
          </span>
        )}
        {!finding.in_snapshot && <span>outside this Snapshot</span>}
        {judged && (
          <span className={judged === "dismissed" ? "text-muted-foreground" : "text-emerald-300/80"}>
            {judged === "dismissed" ? `Dismissed (${(finding.disposition?.reason ?? "").replace(/_/g, " ")})` : "Confirmed"}
          </span>
        )}
      </div>
      {actionable && (
        <div className="mt-1 flex flex-wrap gap-1.5">
          {judged ? (
            <UiButton type="button" size="xs" variant="ghost" disabled={busy} onClick={() => onJudge(finding, "open")}>
              Reopen
            </UiButton>
          ) : dismissing ? (
            <>
              {DISMISS_REASONS.map((reason) => (
                <UiButton
                  key={reason.value}
                  type="button"
                  size="xs"
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    setDismissing(false);
                    onJudge(finding, "dismissed", reason.value);
                  }}
                >
                  {reason.label}
                </UiButton>
              ))}
              <UiButton type="button" size="xs" variant="ghost" onClick={() => setDismissing(false)}>
                Cancel
              </UiButton>
            </>
          ) : (
            <>
              <UiButton type="button" size="xs" variant="outline" disabled={busy} onClick={() => onJudge(finding, "confirmed")}>
                Confirm
              </UiButton>
              <UiButton type="button" size="xs" variant="ghost" disabled={busy} onClick={() => setDismissing(true)}>
                Dismiss…
              </UiButton>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ChainRow({ chain }: { chain: StaticFindingChain }) {
  return (
    <div className="border-t border-border/60 py-1.5 first:border-t-0" data-testid={`static-chain-${chain.id}`}>
      <div className="flex items-baseline gap-2">
        <span className={`text-caption ${SEVERITY_TONE[chain.severity] ?? "text-muted-foreground"}`}>{chain.severity_label}</span>
        <span className="min-w-0 flex-1 truncate text-label text-neutral-200">{chain.title}</span>
        <span className="text-micro text-muted-foreground">
          {chain.members.length} findings · {chain.edges.length} links
        </span>
      </div>
      <div className="text-micro text-muted-foreground">
        {chain.members.map((member) => `${member.tool} ${member.rule_id} L${member.start_line}`).join(" → ")}
      </div>
      <details>
        <summary className="cursor-pointer text-micro text-muted-foreground">Why this chain</summary>
        <ul className="mt-1 space-y-0.5">
          {chain.explanation.map((line) => (
            <li key={line} className="text-micro leading-relaxed text-muted-foreground">
              {line}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

/**
 * Imported deterministic findings for one file of the Snapshot, in the Checks rail.
 *
 * Self-contained like the run plan panel. Renders nothing when no imported
 * check reports anything for the file and no import was truncated. Findings the
 * finding threshold hides (`hiddenFindingIds`, by ref, from the Review triage)
 * fold behind one "N below threshold" disclosure; nothing is removed.
 */
export default function StaticFindingsPanel({
  reviewId,
  revisionId = "",
  path,
  readOnly = false,
  hiddenFindingIds,
}: {
  reviewId: string;
  revisionId?: string;
  path: string;
  readOnly?: boolean;
  hiddenFindingIds?: readonly string[];
}) {
  const [data, setData] = useState<StaticFindingsProjection | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError("");
    setActionError("");
    fetchStaticFindings(reviewId, revisionId, path)
      .then((projection) => {
        if (!cancelled) setData(projection);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "static findings unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [reviewId, revisionId, path]);

  if (error) {
    return <div className="text-micro text-muted-foreground">Static findings unavailable: {error}</div>;
  }
  if (!data) return null;
  const { findings, stale, chains, truncation_notes: notes } = data;
  if (findings.length === 0 && stale.length === 0 && chains.length === 0 && notes.length === 0) return null;
  const actionable = !readOnly && data.latest;
  const hidden = new Set(hiddenFindingIds ?? []);
  const surfaced = findings.filter((finding) => !hidden.has(finding.ref));
  const below = findings.filter((finding) => hidden.has(finding.ref));
  const row = (finding: StaticFinding) => (
    <FindingRow
      key={finding.ref}
      finding={finding}
      actionable={actionable}
      busy={busy}
      onJudge={(item, disposition, reason) => void judge(item, disposition, reason)}
    />
  );

  async function judge(finding: StaticFinding, disposition: StaticDisposition, reason?: StaticDismissalReason) {
    setBusy(true);
    setActionError("");
    try {
      await postStaticDisposition(reviewId, data?.snapshot_id ?? revisionId, finding.id, disposition, reason ?? "");
      setData(await fetchStaticFindings(reviewId, revisionId, path));
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "could not record");
    } finally {
      setBusy(false);
    }
  }

  return (
    <PanelCard
      title="Static findings"
      action={<span className="font-mono text-caption text-muted-foreground">{findings.length}</span>}
    >
      <div className="space-y-2" data-testid="static-findings-panel">
        {notes.map((note) => (
          <div key={note} className="text-micro leading-relaxed text-amber-300/80" data-testid="static-findings-truncation">
            {note}
          </div>
        ))}
        {chains.length > 0 && (
          <div>
            <div className="text-micro uppercase tracking-wider text-muted-foreground">
              Chains · {chains.length} · display only, never a gate
            </div>
            {chains.map((chain) => (
              <ChainRow key={chain.id} chain={chain} />
            ))}
          </div>
        )}
        {findings.length > 0 && (
          <div>
            {surfaced.map(row)}
            {below.length > 0 && (
              <details data-testid="static-findings-below-threshold" className="py-1 text-caption text-muted-foreground">
                <summary className="cursor-pointer select-none">{below.length} below threshold</summary>
                {below.map(row)}
              </details>
            )}
          </div>
        )}
        {actionError && <div className="text-micro text-rose-300">{actionError}</div>}
        {stale.length > 0 && (
          <details data-testid="static-findings-stale">
            <summary className="cursor-pointer text-micro uppercase tracking-wider text-muted-foreground">
              Earlier Snapshots · {stale.length}
            </summary>
            {stale.map((finding) => (
              <FindingRow key={finding.ref} finding={finding} actionable={false} busy={busy} onJudge={() => undefined} />
            ))}
          </details>
        )}
      </div>
    </PanelCard>
  );
}
