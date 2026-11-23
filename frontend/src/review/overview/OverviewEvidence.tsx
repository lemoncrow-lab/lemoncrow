import { useState } from "react";
import { Check, ChevronRight, ShieldAlert, Sparkles } from "lucide-react";

import { Button } from "../../components/ui/button";
import { DisclosureCard } from "../../components/ui/disclosure-card";
import type { ReviewTarget } from "../types";
import { AiBadge, BasedOn, EvidenceChip, Label } from "./OverviewParts";
import {
  linesAria,
  linesLabel,
  partName,
  plural,
  type AiHint,
  type AskedPlanned,
  type CheckedRow,
  type EvidenceSectionId,
  type HiddenText,
  type OverviewCounts,
  type ReachModel,
  hiddenSummary,
} from "./overviewModel";

export interface OverviewEvidenceProps {
  sections: EvidenceSectionId[];
  /** The server is still preparing the facts: the header says so and the body is closed. */
  checking: boolean;
  open: boolean;
  onToggle: () => void;
  agent: string;
  counts: OverviewCounts;
  /** The server's +/- totals; null when it sent none. */
  totals: { additions: number; deletions: number } | null;
  asked: AskedPlanned;
  planApproved: boolean;
  onTogglePlan: () => void;
  connects: { text: string } | null;
  onOpenMap: () => void;
  checkedRows: CheckedRow[];
  aiAvailable: boolean;
  aiHints: AiHint[];
  onOpenPart: (target: ReviewTarget) => void;
  /** Why new AI cannot run ("AI budget is $0"), or "": the revision has no hints for that reason, not because AI is missing. */
  aiBlocked: string;
  onOpenSettings: () => void;
  reach: ReachModel | null;
  hidden: { scanned: boolean; found: HiddenText[] };
  notChecked: string[];
}

function Section({ id, title, children }: { id: EvidenceSectionId; title?: string; children: React.ReactNode }) {
  return (
    <section data-evidence-section={id} className="flex flex-col gap-2 border-b border-border pb-3 last:border-b-0 last:pb-0">
      {title ? <h3 className="text-label font-semibold text-foreground">{title}</h3> : null}
      {children}
    </section>
  );
}

function AskedPlannedDid(props: OverviewEvidenceProps) {
  const { asked, counts, agent } = props;
  return (
    <Section id="asked-planned-did">
      <div role="group" aria-label="Asked, planned, did" className="grid gap-3 md:grid-cols-3">
        {asked.asked ? (
          <div className="flex min-w-0 flex-col gap-1" data-testid="overview-asked">
            <Label>Asked</Label>
            <p className="break-words text-label text-foreground">{asked.asked}</p>
            <p className="text-meta text-muted-foreground">{agent} · from the agent session</p>
          </div>
        ) : null}
        {asked.planned.length > 0 ? (
          <div className="flex min-w-0 flex-col gap-1" data-testid="overview-planned">
            <Label>Planned</Label>
            <ol className="list-decimal pl-4 text-label text-foreground">
              {asked.planned.map((line, index) => (
                <li key={`${index}:${line}`} className="break-words">{line}</li>
              ))}
            </ol>
            <p className="text-meta text-muted-foreground">From the plan in the agent session</p>
            <div className="flex flex-wrap items-center gap-2">
              {props.planApproved ? (
                <Button variant="success" size="sm" aria-pressed="true" data-testid="overview-plan" onClick={props.onTogglePlan}>
                  <Check size={14} aria-hidden="true" />
                  Plan approved by you
                </Button>
              ) : (
                <>
                  <span className="text-meta text-p1">Plan approved by you? Not yet</span>
                  <Button variant="outline" size="sm" aria-pressed="false" data-testid="overview-plan" onClick={props.onTogglePlan}>
                    Approve plan
                  </Button>
                </>
              )}
            </div>
          </div>
        ) : null}
        <div className="flex min-w-0 flex-col gap-1" data-testid="overview-did">
          <Label>Did</Label>
          <p className="text-label text-foreground">
            {plural(counts.parts, "part")} in {plural(counts.files, "file")}
            {props.totals ? `, +${props.totals.additions} −${props.totals.deletions}` : ""}
          </p>
        </div>
      </div>
    </Section>
  );
}

function AskedVsDid(props: OverviewEvidenceProps) {
  const { checkedRows: rows, aiHints: hints, aiAvailable } = props;
  // "Asked vs did" only when a task was recorded; without one the section is what was checked, under its own line.
  return (
    <Section id="asked-vs-did" title={props.asked.asked ? "Asked vs did" : undefined}>
      <div className="flex flex-col gap-2" data-testid="overview-asked-vs-did">
        <p className="text-meta text-muted-foreground" data-sub="checked">
          <span className="font-medium text-foreground">Checked in the code</span> · counted from the diff, no AI
        </p>
        {rows.map((row) => (
          <div key={row.key} data-row="checked" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-label">
            <span data-chip-tone={row.chip.tone} className="text-p0">{row.chip.label}</span>
            <span className="min-w-0 break-words text-foreground"><span className="font-mono">{partName(row.target)}</span> changed</span>
            <EvidenceChip evidence={{ state: "checked" }} />
            <Button variant="link" size="compact" onClick={() => props.onOpenPart(row.target)}>
              Decision {row.order}
              <ChevronRight size={14} aria-hidden="true" />
            </Button>
          </div>
        ))}
        {aiAvailable && hints.length > 0 ? (
          <>
            <p className="text-meta text-muted-foreground" data-sub="ai">
              <Sparkles size={14} aria-hidden="true" className="mr-1 inline text-ai" />
              <span className="font-medium text-foreground">AI hints</span> · each one links to the lines it rests on
            </p>
            {hints.map((hint) => (
              <div key={hint.id} data-row="ai" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-label">
                <AiBadge />
                <span className="min-w-0 break-words text-foreground">{hint.text}</span>
                <BasedOn label={linesLabel(hint.target)} aria={linesAria(hint.target)} onOpen={() => props.onOpenPart(hint.target)} />
              </div>
            ))}
          </>
        ) : null}
        {!aiAvailable ? (
          <div data-row="ai-off" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-label">
            <Sparkles size={14} aria-hidden="true" className="text-muted-foreground" />
            <span className="min-w-0 text-foreground">
              <span className="font-medium">AI hints are unavailable</span>{" "}
              <span className="text-muted-foreground">· Facts and your review still work.</span>
            </span>
          </div>
        ) : null}
        {aiAvailable && hints.length === 0 && props.aiBlocked ? (
          <div data-row="ai-budget" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-label">
            <Sparkles size={14} aria-hidden="true" className="text-muted-foreground" />
            <span className="min-w-0 text-foreground">
              <span className="font-medium">No AI hints for this revision</span>{" "}
              <span className="text-muted-foreground">· {props.aiBlocked}. The checked rows above stay.</span>
            </span>
            <Button variant="link" size="compact" onClick={props.onOpenSettings}>Settings</Button>
          </div>
        ) : null}
      </div>
    </Section>
  );
}

function Reach({ reach }: { reach: ReachModel }) {
  return (
    <Section id="reach" title="Who can reach this">
      <div className="flex flex-col gap-2" data-testid="overview-reach">
        {reach.rows.map((row) => (
          <div key={row.key} data-reach={row.name} className="flex flex-col gap-1 text-label">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="font-mono text-foreground">{row.name}</span>
              {row.outcome === "none" ? <span>No caller found in this repository</span> : null}
              {row.outcome !== "callers" ? <EvidenceChip evidence={row.evidence} /> : null}
            </div>
            {row.sites.map((site) => (
              <div key={site.label} className="flex flex-wrap items-center gap-x-2 gap-y-1 pl-3">
                <ChevronRight size={14} aria-hidden="true" className="text-muted-foreground" />
                <span className="break-all font-mono text-foreground">{site.label}</span>
                <EvidenceChip evidence={site.evidence} />
              </div>
            ))}
          </div>
        ))}
      </div>
    </Section>
  );
}

function Untrusted({ hidden }: { hidden: { scanned: boolean; found: HiddenText[] } }) {
  const [shown, setShown] = useState(false);
  return (
    <Section id="untrusted">
      <div data-testid="overview-untrusted" className="flex flex-wrap items-start gap-2 text-label">
        <ShieldAlert size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <p className="text-foreground">
            Agent and PR text treated as untrusted · AI checks run without tools or secrets
            {hidden.found.length > 0 ? ` · ${hiddenSummary(hidden.found)}` : ""}
          </p>
          {shown ? (
            <div data-testid="overview-hidden-text" className="mt-2 flex flex-col gap-1">
              <p className="text-meta text-muted-foreground">Shown as plain text, never followed:</p>
              {hidden.found.map((row, index) => (
                <pre key={index} className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted p-2 font-mono text-meta text-foreground">
                  {row.text}
                </pre>
              ))}
            </div>
          ) : null}
        </div>
        {hidden.found.length > 0 ? (
          <Button variant="ghost" size="sm" aria-expanded={shown} data-testid="overview-hidden-toggle" onClick={() => setShown((value) => !value)}>
            {shown ? "Hide hidden text" : "Show hidden text"}
          </Button>
        ) : null}
      </div>
    </Section>
  );
}

export function OverviewEvidence(props: OverviewEvidenceProps) {
  const count = props.sections.length;
  if (props.checking) {
    return (
      <section aria-label="Evidence" data-testid="overview-evidence" className="rounded-lg border border-border bg-card px-4 py-3 text-label">
        <span className="font-semibold text-foreground">Evidence · checking…</span>
      </section>
    );
  }
  return (
    <section aria-label="Evidence" data-testid="overview-evidence">
      <DisclosureCard
        open={props.open}
        onToggle={props.onToggle}
        triggerClassName="flex items-center gap-2 text-label"
        header={
          <>
            <ChevronRight size={16} aria-hidden="true" className={props.open ? "rotate-90" : undefined} />
            <span data-testid="overview-evidence-header">
              <span className="font-semibold text-foreground">Evidence · {count}</span>
              <span className="text-muted-foreground"> · the proof behind this summary</span>
            </span>
          </>
        }
      >
        <h2 className="sr-only">Evidence</h2>
        <div className="flex flex-col gap-3">
          {props.sections.map((id) => {
            switch (id) {
              case "asked-planned-did":
                return <AskedPlannedDid key={id} {...props} />;
              case "connects":
                return props.connects ? (
                  <Section key={id} id={id}>
                    <p className="flex flex-wrap items-center gap-2 text-label text-muted-foreground" data-testid="overview-connects">
                      <span>{props.connects.text}</span>
                      <Button variant="link" size="compact" onClick={props.onOpenMap}>Open map</Button>
                    </p>
                  </Section>
                ) : null;
              case "asked-vs-did":
                return <AskedVsDid key={id} {...props} />;
              case "reach":
                return props.reach ? <Reach key={id} reach={props.reach} /> : null;
              case "untrusted":
                return <Untrusted key={id} hidden={props.hidden} />;
              case "not-checked":
                return (
                  <Section key={id} id={id}>
                    <p className="flex items-start gap-2 text-label" data-testid="overview-not-checked">
                      <span aria-hidden="true" className="mt-1.5 size-2 shrink-0 rounded-full bg-p1-solid" />
                      <span><span className="font-semibold text-foreground">Not checked:</span> {props.notChecked.join(" · ")}</span>
                    </p>
                  </Section>
                );
            }
          })}
        </div>
      </DisclosureCard>
    </section>
  );
}
