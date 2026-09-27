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
      const lineType = row.getAttribute("data-line-type") ?? "";
      if (side === "additions") return !lineType.includes("deletion");
      return !lineType.includes("addition");
    });
    return preferred ?? rows[0] ?? null;
  }
  return null;
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
    const blockLevel = target.kind === "symbol" || target.kind === "hunk";
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
      borderTop: `1px solid ${edge}`,
      borderLeft: leftEdge ? `${active ? 2 : 1}px solid ${edge}` : "0 solid transparent",
      borderRight: rightEdge ? `1px solid ${edge}` : "0 solid transparent",
      borderBottom: `1px solid ${edge}`,
      borderTopLeftRadius: leftEdge ? "6px" : "0",
      borderTopRightRadius: rightEdge ? "6px" : "0",
      borderBottomLeftRadius: leftEdge ? "6px" : "0",
      borderBottomRightRadius: rightEdge ? "6px" : "0",
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
