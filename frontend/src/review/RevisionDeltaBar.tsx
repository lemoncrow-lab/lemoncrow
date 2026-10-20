import { Button as UiButton } from "../components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import type { DiscardedVerdict, RefreshInfo, RevisionTargetRef } from "./types";

interface RevisionDeltaBarProps {
  info: RefreshInfo | null;
  discarded: DiscardedVerdict[];
  focused: boolean;
  remainingActive?: number;
  onShowAll(): void;
  onSelectTarget(targetId: string): void;
  onDismiss(): void;
}

function Queue({
  title,
  rows,
  tone = "text-subtle",
  onSelectTarget,
  interactive = true,
}: {
  title: string;
  rows: RevisionTargetRef[];
  tone?: string;
  onSelectTarget(targetId: string): void;
  interactive?: boolean;
}) {
  if (rows.length === 0) return null;
  return (
    <div>
      <div className={`pb-1 text-caption uppercase tracking-tag ${tone}`}>{title} · {rows.length}</div>
      <div className="space-y-0.5">
        {rows.slice(0, 8).map((row) => interactive ? (
          <UiButton unstyled
            key={row.target_id}
            type="button"
            onClick={() => onSelectTarget(row.target_id)}
            className="block w-full truncate py-0.5 text-left font-mono text-meta text-muted-foreground hover:text-foreground"
            title={row.label}
          >
            {row.label}
          </UiButton>
        ) : (
          <div key={row.target_id} className="truncate py-0.5 font-mono text-caption text-muted-foreground" title={row.label}>
            {row.label}
          </div>
        ))}
        {rows.length > 8 && <div className="text-caption text-muted-foreground">+{rows.length - 8} more</div>}
      </div>
    </div>
  );
}

export default function RevisionDeltaBar({
  info,
  discarded,
  focused,
  remainingActive,
  onShowAll,
  onSelectTarget,
  onDismiss,
}: RevisionDeltaBarProps) {
  const delta = info?.target_delta;
  if (!delta) {
    if (discarded.length === 0) return null;
    return (
      <div className="shrink-0 border-b border-red-900/40 bg-red-950/10 px-3 py-1.5 text-caption" data-testid="discarded-verdicts">
        <details>
          <summary className="cursor-pointer text-red-300">Past judgments no longer apply · {discarded.length}</summary>
          <div className="mt-1 space-y-1">
            {discarded.map((item) => (
              <div key={item.unit_key}>
                <span className="font-mono text-red-200">{item.label}</span>
                <span className="pl-2 text-muted-foreground">{item.state} · {item.reason}</span>
              </div>
            ))}
          </div>
        </details>
      </div>
    );
  }
  const active = remainingActive ?? delta.active.length;
  const preserved = delta.preserved.length;
  const reopened = delta.reopened.length;
  const added = delta.added.length;
  const reopenedIds = new Set(delta.reopened.map((target) => target.target_id));
  const addedIds = new Set(delta.added.map((target) => target.target_id));
  const carriedPending = delta.active.filter(
    (target) => !reopenedIds.has(target.target_id) && !addedIds.has(target.target_id),
  ).length;
  const appliedProposals = info?.applied_proposals ?? [];
  const targetsById = new Map(
    [...delta.reopened, ...delta.added, ...delta.preserved, ...delta.active].map((target) => [target.target_id, target]),
  );

  return (
    <div className="shrink-0 border-b border-cyan-900/50 bg-cyan-950/15 text-caption text-cyan-100" data-testid="revision-delta" role="status" aria-live="polite">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="font-medium">Snapshot {info.revision_number}</span>
        {reopened + added > 0 ? (
          <span className="text-cyan-300/80">{reopened + added} returned</span>
        ) : (
          <span className="text-cyan-300/80">No work returned</span>
        )}
        {reopened > 0 && <span className="text-amber-300/80">{reopened} changed</span>}
        {added > 0 && <span className="text-muted-foreground">{added} new</span>}
        {carriedPending > 0 && <span className="text-muted-foreground">{carriedPending} still pending</span>}
        {(info.rebased ?? 0) > 0 && (
          <span
            className="text-muted-foreground"
            title="Only the base moved under these review areas (rebase or merge-base); their authored change is equivalent, so your judgments were kept."
            data-testid="revision-delta-base-only"
          >
            {info.rebased} base-only, kept
          </span>
        )}
        {appliedProposals.length > 0 && <span className="text-muted-foreground">{appliedProposals.length} proposal{appliedProposals.length === 1 ? "" : "s"} captured</span>}
        <span className="flex-1" />
        {focused && active > 0 && (
          <UiButton unstyled
            type="button"
            onClick={onShowAll}
            className="border border-cyan-800 px-2 py-0.5 text-cyan-200 hover:bg-cyan-950/40 hover:text-cyan-100"
          >
            Show all
          </UiButton>
        )}
        <Popover>
          <PopoverTrigger asChild>
            <UiButton unstyled type="button" className="text-cyan-400/80 hover:text-cyan-200">Details</UiButton>
          </PopoverTrigger>
          <PopoverContent align="end" aria-label="Snapshot delta details" className="w-[420px] max-w-[85vw] border-border p-3">
            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
              <Queue title="Changed since review" rows={delta.reopened} tone="text-amber-300" onSelectTarget={onSelectTarget} />
              <Queue title="New" rows={delta.added} tone="text-muted-foreground" onSelectTarget={onSelectTarget} />
              <Queue title="Removed" rows={delta.removed} tone="text-muted-foreground" onSelectTarget={onSelectTarget} interactive={false} />
              {carriedPending > 0 && (
                <div>
                  <div className="pb-1 text-caption uppercase tracking-tag text-muted-foreground">Still pending · {carriedPending}</div>
                  <div className="text-caption leading-relaxed text-muted-foreground">These already needed review before this Snapshot and still do. No prior reviewed judgment was invalidated here.</div>
                </div>
              )}
              {preserved > 0 && (
                <div>
                  <div className="pb-1 text-caption uppercase tracking-tag text-emerald-400/70">Still reviewed · {preserved}</div>
                  <div className="text-caption leading-relaxed text-muted-foreground">These targets did not change, so your previous reviewed judgment still applies.</div>
                </div>
              )}
            </div>
            {appliedProposals.length > 0 && (
              <div className="mt-3 border-t border-border pt-2">
                <div className="pb-1 text-caption uppercase tracking-tag text-muted-foreground">Reviewer source proposals</div>
                {appliedProposals.map((proposal) => (
                  <div key={proposal.id} className="py-1 text-caption">
                    <div className="font-mono text-subtle">{proposal.path}:{proposal.start_line}{proposal.end_line !== proposal.start_line ? `-${proposal.end_line}` : ""}</div>
                    {proposal.result_target_ids.length > 0 ? (
                      <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5">
                        {proposal.result_target_ids.map((targetId) => (
                          <UiButton unstyled
                            key={targetId}
                            type="button"
                            className="text-muted-foreground hover:text-foreground"
                            aria-label={`Proposal result: ${targetsById.get(targetId)?.label ?? targetId}`}
                            onClick={() => onSelectTarget(targetId)}
                          >
                            {targetsById.get(targetId)?.label ?? targetId}
                          </UiButton>
                        ))}
                      </div>
                    ) : (
                      <div className="text-muted-foreground">Captured in this Snapshot; no current target overlaps the original range.</div>
                    )}
                  </div>
                ))}
              </div>
            )}
            {info.notes.length > 0 && (
              <div className="mt-3 border-t border-border pt-2">
                <div className="pb-1 text-caption uppercase tracking-tag text-amber-300">Why judgments changed</div>
                {info.notes.map((note) => <div key={note} className="text-caption leading-relaxed text-muted-foreground">{note}</div>)}
              </div>
            )}
            {discarded.length > 0 && (
              <div className="mt-3 border-t border-red-900/50 pt-2">
                <div className="pb-1 text-caption uppercase tracking-tag text-red-300">Past judgments no longer apply · {discarded.length}</div>
                {discarded.map((item) => (
                  <div key={item.unit_key} className="py-1 text-caption leading-relaxed">
                    <div className="font-mono text-red-200">{item.label}</div>
                    <div className="text-muted-foreground">{item.state} · {item.reason}</div>
                  </div>
                ))}
              </div>
            )}
          </PopoverContent>
        </Popover>
        <UiButton unstyled type="button" onClick={onDismiss} className="text-cyan-500/70 hover:text-cyan-200" aria-label="Dismiss Snapshot delta">×</UiButton>
      </div>
    </div>
  );
}
