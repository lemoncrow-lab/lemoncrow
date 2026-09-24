import { Check, MessageSquareText, PanelRight, X } from "lucide-react";

import type { ContextDrawerTab } from "./ContextDrawer";
import { reviewScopeTone, targetDisplayLabel } from "./readerModel";
import type { MarkState, ReviewTarget } from "./types";

interface ReviewTargetHeaderProps {
  target: ReviewTarget;
  active: boolean;
  busy: boolean;
  scopeTop?: boolean;
  comparisonMode?: boolean;
  onFocus(): void;
  onMark(state: MarkState, advance?: boolean): void;
  onComment(): void;
  onContext(tab?: ContextDrawerTab): void;
}

function stateLabel(target: ReviewTarget): string {
  if (target.state === "reviewed") return "Reviewed; this target is unchanged since your judgment.";
  if (target.state === "changed_since_review") return "Changed since you reviewed; your earlier judgment no longer covers this target.";
  if (target.state === "needs_changes") return "Needs changes; you asked for this target to be changed.";
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
  if (target.kind === "symbol" && target.start_line > 0) {
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

const GENERIC_INLINE_REASONS = new Set([
  "substantial new production surface",
  "symbol added",
  "symbol modified",
  "symbol deleted",
  "symbol unknown",
  "generated/vendor — skim",
  "generated/vendor -- skim",
  "generated/vendor - skim",
]);

export default function ReviewTargetHeader({
  target,
  active,
  busy,
  scopeTop = false,
  comparisonMode = false,
  onFocus,
  onMark,
  onComment,
  onContext,
}: ReviewTargetHeaderProps) {
  const reason = target.attention_level === "high"
    ? target.reasons.find((item) => (
      !/^\+\d+\s+-\d+$/.test(item.trim())
      && !GENERIC_INLINE_REASONS.has(item.trim())
    ))
    : undefined;
  const label = targetDisplayLabel(target);
  const coverage = coverageLabel(target);
  const scopeTone = reviewScopeTone(target);
  const scopeToneClass = scopeTone === "added"
    ? "border-emerald-700/45 bg-emerald-950/10 hover:border-emerald-600/55 hover:bg-emerald-950/15"
    : scopeTone === "deleted"
      ? "border-rose-700/45 bg-rose-950/10 hover:border-rose-600/55 hover:bg-rose-950/15"
      : "border-sky-700/35 bg-sky-950/10 hover:border-sky-600/45 hover:bg-sky-950/12";
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
    ? { label: "Reviewed", title: "Still reviewed; this target did not change after your judgment.", tone: "border-emerald-800/70 bg-emerald-950/45 text-emerald-200" }
    : target.state === "needs_changes"
      ? { label: "Needs changes", title: "You asked for this target to be changed.", tone: "border-rose-800/70 bg-rose-950/45 text-rose-200" }
      : target.state === "changed_since_review"
        ? { label: "Changed · re-review", title: "Changed since you reviewed; your earlier judgment no longer covers this target.", tone: "border-sky-800/70 bg-sky-950/45 text-sky-200" }
        : target.state === "unknown"
          ? { label: "State uncertain", title: "LemonCrow could not safely carry your earlier judgment forward; review this target again.", tone: "border-amber-800/70 bg-amber-950/45 text-amber-200" }
          : null;
  const stateExplanation = active && target.state === "changed_since_review"
    ? "Changed since you reviewed · your earlier judgment no longer covers this target."
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
  return (
    <div
      data-review-target-id={target.target_id}
      role="group"
      aria-label={`${label} review target`}
      className={[
        "review-target-header",
        scopeTop
          ? "group/target my-0 flex min-h-8 items-center gap-2 rounded-t-md rounded-b-none border px-2 py-1 text-[10px] transition-[border-color,background-color] duration-150 ease-out"
          : "group/target my-0.5 flex min-h-8 items-center gap-2 rounded-md border px-2 py-1 text-[10px] transition-colors",
        judgmentToneClass
          || (scopeTop
            ? scopeToneClass
            : active
              ? "border-sky-900/60 bg-sky-950/12"
              : "border-neutral-900 bg-neutral-950/45 hover:border-neutral-800 hover:bg-neutral-900/25"),
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
          <button
            type="button"
            onClick={onFocus}
            aria-current={active ? "true" : undefined}
            aria-label={`${label} ${target.kind}`}
            className="min-w-0 truncate text-left"
          >
            <span className={`font-mono font-medium ${target.state === "reviewed" ? "text-emerald-200" : "text-neutral-200"}`}>
              {label}
            </span>
          </button>
          {coverage && (
            <span className="review-target-coverage" title={coverage}>
              · {coverage}
            </span>
          )}
          {!comparisonMode && stateBadge && (
            <span
              className={`inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 text-[9px] font-semibold ${stateBadge.tone}`}
              title={stateBadge.title}
            >
              {stateBadge.label}
            </span>
          )}
          {!comparisonMode && <span className="sr-only">{target.state.replaceAll("_", " ")}</span>}
        </div>

        {!comparisonMode && hasSecondary && (
          <div className="mt-0.5 flex min-w-0 items-center gap-2 truncate text-[10px] text-neutral-600">
            {stateExplanation && <span className="min-w-0 truncate text-neutral-500" title={stateExplanation}>{stateExplanation}</span>}
            {reason && (
              <button
                type="button"
                onClick={() => onContext("impact")}
                className="min-w-0 truncate text-amber-400/75 hover:text-amber-300"
                title={reason}
              >
                {reason}
              </button>
            )}
            {(target.verification.fail > 0 || target.verification.unknown > 0) && (
              <button type="button" onClick={() => onContext("checks")} className="shrink-0 text-rose-300/80 hover:text-rose-200">
                {target.verification.fail > 0
                  ? `${target.verification.fail} failed`
                  : `${target.verification.unknown} unknown`}
              </button>
            )}
            {target.annotation_counts.addressed_needs_rereview > 0 && (
              <button type="button" onClick={() => onContext("discussion")} className="shrink-0 text-sky-300 hover:text-sky-200">
                marked addressed · re-review
              </button>
            )}
          </div>
        )}
      </div>

      {!comparisonMode && <div className={`flex shrink-0 items-center transition-opacity ${active || target.state !== "unreviewed" ? "opacity-100" : "opacity-45 group-hover/target:opacity-100 group-focus-within/target:opacity-100"}`}>
        <button
          type="button"
          aria-label={`Mark ${label} reviewed`}
          title="Mark reviewed"
          disabled={busy || target.state === "reviewed"}
          onClick={() => onMark("reviewed", true)}
          className={target.state === "reviewed"
            ? "review-icon-button h-6 w-6 border-emerald-600/70 bg-emerald-900/40 text-emerald-100 disabled:opacity-100"
            : "review-icon-button h-6 w-6 hover:border-emerald-800/60 hover:bg-emerald-950/30 hover:text-emerald-300"}
        >
          <Check size={13} strokeWidth={1.8} />
          {target.state === "reviewed" && <span className="sr-only">✓ Reviewed</span>}
        </button>
        <button
          type="button"
          aria-label={`Mark ${label} needs changes`}
          title="Needs changes"
          disabled={busy}
          onClick={() => onMark("needs_changes")}
          className={target.state === "needs_changes"
            ? "review-icon-button h-6 w-6 border-rose-600/70 bg-rose-900/40 text-rose-100"
            : "review-icon-button h-6 w-6 hover:border-rose-800/60 hover:bg-rose-950/30 hover:text-rose-300"}
        >
          <X size={13} strokeWidth={1.8} />
        </button>
        <button
          type="button"
          aria-label={`Comment on ${label}`}
          title="Add comment"
          disabled={busy}
          onClick={onComment}
          className="review-icon-button h-6 w-6 hover:text-sky-300"
        >
          <MessageSquareText size={12} strokeWidth={1.8} />
        </button>
        <button
          type="button"
          aria-label={`Open context for ${label}`}
          title="Open context"
          onClick={() => onContext("impact")}
          className="review-icon-button h-6 w-6"
        >
          <PanelRight size={12} strokeWidth={1.8} />
        </button>
      </div>}
    </div>
  );
}
