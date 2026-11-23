import { Button as UiButton } from "../components/ui/button";
import { createContext, useContext } from "react";
import { Check, ChevronDown, ChevronUp, MessageSquareText, PanelRight, Sparkles, X } from "lucide-react";

import type { ContextDrawerTab } from "./ContextDrawer";
import { MovedFromLabel } from "./MovedFromLabel";
import { reviewScopeTone, targetAttention, targetDisplayLabel } from "./readerModel";
import type { MarkState, ReviewTarget } from "./types";

interface ReviewTargetHeaderProps {
  target: ReviewTarget;
  active: boolean;
  busy: boolean;
  scopeTop?: boolean;
  comparisonMode?: boolean;
  /** Only sticky copies pass this; it keeps file identity visible while the inline header has scrolled away. */
  stickyPath?: string;
  /**
   * A pointer-only duplicate of the inline header (the sticky copy). Its
   * controls leave the tab order; the caller hides it from assistive tech, so
   * each action is announced once per part, by the inline header.
   */
  visualCopy?: boolean;
  /** The part's signature line when the diff cannot show it (too far above the change). */
  signature?: { line: number; text: string } | null;
  folded?: boolean;
  onToggleFold?(): void;
  onFocus(): void;
  onMark(state: MarkState, advance?: boolean): void;
  onComment(): void;
  onAsk?(): void;
  onContext(tab?: ContextDrawerTab): void;
}

function stateLabel(target: ReviewTarget): string {
  if (target.state === "reviewed") return "Reviewed; this part is unchanged since your judgment.";
  if (target.state === "changed_since_review") return "Changed since you reviewed; your earlier judgment no longer covers this part.";
  if (target.state === "needs_changes") return "Needs changes; you asked for this part to be changed.";
  if (target.state === "unknown") return "Review state uncertain; LemonCrow could not safely carry your earlier judgment forward.";
  return "Not reviewed yet.";
}

function stateDot(target: ReviewTarget): string {
  if (target.state === "reviewed") return "bg-ok-solid";
  if (target.state === "changed_since_review") return "bg-p2-solid";
  if (target.state === "needs_changes") return "bg-p0-solid";
  if (target.state === "unknown") return "bg-p1-solid";
  return "bg-muted-foreground/60";
}

function spanRange(spans: ReviewTarget["spans"]): string {
  if (spans.length === 0) return "";
  const start = Math.min(...spans.map((span) => span.start_line));
  const end = Math.max(...spans.map((span) => span.end_line));
  return start === end ? String(start) : `${start}–${end}`;
}

function linesWord(range: string): string {
  return range.includes("–") || range.includes(",") ? `lines ${range}` : `line ${range}`;
}

/** What kind of change the part holds, in plain words. */
function changeWord(target: ReviewTarget, regions: number): string {
  const tone = reviewScopeTone(target);
  const scope = target.start_line > 0 && target.end_line >= target.start_line ? target.end_line - target.start_line + 1 : 0;
  const word = tone === "modified"
    ? "modified"
    : tone === "added"
      ? (target.kind === "symbol" && scope > 0 && target.deletions === 0 && target.additions >= scope ? "new" : "lines added")
      : (target.kind === "symbol" && scope > 0 && target.additions === 0 && target.deletions >= scope ? "removed" : "lines removed");
  return regions > 1 ? `${word} in ${regions} places` : word;
}

/**
 * One compact summary for every part: "lines 1–5 · modified". The range is the
 * part's own scope for a symbol (old-side numbers for a removal), the changed
 * regions otherwise; a hunk whose label already names its lines skips it.
 */
export function partSummary(target: ReviewTarget): string {
  const oldSpans = target.spans.filter((span) => span.side === "old");
  const newSpans = target.spans.filter((span) => span.side === "new");
  const regionSpans = newSpans.length > 0 ? newSpans : oldSpans;
  const regions = Math.max(oldSpans.length, newSpans.length);
  let range = "";
  if ((target.kind === "symbol" || target.kind === "document_section") && target.start_line > 0) {
    range = target.end_line > target.start_line ? `${target.start_line}–${target.end_line}` : String(target.start_line);
  } else if (regionSpans.length > 3) {
    range = "";
  } else if (regionSpans.length > 1) {
    range = regionSpans.map((span) => spanRange([span])).join(", ");
  } else if (regionSpans.length === 1) {
    range = spanRange(regionSpans);
  } else if (target.start_line > 0) {
    range = target.end_line > target.start_line ? `${target.start_line}–${target.end_line}` : String(target.start_line);
  }
  const label = targetDisplayLabel(target);
  const lines = range && !label.endsWith(range) ? linesWord(range) : "";
  // A hunk's label already counts its places ("Other changed lines · 3 places").
  const change = target.spans.length > 0 || target.additions > 0 || target.deletions > 0
    ? changeWord(target, target.kind === "hunk" ? 1 : regions)
    : "";
  return [lines, change].filter(Boolean).join(" · ");
}

/** Why the review is read-only ("" when it is not); disabled judgment controls show it as their tooltip. */
export const ReviewLockReason = createContext("");

export default function ReviewTargetHeader({
  target,
  active,
  busy,
  scopeTop = false,
  comparisonMode = false,
  stickyPath = "",
  visualCopy = false,
  signature = null,
  folded = false,
  onToggleFold,
  onFocus,
  onMark,
  onComment,
  onAsk,
  onContext,
}: ReviewTargetHeaderProps) {
  const lock = useContext(ReviewLockReason);
  // The amber line names why the queue put this area in Review first -- the
  // canonical classification's reason, never a ranker descriptor such as a
  // caller count -- so it matches the outline and the Next card.
  const attention = targetAttention(target);
  const reason = attention.attention === "review_first" ? attention.reason_label : "";
  const reasonTab: ContextDrawerTab = attention.reason_code.startsWith("check_")
    ? "checks"
    : attention.reason_code !== "changed_since_judgment"
      ? "discussion"
      : "activity";
  const label = targetDisplayLabel(target);
  const stickyFilename = stickyPath ? stickyPath.split("/").pop() ?? stickyPath : "";
  const showStickyFilename = Boolean(stickyFilename && label !== stickyPath && label !== stickyFilename);
  const coverage = partSummary(target);
  const tabIndex = visualCopy ? -1 : undefined;
  const scopeTone = reviewScopeTone(target);
  // The per-hunk strip is the canonical hunk header: neutral strip, mono label, and a 3px rail that carries
  // state only (judgment first, otherwise the kind of change). Badges repeat it in words.
  const scopeToneClass = scopeTone === "added"
    ? "border-border border-l-rail border-l-ok-solid bg-muted hover:bg-accent/60"
    : scopeTone === "deleted"
      ? "border-border border-l-rail border-l-p0-solid bg-muted hover:bg-accent/60"
      : "border-border border-l-rail border-l-p2-solid bg-muted hover:bg-accent/60";
  const judgmentToneClass = target.state === "reviewed"
    ? "border-border border-l-rail border-l-ok-solid bg-muted"
    : target.state === "needs_changes"
      ? "border-border border-l-rail border-l-p0-solid bg-muted"
      : target.state === "changed_since_review"
        ? "border-border border-l-rail border-l-p2-solid bg-muted"
        : target.state === "unknown"
          ? "border-border border-l-rail border-l-p1-solid bg-muted"
          : "";
  const stateBadge = target.state === "reviewed"
    ? { label: "Reviewed", title: "Still reviewed; this part did not change after your judgment.", tone: "border-ok-line bg-ok-bg text-ok" }
    : target.state === "needs_changes"
      ? { label: "Needs changes", title: "You asked for this part to be changed.", tone: "border-p0-line bg-p0-bg text-p0" }
      : target.state === "changed_since_review"
        ? { label: "Changed · re-review", title: "Changed since you reviewed; your earlier judgment no longer covers this part.", tone: "border-p2-line bg-p2-bg text-p2" }
        : target.state === "unknown"
          ? { label: "State uncertain", title: "LemonCrow could not safely carry your earlier judgment forward; review this part again.", tone: "border-p1-line bg-p1-bg text-p1" }
          : null;
  const stateExplanation = active && target.state === "changed_since_review"
    ? "Changed since you reviewed · your earlier judgment no longer covers this part."
    : active && target.state === "unknown"
      ? "Review state uncertain · LemonCrow could not safely carry your earlier judgment forward."
      : "";
  const hasSecondary = Boolean(
    stateExplanation
    || reason
    || target.verification.fail > 0
    || target.verification.unknown > 0
    || target.annotation_counts.addressed_needs_rereview > 0,
  );

  if (folded && !comparisonMode) {
    const needsChanges = target.state === "needs_changes";
    const commentCount = target.annotation_counts.open + target.annotation_counts.addressed_needs_rereview;
    return (
      <div
        data-review-target-id={target.target_id}
        data-review-target-folded="true"
        role="group"
        aria-label={`${label} part · ${needsChanges ? "needs changes" : "reviewed"} · folded`}
        className={`review-target-header group/target my-0.5 flex min-h-7 items-center rounded-md border px-1.5 text-caption transition-colors duration-fast ${
          needsChanges
            ? "border-p0-line bg-p0-bg text-p0 hover:bg-p0-bg/70"
            : "border-border bg-muted text-muted-foreground hover:bg-accent/60 hover:text-foreground"
        }`}
      >
        <UiButton unstyled
          type="button"
          tabIndex={tabIndex}
          onClick={() => {
            onFocus();
            onToggleFold?.();
          }}
          aria-label={`Expand ${label}`}
          tooltip={needsChanges ? "Needs changes · click to inspect this part" : "Reviewed · click to inspect this part"}
          className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 rounded px-1 py-0.5 text-left"
        >
          {needsChanges
            ? <X size={14} className="shrink-0 text-p0" />
            : <Check size={14} className="shrink-0 text-ok" />}
          {showStickyFilename && (
            <>
              <span className="max-w-36 shrink-0 truncate font-mono text-caption font-normal text-muted-foreground" title={stickyPath}>{stickyFilename}</span>
              <span className="shrink-0 text-muted-foreground/60">·</span>
            </>
          )}
          <span className="review-target-name font-mono font-medium text-foreground">{label}</span>
          <span className="shrink-0">
            · {needsChanges ? "Needs changes" : "Reviewed"}
          </span>
          {commentCount > 0 && <span className="shrink-0 text-muted-foreground">· {commentCount} comment{commentCount === 1 ? "" : "s"}</span>}
          {coverage && <span className="text-muted-foreground">· {coverage}</span>}
          <ChevronDown size={14} className="ml-auto shrink-0 text-muted-foreground group-hover/target:text-foreground" />
        </UiButton>
      </div>
    );
  }

  return (
    <div
      data-review-target-id={target.target_id}
      role="group"
      aria-label={`${label} part`}
      className={[
        "review-target-header",
        scopeTop
          ? "group/target my-0.5 flex min-h-8 items-start gap-2 rounded-md border px-2 py-1 text-caption transition-[border-color,background-color] duration-fast ease-out"
          : "group/target my-0.5 flex min-h-8 items-start gap-2 rounded-md border px-2 py-1 text-caption transition-colors duration-fast",
        judgmentToneClass
          || (scopeTop
            ? scopeToneClass
            : active
              ? "border-input bg-accent/50"
              : "border-border bg-muted hover:bg-accent/60"),
      ].join(" ")}
      onFocus={onFocus}
    >
      <div className="min-w-0 flex-1 text-left">
        {/* The name and summary wrap rather than truncate: the reader needs both whole.
            The actions sit level with this first row (the header aligns to the top), so
            rows that appear below it on focus never move a button out from under a click. */}
        <div className="review-target-label flex min-h-6 min-w-0 flex-wrap items-center gap-x-1.5">
          {!comparisonMode && (
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${stateDot(target)}`}
              title={stateLabel(target)}
              aria-hidden="true"
            />
          )}
          {showStickyFilename && (
            <>
              <span className="max-w-36 shrink-0 truncate font-mono text-caption text-muted-foreground" title={stickyPath}>{stickyFilename}</span>
              <span className="shrink-0 text-muted-foreground/60">·</span>
            </>
          )}
          <UiButton unstyled
            type="button"
            tabIndex={tabIndex}
            onClick={onFocus}
            aria-current={active ? "true" : undefined}
            className="min-w-0 text-left [overflow-wrap:anywhere]"
          >
            <span className="review-target-name font-mono font-medium text-foreground">
              {label}
            </span>
          </UiButton>
          {coverage && (
            <span className="review-target-coverage">
              · {coverage}
            </span>
          )}
          {!comparisonMode && stateBadge && (
            <span
              className={`inline-flex h-5 shrink-0 items-center rounded-sm border px-1.5 font-mono text-tag font-medium uppercase leading-none ${stateBadge.tone}`}
              title={stateBadge.title}
              data-testid="target-state-badge"
            >
              {stateBadge.label}
            </span>
          )}
          {!comparisonMode && target.movement === "base_only" && (
            <span
              className="inline-flex h-5 shrink-0 items-center rounded-sm border border-border px-1.5 font-mono text-tag font-medium uppercase leading-none text-muted-foreground"
              title="Only the base moved under this part (rebase or merge-base). The authored change is equivalent, so your judgment was kept."
              data-testid="target-base-only"
            >
              Base moved
            </span>
          )}
          <MovedFromLabel target={target} />
          {!comparisonMode && <span className="sr-only">{target.state.replaceAll("_", " ")}</span>}
        </div>

        {signature && (
          <div className="review-target-signature mt-0.5 flex min-w-0 items-baseline gap-2 font-mono text-caption" data-testid="target-signature">
            <span className="shrink-0 tabular-nums text-muted-foreground" aria-label={`line ${signature.line}`}>{signature.line}</span>
            <code className="min-w-0 text-foreground [overflow-wrap:anywhere]">{signature.text.trim()}</code>
          </div>
        )}

        {!comparisonMode && hasSecondary && (
          <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 text-caption text-muted-foreground">
            {stateExplanation && <span className="min-w-0 text-muted-foreground">{stateExplanation}</span>}
            {reason && (
              <UiButton unstyled
                type="button"
                tabIndex={tabIndex}
                onClick={() => onContext(reasonTab)}
                className="min-w-0 text-left text-p1 hover:text-foreground"
              >
                {reason}
              </UiButton>
            )}
            {(target.verification.fail > 0 || target.verification.unknown > 0) && (
              <UiButton unstyled type="button" tabIndex={tabIndex} onClick={() => onContext("checks")} className="shrink-0 text-p0 hover:text-foreground">
                {target.verification.fail > 0
                  ? `${target.verification.fail} failed`
                  : `${target.verification.unknown} unknown`}
              </UiButton>
            )}
            {target.annotation_counts.addressed_needs_rereview > 0 && (
              <UiButton unstyled type="button" tabIndex={tabIndex} onClick={() => onContext("discussion")} className="shrink-0 text-p2 hover:text-foreground">
                marked addressed · re-review
              </UiButton>
            )}
          </div>
        )}
      </div>

      {!comparisonMode && <div className={`review-target-actions flex shrink-0 items-center transition-opacity ${active || target.state !== "unreviewed" ? "opacity-100" : "opacity-45 pointer-coarse:opacity-100 max-sm:opacity-100 group-hover/target:opacity-100 group-focus-within/target:opacity-100"}`}>
        {(target.state === "reviewed" || target.state === "needs_changes") && onToggleFold && (
          <UiButton
            variant="ghost"
            size="icon"
            type="button"
            tabIndex={tabIndex}
            aria-label={`Fold ${label}`}
            tooltip="Fold completed part"
            onClick={onToggleFold}
            className="h-6 w-6 text-muted-foreground hover:text-foreground"
          >
            <ChevronUp size={14} />
          </UiButton>
        )}
        <PartMarkButtons target={target} label={label} busy={busy} tabIndex={tabIndex} onMark={onMark} />
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          tabIndex={tabIndex}
          aria-label={`Comment on ${label}`}
          tooltip={lock || "Add comment"}
          disabled={busy}
          onClick={onComment}
          className="h-6 w-6 hover:text-foreground"
        >
          <MessageSquareText size={14} />
        </UiButton>
        {onAsk && (
          <UiButton
            variant="ghost"
            size="compact"
            type="button"
            tabIndex={tabIndex}
            aria-label={`Ask about ${label}`}
            tooltip={lock || "Ask about this code"}
            disabled={busy}
            onClick={onAsk}
            className="h-6 gap-1 px-1.5 text-meta"
          >
            <Sparkles size={14} />
            Ask
          </UiButton>
        )}
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          tabIndex={tabIndex}
          aria-label={`Open context for ${label}`}
          tooltip="Open context"
          onClick={() => onContext("context")}
          className="h-6 w-6"
        >
          <PanelRight size={14} />
        </UiButton>
      </div>}
    </div>
  );
}

/**
 * The two judgment buttons, shared by the part header and the end-of-part
 * shortcut. Both carry visible text: a touch screen has no hover for a
 * tooltip, and "Needs changes" keeps both words at every width so it never
 * reads as "show changes".
 */
export function PartMarkButtons({
  target,
  label,
  busy,
  tabIndex,
  onMark,
}: {
  target: ReviewTarget;
  label: string;
  busy: boolean;
  tabIndex?: number;
  onMark(state: MarkState, advance?: boolean): void;
}) {
  const lock = useContext(ReviewLockReason);
  return (
    <>
      <UiButton
        variant="outline"
        size="compact"
        type="button"
        tabIndex={tabIndex}
        aria-label={`Mark ${label} reviewed`}
        aria-pressed={target.state === "reviewed"}
        data-mark="reviewed"
        tooltip={lock || "Mark reviewed"}
        disabled={busy || target.state === "reviewed"}
        onClick={() => onMark("reviewed", true)}
        className={target.state === "reviewed"
          ? "mr-1 h-6 max-sm:h-10 border-ok-line bg-ok-bg text-ok disabled:opacity-100 aria-disabled:opacity-100"
          : "mr-1 h-6 max-sm:h-10 text-muted-foreground hover:border-ok-line hover:bg-ok-bg hover:text-ok"}
      >
        <Check size={14} />
        <span data-mark-label>Reviewed</span>
      </UiButton>
      <UiButton
        variant="outline"
        size="compact"
        type="button"
        tabIndex={tabIndex}
        aria-label={`Mark ${label} needs changes`}
        aria-pressed={target.state === "needs_changes"}
        data-mark="needs_changes"
        tooltip={lock || "Needs changes"}
        disabled={busy}
        onClick={() => onMark("needs_changes")}
        className={target.state === "needs_changes"
          ? "mr-1 h-6 max-sm:h-10 whitespace-nowrap border-p0-line bg-p0-bg text-p0"
          : "mr-1 h-6 max-sm:h-10 whitespace-nowrap text-muted-foreground hover:border-p0-line hover:bg-p0-bg hover:text-p0"}
      >
        <X size={14} />
        <span data-mark-label>Needs changes</span>
      </UiButton>
    </>
  );
}
