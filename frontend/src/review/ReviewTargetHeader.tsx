import type { ContextDrawerTab } from "./ContextDrawer";
import { targetDisplayLabel } from "./readerModel";
import type { MarkState, ReviewTarget } from "./types";

interface ReviewTargetHeaderProps {
  target: ReviewTarget;
  active: boolean;
  busy: boolean;
  onFocus(): void;
  onMark(state: MarkState, advance?: boolean): void;
  onComment(): void;
  onContext(tab?: ContextDrawerTab): void;
}

function stateLabel(target: ReviewTarget): string {
  return target.state.replaceAll("_", " ");
}

export default function ReviewTargetHeader({
  target,
  active,
  busy,
  onFocus,
  onMark,
  onComment,
  onContext,
}: ReviewTargetHeaderProps) {
  const reason = target.attention_level === "high"
    ? target.reasons.find((item) => !/^\+\d+\s+-\d+$/.test(item.trim()))
    : undefined;
  const label = targetDisplayLabel(target);
  const stateTone = target.state === "reviewed"
    ? "text-emerald-400"
    : target.state === "changed_since_review"
      ? "text-sky-300"
      : target.state === "needs_changes"
        ? "text-rose-300"
        : target.state === "unknown"
          ? "text-amber-300"
          : "text-neutral-500";

  return (
    <div
      data-review-target-id={target.target_id}
      role="group"
      aria-label={`${label} review target`}
      className={[
        "my-1 flex items-center gap-2 border-y px-2.5 py-1.5 text-[10px]",
        active ? "border-sky-900/80 bg-sky-950/15" : "border-neutral-900 bg-neutral-950/85",
        target.state === "reviewed" ? "opacity-70" : "",
      ].join(" ")}
      onFocus={onFocus}
    >
      <div className="min-w-0 flex-1 text-left">
        <button type="button" onClick={onFocus} aria-current={active ? "true" : undefined} className="text-left">
          <span className="font-mono font-medium text-neutral-300">{label}</span>
          <span className="ml-2 text-neutral-700">{target.kind}</span>
        </button>
        {reason && (
          <button
            type="button"
            onClick={() => onContext("impact")}
            className="ml-2 text-amber-400/75 hover:text-amber-300"
          >
            ⚠ {reason}
          </button>
        )}
        {(target.verification.fail > 0 || target.verification.unknown > 0) && (
          <button
            type="button"
            onClick={() => onContext("checks")}
            className="ml-2 text-rose-300/80 hover:text-rose-200"
          >
            {target.verification.fail > 0
              ? `${target.verification.fail} failed check${target.verification.fail === 1 ? "" : "s"}`
              : `${target.verification.unknown} unknown check${target.verification.unknown === 1 ? "" : "s"}`}
          </button>
        )}
        {target.annotation_counts.addressed_needs_rereview > 0 && (
          <button
            type="button"
            onClick={() => onContext("discussion")}
            className="ml-2 text-sky-300 hover:text-sky-200"
          >
            author says addressed · re-review
          </button>
        )}
      </div>
      <span className="font-mono text-emerald-500/75">+{target.additions}</span>
      <span className="font-mono text-rose-500/75">−{target.deletions}</span>
      <span className={`shrink-0 ${stateTone}`}>{stateLabel(target)}</span>
      <div className="flex shrink-0 items-center gap-1">
        <button type="button" aria-label={`Mark ${label} reviewed`} disabled={busy} onClick={() => onMark("reviewed", true)} className="border border-neutral-800 px-1.5 py-0.5 text-neutral-500 hover:text-emerald-300 disabled:opacity-40">r Reviewed</button>
        <button type="button" aria-label={`Mark ${label} needs changes`} disabled={busy} onClick={() => onMark("needs_changes")} className="border border-neutral-800 px-1.5 py-0.5 text-neutral-500 hover:text-rose-300 disabled:opacity-40">x Changes</button>
        <button type="button" aria-label={`Comment on ${label}`} disabled={busy} onClick={onComment} className="border border-neutral-800 px-1.5 py-0.5 text-neutral-500 hover:text-sky-300 disabled:opacity-40">c Comment</button>
        <button type="button" aria-label={`Open context for ${label}`} onClick={() => onContext("impact")} className="border border-neutral-800 px-1.5 py-0.5 text-neutral-500 hover:text-neutral-200">e Context</button>
      </div>
    </div>
  );
}
