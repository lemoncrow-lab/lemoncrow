import { Badge } from "../../components/ui/badge";
import { HOW_THIS_WORKS, initials, stripFor, type StripTone } from "./secondCopy";
import { useSecondReviewer } from "./useSecondReviewer";
import type { SecondReviewerStatus } from "./secondApi";

const CHIP_TONE: Record<StripTone, "neutral" | "warning" | "success" | "destructive"> = {
  neutral: "neutral",
  warning: "warning",
  success: "success",
  danger: "destructive",
};

/**
 * The second reviewer's strip on Overview and Wrap up (SPEC 4.16). It draws only when the server says a second
 * signature is involved; a local install never answers, so nothing shows and nothing reads as unavailable.
 */
export function SecondStrip({ apiReviewId, choice = null, status: given }: {
  apiReviewId: string;
  /** The verdict picked on Wrap up; null on the Overview. */
  choice?: string | null;
  /** For tests: skip the read. */
  status?: SecondReviewerStatus | null;
}) {
  const read = useSecondReviewer(apiReviewId, { enabled: given === undefined, watchMs: 4000 });
  const status = given === undefined ? read.status : given;
  if (!status) return null;
  const strip = stripFor(status, choice);
  if (!strip || !status.reviewer) return null;
  return (
    <section aria-label="Second reviewer" className="rounded-lg border border-border bg-card p-4" data-testid="second-strip">
      <div className="flex flex-wrap items-center gap-3">
        <span aria-hidden="true" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border font-mono text-meta">{initials(status.reviewer.name)}</span>
        <p className="min-w-0 flex-1 text-label" data-testid="second-strip-line">{strip.line}</p>
        {strip.chip ? <Badge tone={CHIP_TONE[strip.chip.tone]} data-testid="second-strip-chip">{strip.chip.text}</Badge> : null}
      </div>
      <details className="mt-2">
        <summary className="cursor-pointer text-meta text-muted-foreground pointer-coarse:flex pointer-coarse:items-center">How this works</summary>
        <p className="mt-1 text-meta text-muted-foreground" data-testid="second-strip-how">{HOW_THIS_WORKS}</p>
      </details>
    </section>
  );
}
