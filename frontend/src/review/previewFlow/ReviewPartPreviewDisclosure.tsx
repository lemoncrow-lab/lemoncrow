import { useState, useSyncExternalStore } from "react";
import { ChevronRight } from "lucide-react";
import { Button } from "../../components/ui/button";
import { useReviewFlowNavigation } from "../useReviewFlowNavigation";
import type { ReviewTarget } from "../types";
import { evidenceForPath, evidenceScope, evidenceVersion, subscribeEvidence, type EvidenceEntry } from "./previewEvidence";
import { changeSummary } from "./previewFlowModel";

function Side({ name, text }: { name: string; text: string }) {
  return <section className="min-w-0">
    <h4 className="text-meta font-medium text-muted-foreground">{name}</h4>
    <pre className="mt-1 max-h-48 max-w-full overflow-auto whitespace-pre rounded border border-border bg-surface-code p-2 font-mono text-meta">{text}</pre>
  </section>;
}
function Evidence({ reviewRef, entry }: { reviewRef: string; entry: EvidenceEntry }) {
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const [open, setOpen] = useState(false);
  const panel = `focus-preview-panel-${entry.surfaceKey}`;
  return <div className="mt-3 rounded-lg border border-border bg-card" data-testid="focus-preview-disclosure">
    <Button unstyled className="flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-muted/50"
      aria-expanded={open} aria-controls={panel} onClick={() => setOpen((v) => !v)}>
      <span className="min-w-0 flex-1 break-words text-label font-medium">Preview evidence · {entry.title} · {changeSummary(entry.old, entry.next)}</span>
      <ChevronRight size={16} aria-hidden="true" className={open ? "rotate-90" : ""}/>
    </Button>
    {open && <div id={panel} className="space-y-3 border-t border-border p-3 text-label" data-testid="focus-preview-panel">
      <div className="grid min-w-0 gap-3 sm:grid-cols-2"><Side name="Before" text={entry.old.text}/><Side name="After" text={entry.next.text}/></div>
      <Button size="sm" variant="outline" onClick={() => goTo({ kind: "preview", surface: entry.surfaceKey })}>Open in Preview</Button>
    </div>}
  </div>;
}
/** A collapsed "Preview evidence · <surface> · <old → new>" row, only where Before and After were really captured for a surface
 * that touches this part. With no captured evidence it renders nothing; opening it never starts a run. */
export default function ReviewPartPreviewDisclosure({ reviewRef, apiReviewId, revisionId, target }: {
  reviewRef: string; apiReviewId: string; revisionId: string; target: ReviewTarget;
}) {
  useSyncExternalStore(subscribeEvidence, evidenceVersion, evidenceVersion);
  const rows = evidenceForPath(evidenceScope(apiReviewId, revisionId), target.path);
  if (rows.length === 0) return null;
  return <>{rows.map((entry) => <Evidence key={entry.surfaceKey} reviewRef={reviewRef} entry={entry}/>)}</>;
}
