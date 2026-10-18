import { useEffect, useState } from "react";

import ImageOverlayCompare from "./ImageOverlayCompare";
import { fetchReviewEvidenceContent } from "./reviewApi";
import type { ReviewEvidence } from "./types";


export default function ScreenshotEvidenceCompare({ before, after }: { before: ReviewEvidence; after: ReviewEvidence }) {
  const [urls, setUrls] = useState<{ before: string; after: string } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    let beforeUrl = "";
    let afterUrl = "";
    setUrls(null);
    setError("");
    void Promise.all([fetchReviewEvidenceContent(before.id), fetchReviewEvidenceContent(after.id)])
      .then(([beforeBlob, afterBlob]) => {
        if (!live) return;
        beforeUrl = URL.createObjectURL(beforeBlob);
        afterUrl = URL.createObjectURL(afterBlob);
        setUrls({ before: beforeUrl, after: afterUrl });
      })
      .catch((reason: unknown) => {
        if (live) setError(String(reason instanceof Error ? reason.message : reason));
      });
    return () => {
      live = false;
      if (beforeUrl) URL.revokeObjectURL(beforeUrl);
      if (afterUrl) URL.revokeObjectURL(afterUrl);
    };
  }, [after.id, before.id]);

  return (
    <section className="space-y-2" aria-label={`Screenshot diff ${after.title || after.path || after.id}`}>
      <div className="flex items-center gap-2 text-caption">
        <span className="font-medium text-neutral-300">Screenshot diff</span>
        <span className="min-w-0 truncate text-muted-foreground">{after.title || after.path}</span>
      </div>
      {urls ? (
        <ImageOverlayCompare
          beforeSrc={urls.before}
          afterSrc={urls.after}
          beforeLabel="Before"
          afterLabel="After"
        />
      ) : error ? (
        <div className="border border-amber-950/70 bg-amber-950/10 px-2.5 py-2 text-caption text-amber-300">Screenshot comparison unavailable: {error}</div>
      ) : (
        <div className="border border-border px-2.5 py-3 text-caption text-muted-foreground">Loading exact Snapshot screenshots…</div>
      )}
    </section>
  );
}
