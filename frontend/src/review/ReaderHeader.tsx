import type { Ref } from "react";

import type { DiffStyle } from "./diffModel";
import type { ReaderOrder } from "./readerModel";
import type { ReviewOverview, ReviewProgress } from "./types";
interface ReaderHeaderProps {
  overview: ReviewOverview;
  progress: ReviewProgress;
  diffStyle: DiffStyle;
  order: ReaderOrder;
  focusMode: boolean;
  contextOpen: boolean;
  busy: boolean;
  /** Every mutation the reader owns refuses: archived session, or a stranded reader. */
  readOnly: boolean;
  /** The reader is showing a revision the server has already superseded. */
  revisionAdvanced: boolean;
  finishButtonRef?: Ref<HTMLButtonElement>;
  onFinish(): void;
  onRefresh(): void;
  onPrepareFeedback(): void;
  onOrderChange(order: ReaderOrder): void;
  onToggleDiffStyle(): void;
  onToggleContext(): void;
  onToggleFocus(): void;
  onShowShortcuts(): void;
  onShowOverview(): void;
}

export default function ReaderHeader({
  overview,
  progress,
  diffStyle,
  order,
  focusMode,
  contextOpen,
  busy,
  readOnly,
  revisionAdvanced,
  finishButtonRef,
  onFinish,
  onRefresh,
  onPrepareFeedback,
  onOrderChange,
  onToggleDiffStyle,
  onToggleContext,
  onToggleFocus,
  onShowShortcuts,
  onShowOverview,
}: ReaderHeaderProps) {
  const story = overview.change_story?.length
    ? `${overview.change_story.join(" · ")}${(overview.change_story_more ?? 0) > 0 ? ` · +${overview.change_story_more} more` : ""}`
    : overview.title || "Review";

  return (
    <header className="shrink-0 border-b border-neutral-800 bg-neutral-950 px-3 py-2">
      <div className="flex min-w-0 items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="shrink-0 text-[13px] font-semibold text-neutral-100">Review · rev {overview.revision.revision_number}</span>
            {!focusMode && (
              <button
                type="button"
                onClick={onShowOverview}
                aria-label="Open change overview"
                className="min-w-0 truncate text-left text-[11px] text-neutral-500 hover:text-neutral-300"
                title={`${story} · Open change overview`}
              >
                {story} <span className="text-neutral-700">⌄</span>
              </button>
            )}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-[10px] text-neutral-500" aria-live="polite">
            <span><span className="font-mono text-neutral-300">{progress.target_count}</span> targets</span>
            <span><span className="font-mono text-emerald-400">{progress.reviewed}</span> reviewed</span>
            {progress.changed_since_review > 0 && (
              <span><span className="font-mono text-sky-300">{progress.changed_since_review}</span> changed</span>
            )}
            {progress.needs_changes > 0 && (
              <span><span className="font-mono text-rose-300">{progress.needs_changes}</span> needs changes</span>
            )}
            {progress.unknown > 0 && (
              <span><span className="font-mono text-amber-300">{progress.unknown}</span> unknown</span>
            )}
          </div>
        </div>

        <button
          ref={finishButtonRef}
          type="button"
          // Finishing is refused while the reader is stranded on a superseded
          // revision, but not on an archived session — there this button is
          // "Restore review", the one control that has to keep working.
          disabled={busy || revisionAdvanced}
          onClick={onFinish}
          className="shrink-0 border border-neutral-600 bg-neutral-900 px-3 py-1.5 text-[11px] font-medium text-neutral-100 hover:border-neutral-400 disabled:opacity-40"
        >
          {overview.session.status === "finished" ? "Reopen review" : overview.session.status === "archived" ? "Restore review" : "Finish review"}
        </button>
        <details className="relative shrink-0">
          <summary
            aria-label="Review actions"
            className="cursor-pointer list-none border border-neutral-800 bg-neutral-900/40 px-2.5 py-1.5 text-[12px] text-neutral-400 hover:border-neutral-600 hover:text-neutral-200"
          >
            ⋯
          </summary>
          <div className="absolute right-0 top-9 z-50 w-52 border border-neutral-800 bg-neutral-950 p-1 text-[11px] shadow-2xl">
            {/* refreshReview refuses outright when the reader is read-only, so the control cannot stay live. */}
            <button type="button" disabled={busy || readOnly} onClick={onRefresh} className="block w-full px-2.5 py-2 text-left text-neutral-300 hover:bg-neutral-900 disabled:opacity-40">Refresh revision</button>
            <button type="button" disabled={busy} onClick={onPrepareFeedback} className="block w-full px-2.5 py-2 text-left text-neutral-300 hover:bg-neutral-900 disabled:opacity-40">Prepare feedback</button>
            <label className="block border-y border-neutral-900 px-2.5 py-2 text-neutral-500">
              <span className="mb-1 block text-[9px] uppercase tracking-wide">Order</span>
              <select
                aria-label="Review order"
                disabled={busy}
                value={order}
                onChange={(event) => onOrderChange(event.target.value as ReaderOrder)}
                className="w-full border border-neutral-800 bg-neutral-950 px-1.5 py-1 text-[11px] text-neutral-300 disabled:opacity-40"
              >
                <option value="recommended">Recommended</option>
                <option value="file">File order</option>
              </select>
            </label>
            <button type="button" onClick={onToggleDiffStyle} className="block w-full px-2.5 py-2 text-left text-neutral-300 hover:bg-neutral-900">Diff: {diffStyle}</button>
            <button type="button" onClick={onToggleContext} className="block w-full px-2.5 py-2 text-left text-neutral-300 hover:bg-neutral-900">{contextOpen ? "Close" : "Open"} context</button>
            <button type="button" onClick={onToggleFocus} className="block w-full px-2.5 py-2 text-left text-neutral-300 hover:bg-neutral-900">{focusMode ? "Exit" : "Enter"} focus mode</button>
            <button type="button" onClick={onShowShortcuts} className="block w-full border-t border-neutral-900 px-2.5 py-2 text-left text-neutral-300 hover:bg-neutral-900">Keyboard shortcuts <span className="float-right font-mono text-neutral-600">?</span></button>
          </div>
        </details>
      </div>
    </header>
  );
}
