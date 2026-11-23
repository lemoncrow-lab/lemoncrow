import type { ReactNode } from "react";
import { Sparkles } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { cn } from "../../lib/utils";
import { evidenceText, type EvidenceState } from "./overviewModel";

/** A small section label. Plain text: no "@@" markers (SPEC 2; DESIGN_GUIDE rule 1). */
export function Label({ children, tone = "muted", className }: { children: ReactNode; tone?: "muted" | "red" | "ai"; className?: string }) {
  return (
    <span
      className={cn(
        "font-mono text-tag uppercase",
        tone === "red" ? "text-p0" : tone === "ai" ? "text-ai" : "text-muted-foreground",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** The two evidence states of SPEC 5.1: "Checked in code" or "Not verified · <reason>". Nothing else. */
export function EvidenceChip({ evidence }: { evidence: EvidenceState }) {
  const checked = evidence.state === "checked";
  return (
    <Badge
      tone={checked ? "neutral" : "warning"}
      className={checked ? undefined : "border-dashed bg-transparent"}
      data-evidence={evidence.state}
    >
      <span aria-hidden="true">{checked ? "●" : "○"}</span>
      {evidenceText(evidence)}
    </Badge>
  );
}

/**
 * The one Heads-up component: amber dot, "Heads-up", one line (SPEC 4.2, 5.1). Not shown on the Overview yet: it waits for a
 * server plan-vs-change fact (SPEC 7.1 P2-1 F).
 */
export function HeadsUp({ fact }: { fact: { name: string; detail: string } }) {
  return (
    <div
      data-testid="overview-headsup"
      className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-lg border border-p1-line bg-p1-bg px-3 py-2 text-label"
    >
      <span aria-hidden="true" className="size-2 shrink-0 rounded-full bg-p1-solid" />
      <span className="font-medium text-p1">Heads-up</span>
      <span className="min-w-0 break-words text-foreground">
        <span className="font-mono">{fact.name}</span> changed{fact.detail ? ` · ${fact.detail}` : ""}
      </span>
    </div>
  );
}

/** The AI badge: violet, attribution only. */
export function AiBadge() {
  return (
    <Badge tone="ai">
      <Sparkles size={14} aria-hidden="true" />
      AI
    </Badge>
  );
}

/** "based on app/orders.py 4–6 ›": opens the lines the hint rests on (SPEC 5.2). */
export function BasedOn({ label, aria, onOpen }: { label: string; aria: string; onOpen: () => void }) {
  return (
    <Button variant="link" size="compact" className="h-auto max-w-full whitespace-normal break-all text-left font-mono text-meta" aria-label={aria} onClick={onOpen}>
      based on {label} ›
    </Button>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden="true" data-skeleton="" className={cn("block h-4 animate-pulse rounded-sm bg-muted", className)} />;
}
