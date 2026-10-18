import type { ReviewOverview } from "./types";

/** A Snapshot is always immutable; freezing the *Review* base is separate. */
export function isMutableReview(rangeMode: string): boolean {
  return rangeMode === "working_tree" || rangeMode === "staged";
}

export function shortBase(sha: string): string {
  return sha.slice(0, 12);
}

/** Quiet orientation in the existing Reader identity row, never a readiness signal. */
export default function ReviewBaseIdentity({ overview }: { overview: ReviewOverview }) {
  const { session, revision } = overview;
  if (!isMutableReview(session.range_mode)) return null;

  const recorded = session.review_base_sha?.trim() || "";
  const effective = revision.base_sha?.trim() || "";
  const frozen = Boolean(recorded);
  const detail = frozen
    ? `Recorded Review base ${recorded}. Effective Snapshot base ${effective || "unavailable"}. The Snapshot base can move past integrated work without treating it as newly authored work.`
    : `Base not frozen. No recorded Review base exists for this capture; the effective Snapshot base is ${effective || "unavailable"}. Captures may use a new HEAD (for example with an older client or detached checkout).`;

  return (
    <span
      className="review-reader-source-context ml-2 inline-flex min-w-0 flex-wrap items-center gap-x-1 border-l border-border pl-2 text-caption text-muted-foreground"
      title={detail}
      aria-label={detail}
      data-testid="review-base-identity"
    >
      {frozen ? (
        <>
          <span>Review base</span>
          <span className="font-mono text-neutral-300">{shortBase(recorded)}</span>
          <span aria-hidden="true">·</span>
          <span>Snapshot base</span>
          <span className="font-mono text-neutral-300">{effective ? shortBase(effective) : "unavailable"}</span>
        </>
      ) : (
        <>
          <span className="font-medium text-neutral-300">Base not frozen</span>
          {effective && (
            <>
              <span aria-hidden="true">·</span>
              <span>Snapshot base</span>
              <span className="font-mono">{shortBase(effective)}</span>
            </>
          )}
        </>
      )}
    </span>
  );
}
