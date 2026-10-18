import { Button as UiButton } from "../components/ui/button";
import { GitCompareArrows, X } from "lucide-react";

export type ReviewCompareBase = number | "head";

interface RevisionCompareBarProps {
  baseRevision: ReviewCompareBase;
  targetRevision: number;
  loading?: boolean;
  error?: string;
  onExit(): void;
}

export default function RevisionCompareBar({
  baseRevision,
  targetRevision,
  loading = false,
  error = "",
  onExit,
}: RevisionCompareBarProps) {
  const baseLabel = baseRevision === "head" ? "Source" : `Snapshot ${baseRevision}`;
  const targetLabel = `Snapshot ${targetRevision}`;

  return (
    <div
      className="flex min-h-8 shrink-0 items-center gap-2 border-b border-sky-950/70 bg-sky-950/10 px-3 text-caption"
      data-testid="revision-compare-bar"
      role="status"
      aria-live="polite"
    >
      <GitCompareArrows size={12} className="shrink-0 text-sky-400/80" aria-hidden="true" />
      <span className="font-medium text-sky-200">Comparing {baseLabel} → {targetLabel}</span>
      {loading && <span className="text-muted-foreground">Loading comparison…</span>}
      {!loading && error && <span className="min-w-0 truncate text-amber-300">{error}</span>}
      <span className="flex-1" />
      <UiButton
        variant="ghost"
        size="compact"
        type="button"
        onClick={onExit}
        className="h-6 gap-1 px-2 text-caption"
        aria-label="Show full review"
        tooltip="Show all changes from the original Review base"
      >
        <X size={11} aria-hidden="true" />
        Full review
      </UiButton>
    </div>
  );
}
