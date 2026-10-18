import { useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import type { CodeTarget } from "../lib/codeLinks";
import { fetchHistoricalReviewMap, fetchReviewMap } from "./reviewApi";
import type { ReviewMapNode } from "./types";

interface ReviewContextFilesProps {
  reviewId: string;
  revisionNumber: number;
  historical: boolean;
  onOpenInCode?(target: CodeTarget): void;
}

function likelyDesignContext(path: string): boolean {
  const lower = path.toLowerCase();
  return (
    lower.includes("architecture")
    || lower.includes("design")
    || lower.includes("spec")
    || lower.includes("schema")
    || lower.endsWith(".md")
    || lower.endsWith(".mdx")
    || lower.endsWith(".yaml")
    || lower.endsWith(".yml")
    || lower.endsWith(".json")
  );
}

export default function ReviewContextFiles({ reviewId, revisionNumber, historical, onOpenInCode }: ReviewContextFilesProps) {
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState<ReviewMapNode[]>([]);
  const [error, setError] = useState("");

  async function load() {
    if (loading || loaded) return;
    setLoading(true);
    setError("");
    try {
      const payload = historical
        ? await fetchHistoricalReviewMap(reviewId, revisionNumber, "architecture")
        : await fetchReviewMap(reviewId, "architecture");
      const context = payload.graph.nodes
        .filter((node) => node.status === "context" && Boolean(node.path))
        .sort((a, b) => Number(likelyDesignContext(b.path)) - Number(likelyDesignContext(a.path)) || a.path.localeCompare(b.path))
        .slice(0, 12);
      setRows(context);
      setLoaded(true);
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="border-t border-neutral-900 pt-3" data-testid="review-context-files">
      <div className="flex items-center gap-2">
        <div className="review-kicker">Context files</div>
        <span className="flex-1" />
        {!loaded && (
          <UiButton variant="ghost" size="compact" type="button" disabled={loading} onClick={() => void load()}>
            {loading ? "Loading…" : "Load"}
          </UiButton>
        )}
      </div>
      <p className="mt-1 text-micro leading-4 text-muted-foreground">
        Unchanged architecture/docs/spec/schema and relationship context. Context-only; opening or loading it never marks a Review target reviewed.
      </p>
      {error && <div className="mt-2 text-caption text-amber-300">Context-file index unavailable.</div>}
      {loaded && rows.length === 0 && <div className="mt-2 text-caption text-muted-foreground">No unchanged context files were returned for this Change.</div>}
      {rows.length > 0 && (
        <div className="mt-2 space-y-0.5">
          {rows.map((row) => (
            <UiButton
              unstyled
              type="button"
              key={row.id}
              disabled={!onOpenInCode}
              onClick={() => onOpenInCode?.({ path: row.path, line: row.line || 1, endLine: row.end_line || row.line || 1 })}
              className="block w-full rounded px-1.5 py-1 text-left hover:bg-muted disabled:cursor-default"
            >
              <div className="flex min-w-0 gap-2 text-caption">
                <span className="min-w-0 flex-1 truncate font-mono text-neutral-300">{row.path}</span>
                <span className="shrink-0 text-muted-foreground">{row.exactness === "snapshot" ? "Snapshot context" : "current index"}</span>
              </div>
              {row.reasons?.[0] && <div className="truncate text-micro text-muted-foreground">{row.reasons[0]}</div>}
            </UiButton>
          ))}
        </div>
      )}
    </section>
  );
}
