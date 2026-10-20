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
    ? { label: "Reviewed", title: "Still reviewed; this review area did not change after your judgment.", tone: "border-ok-line bg-ok-bg text-ok" }
    : target.state === "needs_changes"
      ? { label: "Needs changes", title: "You asked for this review area to be changed.", tone: "border-p0-line bg-p0-bg text-p0" }
      : target.state === "changed_since_review"
        ? { label: "Changed · re-review", title: "Changed since you reviewed; your earlier judgment no longer covers this review area.", tone: "border-p2-line bg-p2-bg text-p2" }
        : target.state === "unknown"
          ? { label: "State uncertain", title: "LemonCrow could not safely carry your earlier judgment forward; review this area again.", tone: "border-p1-line bg-p1-bg text-p1" }
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
        className={`review-target-header group/target my-0.5 flex min-h-7 items-center rounded-md border px-1.5 text-caption transition-colors duration-fast ${
          needsChanges
            ? "border-p0-line bg-p0-bg text-p0 hover:bg-p0-bg/70"
            : "border-border bg-muted text-muted-foreground hover:bg-accent/60 hover:text-foreground"
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
            ? <X size={12} strokeWidth={1.8} className="shrink-0 text-p0" />
            : <Check size={12} strokeWidth={1.8} className="shrink-0 text-ok" />}
          {showStickyFilename && (
            <>
              <span className="max-w-36 shrink-0 truncate font-mono text-caption font-normal text-muted-foreground" title={stickyPath}>{stickyFilename}</span>
              <span className="shrink-0 text-muted-foreground/60">·</span>
            </>
          )}
          <span className="hunk-label font-mono font-medium text-foreground">{label}</span>
          <span className="shrink-0">
            · {needsChanges ? "Needs changes" : "Reviewed"}
          </span>
          {commentCount > 0 && <span className="shrink-0 text-muted-foreground">· {commentCount} comment{commentCount === 1 ? "" : "s"}</span>}
          {coverage && <span className="min-w-0 truncate text-muted-foreground">· {coverage}</span>}
          <ChevronDown size={12} className="ml-auto shrink-0 text-muted-foreground group-hover/target:text-foreground" />
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
          ? "group/target my-0.5 flex min-h-8 items-center gap-2 rounded-md border px-2 py-1 text-caption transition-[border-color,background-color] duration-fast ease-out"
          : "group/target my-0.5 flex min-h-8 items-center gap-2 rounded-md border px-2 py-1 text-caption transition-colors duration-fast",
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
              <span className="max-w-36 shrink-0 truncate font-mono text-caption text-muted-foreground" title={stickyPath}>{stickyFilename}</span>
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
            <span className="hunk-label font-mono font-medium text-foreground">
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
              className={`inline-flex h-5 shrink-0 items-center rounded-sm border px-1.5 font-mono text-tag font-medium uppercase leading-none ${stateBadge.tone}`}
              title={stateBadge.title}
            >
              {stateBadge.label}
            </span>
          )}
          {!comparisonMode && target.movement === "base_only" && (
            <span
              className="inline-flex h-5 shrink-0 items-center rounded-sm border border-border px-1.5 font-mono text-tag font-medium uppercase leading-none text-muted-foreground"
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
                className="min-w-0 truncate text-p1 hover:text-foreground"
                tooltip={reason}
              >
                {reason}
              </UiButton>
            )}
            {(target.verification.fail > 0 || target.verification.unknown > 0) && (
              <UiButton unstyled type="button" onClick={() => onContext("checks")} className="shrink-0 text-p0 hover:text-foreground">
                {target.verification.fail > 0
                  ? `${target.verification.fail} failed`
                  : `${target.verification.unknown} unknown`}
              </UiButton>
            )}
            {target.annotation_counts.addressed_needs_rereview > 0 && (
              <UiButton unstyled type="button" onClick={() => onContext("discussion")} className="shrink-0 text-p2 hover:text-foreground">
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
            className="h-6 w-6 text-muted-foreground hover:text-foreground"
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
            ? "h-6 w-6 border-ok-line bg-ok-bg text-ok disabled:opacity-100"
            : "h-6 w-6 hover:bg-ok-bg hover:text-ok"}
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
            ? "h-6 w-6 border-p0-line bg-p0-bg text-p0"
            : "h-6 w-6 hover:bg-p0-bg hover:text-p0"}
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
          className="h-6 w-6 hover:text-foreground"
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
            className="h-6 gap-1 px-1.5 text-meta"
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
