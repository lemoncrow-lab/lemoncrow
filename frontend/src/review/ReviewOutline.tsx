import { useMemo, useState, type RefObject } from "react";

import { outlineSections } from "./readerModel";
import type { OutlineSectionKey } from "./readerModel";
import type { ReviewOutlineItem } from "./types";

const GLYPH: Record<OutlineSectionKey, string> = {
  attention: "⚠",
  changed: "↻",
  remaining: "○",
  tests: "○",
  mechanical: "·",
  done: "✓",
};

interface ReviewOutlineProps {
  rows: ReviewOutlineItem[];
  activePath: string;
  query: string;
  searchInputRef: RefObject<HTMLInputElement>;
  matchTargetCount: number;
  matchFileCount: number;
  matchCountsByPath: ReadonlyMap<string, number>;
  onQuery(value: string): void;
  onNavigateMatch(delta: number): void;
  onDismissSearch(): void;
  onSelectPath(path: string): void;
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
  onQuery,
  onNavigateMatch,
  onDismissSearch,
  onSelectPath,
  collapsed,
  onToggleCollapsed,
}: ReviewOutlineProps) {
  const [closedSections, setClosedSections] = useState<ReadonlySet<OutlineSectionKey>>(new Set(["done"]));
  const sections = useMemo(() => outlineSections(rows), [rows]);
  const searching = query.trim().length > 0;

  if (collapsed) {
    return (
      <aside className="flex w-9 shrink-0 flex-col items-center border-r border-neutral-800 bg-neutral-950 py-2" aria-label="Review outline">
        <button
          type="button"
          aria-label="Open review outline"
          onClick={onToggleCollapsed}
          className="px-2 py-1 text-xs text-neutral-500 hover:text-neutral-200"
        >
          »
        </button>
        <span className="mt-3 [writing-mode:vertical-rl] text-[9px] uppercase tracking-[0.18em] text-neutral-700">Review outline</span>
      </aside>
    );
  }

  return (
    <aside className="flex w-[248px] shrink-0 flex-col border-r border-neutral-800 bg-neutral-950" aria-label="Review outline">
      <div className="flex items-center gap-2 border-b border-neutral-900 px-2.5 py-2">
        <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-neutral-600">Review outline</span>
        <span className="flex-1" />
        <button type="button" aria-label="Collapse review outline" onClick={onToggleCollapsed} className="text-[11px] text-neutral-600 hover:text-neutral-300">«</button>
      </div>
      <div className="border-b border-neutral-900 p-2">
        <div className="flex items-center gap-1">
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
            placeholder="/ path, symbol, reason…"
            className="min-w-0 flex-1 border border-neutral-800 bg-neutral-900/35 px-2 py-1.5 text-[10px] text-neutral-300 outline-none placeholder:text-neutral-700 focus:border-neutral-600"
          />
          {searching && (
            <button
              type="button"
              aria-label="Clear review search"
              onClick={onDismissSearch}
              className="px-1.5 py-1 text-[10px] text-neutral-600 hover:text-neutral-300"
            >
              ×
            </button>
          )}
        </div>
        {searching && (
          <div className="mt-1.5 flex items-center gap-1 text-[9px] text-neutral-600" aria-live="polite">
            <span className="min-w-0 flex-1 truncate">
              {matchTargetCount} target{matchTargetCount === 1 ? "" : "s"} · {matchFileCount} file{matchFileCount === 1 ? "" : "s"}
            </span>
            <button
              type="button"
              aria-label="Previous search match"
              disabled={matchTargetCount === 0}
              onClick={() => onNavigateMatch(-1)}
              className="px-1 text-neutral-500 hover:text-neutral-200 disabled:opacity-30"
            >
              ↑
            </button>
            <button
              type="button"
              aria-label="Next search match"
              disabled={matchTargetCount === 0}
              onClick={() => onNavigateMatch(1)}
              className="px-1 text-neutral-500 hover:text-neutral-200 disabled:opacity-30"
            >
              ↓
            </button>
            <span className="text-neutral-700">Enter</span>
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
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[9px] font-semibold uppercase tracking-[0.1em] text-neutral-600 hover:bg-neutral-900/50"
              >
                <span>{closed ? "▸" : "▾"}</span>
                <span className="flex-1">{section.label}</span>
                <span className="font-mono font-normal">{section.rows.length}</span>
              </button>
              {!closed && <div id={`review-outline-section-${section.key}`}>
                {section.rows.map((row) => {
                const filename = row.path.split("/").pop() ?? row.path;
                const complete = row.reviewed === row.target_count && row.target_count > 0;
                const settled = row.reviewed;
                const matchCount = matchCountsByPath.get(row.path) ?? 0;
                const reason = row.reasons.find((item) => !/^\+\d+\s+-\d+$/.test(item.trim())) ?? "";
                return (
                  <button
                    type="button"
                    key={row.path}
                    aria-current={row.path === activePath ? "location" : undefined}
                    onClick={() => onSelectPath(row.path)}
                    className={[
                      "block w-full border-l-2 px-2.5 py-1.5 text-left",
                      row.path === activePath ? "border-sky-500 bg-sky-950/20" : "border-transparent hover:bg-neutral-900/45",
                      complete ? "opacity-55" : "",
                    ].join(" ")}
                  >
                    <div className="flex items-center gap-1.5 text-[11px]">
                      <span className={section.key === "changed" ? "text-sky-300" : section.key === "attention" ? "text-amber-300" : complete ? "text-emerald-500" : "text-neutral-600"}>{GLYPH[section.key]}</span>
                      <span className="min-w-0 flex-1 truncate text-neutral-300">{filename}</span>
                      <span className="shrink-0 font-mono text-[9px] text-neutral-600">
                        {searching ? `${matchCount} match${matchCount === 1 ? "" : "es"}` : `${settled}/${row.target_count}`}
                      </span>
                    </div>
                    {reason && <div className="mt-0.5 truncate pl-4 text-[9px] text-neutral-700" title={reason}>{reason}</div>}
                  </button>
                );
              })}
              </div>}
            </section>
          );
        })}
      </div>
    </aside>
  );
}
