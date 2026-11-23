import { useEffect, useState } from "react";
import { ArrowLeft, Eye, ExternalLink } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { fetchRelatedSource } from "../reviewApi";
import type { RelatedSource } from "../types";
import { siteEvidenceLabel, verifiedSite, type AffectedSite } from "./allFilesModel";

/**
 * Unchanged callers from the recorded impact projection. Reading, following or
 * peeking here never updates a human mark or the changed-line coverage ledger.
 */
export function ReviewAffectedSources({ reviewId, sites, inChangedFiles = 0, onJumpToChanged }: {
  reviewId: string;
  sites: readonly AffectedSite[];
  /** Callers the server lists inside changed files; they are not unchanged code, so they are counted here, not dropped. */
  inChangedFiles?: number;
  onJumpToChanged: (targetId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [trail, setTrail] = useState<AffectedSite[]>([]);
  const selected = trail.at(-1) ?? null;
  const [result, setResult] = useState<{ key: string; value: RelatedSource | null; error: string } | null>(null);

  useEffect(() => {
    if (!selected) { setResult(null); return; }
    let live = true;
    setResult(null);
    // This is the Reader's existing bounded source endpoint, not a workspace or live-checkout read.
    fetchRelatedSource(reviewId, selected.path, selected.line || 1).then((response) => {
      if (!live) return;
      if (response.path !== selected.path || response.focus_line !== (selected.line || 1)) {
        setResult({ key: selected.key, value: null, error: "Source location did not match the recorded site." });
      } else {
        setResult({ key: selected.key, value: response, error: "" });
      }
    }).catch(() => { if (live) setResult({ key: selected.key, value: null, error: "Source could not be loaded for this Review." }); });
    return () => { live = false; };
  }, [reviewId, selected?.key]);

  if (sites.length === 0 && inChangedFiles === 0) return null;
  if (sites.length === 0) return <p className="text-meta text-muted-foreground" data-testid="all-files-affected-changed">{inChangedFiles} {inChangedFiles === 1 ? "caller is" : "callers are"} in files this change touches, so no unchanged callers are listed.</p>;
  return <section className="space-y-3" aria-label="Not changed, but affected" data-testid="all-files-affected">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-2">
      <div><h2 className="font-mono text-tag font-semibold">Not changed, but affected · {sites.length}</h2>
        <p className="mt-1 text-meta text-muted-foreground">Recorded caller/consumer locations, not changed code or human decisions.</p>
        {inChangedFiles > 0 && <p className="mt-1 text-meta text-muted-foreground" data-testid="all-files-affected-changed">{inChangedFiles} more {inChangedFiles === 1 ? "caller is" : "callers are"} in files this change touches and {inChangedFiles === 1 ? "is" : "are"} not listed here.</p>}</div>
      <Button variant="outline" size="sm" aria-expanded={expanded} onClick={() => { setExpanded((value) => !value); setTrail([]); }}>
        {expanded ? "Hide related code" : "Inspect related code"} <Eye size={14} aria-hidden="true"/>
      </Button>
    </div>
    {expanded && <>
      <p className="text-meta text-muted-foreground">These sites may be affected by a changed part. A missing site never means there are no other callers.</p>
      <div className="grid gap-2 sm:grid-cols-2">
        {sites.map((site) => <div key={site.key} className="min-w-0 space-y-2 rounded-lg border border-border bg-card p-3">
          <p className="break-all font-mono text-meta">{site.symbol || site.path}</p>
          <p className="break-all text-tag text-muted-foreground">{site.path}{site.line > 0 ? ` · line ${site.line}` : " · line unknown"}</p>
          <div className="flex flex-wrap items-center gap-2"><Badge tone="neutral">Read-only</Badge>
            <Badge tone={verifiedSite(site) ? "neutral" : "warning"}>{siteEvidenceLabel(site)}</Badge>
          </div>
          <p className="text-meta text-muted-foreground">{site.kind === "caller" ? "Calls" : "Consumes"} {site.changedTargetLabel || "changed code"}.</p>
          {site.inspectedByAgent === null && <p className="text-meta text-muted-foreground">Agent inspection: not recorded</p>}
          {site.snippet && <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-background p-2 font-mono text-tag">{site.snippet}</pre>}
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" disabled={!site.line} onClick={() => setTrail((previous) => [...previous, site])}>Peek at code</Button>
            {site.changedTargetId && <Button variant="ghost" size="sm" onClick={() => onJumpToChanged(site.changedTargetId!)}>Go to changed part <ExternalLink size={14} aria-hidden="true"/></Button>}
          </div>
        </div>)}
      </div>
      {selected && <div className="space-y-3 rounded-lg border border-border bg-card p-3" aria-label="Related source Peek" data-testid="all-files-peek">
        <nav aria-label="Peek backtrail" className="flex flex-wrap items-center gap-2 text-meta">
          <Button variant="ghost" size="sm" onClick={() => setTrail((previous) => previous.slice(0, -1))}><ArrowLeft size={14} aria-hidden="true"/> Back</Button>
          <span className="text-muted-foreground">All files / Affected code{trail.length > 1 ? ` / ${trail.length} peeks` : ""}</span>
        </nav>
        <h3 className="break-all font-mono text-label font-semibold">{selected.path} · line {selected.line}</h3>
        {result?.key !== selected.key && <p role="status" className="text-meta">Loading related source…</p>}
        {result?.key === selected.key && result.error && <p role="alert" className="text-meta text-p1">{result.error}</p>}
        {result?.key === selected.key && result.value && <>
          <p className="text-meta text-muted-foreground">Code excerpt from the Review's related-source endpoint. This response does not carry a revision identifier; it is context, not frozen patch proof or a review decision.</p>
          <div className="max-w-full overflow-x-auto rounded-md border border-border bg-background" role="region" aria-label={`Related code in ${selected.path}`} tabIndex={0}>
            <table className="w-max min-w-full border-collapse font-mono text-meta leading-6">
              <tbody>{result.value.text.split("\n").map((line, i) => {
                const n = result.value!.start_line + i;
                return <tr key={n} className={n === selected.line ? "bg-p2-bg" : ""}>
                  <td className="select-none border-r border-border px-2 text-right text-muted-foreground">{n}</td>
                  <td className="whitespace-pre px-3">{line || " "}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
        </>}
      </div>}
    </>}
  </section>;
}
