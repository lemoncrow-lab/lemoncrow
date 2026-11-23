/**
 * The inspector for whatever is selected: a component, an arrow or a flow step. It says what changed and how we know
 * (recorded in a run, checked in code, or an AI guess), lists where it is in the code with a way to open it, and for
 * anything that carries data shows the request and response before and after, field by field.
 */
import { ArrowRight, X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { Badge, type BadgeTone } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../../components/ui/tabs";
import { cn } from "../../../lib/utils";
import { KIND_WORDS, kindIcon } from "./GraphCard";
import { deltaGlyph, deltaWord, evidenceWord, payloadForEdge, stepKey, type Hit } from "./graphModel";
import { diffCounts, diffJson, diffRows } from "./payloadDiff";
import type { XRayAnchor, XRayDelta, XRayEdge, XRayEvidence, XRayFlowStep, XRayGraph, XRayGraphFlow, XRayNode, XRayPayload } from "./xrayGraphTypes";

export const EDGE_KIND_WORDS: Record<XRayEdge["kind"], string> = {
  http: "HTTP call",
  call: "Function call",
  data: "Reads or writes data",
  event: "Event",
  dependency: "Depends on",
  render: "Renders",
};
const STEP_KIND_WORDS: Record<XRayFlowStep["kind"], string> = { sync: "Call", async: "Async call", return: "Return", self: "Step inside" };

const DELTA_TONE: Record<XRayDelta, BadgeTone> = { added: "success", modified: "warning", removed: "destructive", unchanged: "neutral" };
const EVIDENCE_TONE: Record<XRayEvidence, BadgeTone> = { recorded: "info", static: "secondary", inferred: "ai" };

export function DeltaBadge({ delta }: { delta: XRayDelta }) {
  return <Badge tone={DELTA_TONE[delta]} data-testid="xg-delta">{delta === "unchanged" ? "Unchanged" : `${deltaGlyph(delta)} ${deltaWord(delta)}`}</Badge>;
}

export function EvidenceBadge({ evidence }: { evidence: XRayEvidence }) {
  return <Badge tone={EVIDENCE_TONE[evidence]} data-testid="xg-evidence">{evidenceWord(evidence)}</Badge>;
}

export interface InspectorProps {
  graph: XRayGraph;
  hit: Hit;
  onOpenAnchor(anchor: XRayAnchor): void;
  /** The behavior change named by a payload's `behavior_key`, with its judge controls; null when this revision has none. */
  renderBehavior?(key: string): ReactNode;
  /** The behavior changes recorded on a component's files, so a change no request places on an arrow still shows here. */
  renderFileBehavior?(paths: string[]): ReactNode;
  onSelect(hit: Hit): void;
  onShowFlow?(flow: string, step?: string): void;
  onClose(): void;
  className?: string;
}

type Subject =
  | { kind: "node"; node: XRayNode }
  | { kind: "edge"; edge: XRayEdge }
  | { kind: "step"; flow: XRayGraphFlow; step: XRayFlowStep; index: number };

function subjectOf(graph: XRayGraph, hit: Hit): Subject | null {
  if (hit.kind === "node") {
    const node = graph.nodes.find((n) => n.id === hit.id);
    return node ? { kind: "node", node } : null;
  }
  if (hit.kind === "edge") {
    const edge = graph.edges.find((e) => e.id === hit.id);
    return edge ? { kind: "edge", edge } : null;
  }
  if (hit.kind !== "step") return null;
  const flow = graph.flows.find((f) => f.id === hit.flow);
  const index = flow ? flow.steps.findIndex((s) => s.id === hit.id) : -1;
  return flow && index >= 0 ? { kind: "step", flow, step: flow.steps[index], index } : null;
}

export function Inspector({ graph, hit, onOpenAnchor, renderBehavior, renderFileBehavior, onSelect, onShowFlow, onClose, className }: InspectorProps) {
  const subject = useMemo(() => subjectOf(graph, hit), [graph, hit]);
  const nodes = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph.nodes]);
  if (!subject) return null;
  const label = (id: string) => nodes.get(id)?.label ?? id;

  let eyebrow: string;
  let title: string;
  let delta: XRayDelta;
  let evidence: XRayEvidence;
  let anchors: XRayAnchor[];
  let payload: { request?: XRayPayload; response?: XRayPayload } | null = null;
  let carriedBy: { flow: XRayGraphFlow; step: XRayFlowStep; index: number }[] = [];
  if (subject.kind === "node") {
    eyebrow = KIND_WORDS[subject.node.kind];
    title = subject.node.label;
    delta = subject.node.delta;
    evidence = subject.node.source;
    anchors = subject.node.anchors;
  } else if (subject.kind === "edge") {
    eyebrow = EDGE_KIND_WORDS[subject.edge.kind];
    title = subject.edge.label || `${label(subject.edge.from)} → ${label(subject.edge.to)}`;
    delta = subject.edge.delta;
    evidence = subject.edge.evidence;
    anchors = subject.edge.anchors;
    const found = payloadForEdge(graph, subject.edge);
    if (found) {
      payload = found;
      carriedBy = found.steps.map(({ flow, step }) => ({ flow, step, index: flow.steps.indexOf(step) }));
    }
  } else {
    eyebrow = `${STEP_KIND_WORDS[subject.step.kind]} · step ${subject.index + 1} of ${subject.flow.steps.length}`;
    title = subject.step.label;
    delta = subject.step.delta;
    evidence = subject.step.evidence;
    anchors = subject.step.anchors;
    payload = subject.step.payload && (subject.step.payload.request || subject.step.payload.response) ? subject.step.payload : null;
  }
  const Icon = subject.kind === "node" ? kindIcon(subject.node) : null;
  const ends = subject.kind === "edge" ? [subject.edge.from, subject.edge.to] : subject.kind === "step" && subject.step.kind !== "self" ? [subject.step.from, subject.step.to] : null;

  return (
    <aside className={cn("xg-inspector", className)} aria-label={`Details: ${title}`} data-testid="xg-inspector" data-kind={subject.kind}>
      <div className="xg-inspector-head">
        <div className="flex items-center justify-between gap-2">
          <span className="xg-eyebrow flex items-center gap-1.5">
            {Icon ? <Icon size={14} aria-hidden="true" /> : null}
            {eyebrow}
          </span>
          <Button variant="ghost" size="icon" aria-label="Close details" onClick={onClose} icon={<X size={14} aria-hidden="true" />} />
        </div>
        <h3 className={cn("m-0 break-words text-label font-semibold", delta === "removed" && "line-through decoration-foreground/50")} data-testid="xg-inspector-title">{title}</h3>
        {subject.kind === "node" && subject.node.subtitle ? <span className="xg-mono text-caption text-muted-foreground">{subject.node.subtitle}</span> : null}
        <div className="flex flex-wrap items-center gap-1.5">
          <DeltaBadge delta={delta} />
          <EvidenceBadge evidence={evidence} />
          {subject.kind === "node" ? subject.node.badges.map((badge) => <Badge key={badge} tone="secondary">{badge}</Badge>) : null}
          {subject.kind === "step" && subject.step.repeat && subject.step.repeat >= 2 ? <Badge tone="secondary">{`×${subject.step.repeat}`}</Badge> : null}
        </div>
        {ends ? (
          <div className="mt-1 flex flex-col gap-0.5" aria-label="Ends">
            {ends.map((id, i) => (
              <Button key={`${id}:${i}`} unstyled className="xg-link-row" onClick={() => onSelect({ kind: "node", id })}>
                <span className="xg-tone-dot" data-tone={nodes.get(id)?.delta} aria-hidden="true" />
                <span className="w-9 text-micro text-muted-foreground">{i === 0 ? "From" : "To"}</span>
                <span className="min-w-0 truncate font-medium">{label(id)}</span>
              </Button>
            ))}
          </div>
        ) : null}
        {subject.kind === "step" && subject.step.note ? <p className="m-0 text-caption text-subtle">{subject.step.note}</p> : null}
      </div>

      <section className="xg-inspector-section" aria-label="How we know">
        <span className="xg-eyebrow">How we know</span>
        <p className="m-0 text-caption text-subtle" data-testid="xg-evidence-text">{evidenceText(graph, subject, evidence)}</p>
      </section>

      {payload ? (
        <section className="xg-inspector-section" aria-label="Data it carries" data-testid="xg-payload">
          <span className="xg-eyebrow">Data it carries</span>
          <PayloadTabs payload={payload} renderBehavior={renderBehavior} />
          {carriedBy.length ? (
            <div className="flex flex-col gap-0.5">
              {carriedBy.map(({ flow, step, index }) => (
                <Button key={stepKey(flow.id, step.id)} unstyled className="xg-link-row text-muted-foreground" onClick={() => onShowFlow?.(flow.id, step.id)}>
                  <ArrowRight size={14} aria-hidden="true" />
                  <span className="min-w-0 truncate">{`Step ${index + 1} of “${flow.title}”`}</span>
                </Button>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="xg-inspector-section" aria-label="In the code">
        <span className="xg-eyebrow">In the code</span>
        {anchors.length ? (
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0" data-testid="xg-anchors">
            {anchors.map((anchor, index) => (
              <li key={`${anchor.path}:${anchor.start_line ?? 0}:${index}`} className="xg-anchor">
                <span className="flex min-w-0 flex-col">
                  <span className="xg-anchor-path" title={anchor.path}>{`‎${anchor.path}`}</span>
                  <span className="text-micro text-muted-foreground">{anchorLines(anchor)}</span>
                </span>
                <Button variant="outline" size="xs" onClick={() => onOpenAnchor(anchor)} aria-label={`Open ${anchor.path}${anchor.start_line ? ` at line ${anchor.start_line}` : ""}`}>Open</Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="m-0 text-caption text-muted-foreground">No place in this repository{subject.kind === "node" && subject.node.kind === "external" ? ": it is an outside service." : "."}</p>
        )}
      </section>

      {subject.kind === "node" && renderFileBehavior && anchors.length ? renderFileBehavior([...new Set(anchors.map((anchor) => anchor.path))]) : null}

      {subject.kind === "node" ? <NodeLinks graph={graph} node={subject.node} label={label} onSelect={onSelect} onShowFlow={onShowFlow} /> : null}
    </aside>
  );
}

function anchorLines(anchor: XRayAnchor): string {
  const side = anchor.side === "base" ? "Before" : "After";
  if (!anchor.start_line) return side;
  const end = anchor.end_line && anchor.end_line !== anchor.start_line ? `–${anchor.end_line}` : "";
  return `${side} · lines ${anchor.start_line}${end}`;
}

function evidenceText(graph: XRayGraph, subject: Subject, evidence: XRayEvidence): string {
  if (evidence === "inferred") return "AI guess: a model proposed this and nothing in the code or a run confirmed it. Treat it as a lead, not a fact.";
  if (evidence === "static") return "Checked in code: the code index found it at the lines below. No run was recorded passing through it.";
  // Only the run that observed it: never every run on the revision.
  const seen = subject.kind === "edge" ? subject.edge.recorded : subject.kind === "step" ? subject.step.recorded : undefined;
  if (seen) {
    const run = graph.provenance.recorded_runs.find((r) => r.ref === seen.ref);
    const times = seen.count === 1 ? "once" : `${seen.count} times`;
    if (run) return `Recorded in a run: seen ${times} in ${run.recipe} (${sideWords(run.side)}, ${formatWhen(run.at)}, ${run.ref}).`;
    return `Recorded in a run: seen ${times} in run ${seen.ref}.`;
  }
  if (subject.kind === "node") {
    // A component names the runs its own recorded arrows and steps name.
    const id = subject.node.id;
    const touches = (link: { from: string; to: string }) => link.from === id || link.to === id;
    const refs = new Set<string>();
    for (const edge of graph.edges) if (edge.recorded && touches(edge)) refs.add(edge.recorded.ref);
    for (const flow of graph.flows) for (const step of flow.steps) if (step.recorded && touches(step)) refs.add(step.recorded.ref);
    const runs = graph.provenance.recorded_runs.filter((r) => refs.has(r.ref));
    if (!runs.length) return "Recorded in a run: a run on this revision reached this component.";
    return `Recorded in a run: ${[...new Set(runs.map((r) => `${r.recipe} (${sideWords(r.side)})`))].join(" and ")} reached this component.`;
  }
  return `Recorded in a run: this ${subject.kind === "edge" ? "arrow" : "step"} was observed in a run on this revision.`;
}

function sideWords(side: "base" | "head"): string {
  return side === "base" ? "before the change" : "after the change";
}

function formatWhen(at: string): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return at;
  return date.toLocaleString("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }) + " UTC";
}

function PayloadTabs({ payload, renderBehavior }: { payload: { request?: XRayPayload; response?: XRayPayload }; renderBehavior?(key: string): ReactNode }) {
  const tabs = (["request", "response"] as const).filter((key) => payload[key]);
  const changed = (p?: XRayPayload) => {
    if (!p) return false;
    const counts = diffCounts(diffJson(p.before, p.after));
    return counts.changed + counts.added + counts.removed > 0;
  };
  const [tab, setTab] = useState<string>(payload.response && (changed(payload.response) || !changed(payload.request)) ? "response" : tabs[0]);
  const value = tabs.includes(tab as (typeof tabs)[number]) ? tab : tabs[0];
  return (
    <Tabs value={value} onValueChange={setTab} className="flex flex-col gap-2">
      <TabsList aria-label="Request or response" className="self-start">
        {tabs.map((key) => (
          <TabsTrigger key={key} value={key} data-testid={`xg-payload-tab-${key}`}>
            {key === "request" ? "Request" : "Response"}
            {changed(payload[key]) ? <span className="xg-tone-dot ml-1.5" data-tone="modified" aria-label="changed" /> : null}
          </TabsTrigger>
        ))}
      </TabsList>
      {tabs.map((key) => (
        <TabsContent key={key} value={key} className="m-0">
          <PayloadPanel payload={payload[key]!} renderBehavior={renderBehavior} />
        </TabsContent>
      ))}
    </Tabs>
  );
}

export function PayloadPanel({ payload, renderBehavior }: { payload: XRayPayload; renderBehavior?(key: string): ReactNode }) {
  const diff = useMemo(() => diffJson(payload.before, payload.after), [payload.before, payload.after]);
  const rows = useMemo(() => diffRows(diff), [diff]);
  const counts = diffCounts(diff);
  const hasBefore = payload.before !== undefined && payload.before !== null;
  const hasAfter = payload.after !== undefined && payload.after !== null;
  const status = payload.status;
  const statusChanged = Boolean(status && status.before !== status.after);
  const summary = !hasBefore && !hasAfter
    ? "No sample was kept on either side."
    : !hasBefore ? "Only After has a sample: this is new."
      : !hasAfter ? "Only Before has a sample: this is gone."
        : counts.changed + counts.added + counts.removed === 0 ? "Same on both sides."
          : [counts.changed && `${counts.changed} changed`, counts.added && `${counts.added} added`, counts.removed && `${counts.removed} removed`].filter(Boolean).join(" · ");
  const behavior = payload.behavior_key && renderBehavior ? renderBehavior(payload.behavior_key) : null;
  return (
    <div className="flex flex-col gap-2" data-testid="xg-payload-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {payload.type ? <span className="xg-mono text-caption font-semibold">{payload.type}</span> : null}
        {status ? (
          <span className="xg-status" data-changed={statusChanged ? "true" : undefined} data-testid="xg-status">
            {`${status.before ?? "—"} → ${status.after ?? "—"}`}
          </span>
        ) : null}
        <Badge tone={payload.evidence === "recorded" ? "info" : "ai"} data-testid="xg-payload-evidence">{evidenceWord(payload.evidence)}</Badge>
      </div>
      {payload.shape ? <code className="xg-mono block break-words text-micro text-muted-foreground">{payload.shape}</code> : null}
      <span className="text-caption text-subtle" data-testid="xg-diff-summary">{summary}</span>
      {rows.length && (hasBefore || hasAfter) ? (
        <div className="xg-diff" data-testid="xg-diff">
          {rows.map((row, index) => (
            <div
              key={index}
              className="xg-diff-row"
              data-kind={row.kind}
              data-frame={row.kind === "changed" && row.was === undefined ? "true" : undefined}
              data-testid="xg-diff-row"
            >
              <span className="xg-diff-glyph" aria-hidden="true">{row.close ? " " : row.kind === "added" ? "+" : row.kind === "removed" ? "−" : row.kind === "changed" ? "~" : " "}</span>
              <span>
                {"  ".repeat(row.depth)}
                {row.key}
                {row.was !== undefined ? <span className="xg-diff-was">{row.was}</span> : null}
                {row.text}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      <span className="text-micro text-muted-foreground">
        {[payload.evidence_ref ? `Sample ${payload.evidence_ref}` : "", payload.redacted ? "Some values were masked before they were stored." : ""].filter(Boolean).join(" · ")}
      </span>
      {payload.behavior_key ? (
        <div className="flex flex-col gap-2" data-testid="xg-behavior" data-key={payload.behavior_key}>
          {behavior ?? <p className="m-0 text-caption text-muted-foreground">{`No behavior change is listed for ${payload.behavior_key} on this revision.`}</p>}
        </div>
      ) : null}
    </div>
  );
}

function NodeLinks({ graph, node, label, onSelect, onShowFlow }: { graph: XRayGraph; node: XRayNode; label(id: string): string; onSelect(hit: Hit): void; onShowFlow?(flow: string, step?: string): void }) {
  const edges = graph.edges.filter((e) => e.from === node.id || e.to === node.id);
  const flows = graph.flows.filter((f) => f.participants.some((p) => p.node === node.id));
  if (!edges.length && !flows.length) return null;
  return (
    <section className="xg-inspector-section" aria-label="Connections">
      <span className="xg-eyebrow">{`Connections · ${edges.length}`}</span>
      <div className="flex flex-col gap-0.5">
        {edges.map((edge) => {
          const out = edge.from === node.id;
          return (
            <Button key={edge.id} unstyled className="xg-link-row" onClick={() => onSelect({ kind: "edge", id: edge.id })}>
              <span className="xg-tone-dot" data-tone={edge.delta} aria-hidden="true" />
              <span className="w-9 shrink-0 text-micro text-muted-foreground">{out ? "To" : "From"}</span>
              <span className="min-w-0 flex-1 truncate font-medium">{label(out ? edge.to : edge.from)}</span>
              {edge.label ? <span className="max-w-[45%] truncate text-micro text-muted-foreground">{edge.label}</span> : null}
            </Button>
          );
        })}
      </div>
      {flows.length && onShowFlow ? (
        <>
          <span className="xg-eyebrow mt-1">Flows</span>
          <div className="flex flex-col gap-0.5">
            {flows.map((flow) => (
              <Button key={flow.id} unstyled className="xg-link-row" onClick={() => onShowFlow(flow.id)}>
                <ArrowRight size={14} aria-hidden="true" />
                <span className="min-w-0 truncate">{flow.title}</span>
              </Button>
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}
