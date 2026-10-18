import { useEffect, useState } from "react";
import { Info } from "lucide-react";
import { authenticatedFetch } from "./reviewApi";
import { isMutableReview } from "./ReviewBaseIdentity";

interface FrontierNotesResult {
  key: string;
  notes: string[];
}

/**
 * Read the server-owned D6 frontier projection. Neither ancestry nor Review
 * status is inferred here. Older servers/no notes render no additional chrome.
 */
export default function ReviewFrontierNotes({
  reviewId,
  revisionId,
  rangeMode,
  historical,
}: {
  reviewId: string;
  revisionId: string;
  rangeMode: string;
  historical: boolean;
}) {
  const key = `${reviewId}:${revisionId}`;
  const [result, setResult] = useState<FrontierNotesResult>({ key: "", notes: [] });

  useEffect(() => {
    if (!reviewId || !revisionId || historical || !isMutableReview(rangeMode)) return;
    let current = true;
    void authenticatedFetch(`/api/reviews/${encodeURIComponent(reviewId)}/frontier`)
      .then(async (response) => response.ok ? response.json() as Promise<{ notes?: unknown }> : null)
      .then((body) => {
        if (!current) return;
        const notes = Array.isArray(body?.notes)
          ? body.notes.filter((note): note is string => typeof note === "string" && note.trim().length > 0)
          : [];
        setResult({ key, notes });
      })
      .catch(() => {
        // A frontier read must never turn an otherwise usable Reader into an error.
        if (current) setResult({ key, notes: [] });
      });
    return () => { current = false; };
  }, [reviewId, revisionId, rangeMode, historical, key]);

  const notes = !historical && isMutableReview(rangeMode) && result.key === key ? result.notes : [];
  if (!notes.length) return null;

  return (
    <div className="flex shrink-0 items-start gap-2 border-b border-border/50 px-3 py-1 text-caption text-muted-foreground" role="note" data-testid="review-frontier-notes">
      <Info size={12} className="mt-0.5 shrink-0 opacity-65" aria-hidden="true" />
      <span className="shrink-0 font-medium text-neutral-400">Base movement</span>
      <span className="min-w-0 break-words">{notes.join(" · ")}</span>
    </div>
  );
}
