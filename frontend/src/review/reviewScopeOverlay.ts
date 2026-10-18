/**
 * Review-area scope overlays drawn over Pierre's rendered diff.
 *
 * Pure DOM decoration extracted from ReviewStream: it owns how a review
 * target's changed lines are boxed/toned, independent of stream state.
 */
import type { Theme } from "../lib/theme";
import type { DiffStyle } from "./diffModel";
import { reviewScopeTone, targetScrollLocation } from "./readerModel";
import type { ReviewTarget } from "./types";

export function targetHasContinuousScope(
  target: ReviewTarget,
  siblings: readonly ReviewTarget[] = [],
): boolean {
  if (target.kind === "file" || target.spans.length === 0) return false;
  const containsChild = target.start_line > 0 && target.end_line >= target.start_line
    && siblings.some((candidate) => (
      candidate.target_id !== target.target_id
      && candidate.path === target.path
      && candidate.start_line > 0
      && candidate.end_line >= candidate.start_line
      && candidate.start_line >= target.start_line
      && candidate.end_line <= target.end_line
      && (
        candidate.start_line > target.start_line
        || candidate.end_line < target.end_line
      )
    ));
  if (containsChild) return false;

  const counts = new Map<"old" | "new", number>();
  for (const span of target.spans) {
    counts.set(span.side, (counts.get(span.side) ?? 0) + 1);
  }
  return [...counts.values()].every((count) => count === 1);
}

export function renderedDiffLine(
  container: HTMLElement,
  side: "additions" | "deletions",
  lineNumber: number,
): HTMLElement | null {
  const hosts = container.matches("diffs-container")
    ? [container]
    : [...container.querySelectorAll<HTMLElement>("diffs-container")];
  for (const host of hosts) {
    const root: ParentNode = host.shadowRoot ?? host;
    const rows = [...root.querySelectorAll<HTMLElement>(`[data-line="${lineNumber}"]`)];
    if (rows.length === 0) continue;
    const preferred = rows.find((row) => {
      // A split diff numbers each column on its own side: the deletions
      // column's context row N is old line N, not new line N. Only the
      // requested side's column can hold the line.
      const column = row.closest("[data-code]");
      if (column?.hasAttribute("data-additions") || column?.hasAttribute("data-deletions")) {
        return column.hasAttribute(side === "additions" ? "data-additions" : "data-deletions");
      }
      const lineType = row.getAttribute("data-line-type") ?? "";
      if (side === "additions") return !lineType.includes("deletion");
      return !lineType.includes("addition");
    });
    return preferred ?? rows[0] ?? null;
  }
  return null;
}

const REVIEW_FOLD_HIDDEN = "lcReviewFoldHidden";
const REVIEW_FOLD_PREVIOUS_DISPLAY = "lcReviewFoldPreviousDisplay";
const REVIEW_FOLD_SHRUNK = "lcReviewFoldShrunk";
const REVIEW_FOLD_PREVIOUS_STYLE = "lcReviewFoldPreviousStyle";

function diffRoots(container: HTMLElement): ParentNode[] {
  const hosts = container.matches("diffs-container")
    ? [container]
    : [...container.querySelectorAll<HTMLElement>("diffs-container")];
  return hosts.map((host) => host.shadowRoot ?? host);
}

function restoreFoldedRows(container: HTMLElement): void {
  for (const root of diffRoots(container)) {
    for (const row of root.querySelectorAll<HTMLElement>("[data-lc-review-fold-hidden]")) {
      row.style.display = row.dataset[REVIEW_FOLD_PREVIOUS_DISPLAY] ?? "";
      delete row.dataset[REVIEW_FOLD_HIDDEN];
      delete row.dataset[REVIEW_FOLD_PREVIOUS_DISPLAY];
    }
    for (const buffer of root.querySelectorAll<HTMLElement>("[data-lc-review-fold-shrunk]")) {
      buffer.setAttribute("style", buffer.dataset[REVIEW_FOLD_PREVIOUS_STYLE] ?? "");
      delete buffer.dataset[REVIEW_FOLD_SHRUNK];
      delete buffer.dataset[REVIEW_FOLD_PREVIOUS_STYLE];
    }
  }
}

/** A diff row hidden because its review area is folded: it has no position of its own. */
export function isFoldHiddenRow(row: HTMLElement): boolean {
  return row.hasAttribute("data-lc-review-fold-hidden");
}

function hideFoldedRow(row: HTMLElement): void {
  row.dataset[REVIEW_FOLD_PREVIOUS_DISPLAY] = row.style.display;
  row.dataset[REVIEW_FOLD_HIDDEN] = "true";
  row.style.display = "none";
}

type FoldColumn = "deletions" | "additions" | "unified";

/** The old/new line numbers a rendered row shows (a context row carries both). */
function rowLines(row: HTMLElement, column: FoldColumn): { old?: number; new?: number } | null {
  const line = Number.parseInt(row.dataset.line ?? "", 10);
  if (!Number.isFinite(line) || line < 1) return null;
  const type = row.getAttribute("data-line-type") ?? "";
  if (type.includes("deletion")) return { old: line };
  if (type.includes("addition")) return { new: line };
  const parsedAlt = Number.parseInt(row.dataset.altLine ?? "", 10);
  const alt = Number.isFinite(parsedAlt) && parsedAlt > 0 ? parsedAlt : undefined;
  // Each column numbers a context row on its own side; the other side is the alt line.
  return column === "deletions" ? { old: line, new: alt } : { new: line, old: alt };
}

/** The children of a Pierre gutter/content column with the grid rows each one occupies. */
function gridSlots(part: Element): { element: HTMLElement; start: number; span: number }[] {
  let start = 0;
  return [...part.children].map((child) => {
    const element = child as HTMLElement;
    const span = Math.max(1, Number.parseInt(element.dataset.bufferSize ?? "1", 10) || 1);
    const slot = { element, start, span };
    start += span;
    return slot;
  });
}

/**
 * Pierre lays each code column out as a grid: a gutter (line numbers) and the
 * content share its rows, and in a split diff the deletions and additions
 * columns stay in step because every row exists in both (a side with no line
 * there holds a buffer). Folding therefore removes whole grid rows -- the
 * content row, its line number and the other column's row or buffer -- never a
 * content row on its own, which shifted the numbers against the code and one
 * column against the other.
 */
function foldGridRows(
  root: ParentNode,
  owned: (lines: { old?: number; new?: number }, targets: readonly ReviewTarget[]) => boolean,
  folded: readonly ReviewTarget[],
  expanded: readonly ReviewTarget[],
): boolean {
  const columns = [...root.querySelectorAll<HTMLElement>("[data-code]")]
    .map((code) => ({
      code,
      content: code.querySelector<HTMLElement>(":scope > [data-content]"),
      gutter: code.querySelector<HTMLElement>(":scope > [data-gutter]"),
    }))
    .filter((column): column is { code: HTMLElement; content: HTMLElement; gutter: HTMLElement | null } => column.content !== null);
  if (columns.length === 0) return false;

  const linesByRow = new Map<number, { old?: number; new?: number }[]>();
  for (const { code, content } of columns) {
    const column: FoldColumn = code.hasAttribute("data-deletions") ? "deletions" : code.hasAttribute("data-additions") ? "additions" : "unified";
    for (const slot of gridSlots(content)) {
      if (!slot.element.hasAttribute("data-line")) continue;
      const lines = rowLines(slot.element, column);
      if (!lines) continue;
      const row = linesByRow.get(slot.start) ?? [];
      row.push(lines);
      linesByRow.set(slot.start, row);
    }
  }
  const hiddenRows = new Set<number>();
  for (const [row, lines] of linesByRow) {
    if (!lines.some((line) => owned(line, folded))) continue;
    if (lines.some((line) => owned(line, expanded))) continue;
    hiddenRows.add(row);
  }
  if (hiddenRows.size === 0) return true;

  for (const { content, gutter } of columns) {
    for (const part of gutter ? [content, gutter] : [content]) {
      for (const { element, start, span } of gridSlots(part)) {
        let hidden = 0;
        for (let row = start; row < start + span; row += 1) if (hiddenRows.has(row)) hidden += 1;
        if (hidden === 0) continue;
        if (hidden >= span) {
          hideFoldedRow(element);
          continue;
        }
        // A buffer spanning folded and open rows keeps only the open ones.
        element.dataset[REVIEW_FOLD_PREVIOUS_STYLE] = element.getAttribute("style") ?? "";
        element.dataset[REVIEW_FOLD_SHRUNK] = "true";
        element.style.gridRow = `span ${span - hidden}`;
        element.style.minHeight = `calc(${span - hidden} * 1lh)`;
      }
    }
  }
  return true;
}

function rowMatchesSide(row: HTMLElement, side: "old" | "new"): boolean {
  const lineType = row.getAttribute("data-line-type") ?? "";
  if (side === "old") return !lineType.includes("addition");
  return !lineType.includes("deletion");
}

function targetOwnsLine(target: ReviewTarget, side: "old" | "new", line: number): boolean {
  if (target.spans.some((span) => span.side === side && span.start_line <= line && span.end_line >= line)) return true;
  // Symbol targets own the rendered body on the new side, not only the changed
  // lines inside it. Folding the whole body is what removes already-reviewed
  // code from the reviewer's working set instead of leaving most of it behind.
  return side === "new"
    && (target.kind === "symbol" || target.kind === "document_section")
    && target.start_line > 0
    && target.start_line <= line
    && target.end_line >= line;
}

/**
 * Visually fold judged review targets without changing their review state.
 *
 * Pierre owns the diff rows, so target folding is a reversible DOM decoration:
 * every pass first restores rows hidden by the previous pass, then hides only
 * rows that belong exclusively to folded targets. If an unresolved target
 * overlaps the same line, that line stays visible.
 */
export function decorateReviewTargetFolds(
  container: HTMLElement,
  path: string,
  targets: readonly ReviewTarget[],
  foldedTargetIds: ReadonlySet<string>,
  phase: "mount" | "update" | "unmount",
): void {
  restoreFoldedRows(container);
  if (phase === "unmount" || foldedTargetIds.size === 0) return;

  const pathTargets = targets.filter((target) => target.path === path);
  const folded = pathTargets.filter((target) => (
    foldedTargetIds.has(target.target_id)
    && targetHasContinuousScope(target, pathTargets)
  ));
  if (folded.length === 0) return;
  const expanded = pathTargets.filter((target) => !foldedTargetIds.has(target.target_id));
  const owned = (lines: { old?: number; new?: number }, owners: readonly ReviewTarget[]) => owners.some((target) => (
    (lines.old !== undefined && targetOwnsLine(target, "old", lines.old))
    || (lines.new !== undefined && targetOwnsLine(target, "new", lines.new))
  ));

  for (const root of diffRoots(container)) {
    if (foldGridRows(root, owned, folded, expanded)) continue;
    for (const row of root.querySelectorAll<HTMLElement>("[data-line]")) {
      const line = Number.parseInt(row.dataset.line ?? "", 10);
      if (!Number.isFinite(line) || line < 1) continue;
      const sides = (["old", "new"] as const).filter((side) => rowMatchesSide(row, side));
      const hiddenByFold = sides.some((side) => folded.some((target) => targetOwnsLine(target, side, line)));
      if (!hiddenByFold) continue;
      const neededByExpandedTarget = sides.some((side) => expanded.some((target) => targetOwnsLine(target, side, line)));
      if (neededByExpandedTarget) continue;

      hideFoldedRow(row);
    }
  }
}

export function decorateReviewScopeBoxes(
  container: HTMLElement,
  path: string,
  targets: readonly ReviewTarget[],
  phase: "mount" | "update" | "unmount",
  chromeTheme: Theme,
  diffStyle: DiffStyle,
  activeTargetId = "",
): void {
  const wrapper = container.closest<HTMLElement>(`[data-testid="code-item:${CSS.escape(path)}"]`);
  if (!wrapper) return;

  for (const overlay of wrapper.querySelectorAll<HTMLElement>("[data-lc-review-scope-overlay]")) {
    overlay.remove();
  }
  if (phase === "unmount") return;

  const rows = targets.filter((target) => target.path === path && targetHasContinuousScope(target, targets));
  if (rows.length === 0) return;

  const wrapperRect = wrapper.getBoundingClientRect();
  if (wrapperRect.width <= 0 || wrapperRect.height <= 0) return;
  wrapper.style.position = "relative";

  const lineHeightValue = getComputedStyle(container).getPropertyValue("--diffs-line-height")
    || getComputedStyle(container).lineHeight;
  const lineHeight = Number.parseFloat(lineHeightValue) || 21;

  const edgeFor = (target: ReviewTarget) => {
    const tone = reviewScopeTone(target);
    const active = target.target_id === activeTargetId;
    if (tone === "added") {
      return chromeTheme === "dark"
        ? (active ? "rgba(74, 222, 128, 0.62)" : "rgba(74, 222, 128, 0.28)")
        : (active ? "rgba(22, 163, 74, 0.58)" : "rgba(22, 163, 74, 0.28)");
    }
    if (tone === "deleted") {
      return chromeTheme === "dark"
        ? (active ? "rgba(251, 113, 133, 0.64)" : "rgba(251, 113, 133, 0.30)")
        : (active ? "rgba(225, 29, 72, 0.58)" : "rgba(225, 29, 72, 0.28)");
    }
    return chromeTheme === "dark"
      ? (active ? "rgba(56, 189, 248, 0.58)" : "rgba(56, 189, 248, 0.22)")
      : (active ? "rgba(2, 132, 199, 0.54)" : "rgba(2, 132, 199, 0.22)");
  };

  const scopeRange = (target: ReviewTarget, side?: "old" | "new") => {
    const spans = side ? target.spans.filter((span) => span.side === side) : target.spans;
    if (spans.length === 0) return null;
    const spanStart = Math.min(...spans.map((span) => span.start_line));
    const spanEnd = Math.max(...spans.map((span) => span.end_line));
    const tone = reviewScopeTone(target);
    const blockLevel = target.kind === "symbol" || target.kind === "document_section" || target.kind === "hunk";
    const targetEndApplies = blockLevel
      && target.end_line >= spanStart
      && (
        side === undefined
        || tone !== "modified"
        || side === "new"
      );
    return {
      startLine: spanStart,
      endLine: targetEndApplies ? Math.max(spanEnd, target.end_line) : spanEnd,
    };
  };

  const appendOverlay = ({
    target,
    side,
    left,
    right,
    leftEdge,
    rightEdge,
  }: {
    target: ReviewTarget;
    side: "old" | "new" | "all";
    left: number;
    right: number;
    leftEdge: boolean;
    rightEdge: boolean;
  }) => {
    const range = scopeRange(target, side === "all" ? undefined : side);
    if (!range) return;

    const renderedSide = side === "old" ? "deletions" : "additions";
    const fallbackSide = side === "all"
      ? (targetScrollLocation(target)?.side ?? "additions")
      : renderedSide;
    const startRow = renderedDiffLine(container, side === "all" ? fallbackSide : renderedSide, range.startLine);
    if (!startRow) return;

    const endCandidates = side === "all"
      ? (["additions", "deletions"] as const)
          .map((candidate) => renderedDiffLine(container, candidate, range.endLine))
          .filter((row): row is HTMLElement => Boolean(row))
      : [renderedDiffLine(container, renderedSide, range.endLine)].filter(
          (row): row is HTMLElement => Boolean(row),
        );

    const startRect = startRow.getBoundingClientRect();
    if (startRect.height <= 0) return;
    const endRects = endCandidates.map((row) => row.getBoundingClientRect()).filter((rect) => rect.height > 0);
    const measuredBottom = endRects.length > 0
      ? Math.max(...endRects.map((rect) => rect.bottom))
      : startRect.top + Math.max(1, range.endLine - range.startLine + 1) * lineHeight;

    const top = Math.max(0, startRect.top - wrapperRect.top);
    const bottom = Math.max(top + 1, measuredBottom - wrapperRect.top);
    const edge = edgeFor(target);
    const overlay = document.createElement("div");
    overlay.dataset.lcReviewScopeOverlay = target.target_id;
    overlay.dataset.lcReviewScopeOverlaySide = side;
    const active = target.target_id === activeTargetId;
    overlay.dataset.lcReviewCoverage = "true";
    overlay.dataset.lcReviewScopeActive = active ? "true" : "false";
    overlay.setAttribute("aria-hidden", "true");
    Object.assign(overlay.style, {
      position: "absolute",
      pointerEvents: "none",
      zIndex: active ? "4" : "3",
      top: `${top}px`,
      left: `${left}px`,
      width: `${Math.max(1, right - left)}px`,
      height: `${bottom - top}px`,
      // The actionable annotation header lives in Pierre's annotation lane,
      // which has different horizontal geometry from the code rows. Do not draw
      // top/bottom edges that try to visually join those two unrelated boxes.
      // Side rails communicate scope cleanly without producing offset corners.
      borderTop: "0 solid transparent",
      borderLeft: leftEdge ? `${active ? 2 : 1}px solid ${edge}` : "0 solid transparent",
      borderRight: rightEdge ? `1px solid ${edge}` : "0 solid transparent",
      borderBottom: "0 solid transparent",
      borderRadius: "0",
      background: active
        ? (chromeTheme === "dark" ? "rgba(255,255,255,0.018)" : "rgba(15,23,42,0.018)")
        : "transparent",
      boxSizing: "border-box",
    });
    wrapper.appendChild(overlay);
  };

  for (const target of rows) {
    const tone = reviewScopeTone(target);
    if (diffStyle === "split" && tone === "modified") {
      const midpoint = wrapperRect.width / 2;
      appendOverlay({
        target,
        side: "old",
        left: 0,
        right: midpoint,
        leftEdge: true,
        rightEdge: false,
      });
      appendOverlay({
        target,
        side: "new",
        left: midpoint,
        right: wrapperRect.width,
        leftEdge: false,
        rightEdge: true,
      });
      continue;
    }

    if (diffStyle === "split" && tone === "deleted") {
      appendOverlay({
        target,
        side: "old",
        left: 0,
        right: wrapperRect.width / 2,
        leftEdge: true,
        rightEdge: true,
      });
      continue;
    }

    if (diffStyle === "split" && tone === "added") {
      appendOverlay({
        target,
        side: "new",
        left: wrapperRect.width / 2,
        right: wrapperRect.width,
        leftEdge: true,
        rightEdge: true,
      });
      continue;
    }

    appendOverlay({
      target,
      side: tone === "deleted" ? "old" : tone === "added" ? "new" : "all",
      left: 0,
      right: wrapperRect.width,
      leftEdge: true,
      rightEdge: true,
    });
  }
}
