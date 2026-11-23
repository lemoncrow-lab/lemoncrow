import { useEffect, useMemo, useState } from "react";

import { surfaceRows } from "./previewFlow/previewFlowModel";
import { fetchReviewSurfaces } from "./reviewApi";
import type { ReviewTarget } from "./types";

/**
 * The "Preview · N" tab count (SPEC 4.6): N is the surfaces that relate to changed code. Null (a bare "Preview") while the
 * list loads, when it cannot be read, and when no surface is configured. Discovery only lists; it never renders or runs one.
 */
export function usePreviewCount(apiReviewId: string, revisionId: string, targets: readonly ReviewTarget[]): number | null {
  const [found, setFound] = useState<{ revisionId: string; rows: Awaited<ReturnType<typeof fetchReviewSurfaces>>["surfaces"] } | null>(null);
  useEffect(() => {
    let live = true;
    fetchReviewSurfaces(apiReviewId, revisionId).then((result) => {
      if (live && result.revision_id === revisionId) setFound({ revisionId, rows: result.surfaces });
    }).catch(() => { if (live) setFound(null); });
    return () => { live = false; };
  }, [apiReviewId, revisionId]);
  const rows = found?.revisionId === revisionId ? found.rows : null;
  return useMemo(() => (rows && rows.length > 0 ? surfaceRows(rows, targets).filter((row) => row.reached).length : null), [rows, targets]);
}
