import { Button as UiButton } from "../components/ui/button";
import { createContext, useContext } from "react";
import { Check, ChevronDown, ChevronUp, MessageSquareText, PanelRight, Sparkles, X } from "lucide-react";

import type { ContextDrawerTab } from "./ContextDrawer";
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
  folded?: boolean;
  onToggleFold?(): void;
  onFocus(): void;
  onMark(state: MarkState, advance?: boolean): void;
  onComment(): void;
  onAsk?(): void;
  onContext(tab?: ContextDrawerTab): void;
}

function stateLabel(target: ReviewTarget): string {
  if (target.state === "reviewed") return "Reviewed; this review area is unchanged since your judgment.";
  if (target.state === "changed_since_review") return "Changed since you reviewed; your earlier judgment no longer covers this review area.";
  if (target.state === "needs_changes") return "Needs changes; you asked for this review area to be changed.";
  if (target.state === "unknown") return "Review state uncertain; LemonCrow could not safely carry your earlier judgment forward.";
  return "Not reviewed yet.";
}

function stateDot(target: ReviewTarget): string {
  if (target.state === "reviewed") return "bg-emerald-400";
  if (target.state === "changed_since_review") return "bg-sky-400";
  if (target.state === "needs_changes") return "bg-rose-400";
  if (target.state === "unknown") return "bg-amber-400";
  return "bg-neutral-600";
}

function spanRange(spans: ReviewTarget["spans"]): string {
  if (spans.length === 0) return "";
  const start = Math.min(...spans.map((span) => span.start_line));
  const end = Math.max(...spans.map((span) => span.end_line));
  return start === end ? String(start) : `${start}–${end}`;
}

function coverageLabel(target: ReviewTarget): string {
  const oldSpans = target.spans.filter((span) => span.side === "old");
  const newSpans = target.spans.filter((span) => span.side === "new");
  if (oldSpans.length > 0 && newSpans.length > 0) {
    const regions = Math.max(oldSpans.length, newSpans.length);
    return `modified · old ${spanRange(oldSpans)} · new ${spanRange(newSpans)}${regions > 1 ? ` · ${regions} changed regions` : ""}`;
  }

  const regions = newSpans.length > 0 ? newSpans : oldSpans;
  if (regions.length === 0) {
    const start = target.start_line;
    const end = target.end_line;
    if (start <= 0) return "";
    return end > start ? `covers lines ${start}–${end}` : `covers line ${start}`;
  }
  if ((target.kind === "symbol" || target.kind === "document_section") && target.start_line > 0) {
    const scope = target.end_line > target.start_line
      ? `${target.start_line}–${target.end_line}`
      : String(target.start_line);
    return regions.length > 1
      ? `covers lines ${scope} · ${regions.length} changed regions`
      : `covers line${scope.includes("–") ? "s" : ""} ${scope}`;
  }
  if (target.kind === "hunk" && regions.length > 1) {
    const labels = regions.map((span) => spanRange([span]));
    return labels.length <= 3
      ? `covers lines ${labels.join(", ")}`
      : `covers ${labels.length} changed regions`;
  }
  const range = spanRange(regions);
  return regions.length > 1
    ? `covers lines ${range} · ${regions.length} changed regions`
    : `covers line${range.includes("–") ? "s" : ""} ${range}`;
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
  const coverage = coverageLabel(target);
  const scopeTone = reviewScopeTone(target);
  const scopeToneClass = scopeTone === "added"
    ? "border-border/80 bg-emerald-950/8 shadow-[inset_2px_0_0_rgba(52,211,153,0.42)] hover:bg-emerald-950/12"
    : scopeTone === "deleted"
      ? "border-border/80 bg-rose-950/8 shadow-[inset_2px_0_0_rgba(251,113,133,0.42)] hover:bg-rose-950/12"
      : "border-border/80 bg-sky-950/8 shadow-[inset_2px_0_0_rgba(56,189,248,0.38)] hover:bg-sky-950/10";
  const judgmentToneClass = target.state === "reviewed"
    ? "border-emerald-700/60 bg-emerald-950/15 shadow-[inset_3px_0_0_rgba(52,211,153,0.5)]"
    : target.state === "needs_changes"
      ? "border-rose-700/60 bg-rose-950/15 shadow-[inset_3px_0_0_rgba(251,113,133,0.5)]"
      : target.state === "changed_since_review"
        ? "border-sky-700/55 bg-sky-950/15 shadow-[inset_3px_0_0_rgba(56,189,248,0.45)]"
        : target.state === "unknown"
          ? "border-amber-700/55 bg-amber-950/15 shadow-[inset_3px_0_0_rgba(251,191,36,0.45)]"
          : "";
  const stateBadge = target.state === "reviewed"
    ? { label: "Reviewed", title: "Still reviewed; this review area did not change after your judgment.", tone: "border-emerald-800/70 bg-emerald-950/45 text-emerald-200" }
    : target.state === "needs_changes"
      ? { label: "Needs changes", title: "You asked for this review area to be changed.", tone: "border-rose-800/70 bg-rose-950/45 text-rose-200" }
      : target.state === "changed_since_review"
        ? { label: "Changed · re-review", title: "Changed since you reviewed; your earlier judgment no longer covers this review area.", tone: "border-sky-800/70 bg-sky-950/45 text-sky-200" }
        : target.state === "unknown"
          ? { label: "State uncertain", title: "LemonCrow could not safely carry your earlier judgment forward; review this area again.", tone: "border-amber-800/70 bg-amber-950/45 text-amber-200" }
          : null;
  const stateExplanation = active && target.state === "changed_since_review"
    ? "Changed since you reviewed · your earlier judgment no longer covers this review area."
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
        aria-label={`${label} review area · ${needsChanges ? "needs changes" : "reviewed"} · folded`}
        className={`review-target-header group/target my-0.5 flex min-h-7 items-center rounded border px-1.5 text-caption transition-colors ${
          needsChanges
            ? "border-rose-900/45 bg-rose-950/8 text-rose-200/85 hover:bg-rose-950/15"
            : "border-neutral-900 bg-background/40 text-muted-foreground hover:bg-muted/35 hover:text-neutral-300"
        }`}
      >
        <UiButton unstyled
          type="button"
          onClick={() => {
            onFocus();
            onToggleFold?.();
          }}
          aria-label={`Expand ${label}`}
          tooltip={needsChanges ? "Needs changes · click to inspect this review area" : "Reviewed · click to inspect this review area"}
          className="flex min-w-0 flex-1 items-center gap-1.5 rounded px-1 py-0.5 text-left"
        >
          {needsChanges
            ? <X size={11} strokeWidth={1.8} className="shrink-0 text-rose-400/85" />
            : <Check size={11} strokeWidth={1.8} className="shrink-0 text-emerald-400/75" />}
          {showStickyFilename && (
            <>
              <span className="max-w-36 shrink-0 truncate font-mono text-micro font-normal text-muted-foreground" title={stickyPath}>{stickyFilename}</span>
              <span className="shrink-0 text-muted-foreground/60">·</span>
            </>
          )}
          <span className="min-w-0 truncate font-mono font-medium text-neutral-300">{label}</span>
          <span className={needsChanges ? "shrink-0 text-rose-300/75" : "shrink-0 text-muted-foreground"}>
            · {needsChanges ? "Needs changes" : "Reviewed"}
          </span>
          {commentCount > 0 && <span className="shrink-0 text-muted-foreground">· {commentCount} comment{commentCount === 1 ? "" : "s"}</span>}
          {coverage && <span className="min-w-0 truncate text-muted-foreground">· {coverage}</span>}
          <ChevronDown size={12} className="ml-auto shrink-0 text-muted-foreground group-hover/target:text-neutral-400" />
        </UiButton>
      </div>
    );
  }

  return (
    <div
      data-review-target-id={target.target_id}
      role="group"
      aria-label={`${label} review area`}
      className={[
        "review-target-header",
        scopeTop
          ? "group/target my-0.5 flex min-h-8 items-center gap-2 rounded-md border px-2 py-1 text-caption transition-[border-color,background-color] duration-150 ease-out"
          : "group/target my-0.5 flex min-h-8 items-center gap-2 rounded-md border px-2 py-1 text-caption transition-colors",
        judgmentToneClass
          || (scopeTop
            ? scopeToneClass
            : active
              ? "border-sky-900/60 bg-sky-950/12"
              : "border-neutral-900 bg-background/45 hover:bg-muted/25"),
      ].join(" ")}
      onFocus={onFocus}
    >
      <div className="min-w-0 flex-1 text-left">
        <div className="review-target-label flex min-w-0 items-center gap-1.5">
          {!comparisonMode && (
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${stateDot(target)}`}
              title={stateLabel(target)}
              aria-hidden="true"
            />
          )}
          {showStickyFilename && (
            <>
              <span className="max-w-36 shrink-0 truncate font-mono text-micro text-muted-foreground" title={stickyPath}>{stickyFilename}</span>
              <span className="shrink-0 text-muted-foreground/60">·</span>
            </>
          )}
          <UiButton unstyled
            type="button"
            onClick={onFocus}
            aria-current={active ? "true" : undefined}
            aria-label={`${label} ${target.kind}`}
            className="min-w-0 truncate text-left"
          >
            <span className={`font-mono font-medium ${target.state === "reviewed" ? "text-emerald-200" : "text-neutral-200"}`}>
              {label}
            </span>
          </UiButton>
          {coverage && (
            <span className="review-target-coverage" title={coverage}>
              · {coverage}
            </span>
          )}
          {!comparisonMode && stateBadge && (
            <span
              className={`inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 text-micro font-semibold ${stateBadge.tone}`}
              title={stateBadge.title}
            >
              {stateBadge.label}
            </span>
          )}
          {!comparisonMode && target.movement === "base_only" && (
            <span
              className="inline-flex shrink-0 items-center rounded border border-border px-1.5 py-0.5 text-micro font-medium text-muted-foreground"
              title="Only the base moved under this review area (rebase or merge-base). The authored change is equivalent, so your judgment was kept."
              data-testid="target-base-only"
            >
              Base moved
            </span>
          )}
          {!comparisonMode && <span className="sr-only">{target.state.replaceAll("_", " ")}</span>}
        </div>

        {!comparisonMode && hasSecondary && (
          <div className="mt-0.5 flex min-w-0 items-center gap-2 truncate text-caption text-muted-foreground">
            {stateExplanation && <span className="min-w-0 truncate text-muted-foreground" title={stateExplanation}>{stateExplanation}</span>}
            {reason && (
              <UiButton unstyled
                type="button"
                onClick={() => onContext(reasonTab)}
                className="min-w-0 truncate text-amber-400/75 hover:text-amber-300"
                tooltip={reason}
              >
                {reason}
              </UiButton>
            )}
            {(target.verification.fail > 0 || target.verification.unknown > 0) && (
              <UiButton unstyled type="button" onClick={() => onContext("checks")} className="shrink-0 text-rose-300/80 hover:text-rose-200">
                {target.verification.fail > 0
                  ? `${target.verification.fail} failed`
                  : `${target.verification.unknown} unknown`}
              </UiButton>
            )}
            {target.annotation_counts.addressed_needs_rereview > 0 && (
              <UiButton unstyled type="button" onClick={() => onContext("discussion")} className="shrink-0 text-sky-300 hover:text-sky-200">
                marked addressed · re-review
              </UiButton>
            )}
          </div>
        )}
      </div>

      {!comparisonMode && <div className={`flex shrink-0 items-center transition-opacity ${active || target.state !== "unreviewed" ? "opacity-100" : "opacity-45 group-hover/target:opacity-100 group-focus-within/target:opacity-100"}`}>
        {(target.state === "reviewed" || target.state === "needs_changes") && onToggleFold && (
          <UiButton
            variant="ghost"
            size="icon"
            type="button"
            aria-label={`Fold ${label}`}
            tooltip="Fold completed review area"
            onClick={onToggleFold}
            className="h-6 w-6 text-muted-foreground hover:text-neutral-300"
          >
            <ChevronUp size={12} strokeWidth={1.8} />
          </UiButton>
        )}
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          aria-label={`Mark ${label} reviewed`}
          tooltip={lock || "Mark reviewed"}
          disabled={busy || target.state === "reviewed"}
          onClick={() => onMark("reviewed", true)}
          className={target.state === "reviewed"
            ? "h-6 w-6 border-emerald-600/70 bg-emerald-900/40 text-emerald-100 disabled:opacity-100"
            : "h-6 w-6 hover:bg-emerald-950/30 hover:text-emerald-300"}
        >
          <Check size={13} strokeWidth={1.8} />
          {target.state === "reviewed" && <span className="sr-only">✓ Reviewed</span>}
        </UiButton>
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          aria-label={`Mark ${label} needs changes`}
          tooltip={lock || "Needs changes"}
          disabled={busy}
          onClick={() => onMark("needs_changes")}
          className={target.state === "needs_changes"
            ? "h-6 w-6 border-rose-600/70 bg-rose-900/40 text-rose-100"
            : "h-6 w-6 hover:bg-rose-950/30 hover:text-rose-300"}
        >
          <X size={13} strokeWidth={1.8} />
        </UiButton>
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          aria-label={`Comment on ${label}`}
          tooltip={lock || "Add comment"}
          disabled={busy}
          onClick={onComment}
          className="h-6 w-6 hover:text-sky-300"
        >
          <MessageSquareText size={12} strokeWidth={1.8} />
        </UiButton>
        {onAsk && (
          <UiButton
            variant="ghost"
            size="compact"
            type="button"
            aria-label={`Ask about ${label}`}
            tooltip={lock || "Ask about this code"}
            disabled={busy}
            onClick={onAsk}
            className="h-6 gap-1 px-1.5 text-micro text-violet-300 hover:text-violet-200"
          >
            <Sparkles size={11} strokeWidth={1.8} />
            Ask
          </UiButton>
        )}
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          aria-label={`Open context for ${label}`}
          tooltip="Open context"
          onClick={() => onContext("context")}
          className="h-6 w-6"
        >
          <PanelRight size={12} strokeWidth={1.8} />
        </UiButton>
      </div>}
    </div>
  );
}
