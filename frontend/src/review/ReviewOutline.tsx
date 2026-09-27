import { useMemo, useState, type RefObject } from "react";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Search,
  X,
} from "lucide-react";

import FileTypeIcon from "./FileTypeIcon";
import { outlineSections } from "./readerModel";
import type { OutlineSectionKey, ReviewGuideTarget, ReviewStoryStep } from "./readerModel";
import type { ReviewOutlineItem } from "./types";

interface ReviewOutlineProps {
  rows: ReviewOutlineItem[];
  activePath: string;
  query: string;
  searchInputRef: RefObject<HTMLInputElement>;
  matchTargetCount: number;
  matchFileCount: number;
  matchCountsByPath: ReadonlyMap<string, number>;
  storySteps?: ReviewStoryStep[];
  storyMore?: number;
  nextAttention?: ReviewGuideTarget | null;
  attentionCount?: number;
  attentionPosition?: number;
  onQuery(value: string): void;
  onNavigateMatch(delta: number): void;
  onDismissSearch(): void;
  onSelectPath(path: string): void;
  onSelectTarget?(targetId: string): void;
  onNavigateAttention?(delta: number): void;
  onShowOverview?(): void;
  collapsed: boolean;
  onToggleCollapsed(): void;
}

export default function ReviewOutline({
  rows,
  activePath,
  query,
  searchInputRef,
  matchTargetCount,
  matchFileCount,
  matchCountsByPath,
  storySteps = [],
  storyMore = 0,
  nextAttention = null,
  attentionCount = 0,
  attentionPosition = 0,
  onQuery,
  onNavigateMatch,
  onDismissSearch,
  onSelectPath,
  onSelectTarget,
  onNavigateAttention,
  onShowOverview,
  collapsed,
  onToggleCollapsed,
}: ReviewOutlineProps) {
  const [closedSections, setClosedSections] = useState<ReadonlySet<OutlineSectionKey>>(new Set(["done"]));
  const sections = useMemo(() => outlineSections(rows), [rows]);
  const searching = query.trim().length > 0;
  const showStory = storySteps.length > 1;
  const showNext = Boolean(nextAttention && rows.length > 1 && onSelectTarget);
  const showGuide = !searching && (showStory || showNext);

  if (collapsed) {
    return (
      <aside className="flex w-9 shrink-0 flex-col items-center border-r border-neutral-800/80 bg-neutral-950/95 py-1.5" aria-label="Review outline">
        <button
          type="button"
          aria-label="Open review outline"
          onClick={onToggleCollapsed}
          className="review-icon-button h-7 w-7"
        >
          <ChevronRight size={14} />
        </button>
        <span className="mt-4 [writing-mode:vertical-rl] text-[10px] font-semibold uppercase tracking-[0.14em] text-neutral-700">
          {showStory ? "Change" : "Files"}
        </span>
      </aside>
    );
  }

  return (
    <aside className="review-outline" aria-label="Review outline">
      <div className="flex h-10 items-center gap-1.5 border-b border-neutral-800/70 px-2">
        <span className="text-[13px] font-semibold text-neutral-200">{showStory ? "Change" : "Files"}</span>
        <span className="flex-1" />
        <span className="font-mono text-[10px] text-neutral-600">{rows.length}</span>
        <button type="button" aria-label="Collapse review outline" onClick={onToggleCollapsed} className="review-icon-button h-7 w-7">
          <ChevronLeft size={13} />
        </button>
      </div>

      {showGuide && (
        <div className="border-b border-neutral-800/70 px-1.5 py-1.5" data-testid="review-guide">
          {showStory && (
            <div>
              <div className="mb-1 text-[11px] font-semibold text-neutral-400">Change story</div>
              <div className="space-y-0.5">
                {storySteps.map((step) => (
                  <button
                    key={`${step.label}:${step.path}`}
                    type="button"
                    onClick={() => onSelectPath(step.path)}
                    className="flex w-full min-w-0 items-center gap-1.5 rounded px-1.5 py-0.5 text-left text-[10px] text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100"
                    title={`${step.explanation || step.label} · ${step.path}`}
                  >
                    <span className="min-w-0 flex-1 truncate">{step.label}</span>
                    {(step.targetCount ?? 0) > 0 && (
                      <span className="shrink-0 font-mono text-[9px] text-neutral-600">
                        {step.reviewed ?? 0}/{step.targetCount} reviewed
                      </span>
                    )}
                    {(step.changedSinceReview ?? 0) > 0 && (
                      <span className="shrink-0 font-mono text-[9px] text-amber-400/80">
                        {step.changedSinceReview} changed
                      </span>
                    )}
                  </button>
                ))}
                {storyMore > 0 && (
                  onShowOverview ? (
                    <button
                      type="button"
                      onClick={onShowOverview}
                      className="rounded px-1.5 py-0.5 font-mono text-[9px] text-neutral-600 hover:bg-neutral-900 hover:text-neutral-300"
                      aria-label={`Show ${storyMore} more change ${storyMore === 1 ? "area" : "areas"}`}
                    >
                      +{storyMore} more
                    </button>
                  ) : (
                    <span className="font-mono text-[9px] text-neutral-700">+{storyMore} more</span>
                  )
                )}
              </div>
            </div>
          )}
          {showNext && nextAttention && (
            <div
              className={`w-full rounded-md border border-neutral-800/80 bg-neutral-900/25 ${showStory ? "mt-2" : ""}`}
              title={`${nextAttention.path} · ${nextAttention.label} · ${nextAttention.reason}`}
            >
              <div className="flex min-w-0 items-center gap-1 px-1.5 pt-1">
                <span className="text-[11px] font-semibold text-amber-300">
                  {attentionPosition <= 1 ? "Review first" : "Attention"}
                </span>
                {attentionCount > 0 && (
                  <span className="font-mono text-[10px] text-neutral-400">
                    {Math.max(1, attentionPosition)}/{attentionCount}
                  </span>
                )}
                <span className="min-w-0 flex-1 truncate text-right font-mono text-[10px] text-neutral-400">
                  {nextAttention.path.split("/").pop() ?? nextAttention.path}
                </span>
                {attentionCount > 1 && onNavigateAttention && (
                  <>
                    <button
                      type="button"
                      aria-label="Previous attention review area"
                      onClick={() => onNavigateAttention(-1)}
                      className="review-icon-button h-5 w-5 border-0"
                    >
                      <ChevronUp size={10} />
                    </button>
                    <button
                      type="button"
                      aria-label="Next attention review area"
                      onClick={() => onNavigateAttention(1)}
                      className="review-icon-button h-5 w-5 border-0"
                    >
                      <ChevronDown size={10} />
                    </button>
                  </>
                )}
              </div>
              <button
                type="button"
                onClick={() => onSelectTarget?.(nextAttention.targetId)}
                className="block w-full px-1.5 pb-1 pt-0.5 text-left hover:bg-neutral-900/55"
                aria-label={`${attentionPosition <= 1 ? "Review first" : "Review attention"}: ${nextAttention.label}`}
              >
                <span className="block text-[11px] leading-4 text-neutral-400">{nextAttention.reason}</span>
              </button>
            </div>
          )}
        </div>
      )}

      <div className="border-b border-neutral-800/70 p-1.5">
        <div className="relative flex items-center">
          <Search size={13} className="pointer-events-none absolute left-2.5 text-neutral-600" />
          <input
            ref={searchInputRef}
            aria-label="Search review"
            aria-keyshortcuts="/"
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                onNavigateMatch(event.shiftKey ? -1 : 1);
              } else if (event.key === "Escape") {
                event.preventDefault();
                onDismissSearch();
              }
            }}
            placeholder="Find file or symbol…"
            className="review-field review-outline-search-field h-7 w-full"
          />
          {searching && (
            <button
              type="button"
              aria-label="Clear review search"
              onClick={onDismissSearch}
              className="review-icon-button absolute right-0.5 h-7 w-7 border-0"
            >
              <X size={12} />
            </button>
          )}
        </div>
        {searching && (
          <div className="mt-1 flex items-center gap-1 text-[10px] text-neutral-600" aria-live="polite">
            <span className="min-w-0 flex-1 truncate">
              <span aria-hidden="true">{matchTargetCount} review area{matchTargetCount === 1 ? "" : "s"} in {matchFileCount} file{matchFileCount === 1 ? "" : "s"}</span>
              <span className="sr-only">{matchTargetCount} review area{matchTargetCount === 1 ? "" : "s"} · {matchFileCount} file{matchFileCount === 1 ? "" : "s"}</span>
            </span>
            <button
              type="button"
              aria-label="Previous search match"
              disabled={matchTargetCount === 0}
              onClick={() => onNavigateMatch(-1)}
              className="review-icon-button h-6 w-6 border-0"
            >
              <ChevronUp size={12} />
            </button>
            <button
              type="button"
              aria-label="Next search match"
              disabled={matchTargetCount === 0}
              onClick={() => onNavigateMatch(1)}
              className="review-icon-button h-6 w-6 border-0"
            >
              <ChevronDown size={12} />
            </button>
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto py-1">
        {sections.map((section) => {
          const closed = !searching && closedSections.has(section.key);
          return (
            <section key={section.key} className="pb-1">
              <button
                type="button"
                aria-expanded={!closed}
                aria-controls={`review-outline-section-${section.key}`}
                disabled={searching}
                onClick={() => {
                  if (searching) return;
                  setClosedSections((current) => {
                    const next = new Set(current);
                    if (next.has(section.key)) next.delete(section.key);
                    else next.add(section.key);
                    return next;
                  });
                }}
                className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-[10px] font-medium text-neutral-400 transition-colors hover:bg-neutral-900/55 hover:text-neutral-50"
              >
                {closed ? <ChevronRight size={11} /> : <ChevronDown size={11} />}
                <span className="flex-1">{section.label}</span>
                <span className="font-mono font-normal text-neutral-400">{section.rows.length}</span>
              </button>
              {!closed && (
                <div id={`review-outline-section-${section.key}`} className="space-y-0.5 px-1.5">
                  {section.rows.map((row) => {
                    const filename = row.path.split("/").pop() ?? row.path;
                    const complete = row.reviewed === row.target_count && row.target_count > 0;
                    const settled = row.reviewed;
                    const matchCount = matchCountsByPath.get(row.path) ?? 0;
                    const reason = row.reasons.find((item) => !/^\+\d+\s+-\d+$/.test(item.trim())) ?? "";
                    const active = row.path === activePath;
                    const showReason = Boolean(reason && (active || section.key === "attention" || section.key === "changed"));
                    return (
                      <button
                        type="button"
                        key={row.path}
                        aria-current={active ? "location" : undefined}
                        title={row.path}
                        onClick={() => onSelectPath(row.path)}
                        className={[
                          "review-outline-file block w-full rounded-md border border-transparent px-2 py-1.5 text-left transition-colors",
                          active
                            ? "border-neutral-800 bg-neutral-900/85 shadow-sm"
                            : "hover:border-neutral-900 hover:bg-neutral-900/45",
                          complete ? "opacity-60" : "",
                        ].join(" ")}
                      >
                        <div className="flex items-center gap-1.5 text-[11px]">
                          <FileTypeIcon path={row.path} size={13} />
                          <span className={active ? "min-w-0 flex-1 truncate font-medium text-neutral-100" : "min-w-0 flex-1 truncate text-neutral-300"} title={row.path}>
                            {filename}
                          </span>
                          <span className={complete ? "shrink-0 font-mono text-[11px] text-emerald-400" : "shrink-0 font-mono text-[11px] text-neutral-400"}>
                            {searching ? (
                              <>
                                <span aria-hidden="true">{matchCount}</span>
                                <span className="sr-only">{matchCount} match{matchCount === 1 ? "" : "es"}</span>
                              </>
                            ) : `${settled}/${row.target_count}`}
                          </span>
                        </div>
                        {showReason && (
                          <div className="mt-0.5 pl-[20px] text-[10px] leading-4 text-neutral-400" title={reason}>
                            {reason}
                          </div>
                        )}
                      </button>
                    );
                  })}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </aside>
  );
}
