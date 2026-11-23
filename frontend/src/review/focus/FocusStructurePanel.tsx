import { Badge } from "../../components/ui/badge";
import { degradedSentence } from "../investigation/impactReadiness";
import type { ReviewProjection, ReviewTarget } from "../types";
import { callerCoverage, siteProvenance, structureGroups, structureLimitations } from "./focusStructureModel";

const NOT_TRACED = "Not verified · not traced for this part";

/** Structure drawer body (SPEC 4.4): every fact is "Checked in code" or "Not verified · <reason>"; a missing relation is never zero. */
export function FocusStructurePanel({ target, projection }: { target: ReviewTarget; projection: ReviewProjection | null }) {
  const groups = structureGroups(target, projection);
  const readiness = projection?.context?.readiness;
  // Recorded codes in plain words; a code with no sentence stays behind "Technical details", never on the page.
  const codes = readiness?.degraded ?? [];
  const gaps = [
    ...codes.map(degradedSentence).filter((text): text is string => Boolean(text)),
    ...(readiness?.unmeasured ?? []).filter((item) => item.path === target.path && (!item.symbol || item.symbol === target.symbol)).map((item) => `Callers were not checked: ${item.reason}.`),
  ];
  const unnamed = codes.filter((code) => !degradedSentence(code));
  return <div className="space-y-4 text-label" data-testid="focus-structure-facts">
    {gaps.length > 0 || unnamed.length > 0 ? <section className="space-y-1" aria-label="What could not be checked" data-testid="focus-structure-gaps">
      <h3 className="font-medium">Not checked for this part</h3>
      <ul className="list-disc space-y-1 pl-5 text-meta text-muted-foreground">{[...new Set(gaps)].map((text) => <li key={text}>{text}</li>)}</ul>
      {unnamed.length > 0 ? <details className="text-meta text-muted-foreground">
        <summary className="cursor-pointer">{gaps.length > 0 ? "Technical details" : `${unnamed.length} more ${unnamed.length === 1 ? "thing was" : "things were"} not checked · technical details`}</summary>
        <ul className="mt-1 font-mono">{unnamed.map((code) => <li key={code}>{code}</li>)}</ul>
      </details> : null}
    </section> : null}
    <section className="space-y-2" aria-label="Callers and consumers">
      <h3 className="font-medium">Called by</h3>
      {groups.length === 0 && (readiness?.source
        ? <p className="text-meta text-muted-foreground">No caller found in this repository. Callers outside this repository are not checked.</p>
        : <p className="text-meta text-muted-foreground">Not verified · no code-index result for this part</p>)}
      {groups.map((group) => <div key={group.key} className="space-y-2 rounded-md border border-border p-3">
        <p className="break-all font-mono text-meta">{group.symbol || target.symbol || target.label} · {group.path || target.path}</p>
        <p className="text-meta text-muted-foreground">{callerCoverage(group)}</p>
        {group.sites.map((site, i) => {
          const provenance = siteProvenance(site);
          return <div key={`${site.path}:${site.line}:${i}`} className="space-y-1 border-t border-border pt-2">
            <p className="break-all font-mono text-meta">{site.path}{site.symbol ? ` · ${site.symbol}` : ""}{site.line > 0 ? ` · line ${site.line}` : ""}</p>
            <div className="flex flex-wrap items-center gap-2"><Badge tone={provenance.checked ? "neutral" : "warning"}>{provenance.label}</Badge><span className="text-meta text-muted-foreground">{site.impact_kind.replaceAll("_", " ")}</span></div>
            {site.snippet && <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-background p-2 font-mono text-meta">{site.snippet}</pre>}
          </div>;
        })}
        {structureLimitations(group).map((item) => <p key={item} className="text-meta text-p1">Not verified · {item}</p>)}
      </div>)}
      {(projection?.context?.consumers_omitted ?? 0) > 0 && <p className="text-meta text-p1">Not verified · {projection!.context!.consumers_omitted} more groups not shown</p>}
    </section>
    {(["Calls", "Routes reaching", "Data path"] as const).map((name) => <section key={name} className="space-y-1">
      <h3 className="font-medium">{name}</h3>
      <p className="text-meta text-muted-foreground">{NOT_TRACED}</p>
    </section>)}
  </div>;
}
