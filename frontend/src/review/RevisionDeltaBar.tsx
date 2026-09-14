import type { DiscardedVerdict, RefreshInfo, RevisionTargetRef } from "./types";

interface RevisionDeltaBarProps {
  info: RefreshInfo | null;
  discarded: DiscardedVerdict[];
  focused: boolean;
  remainingActive?: number;
  onFocusDelta(): void;
  onShowAll(): void;
  onSelectTarget(targetId: string): void;
  onDismiss(): void;
}

function Queue({
  title,
  rows,
  tone = "text-neutral-300",
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
      <div className={`pb-1 text-[9px] uppercase tracking-[0.14em] ${tone}`}>{title} · {rows.length}</div>
      <div className="space-y-0.5">
        {rows.slice(0, 8).map((row) => interactive ? (
          <button
            key={row.target_id}
            type="button"
            onClick={() => onSelectTarget(row.target_id)}
            className="block w-full truncate py-0.5 text-left font-mono text-[10px] text-neutral-400 hover:text-neutral-100"
            title={row.label}
          >
            {row.label}
          </button>
        ) : (
          <div key={row.target_id} className="truncate py-0.5 font-mono text-[10px] text-neutral-500" title={row.label}>
            {row.label}
          </div>
        ))}
        {rows.length > 8 && <div className="text-[9px] text-neutral-600">+{rows.length - 8} more</div>}
      </div>
    </div>
  );
}

export default function RevisionDeltaBar({
  info,
  discarded,
  focused,
  remainingActive,
  onFocusDelta,
  onShowAll,
  onSelectTarget,
  onDismiss,
}: RevisionDeltaBarProps) {
  const delta = info?.target_delta;
  if (!delta) {
    if (discarded.length === 0) return null;
    return (
      <div className="shrink-0 border-b border-rose-900/40 bg-rose-950/10 px-3 py-1.5 text-[10px]" data-testid="discarded-verdicts">
        <details>
          <summary className="cursor-pointer text-rose-300">Verdicts discarded · {discarded.length}</summary>
          <div className="mt-1 space-y-1">
            {discarded.map((item) => (
              <div key={item.unit_key}>
                <span className="font-mono text-rose-200">{item.label}</span>
                <span className="pl-2 text-neutral-500">{item.state} · {item.reason}</span>
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

  return (
    <div className="shrink-0 border-b border-sky-900/50 bg-sky-950/15 text-[10px] text-sky-100" data-testid="revision-delta" role="status" aria-live="polite">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="font-medium">Revision {info.revision_number}</span>
        <span className="text-sky-300/80">{active} need your eyes</span>
        {reopened > 0 && <span className="text-amber-300/80">{reopened} reopened</span>}
        {added > 0 && <span className="text-violet-300/80">{added} new</span>}
        {carriedPending > 0 && <span className="text-neutral-400">{carriedPending} carried pending</span>}
        {preserved > 0 && <span className="text-emerald-300/70">{preserved} preserved</span>}
        <span className="flex-1" />
        {active > 0 && (
          <button
            type="button"
            onClick={focused ? onShowAll : onFocusDelta}
            className="border border-sky-800 px-2 py-0.5 text-sky-200 hover:border-sky-600"
          >
            {focused ? "Show all" : `Review ${active}`}
          </button>
        )}
        <details className="relative">
          <summary className="cursor-pointer list-none text-sky-400/80 hover:text-sky-200">Details</summary>
          <div className="absolute right-0 top-5 z-[70] w-[420px] max-w-[85vw] border border-neutral-700 bg-neutral-950 p-3 shadow-2xl">
            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
              <Queue title="Reopened" rows={delta.reopened} tone="text-amber-300" onSelectTarget={onSelectTarget} />
              <Queue title="New" rows={delta.added} tone="text-violet-300" onSelectTarget={onSelectTarget} />
              <Queue title="Removed" rows={delta.removed} tone="text-neutral-500" onSelectTarget={onSelectTarget} interactive={false} />
              {carriedPending > 0 && (
                <div>
                  <div className="pb-1 text-[9px] uppercase tracking-[0.14em] text-neutral-400">Carried pending · {carriedPending}</div>
                  <div className="text-[10px] leading-relaxed text-neutral-500">Targets that were already unresolved in the previous revision and remain unresolved. No reviewed judgment was invalidated for this category.</div>
                </div>
              )}
              {preserved > 0 && (
                <div>
                  <div className="pb-1 text-[9px] uppercase tracking-[0.14em] text-emerald-400/70">Preserved · {preserved}</div>
                  <div className="text-[10px] leading-relaxed text-neutral-500">Reviewed targets whose content identity still matches. They stay out of the delta queue.</div>
                </div>
              )}
            </div>
            {info.notes.length > 0 && (
              <div className="mt-3 border-t border-neutral-800 pt-2">
                <div className="pb-1 text-[9px] uppercase tracking-[0.14em] text-amber-300">Reset explanations</div>
                {info.notes.map((note) => <div key={note} className="text-[10px] leading-relaxed text-neutral-400">{note}</div>)}
              </div>
            )}
            {discarded.length > 0 && (
              <div className="mt-3 border-t border-rose-900/50 pt-2">
                <div className="pb-1 text-[9px] uppercase tracking-[0.14em] text-rose-300">Verdicts discarded · {discarded.length}</div>
                {discarded.map((item) => (
                  <div key={item.unit_key} className="py-1 text-[10px] leading-relaxed">
                    <div className="font-mono text-rose-200">{item.label}</div>
                    <div className="text-neutral-500">{item.state} · {item.reason}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </details>
        <button type="button" onClick={onDismiss} className="text-sky-500/70 hover:text-sky-200" aria-label="Dismiss revision delta">×</button>
      </div>
    </div>
  );
}
