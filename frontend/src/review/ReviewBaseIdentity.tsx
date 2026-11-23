import type { ReviewOverview } from "./types";

/** A Snapshot is always immutable; freezing the *Review* base is separate. */
export function isMutableReview(rangeMode: string): boolean {
  return rangeMode === "working_tree" || rangeMode === "staged";
}

export function shortBase(sha: string): string {
  return sha.slice(0, 12);
}

/** A commit as the header names it: 7 characters, like `git log --oneline`. */
function headerSha(sha: string): string {
  return sha.slice(0, 7);
}
/**
 * Quiet orientation in the existing Reader identity row, never a readiness signal.
 * One base is shown once; two different bases say plainly which is which.
 */
export default function ReviewBaseIdentity({ overview }: { overview: ReviewOverview }) {
  const { session, revision } = overview;
  if (!isMutableReview(session.range_mode)) return null;

  const recorded = session.review_base_sha?.trim() || "";
  const effective = revision.base_sha?.trim() || "";
  const frozen = Boolean(recorded);
  const moved = frozen && Boolean(effective) && effective !== recorded;
  const detail = !frozen
    ? `Compared against ${effective || "an unknown commit"}. This review has no fixed base, so a later update may compare against a newer commit.`
    : moved
      ? `The review started from ${recorded}. This revision is compared against ${effective}, a later base that already includes integrated work, so that work is not shown as new.`
      : `Changes are compared against ${recorded}.`;

  return (
    <span
      className="review-reader-source-context ml-2 inline-flex min-w-0 flex-wrap items-center gap-x-1 border-l border-border pl-2 text-caption text-muted-foreground"
      title={detail}
      aria-label={detail}
      data-testid="review-base-identity"
    >
      {!frozen ? (
        <>
          <span>Base</span>
          <span className="font-mono text-subtle">{effective ? headerSha(effective) : "unknown"}</span>
          <span>· not fixed</span>
        </>
      ) : moved ? (
        <>
          <span>Started from</span>
          <span className="font-mono text-subtle">{headerSha(recorded)}</span>
          <span aria-hidden="true">·</span>
          <span>now compared to</span>
          <span className="font-mono text-subtle">{headerSha(effective)}</span>
        </>
      ) : (
        <>
          <span>Base</span>
          <span className="font-mono text-subtle">{headerSha(recorded)}</span>
        </>
      )}
    </span>
  );
}
