import { useEffect, useState } from "react";

import { useRuntimeCapabilities } from "../lib/RuntimeCapabilities";

import { fetchFixLines, reviewHref } from "./reviewApi";
import { fixLineChip } from "./ReviewFixLines";
import type { FixLine } from "./types";

export function useReviewFixOf(reviewId: string, enabled = true): FixLine | null {
  const [fixOf, setFixOf] = useState<FixLine | null>(null);

  useEffect(() => {
    let live = true;
    if (!enabled || !reviewId) {
      setFixOf(null);
      return () => { live = false; };
    }
    fetchFixLines(reviewId)
      .then((result) => {
        if (live) setFixOf(result.fix_of);
      })
      .catch(() => {
        if (live) setFixOf(null);
      });
    return () => {
      live = false;
    };
  }, [enabled, reviewId]);

  return fixOf;
}

export default function ReviewFixOfBanner({ reviewId }: { reviewId: string }) {
  const { localMachine } = useRuntimeCapabilities();
  const fixOf = useReviewFixOf(reviewId, localMachine);

  if (!fixOf) return null;

  return (
    <div
      className="flex items-center gap-2 border-b border-border px-4 py-1.5 text-caption text-muted-foreground"
      data-testid="fix-of-banner"
    >
      <span className="font-medium text-foreground">
        {`Fix of Snapshot ${fixOf.snapshot_number}`}
      </span>
      <span className="rounded-md bg-muted px-1.5 py-0.5">
        {fixLineChip(fixOf)}
      </span>
      <a
        className="text-sky-300/80 hover:underline"
        href={reviewHref(fixOf.review_id)}
      >
        Back to the Review
      </a>
    </div>
  );
}
