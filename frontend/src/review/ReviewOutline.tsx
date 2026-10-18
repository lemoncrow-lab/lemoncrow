import { Button as UiButton } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { Input as UiInput } from "../components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "../components/ui/tooltip";
import { useEffect, useMemo, useRef, useState, type CSSProperties, type RefObject } from "react";
import { PanelResizeHandle } from "../components/ui/panel-resize-handle";
import {
  ArrowDownWideNarrow,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Search,
  X,
} from "lucide-react";

import FileTypeIcon from "./FileTypeIcon";
import { outlineSections } from "./readerModel";
import type { OutlineSectionKey, ReaderOrder, ReviewGuideTarget, TargetAttention } from "./readerModel";
import type { ReviewOutlineItem } from "./types";

interface ReviewOutlineProps {
  rows: ReviewOutlineItem[];
  /**
   * Each file's canonical attention (`fileAttention`). When given, a row's
   * section and reason come from it, so they agree with the Next card and the
   * target headers; otherwise the server row's own section and reason.
   */
  attentionByPath?: ReadonlyMap<string, TargetAttention>;
  /** One-based page containing each complete file. */
  pageByPath?: ReadonlyMap<string, number>;
  activePath: string;
  query: string;
  searchInputRef: RefObject<HTMLInputElement>;
  matchTargetCount: number;
  matchFileCount: number;
  matchCountsByPath: ReadonlyMap<string, number>;
  /** The one canonical conceptual cohort containing the active Code target. */
  currentCohort?: { id: string; label: string; path: string } | null;
  nextAttention?: ReviewGuideTarget | null;
  attentionCount?: number;
  attentionPosition?: number;
  onQuery(value: string): void;
  onNavigateMatch(delta: number): void;
  onDismissSearch(): void;
  onSelectPath(path: string): void;
  onSelectTarget?(targetId: string): void;
  onNavigateAttention?(delta: number): void;
  onOpenCohort?(cohortId: string): void;
  /** Review-level tier-1 actions (a failing required check); they outrank the target queue. */
  reviewActions?: readonly { id: string; title: string; detail: string; tier?: number }[];
  onReviewAction?(id: string): void;
  collapsed: boolean;
  onToggleCollapsed(): void;
  /** Recommended keeps the queue's sections; File order lists every file by path, matching the stream. */
  order?: ReaderOrder;
  orderDisabled?: boolean;
  onOrderChange?(order: ReaderOrder): void;
}

const ORDER_LABEL: Record<ReaderOrder, string> = {
  recommended: "Recommended",
  file: "File order",
};

interface OutlineListSection {
  key: OutlineSectionKey | "files";
  label: string;
  rows: ReviewOutlineItem[];
}

/** Code-point path order, the same comparison the server's File order uses. */
function byPath(a: ReviewOutlineItem, b: ReviewOutlineItem): number {
  return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
}

function parentDirectory(path: string): string {
  const separator = path.lastIndexOf("/");
  return separator < 0 ? "./" : path.slice(0, separator + 1);
}

/**
 * Consecutive rows that share a directory share one heading. Grouping only
 * *runs* keeps the server's canonical file order intact (spec §4.6); a
 * directory that reappears later in the queue gets its heading again.
 */
function groupByDirectory(rows: readonly ReviewOutlineItem[]): { key: string; directory: string; rows: ReviewOutlineItem[] }[] {
  const groups: { key: string; directory: string; rows: ReviewOutlineItem[] }[] = [];
  for (const row of rows) {
    const directory = parentDirectory(row.path);
    const last = groups[groups.length - 1];
    if (last && last.directory === directory) last.rows.push(row);
    else groups.push({ key: `${groups.length}:${directory}`, directory, rows: [row] });
  }
  return groups;
}

const LARGE_OUTLINE_THRESHOLD = 400;
const VIRTUAL_ROW_HEIGHT = 34;
const VIRTUAL_ROW_GAP = 2;
const VIRTUAL_ROW_PITCH = VIRTUAL_ROW_HEIGHT + VIRTUAL_ROW_GAP;
const VIRTUAL_OVERSCAN = 12;

interface VirtualOutlineRowsProps {
  rows: readonly ReviewOutlineItem[];
  pageByPath?: ReadonlyMap<string, number>;
  activePath: string;
  matchCountsByPath: ReadonlyMap<string, number>;
  searching: boolean;
  attentionByPath?: ReadonlyMap<string, TargetAttention>;
  onSelectPath(path: string): void;
}

function VirtualOutlineRows({
  rows,
  pageByPath,
  activePath,
  matchCountsByPath,
  searching,
  attentionByPath,
  onSelectPath,
}: VirtualOutlineRowsProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(480);
  const pendingFocusRef = useRef<string | null>(null);
  const activeIndex = useMemo(() => rows.findIndex((row) => row.path === activePath), [activePath, rows]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const update = () => setViewportHeight(Math.max(240, root.clientHeight || 480));
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || activeIndex < 0) return;
    const top = activeIndex * VIRTUAL_ROW_PITCH;
    const bottom = top + VIRTUAL_ROW_HEIGHT;
    const visibleTop = root.scrollTop;
    const visibleBottom = visibleTop + Math.max(240, root.clientHeight || viewportHeight);
    if (top >= visibleTop && bottom <= visibleBottom) return;
    const next = Math.max(0, top - Math.floor(viewportHeight / 3));
    root.scrollTop = next;
    setScrollTop(next);
  }, [activeIndex, viewportHeight]);

  const visibleCount = Math.ceil(viewportHeight / VIRTUAL_ROW_PITCH) + 2 * VIRTUAL_OVERSCAN;
  // A filter/section collapse can shorten the list while its previous scroll
  // offset is still committed. Keep the last window reachable in that frame.
  const first = Math.min(
    Math.max(0, rows.length - visibleCount),
    Math.max(0, Math.floor(scrollTop / VIRTUAL_ROW_PITCH) - VIRTUAL_OVERSCAN),
  );
  const last = Math.min(rows.length, first + visibleCount);
  const visible = rows.slice(first, last);

  useEffect(() => {
    const path = pendingFocusRef.current;
    if (!path) return;
    const control = [...(rootRef.current?.querySelectorAll<HTMLElement>("[data-review-path]") ?? [])]
      .find((node) => node.dataset.reviewPath === path);
    if (control) {
      pendingFocusRef.current = null;
      control.focus({ preventScroll: true });
    }
  }, [first, last, rows]);

  return (
    <div
      ref={rootRef}
      data-testid="review-outline-virtualized"
      role="group"
      aria-label="Changed files"
      aria-keyshortcuts="ArrowDown ArrowUp Home End PageDown PageUp"
      tabIndex={0}
      className="min-h-0 flex-1 overflow-y-auto px-1.5 py-1"
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      onKeyDown={(event) => {
        if (event.altKey || event.ctrlKey || event.metaKey || rows.length === 0) return;
        const keys = ["ArrowDown", "ArrowUp", "Home", "End", "PageDown", "PageUp"];
        if (!keys.includes(event.key)) return;
        event.preventDefault();
        // Keyboard traversal crosses virtual windows without selecting a file
        // until native button activation. Search and current-target identity stay intact.
        const focusedPath = (event.target as HTMLElement).closest<HTMLElement>("[data-review-path]")?.dataset.reviewPath;
        const focusedIndex = focusedPath ? rows.findIndex((row) => row.path === focusedPath) : -1;
        const index = focusedIndex >= 0 ? focusedIndex : activeIndex >= 0 ? activeIndex : 0;
        const step = Math.max(1, Math.floor(viewportHeight / VIRTUAL_ROW_PITCH));
        const next = event.key === "Home" ? 0 : event.key === "End" ? rows.length - 1
          : index + (event.key === "PageDown" ? step : event.key === "PageUp" ? -step
            : event.key === "ArrowDown" ? (focusedIndex < 0 && activeIndex < 0 ? 0 : 1) : -1);
        const destination = Math.min(rows.length - 1, Math.max(0, next));
        const root = event.currentTarget;
        const top = destination * VIRTUAL_ROW_PITCH;
        const nextScroll = top < root.scrollTop ? top
          : top + VIRTUAL_ROW_HEIGHT > root.scrollTop + viewportHeight ? top + VIRTUAL_ROW_HEIGHT - viewportHeight
            : root.scrollTop;
        pendingFocusRef.current = rows[destination].path;
        const mounted = [...root.querySelectorAll<HTMLElement>("[data-review-path]")]
          .find((node) => node.dataset.reviewPath === pendingFocusRef.current);
        if (mounted) {
          pendingFocusRef.current = null;
          mounted.focus({ preventScroll: true });
        }
        root.scrollTop = Math.max(0, nextScroll);
        setScrollTop(root.scrollTop);
      }}
    >
      <div aria-hidden="true" style={{ height: first * VIRTUAL_ROW_PITCH }} />
      {visible.map((row) => {
        const filename = row.path.split("/").pop() ?? row.path;
        const directory = parentDirectory(row.path);
        const complete = row.reviewed === row.target_count && row.target_count > 0;
        const matchCount = matchCountsByPath.get(row.path) ?? 0;
        const attention = attentionByPath?.get(row.path);
        const reason = attention
          ? (attention.actionable ? attention.reason_label : "")
          : complete ? "" : row.next_reason ?? "";
        const active = row.path === activePath;
        const page = pageByPath?.get(row.path);
        return (
          <UiButton
            unstyled
            type="button"
            key={row.path}
            aria-current={active ? "location" : undefined}
            data-review-path={row.path}
            data-review-page={page}
            onClick={() => onSelectPath(row.path)}
            tooltip={[row.path, page ? `Page ${page}` : "", reason].filter(Boolean).join(" · ")}
            className={[
              "review-outline-file flex w-full items-center gap-1.5 rounded-md border border-transparent px-2 text-left transition-colors",
              active ? "border-border bg-muted/85 shadow-sm" : "hover:bg-muted/45",
              complete ? "opacity-60" : "",
            ].join(" ")}
            style={{ height: VIRTUAL_ROW_HEIGHT, marginBottom: VIRTUAL_ROW_GAP }}
          >
            <FileTypeIcon path={row.path} size={13} />
            <span className="min-w-0 flex-1 truncate text-label text-neutral-300" title={row.path}>{filename}</span>
            {directory !== "./" && (
              <span className="max-w-[42%] truncate font-mono text-micro text-muted-foreground" title={directory}>{directory}</span>
            )}
            {page && <span className="shrink-0 text-caption tabular-nums text-muted-foreground" aria-label={`Page ${page}`}>p. {page}</span>}
            <span className={complete ? "shrink-0 font-mono text-label text-emerald-400" : "shrink-0 font-mono text-label text-neutral-400"}>
              {searching ? (
                <><span aria-hidden="true">{matchCount}</span><span className="sr-only">{matchCount} match{matchCount === 1 ? "" : "es"}</span></>
              ) : `${row.reviewed}/${row.target_count}`}
            </span>
          </UiButton>
        );
      })}
      <div aria-hidden="true" style={{ height: Math.max(0, rows.length - last) * VIRTUAL_ROW_PITCH }} />
    </div>
  );
}

export default function ReviewOutline({
  rows,
  attentionByPath,
  pageByPath,
  activePath,
  query,
  searchInputRef,
  matchTargetCount,
  matchFileCount,
  matchCountsByPath,
  currentCohort = null,
  nextAttention = null,
  attentionCount = 0,
  attentionPosition = 0,
  onQuery,
  onNavigateMatch,
  onDismissSearch,
  onSelectPath,
  onSelectTarget,
  onNavigateAttention,
  onOpenCohort,
  reviewActions = [],
  onReviewAction,
  collapsed,
  onToggleCollapsed,
  order = "recommended",
  orderDisabled = false,
  onOrderChange,
}: ReviewOutlineProps) {
  const [outlineWidth, setOutlineWidth] = useState(232);
  const [closedSections, setClosedSections] = useState<ReadonlySet<OutlineListSection["key"]>>(new Set(["done"]));
  const fileOrder = order === "file";
  const sections = useMemo<OutlineListSection[]>(
    () => fileOrder
      ? (rows.length ? [{ key: "files", label: "Files", rows: [...rows].sort(byPath) }] : [])
      : outlineSections(rows, "", attentionByPath),
    [fileOrder, rows, attentionByPath],
  );
  const searching = query.trim().length > 0;
  const largeOutline = rows.length > LARGE_OUTLINE_THRESHOLD;
  const virtualRows = useMemo(() => {
    if (!largeOutline) return [];
    const ordered = fileOrder ? [...rows].sort(byPath) : sections.flatMap((section) => section.rows);
    return fileOrder || searching
      ? ordered
      : sections.flatMap((section) => closedSections.has(section.key) ? [] : section.rows);
  }, [closedSections, fileOrder, largeOutline, rows, searching, sections]);
  const showCohort = Boolean(currentCohort);
  const showNext = Boolean(nextAttention && rows.length > 1 && onSelectTarget);
  const showActions = reviewActions.length > 0 && Boolean(onReviewAction);
  const showGuide = !searching && (showCohort || showNext || showActions);
  // The heading names the queue item's attention level, not its position.
  const nextHeading = nextAttention?.attention === "review_first" ? "Review first" : "Next";
  const nextHeadingClass = nextAttention?.attention === "review_first"
    ? "text-label font-semibold text-amber-300"
    : "text-label font-semibold text-neutral-300";

  if (collapsed) {
    return (
      <aside className="review-outline-collapsed flex w-9 shrink-0 flex-col items-center border-r border-border/80 bg-background/95 py-1.5" aria-label="Review outline">
        <UiButton variant="ghost" size="icon" type="button" aria-label="Open review outline" onClick={onToggleCollapsed}>
          <ChevronRight size={14} />
        </UiButton>
        <span className="mt-4 [writing-mode:vertical-rl] text-caption font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Files
        </span>
      </aside>
    );
  }

  return (
    <aside className="review-outline relative" style={{ "--outline-width": `min(${outlineWidth}px, 45vw)` } as CSSProperties} aria-label="Review outline">
      <PanelResizeHandle label="Resize review outline" onResize={setOutlineWidth} className="hidden min-[640px]:flex" />
      <div className="flex h-10 items-center gap-1.5 border-b border-border/70 px-2">
        <span className="text-heading font-semibold text-neutral-200">Files</span>
        <span className="flex-1" />
        <span className="font-mono text-caption text-muted-foreground">{rows.length}</span>
        <UiButton variant="ghost" size="icon" type="button" aria-label="Collapse review outline" onClick={onToggleCollapsed}>
          <ChevronLeft size={13} />
        </UiButton>
      </div>

      {showGuide && (
        <div className="border-b border-border/70 px-1.5 py-1.5" data-testid="review-guide">
          {showCohort && currentCohort && (
            <UiButton
              unstyled
              type="button"
              data-cohort-id={currentCohort.id}
              onClick={() => onOpenCohort?.(currentCohort.id)}
              disabled={!onOpenCohort}
              className="block w-full rounded px-1.5 py-1 text-left hover:bg-muted disabled:cursor-default"
              tooltip={currentCohort.path ? `${currentCohort.label} · ${currentCohort.path}` : currentCohort.label}
            >
              <span className="block text-micro font-semibold uppercase tracking-wide text-muted-foreground">Current concern</span>
              <span className="block truncate text-caption text-neutral-300">{currentCohort.label}</span>
            </UiButton>
          )}
          {showActions && (
            <div className={`space-y-1 ${showCohort ? "mt-2" : ""}`}>
              {reviewActions.map((action) => (
                <UiButton unstyled
                  key={action.id}
                  type="button"
                  onClick={() => onReviewAction?.(action.id)}
                  className={(action.tier ?? 1) <= 1
                    ? "block w-full rounded-md border border-rose-500/40 bg-rose-500/10 px-1.5 py-1 text-left hover:bg-rose-500/15"
                    : "block w-full rounded-md border border-amber-500/30 bg-amber-500/10 px-1.5 py-1 text-left hover:bg-amber-500/15"}
                  aria-label={`${action.title}: ${action.detail}`}
                >
                  <span className={(action.tier ?? 1) <= 1 ? "block text-label font-semibold text-rose-300" : "block text-label font-semibold text-amber-300"}>{action.title}</span>
                  <span className="block text-caption leading-4 text-neutral-400">{action.detail}</span>
                </UiButton>
              ))}
            </div>
          )}
          {showNext && nextAttention && (
            <Tooltip>
              <TooltipTrigger asChild>
                <div className={`w-full rounded-md border border-border/80 bg-muted/25 ${showCohort || showActions ? "mt-2" : ""}`}>
              <div className="flex min-w-0 items-center gap-1 px-1.5 pt-1">
                <span className={nextHeadingClass}>{nextHeading}</span>
                {attentionCount > 0 && (
                  <span className="font-mono text-caption text-neutral-400">
                    {Math.max(1, attentionPosition)}/{attentionCount}
                  </span>
                )}
                <span className="min-w-0 flex-1 truncate text-right font-mono text-caption text-neutral-400">
                  {nextAttention.path.split("/").pop() ?? nextAttention.path}
                </span>
                {attentionCount > 1 && onNavigateAttention && (
                  <>
                    <UiButton
                      variant="ghost"
                      size="icon"
                      type="button"
                      aria-label="Previous review item"
                      onClick={() => onNavigateAttention(-1)}
                      className="h-6 w-6 border-0"
                    >
                      <ChevronUp size={10} />
                    </UiButton>
                    <UiButton
                      variant="ghost"
                      size="icon"
                      type="button"
                      aria-label="Next review item"
                      onClick={() => onNavigateAttention(1)}
                      className="h-6 w-6 border-0"
                    >
                      <ChevronDown size={10} />
                    </UiButton>
                  </>
                )}
              </div>
              <UiButton unstyled
                type="button"
                onClick={() => onSelectTarget?.(nextAttention.targetId)}
                className="block w-full px-1.5 pb-1 pt-0.5 text-left hover:bg-muted/55"
                aria-label={`${nextHeading}: ${nextAttention.label}`}
              >
                <span className="block truncate font-mono text-micro leading-3 text-muted-foreground">
                  {parentDirectory(nextAttention.path)}
                </span>
                <span className="block text-label leading-4 text-neutral-400">{nextAttention.reason}</span>
              </UiButton>
                </div>
              </TooltipTrigger>
              <TooltipContent
                side="right"
                align="start"
                collisionPadding={8}
                className="max-w-[min(20rem,var(--radix-tooltip-content-available-width))] [overflow-wrap:anywhere]"
              >
                {nextAttention.path} · {nextAttention.label} · {nextAttention.reason}
              </TooltipContent>
            </Tooltip>
          )}
        </div>
      )}

      <div className="border-b border-border/70 p-1.5">
        <div className="flex items-center gap-1">
        <div className="relative flex min-w-0 flex-1 items-center">
          <Search size={13} className="pointer-events-none absolute left-2.5 text-muted-foreground" />
          <UiInput
            uiSize="xs"
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
            placeholder="Find file, symbol, or concept…"
            className="review-outline-search-field h-7 w-full"
          />
          {searching && (
            <UiButton
              variant="ghost"
              size="icon"
              type="button"
              aria-label="Clear review search"
              onClick={onDismissSearch}
              className="absolute right-0.5 h-7 w-7 border-0"
            >
              <X size={12} />
            </UiButton>
          )}
        </div>
        {onOrderChange && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <UiButton
                variant="ghost"
                size="icon"
                type="button"
                disabled={orderDisabled}
                aria-label={`Review order: ${ORDER_LABEL[order]}`}
                title={`Review order: ${ORDER_LABEL[order]}`}
                className={fileOrder ? "h-7 w-7 shrink-0 border-0 text-sky-300" : "h-7 w-7 shrink-0 border-0"}
              >
                <ArrowDownWideNarrow size={13} />
              </UiButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuLabel>Review order</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={order} onValueChange={(value) => onOrderChange(value as ReaderOrder)}>
                <DropdownMenuRadioItem value="recommended" className="items-start">
                  <span className="min-w-0 flex-1">
                    <span className="block">Recommended</span>
                    <span className="block text-caption text-muted-foreground">Open work and risk first</span>
                  </span>
                </DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="file" className="items-start">
                  <span className="min-w-0 flex-1">
                    <span className="block">File order</span>
                    <span className="block text-caption text-muted-foreground">By path</span>
                  </span>
                </DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        </div>
        {searching && (
          <div className="mt-1 flex items-center gap-1 text-caption text-muted-foreground" aria-live="polite">
            <span className="min-w-0 flex-1 truncate">
              <span aria-hidden="true">{matchTargetCount} review area{matchTargetCount === 1 ? "" : "s"} in {matchFileCount} file{matchFileCount === 1 ? "" : "s"}</span>
              <span className="sr-only">{matchTargetCount} review area{matchTargetCount === 1 ? "" : "s"} · {matchFileCount} file{matchFileCount === 1 ? "" : "s"}</span>
            </span>
            <UiButton
              variant="ghost"
              size="icon"
              type="button"
              aria-label="Previous search match"
              disabled={matchTargetCount === 0}
              onClick={() => onNavigateMatch(-1)}
              className="h-6 w-6 border-0"
            >
              <ChevronUp size={12} />
            </UiButton>
            <UiButton
              variant="ghost"
              size="icon"
              type="button"
              aria-label="Next search match"
              disabled={matchTargetCount === 0}
              onClick={() => onNavigateMatch(1)}
              className="h-6 w-6 border-0"
            >
              <ChevronDown size={12} />
            </UiButton>
          </div>
        )}
      </div>

      {largeOutline ? (
        <>
          {!fileOrder && !searching && (
            <div className="flex shrink-0 flex-wrap gap-1 border-b border-border/70 px-1.5 py-1">
              {sections.map((section) => {
                const closed = closedSections.has(section.key);
                return (
                  <UiButton
                    key={section.key}
                    unstyled
                    type="button"
                    aria-expanded={!closed}
                    onClick={() => {
                      setClosedSections((current) => {
                        const next = new Set(current);
                        if (next.has(section.key)) next.delete(section.key);
                        else next.add(section.key);
                        return next;
                      });
                    }}
                    className="rounded px-1.5 py-1 text-caption text-neutral-400 hover:bg-muted/55 hover:text-neutral-100"
                  >
                    {section.label} · {section.rows.length}
                  </UiButton>
                );
              })}
            </div>
          )}
          <VirtualOutlineRows
            rows={virtualRows}
            pageByPath={pageByPath}
            activePath={activePath}
            matchCountsByPath={matchCountsByPath}
            searching={searching}
            attentionByPath={attentionByPath}
            onSelectPath={onSelectPath}
          />
        </>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto py-1">
          {sections.map((section) => {
            const closed = !fileOrder && !searching && closedSections.has(section.key);
            return (
              <section key={section.key} className="pb-1">
                {!fileOrder && (
                <UiButton unstyled
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
                  className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-caption font-medium text-neutral-400 transition-colors hover:bg-muted/55 hover:text-neutral-50"
                >
                  {closed ? <ChevronRight size={11} /> : <ChevronDown size={11} />}
                  <span className="flex-1">{section.label}</span>
                  <span className="font-mono font-normal text-neutral-400">{section.rows.length}</span>
                </UiButton>
                )}
                {!closed && (
                  <div id={`review-outline-section-${section.key}`} className="space-y-1 px-1.5">
                    {groupByDirectory(section.rows).map((group) => (
                      <div key={group.key} className="min-w-0">
                        {group.rows.length > 1 && (
                          <div
                            className="truncate px-2 pb-0.5 pt-1 font-mono text-micro leading-3 text-muted-foreground"
                            title={group.directory}
                            data-testid="review-outline-directory"
                          >
                            {group.directory}
                          </div>
                        )}
                        <div className="space-y-0.5">
                          {group.rows.map((row) => {
                            const filename = row.path.split("/").pop() ?? row.path;
                            const complete = row.reviewed === row.target_count && row.target_count > 0;
                            const settled = row.reviewed;
                            const matchCount = matchCountsByPath.get(row.path) ?? 0;
                            const attention = attentionByPath?.get(row.path);
                            const reason = attention
                              ? (attention.actionable ? attention.reason_label : "")
                              : row.next_reason ?? "";
                            const active = row.path === activePath;
                            const showReason = Boolean(reason && (active || section.key === "review_first"));
                            return (
                              <UiButton unstyled
                                type="button"
                                key={row.path}
                                aria-current={active ? "location" : undefined}
                                data-review-path={row.path}
                                data-review-page={pageByPath?.get(row.path)}
                                tooltip={pageByPath?.has(row.path) ? `${row.path} · Page ${pageByPath.get(row.path)}` : row.path}
                                onClick={() => onSelectPath(row.path)}
                                className={[
                                  "review-outline-file block w-full rounded-md border border-transparent px-2 py-1.5 text-left transition-colors",
                                  active
                                    ? "border-border bg-muted/85 shadow-sm"
                                    : "hover:bg-muted/45",
                                  complete ? "opacity-60" : "",
                                ].join(" ")}
                              >
                                <div className="flex items-center gap-1.5 text-label">
                                  <FileTypeIcon path={row.path} size={13} />
                                  <span className={[active ? "font-medium text-foreground" : "text-neutral-300", "truncate", group.rows.length === 1 && group.directory !== "./" ? "max-w-[70%] shrink-0" : "min-w-0 flex-1"].join(" ")} title={row.path}>
                                    {filename}
                                  </span>
                                  {group.rows.length === 1 && group.directory !== "./" && (
                                    <span
                                      className="min-w-0 flex-1 truncate text-left font-mono text-micro text-muted-foreground"
                                      style={{ direction: "rtl" }}
                                      title={row.path}
                                      data-testid="review-outline-directory"
                                    >
                                      <bdi dir="ltr">{group.directory}</bdi>
                                    </span>
                                  )}
                                  {pageByPath?.has(row.path) && (
                                    <span className="shrink-0 text-caption tabular-nums text-muted-foreground" aria-label={`Page ${pageByPath.get(row.path)}`}>
                                      p. {pageByPath.get(row.path)}
                                    </span>
                                  )}
                                  <span className={complete ? "shrink-0 font-mono text-label text-emerald-400" : "shrink-0 font-mono text-label text-neutral-400"}>
                                    {searching ? (
                                      <>
                                        <span aria-hidden="true">{matchCount}</span>
                                        <span className="sr-only">{matchCount} match{matchCount === 1 ? "" : "es"}</span>
                                      </>
                                    ) : `${settled}/${row.target_count}`}
                                  </span>
                                </div>
                                {showReason && (
                                  <div className="mt-0.5 pl-[20px] text-caption leading-4 text-neutral-400" title={reason}>
                                    {reason}
                                  </div>
                                )}
                              </UiButton>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      )}
    </aside>
  );
}
