import { useState } from "react";
import { ArrowRight, Code2, ExternalLink, Focus, Sparkles, X } from "lucide-react";

import { Button } from "../components/ui/button";
import type { CodeTarget } from "../lib/codeLinks";
import { InspectorFacts, InspectorList, InspectorRow, InspectorSection as Section } from "../map/MapInspector";
import ExecutionFlowSteps from "./ExecutionFlowSteps";
import FileTypeIcon from "./FileTypeIcon";
import { MapProjectHistory } from "./ProjectHistory";
import { ChangeBadge, NodeSignals } from "./ReviewMapCanvas";
import {
  EDGE_KIND_LABEL,
  baseName,
  edgeKind,
  isChangedStatus,
  isFullyReviewed,
  nodeName,
  reasonsOf,
  relationshipsOf,
  relationshipsPending,
  reviewQueue,
  reviewableSymbols,
  symbolKindLabel,
  visualEnds,
  type EdgeKind,
  type Relationship,
} from "./reviewMapModel";
import type {
  ReviewArchitectureExplanation,
  ReviewArchitectureProvenance,
  ReviewMapEdge,
  ReviewMapMemberSymbol,
  ReviewMapNode,
  ReviewMapPayload,
  ReviewMapSelection,
  ReviewProjectContext,
  ReviewProjectContextSelection,
} from "./types";

function plural(count: number, one: string, many = one + "s"): string {
  return count.toLocaleString() + " " + (count === 1 ? one : many);
}

export function KindSwatch({ kind }: { kind: EdgeKind }) {
  return (
    <svg className="rm-swatch shrink-0" width="22" height="8" viewBox="0 0 22 8" aria-hidden="true">
      <path className="rm-edge" data-kind={kind} data-state={kind === "impacts" ? "idle" : "active"} d="M1,4 H15" strokeWidth={1.4} />
      <path className="rm-arrow" data-kind={kind} data-state={kind === "impacts" ? "idle" : "active"} d="M19.5,4 L15,1.5 L15,6.5 Z" />
    </svg>
  );
}

function statusLine(node: ReviewMapNode): string {
  if (node.status === "new") return "Added in this change";
  if (node.status === "changed") {
    const change = (node.change || node.change_kinds?.[0] || "modified").toLowerCase();
    return change === "deleted" || change === "removed" ? "Deleted in this change" : change === "renamed" ? "Renamed in this change" : "Modified in this change";
  }
  if (node.status === "impacted") return "Unchanged, but reached by the change";
  return "Surrounding code";
}

function NodeGlyph({ node }: { node: ReviewMapNode }) {
  return isChangedStatus(node) ? <ChangeBadge node={node} /> : <FileTypeIcon path={node.path} size={15} />;
}

function RelationshipList({
  title,
  rows,
  emptyText,
  onSelectNode,
  onSelectEdge,
}: {
  title: string;
  rows: Relationship[];
  emptyText: string;
  onSelectNode(id: string): void;
  onSelectEdge(id: string): void;
}) {
  const [all, setAll] = useState(false);
  // One row per neighbor; each link kind is its own inspectable chip.
  const grouped: { otherId: string; other: ReviewMapNode | undefined; links: Relationship[] }[] = [];
  const index = new Map<string, number>();
  for (const row of rows) {
    const at = index.get(row.otherId);
    if (at === undefined) {
      index.set(row.otherId, grouped.length);
      grouped.push({ otherId: row.otherId, other: row.other, links: [row] });
    } else {
      grouped[at].links.push(row);
    }
  }
  const shown = all ? grouped : grouped.slice(0, 8);
  return (
    <Section title={title} aside={grouped.length ? grouped.length : undefined}>
      {grouped.length === 0 ? (
        <p className="text-label leading-5 text-muted-foreground">{emptyText}</p>
      ) : (
        <InspectorList>
          {shown.map((group) => (
            <li key={group.otherId} className={group.links.length > 1 ? "flex flex-wrap items-center gap-x-1" : "flex items-center gap-1"}>
              <InspectorRow
                className={(group.links.length > 1 ? "basis-full " : "") + "flex-1"}
                onClick={() => (group.other ? onSelectNode(group.otherId) : onSelectEdge(group.links[0].edge.id))}
                aria-label={`Select ${group.other ? nodeName(group.other) : "relationship"}`}
                title={group.other?.path}
              >
                {group.other ? <NodeGlyph node={group.other} /> : <span className="h-[15px] w-[15px]" />}
                <span className="min-w-0 flex-1 truncate text-body text-neutral-200">{group.other ? nodeName(group.other) : group.otherId}</span>
              </InspectorRow>
              {group.links.map((row, linkIndex) => (
                <Button
                  unstyled
                  key={row.edge.id}
                  className={(group.links.length > 1 ? (linkIndex === 0 ? "-mt-1 ml-7 " : "-mt-1 ") : "") + "flex shrink-0 items-center gap-1.5 rounded-md px-1.5 py-1.5 text-caption tabular-nums text-muted-foreground transition-colors hover:bg-muted hover:text-neutral-200"}
                  onClick={() => onSelectEdge(row.edge.id)}
                  aria-label={`Inspect ${EDGE_KIND_LABEL[row.kind]} link${group.other ? " with " + nodeName(group.other) : ""}`}
                >
                  <KindSwatch kind={row.kind} />
                  {EDGE_KIND_LABEL[row.kind]}{row.edge.weight > 1 ? ` ×${row.edge.weight}` : ""}
                </Button>
              ))}
            </li>
          ))}
        </InspectorList>
      )}
      {grouped.length > 8 && (
        <Button variant="link" size="xs" className="mt-1.5 h-auto text-label" onClick={() => setAll((value) => !value)}>
          {all ? "Show fewer" : `Show all ${grouped.length}`}
        </Button>
      )}
    </Section>
  );
}

function ExactnessNote({
  exactness,
  subject,
  deltaStatus,
  provenance,
}: {
  exactness: string;
  subject: string;
  deltaStatus?: string;
  provenance?: ReviewArchitectureProvenance;
}) {
  let text: string;
  if (provenance?.source === "snapshot_comparison" && provenance.comparison === "base_vs_snapshot") {
    text = `This ${subject} is backed by an exact base-to-Snapshot comparison${deltaStatus ? ` (${deltaStatus})` : ""}.`;
  } else if (exactness === "snapshot") {
    text = `This ${subject} is an exact Snapshot fact, but the base-to-Snapshot architectural change is ${deltaStatus === "unknown" ? "unknown" : "not established by this evidence"}.`;
  } else if (exactness === "current_index") {
    text = `This ${subject} comes from the current code index, not the reviewed Snapshot. Treat it as navigation context, never as a claim that this Change altered the architecture.`;
  } else if (exactness === "inferred") {
    text = `This ${subject} is an inferred explanation over evidence, not an architectural fact.`;
  } else {
    text = `The exactness of this ${subject} is unavailable.`;
  }
  return <p className="px-4 py-3 text-caption leading-4 text-muted-foreground">{text}</p>;
}

export type { CodeTarget };

interface InspectorProps {
  payload: ReviewMapPayload;
  overview: ReviewMapPayload | null;
  selected: ReviewMapNode | null;
  selectedEdge: ReviewMapEdge | null;
  onSelectNode(id: string): void;
  onSelectEdge(id: string): void;
  onClear(): void;
  onFocusFile(id: string): void;
  onOpenSymbol(symbol: ReviewMapMemberSymbol): void;
  selectionHref?(selection: ReviewMapSelection): string | null;
  onOpenSelection(selection: ReviewMapSelection): void;
  onAskSelection(selection: ReviewMapSelection, label: string): void;
  /** Opens the working tree's current file in Code; absent without a trusted local workspace. */
  onOpenInCode?(target: CodeTarget): void;
  /** Overlaying the canvas (narrow windows): the guide needs its own close. */
  overlay?: boolean;
  architectureExplanation?: ReviewArchitectureExplanation | null;
  architectureExplainLoading?: boolean;
  architectureExplainError?: string;
  onExplainArchitecture?(): void;
  /** PK2 landed history for a selection; absent when Project Knowledge is not available. */
  loadProjectHistory?(selection: ReviewProjectContextSelection): Promise<ReviewProjectContext | null>;
}

function nodeHistorySelection(node: ReviewMapNode): ReviewProjectContextSelection {
  const paths = (node.paths?.length ? node.paths : node.path ? [node.path] : []).slice(0, 16);
  return {
    paths,
    components: [node.component, node.module].filter((value): value is string => Boolean(value)),
    symbols: node.qualified_name ? [node.qualified_name] : [],
  };
}

// Project Knowledge defines no edge subject key yet (it waits for a PK0/PK1 edge
// subject), so an edge's history is its two endpoints' paths and components;
// the Map never invents an edge key format.
export function edgeHistorySelection(from?: ReviewMapNode, to?: ReviewMapNode): ReviewProjectContextSelection {
  const ends = [from, to].filter((node): node is ReviewMapNode => Boolean(node));
  const paths = ends.flatMap((node) => (node.paths?.length ? node.paths : node.path ? [node.path] : [])).slice(0, 16);
  const components = ends.flatMap((node) => [node.component, node.module]).filter((value): value is string => Boolean(value));
  return { paths: [...new Set(paths)], components: [...new Set(components)] };
}

export default function ReviewMapInspector(props: InspectorProps) {
  const { payload, selected, selectedEdge } = props;
  return (
    <aside
      className="flex h-full min-h-0 w-full flex-col overflow-y-auto border-l border-border/80 bg-background"
      aria-label="Review map inspector"
    >
      {selectedEdge ? (
        <EdgeDetails {...props} edge={selectedEdge} />
      ) : selected ? (
        <NodeDetails {...props} node={selected} />
      ) : payload.level === "architecture" ? (
        payload.projection === "architecture_delta" ? <ArchitectureDeltaGuide {...props} /> : <ChangeGuide {...props} />
      ) : null}
    </aside>
  );
}

function ArchitectureDeltaGuide({
  payload,
  onSelectNode,
  onClear,
  overlay,
  architectureExplanation,
  architectureExplainLoading,
  architectureExplainError,
  onExplainArchitecture,
}: InspectorProps) {
  const delta = payload.architecture_delta;
  if (!delta) return <ChangeGuide payload={payload} overview={null} selected={null} selectedEdge={null} onSelectNode={onSelectNode} onSelectEdge={() => undefined} onClear={onClear} onFocusFile={() => undefined} onOpenSymbol={() => undefined} onOpenSelection={() => undefined} onAskSelection={() => undefined} overlay={overlay} />;
  const summary = delta.summary;
  const changedComponents = delta.grouping.components.filter((item) => item.delta_status !== "unchanged");
  const changedNodes = payload.graph.nodes.filter((node) => node.delta_status === "added" || node.delta_status === "removed" || node.delta_status === "changed");
  const canExplain = summary.material && summary.changed_edges > 0 && Boolean(onExplainArchitecture);
  return (
    <>
      <div className="px-4 pb-3.5 pt-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-title font-semibold text-foreground">Architecture delta</h2>
            <p className="mt-1.5 text-body leading-5 text-neutral-400">
              {delta.status === "unavailable"
                ? delta.reason || "Exact base-to-Snapshot architecture comparison is unavailable."
                : summary.material
                  ? `${plural(summary.changed_components, "component")} and ${plural(summary.changed_edges, "relationship")} changed between the immutable base and Snapshot.`
                  : delta.reason || "No material architecture relationship change was detected."}
            </p>
          </div>
          {overlay && <Button variant="ghost" size="icon" className="-mr-1.5 -mt-1" onClick={onClear} aria-label="Close architecture delta guide"><X size={13} /></Button>}
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5 text-caption">
          <span className="rounded-md border border-border bg-muted/60 px-2 py-1 text-neutral-300">{summary.added_edges} added</span>
          <span className="rounded-md border border-border bg-muted/60 px-2 py-1 text-neutral-300">{summary.removed_edges} removed</span>
          <span className="rounded-md border border-border bg-muted/60 px-2 py-1 text-neutral-300">{summary.modified_edges} changed</span>
          {summary.context_only_edges > 0 && <span className="rounded-md border border-border bg-muted/60 px-2 py-1 text-muted-foreground">{summary.context_only_edges} current-index context</span>}
        </div>
      </div>

      {changedNodes.length > 0 && (
        <Section title="Changed shape" aside="base → Snapshot">
          <ol className="-mx-1.5 space-y-0.5">
            {changedNodes.slice(0, 10).map((node) => (
              <li key={node.id}>
                <InspectorRow onClick={() => onSelectNode(node.id)} aria-label={`Select ${nodeName(node)}`}>
                  <span className="rm-kind-tag">{node.delta_status}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-label text-neutral-200">{node.path}</span>
                </InspectorRow>
              </li>
            ))}
          </ol>
        </Section>
      )}

      {changedComponents.length > 0 && (
        <Section title="Components" aside={`${changedComponents.length} changed`}>
          <ul className="space-y-1.5 text-label text-neutral-400">
            {changedComponents.slice(0, 12).map((component) => (
              <li key={component.id} className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate font-mono text-caption text-neutral-300">{component.label}</span>
                <span className="shrink-0 text-caption text-muted-foreground">{component.delta_status} · {component.base_file_count}→{component.snapshot_file_count}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Relationship evidence">
        <p className="text-label leading-5 text-neutral-400">
          Added, removed, and changed links are computed from deterministic reference graphs over the immutable base and Snapshot. Snapshot-only facts without comparable base evidence stay <span className="text-neutral-200">unknown</span>; live index links stay <span className="text-neutral-200">context only</span>.
        </p>
      </Section>

      <Section title="Execution flow">
        {delta.flow.applicable ? (
          <ExecutionFlowSteps flow={delta.flow} />
        ) : (
          <p className="text-label leading-5 text-muted-foreground">{delta.flow.reason}</p>
        )}
        {canExplain && (
          <Button
            variant="outline"
            size="xs"
            className="mt-3"
            disabled={architectureExplainLoading}
            onClick={onExplainArchitecture}
            icon={architectureExplainLoading ? undefined : <Sparkles size={12} />}
          >
            {architectureExplainLoading ? "Explaining…" : "Explain exact delta"}
          </Button>
        )}
        {architectureExplainError && <p role="alert" className="mt-2 text-caption leading-4 text-rose-300">{architectureExplainError}</p>}
      </Section>

      {architectureExplanation && (
        <Section title="AI explanation" aside={architectureExplanation.exactness === "inferred" ? "inferred" : architectureExplanation.exactness}>
          <p className="text-label leading-5 text-neutral-300">{architectureExplanation.summary}</p>
          {architectureExplanation.why_it_matters.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-4 text-caption leading-4 text-neutral-400">
              {architectureExplanation.why_it_matters.map((item, index) => <li key={index}>{item}</li>)}
            </ul>
          )}
          {architectureExplanation.flow.length > 0 && (
            <ul className="mt-2 space-y-1 text-caption leading-4 text-neutral-400">
              {architectureExplanation.flow.map((item) => <li key={item.edge_id}><span className="font-mono text-muted-foreground">{item.edge_id}</span> · {item.explanation}</li>)}
            </ul>
          )}
          {architectureExplanation.limitations.length > 0 && <p className="mt-2 text-caption leading-4 text-muted-foreground">{architectureExplanation.limitations.join(" · ")}</p>}
        </Section>
      )}

      {(summary.important_concerns.length > 0 || summary.degraded.length > 0) && (
        <Section title="Coverage">
          <ul className="space-y-1.5 text-caption leading-4 text-muted-foreground">
            {summary.important_concerns.map((item) => <li key={item}>{item}</li>)}
            {summary.degraded.map((item) => <li key={item}>{item.replace(/_/g, " ")}.</li>)}
          </ul>
        </Section>
      )}
    </>
  );
}

function ChangeGuide({ payload, onSelectNode, onClear, overlay }: InspectorProps) {
  const nodes = payload.graph.nodes;
  const stats = payload.graph.stats;
  const changed = stats?.changed_files ?? nodes.filter(isChangedStatus).length;
  const added = stats?.new_files ?? nodes.filter((node) => node.status === "new").length;
  const affected = stats?.impacted_files ?? nodes.filter((node) => node.status === "impacted").length;
  const folders = new Set(nodes.filter(isChangedStatus).map((node) => node.module ?? node.path.slice(0, Math.max(0, node.path.lastIndexOf("/"))))).size;
  const queue = reviewQueue(payload);
  const reviewedAll = nodes.filter(isChangedStatus).every(isFullyReviewed) && nodes.some((node) => node.review?.targets);
  const verification = payload.snapshot.verification;
  const filtered = stats?.filtered_relationships ?? 0;
  const pending = relationshipsPending(payload);
  const impactSkipped = payload.snapshot.degraded.some((name) => name.startsWith("impact_"));
  const degradedNotes = payload.snapshot.degraded_notes
    ?? payload.snapshot.degraded.map((name) => ({ name, note: name.replace(/_/g, " ") }));
  const summary = [
    `${plural(changed, "file")} changed across ${plural(folders, "folder")}`,
    added ? `${added} added` : "",
  ].filter(Boolean).join(", ") + ".";
  return (
    <>
      <div className="px-4 pb-3.5 pt-4">
        <div className="flex items-start justify-between gap-3">
          <h2 className="text-title font-semibold text-foreground">This change</h2>
          {overlay && <Button variant="ghost" size="icon" className="-mr-1.5 -mt-1" onClick={onClear} aria-label="Close map guide"><X size={13} /></Button>}
        </div>
        <p className="mt-1.5 text-body leading-5 text-neutral-400">
          {summary}{" "}
          {affected
            ? `${plural(affected, "unchanged file")} ${affected === 1 ? "is" : "are"} reached by it.`
            : impactSkipped
              ? "Change-impact analysis did not run for this Snapshot."
              : "No unchanged file was found to be reached by it."}
          {pending && " Surrounding relationships are still being indexed and will appear shortly."}
        </p>
      </div>

      <Section title={reviewedAll ? "All changed files reviewed" : "Start here"} aside={queue.length ? "by review priority" : undefined}>
        {queue.length ? (
          <ol className="-mx-1.5 space-y-0.5">
            {queue.map((node) => (
              <li key={node.id}>
                <InspectorRow
                  className="items-start"
                  onClick={() => onSelectNode(node.id)}
                  aria-label={`Select ${nodeName(node)}`}
                >
                  <ChangeBadge node={node} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-body font-medium text-foreground">{nodeName(node)}</span>
                      <NodeSignals node={node} size={11} />
                    </span>
                    <span className="mt-0.5 block truncate text-caption text-muted-foreground first-letter:uppercase">
                      {reasonsOf(node)[0] ?? node.path}
                    </span>
                  </span>
                </InspectorRow>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-label leading-5 text-muted-foreground">Every changed file with review targets is marked reviewed.</p>
        )}
      </Section>

      <Section title="Reading the map">
        <ul className="space-y-1.5 text-label leading-4 text-neutral-400">
          <li><span className="text-neutral-200">Changed</span> files sit in the middle, grouped by folder and ordered by review priority.</li>
          <li><span className="text-neutral-200">Used by</span> (left) is code that depends on the change; <span className="text-neutral-200">Uses</span> (right) is code the change depends on.</li>
          <li>Hover or select a file to trace its links. Double-click to map one file; press <kbd className="rounded border border-neutral-700 px-1 font-mono text-caption">o</kbd> to open it in the diff.</li>
        </ul>
        <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-caption text-neutral-400">
          <span className="flex items-center gap-1.5"><KindSwatch kind="impacts" />affects (Snapshot)</span>
          <span className="flex items-center gap-1.5"><KindSwatch kind="calls" />calls</span>
          <span className="flex items-center gap-1.5"><KindSwatch kind="imports" />imports</span>
          <span className="flex items-center gap-1.5"><KindSwatch kind="references" />references</span>
        </div>
      </Section>

      <Section title="Checks">
        {verification.pass + verification.fail + verification.not_run + verification.unknown === 0 ? (
          <p className="text-label text-muted-foreground">No verification was recorded for this Snapshot.</p>
        ) : (
          <p className="text-label tabular-nums text-neutral-400">
            <span className={verification.fail ? "text-rose-400" : ""}>{plural(verification.fail, "failing check")}</span>
            {" · "}{verification.pass} passing
            {verification.not_run + verification.unknown > 0 ? ` · ${verification.not_run + verification.unknown} not run or unknown` : ""}
          </p>
        )}
      </Section>

      {(payload.context.source !== "current_index" || filtered > 0 || payload.snapshot.degraded.length > 0) && (
        <Section title="Coverage">
          <ul className="space-y-1.5 text-caption leading-4 text-muted-foreground">
            {payload.context.source !== "current_index" && <li>{payload.context.note || "Surrounding relationships are unavailable; only Snapshot facts are shown."}</li>}
            {filtered > 0 && <li>{plural(filtered, "name-collision link")} between unrelated languages or into test code {filtered === 1 ? "was" : "were"} left out.</li>}
            {degradedNotes.map((item) => (
              <li key={item.name} className="first-letter:uppercase" title={item.name}>{item.note}.</li>
            ))}
          </ul>
        </Section>
      )}
    </>
  );
}

function NodeDetails({
  payload,
  overview,
  node,
  onSelectNode,
  onSelectEdge,
  onClear,
  onFocusFile,
  onOpenSymbol,
  selectionHref,
  onOpenSelection,
  onAskSelection,
  onOpenInCode,
  loadProjectHistory,
}: InspectorProps & { node: ReviewMapNode }) {
  const exact = node.exactness === "snapshot";
  const isFile = node.kind === "file";
  const level = payload.level;
  const { usedBy, uses } = relationshipsOf(payload, node.id);
  const review = node.review;
  const symbols = reviewableSymbols(node.symbols ?? []);
  const reasons = reasonsOf(node);
  const pending = relationshipsPending(payload);
  const siblingFile = level === "symbol" && node.id === payload.graph.focus
    ? overview?.graph.nodes.find((item) => item.kind === "file" && item.path === node.path)
    : undefined;
  const siblings = (siblingFile?.symbols ?? []).filter((symbol) => symbol.id !== node.id);
  const canFocusFile = isFile && exact && level !== "file";
  const diffHref = exact && node.path ? selectionHref?.(node.selection) ?? null : null;
  const openLabel = !exact ? "" : diffHref || isChangedStatus(node) || level === "symbol" ? "Open in diff" : "Open source";
  const location = node.path + (!isFile && node.line > 0 ? `:${node.line}` : "");
  const subject = isFile ? "file" : "symbol";

  return (
    <>
      <InspectorFacts
        glyph={<NodeGlyph node={node} />}
        title={nodeName(node)}
        titleHint={node.label}
        location={location}
        headerAction={
          <Button variant="ghost" size="xs" onClick={() => onAskSelection(node.selection, node.label)} icon={<Sparkles size={12} />} aria-label={`Ask about this ${subject}`}>
            Ask
          </Button>
        }
        onClose={onClear}
        closeLabel="Close map inspector"
      >
        <div className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-label text-neutral-400">
          <span>{statusLine(node)}</span>
          {(node.additions || node.deletions) ? (
            <span className="tabular-nums"><span className="text-emerald-400">+{node.additions ?? 0}</span> <span className="text-rose-400">−{node.deletions ?? 0}</span></span>
          ) : null}
          {node.delta_status && <span className="text-muted-foreground">· architecture {node.delta_status.replace(/_/g, " ")}</span>}
          {node.exactness === "current_index" && <span className="text-muted-foreground">· current index</span>}
          {node.exactness === "unknown" && <span className="text-muted-foreground">· exactness unavailable</span>}
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {openLabel && node.path && (
            // In-Review jump; Back returns to this Map location.
            <Button variant="default" size="xs" onClick={() => onOpenSelection(node.selection)}>
              <ArrowRight size={12} aria-hidden="true" />{openLabel}
            </Button>
          )}
          {diffHref && (
            <Button asChild variant="ghost" size="xs" tooltip="Opens the file's diff in a new tab">
              <a href={diffHref} target="_blank" rel="noopener noreferrer" aria-label={`${openLabel} in a new tab`}>
                <ExternalLink size={12} aria-hidden="true" />New tab
              </a>
            </Button>
          )}
          {canFocusFile && (
            <Button variant="outline" size="xs" onClick={() => onFocusFile(node.id)} icon={<Focus size={12} />}>
              Map this file
            </Button>
          )}
          {onOpenInCode && node.path && (isFile || node.line > 0) && (
            <Button
              variant="outline"
              size="xs"
              onClick={() => onOpenInCode({ path: node.path, line: isFile ? 0 : node.line, endLine: isFile ? 0 : node.end_line })}
              icon={<Code2 size={12} />}
              title="Opens the working tree's current file in Code"
            >
              Open in Code
            </Button>
          )}
        </div>
      </InspectorFacts>

      {(reasons.length || node.important_public_contract || node.boundaries.length || node.uncertain) ? (
        <Section title="Why it matters">
          <ul className="space-y-1 text-label leading-5 text-neutral-300">
            {node.important_public_contract && !reasons.some((reason) => reason.toLowerCase().includes("public contract")) && <li>Public contract changed</li>}
            {reasons.map((reason) => <li key={reason} className="first-letter:uppercase">{reason}</li>)}
            {node.boundaries.map((boundary) => <li key={boundary}>Touches a {boundary}-sensitive path</li>)}
            {node.uncertain && <li className="text-neutral-400">{node.uncertainty || "Impact is a candidate, not a confirmed fact"}</li>}
          </ul>
        </Section>
      ) : null}

      {review && review.targets > 0 && (
        <Section title="Your review" aside={`${review.reviewed}/${review.targets} marked`}>
          <div className="h-1 overflow-hidden rounded-full bg-neutral-800" aria-hidden="true">
            <div className="h-full rounded-full bg-emerald-400" style={{ width: `${Math.round((review.reviewed / review.targets) * 100)}%` }} />
          </div>
          <p className="mt-2 text-label text-neutral-400">
            {[
              `${review.reviewed} of ${plural(review.targets, "target")} reviewed`,
              review.needs_changes ? `${review.needs_changes} ${review.needs_changes === 1 ? "needs" : "need"} changes` : "",
              review.changed_since_review ? `${review.changed_since_review} changed since review` : "",
              review.open_threads ? plural(review.open_threads, "open thread") : "",
            ].filter(Boolean).join(" · ")}
          </p>
        </Section>
      )}

      {level === "symbol" && node.id === payload.graph.focus && (
        <Section title="Symbol">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-label">
            {node.kind && <><dt className="text-muted-foreground">Kind</dt><dd className="text-neutral-300" title={node.kind}>{symbolKindLabel(node.kind)}</dd></>}
            {node.qualified_name && <><dt className="text-muted-foreground">Name</dt><dd className="break-all font-mono text-caption text-neutral-300">{node.qualified_name}</dd></>}
            {node.line > 0 && <><dt className="text-muted-foreground">Lines</dt><dd className="tabular-nums text-neutral-300">{node.line}{node.end_line > node.line ? `–${node.end_line}` : ""}</dd></>}
            {typeof node.caller_count === "number" && node.caller_count >= 0 && <><dt className="text-muted-foreground">Callers</dt><dd className="tabular-nums text-neutral-300">{node.caller_count} at capture</dd></>}
            {typeof node.centrality_rank === "number" && <><dt className="text-muted-foreground">Centrality</dt><dd className="tabular-nums text-neutral-300">#{node.centrality_rank} in the repository</dd></>}
          </dl>
        </Section>
      )}

      {isFile && symbols.length > 0 && level !== "file" && (
        <Section title="Changed symbols" aside={node.symbol_count ?? symbols.length}>
          <SymbolList symbols={symbols} onOpenSymbol={onOpenSymbol} />
          {node.symbols_truncated && <p className="mt-1.5 text-caption text-muted-foreground">More symbols changed than this view lists. Map the file to see all of them.</p>}
        </Section>
      )}
      {isFile && symbols.length === 0 && isChangedStatus(node) && level !== "file" && (
        <Section title="Changed symbols">
          <p className="text-label leading-5 text-muted-foreground">No stable symbol boundary was captured for this file. Open the diff to read the change.</p>
        </Section>
      )}
      {siblings.length > 0 && (
        <Section title={`Also changed in ${baseName(node.path)}`} aside={siblings.length}>
          <SymbolList symbols={siblings} onOpenSymbol={onOpenSymbol} />
        </Section>
      )}

      <RelationshipList
        title={level === "symbol" ? "Called or reached by" : "Used by"}
        rows={usedBy}
        emptyText={
          pending
            ? "Relationships are still being indexed."
            : level === "symbol"
              ? payload.context.source === "current_index"
                ? "The current code index shows no callers, and the Snapshot recorded no affected sites."
                : "No callers are known: surrounding code intelligence is unavailable."
              : exact ? "Nothing on this map depends on it." : "No link into it is shown on this map."
        }
        onSelectNode={onSelectNode}
        onSelectEdge={onSelectEdge}
      />
      <RelationshipList
        title={level === "symbol" ? "Calls" : "Uses"}
        rows={uses}
        emptyText={
          pending
            ? "Relationships are still being indexed."
            : level === "symbol" && node.id === payload.graph.focus && !payload.graph.stats?.outgoing_attributed
              ? "Calls out of this symbol are shown only while the code index is at the Snapshot's revision."
              : level === "symbol"
                ? "The code index resolved no calls out of this symbol."
                : "It does not depend on anything shown on this map."
        }
        onSelectNode={onSelectNode}
        onSelectEdge={onSelectEdge}
      />

      {exact && node.provenance?.host && (
        <Section title="Written by">
          <p className="text-label text-neutral-400">
            <span className="text-neutral-200">{node.provenance.host}</span>
            {node.provenance.model ? ` · ${node.provenance.model}` : ""}
            {node.provenance.certainty ? ` · ${node.provenance.certainty} attribution` : ""}
          </p>
        </Section>
      )}
      {node.architecture_provenance && (
        <Section title="Architecture provenance">
          <p className="text-label leading-5 text-neutral-400">{node.architecture_provenance.note}</p>
        </Section>
      )}
      <ExactnessNote exactness={node.exactness} subject={subject} deltaStatus={node.delta_status} provenance={node.architecture_provenance} />
      {loadProjectHistory && (
        <Section title="Evolution">
          <MapProjectHistory selectionKey={node.id} load={() => loadProjectHistory(nodeHistorySelection(node))} />
        </Section>
      )}
    </>
  );
}

function SymbolList({ symbols, onOpenSymbol }: { symbols: ReviewMapMemberSymbol[]; onOpenSymbol(symbol: ReviewMapMemberSymbol): void }) {
  const [all, setAll] = useState(false);
  const shown = all ? symbols : symbols.slice(0, 10);
  return (
    <>
      <InspectorList>
        {shown.map((symbol) => (
          <li key={symbol.id}>
            <InspectorRow
              onClick={() => onOpenSymbol(symbol)}
              aria-label={`Open symbol ${symbol.label}`}
              title={symbol.qualified_name || symbol.label}
            >
              <span className="rm-kind-tag" title={symbol.kind || undefined}>{symbolKindLabel(symbol.kind)}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-label text-neutral-200">{symbol.label}</span>
              <span className="shrink-0 text-caption tabular-nums text-muted-foreground">
                {symbol.change && symbol.change !== "modified" ? symbol.change + " · " : ""}
                {symbol.caller_count >= 0 ? plural(symbol.caller_count, "caller") : `L${symbol.line}`}
              </span>
            </InspectorRow>
          </li>
        ))}
      </InspectorList>
      {symbols.length > 10 && (
        <Button variant="link" size="xs" className="mt-1.5 h-auto text-label" onClick={() => setAll((value) => !value)}>
          {all ? "Show fewer" : `Show all ${symbols.length}`}
        </Button>
      )}
    </>
  );
}

function EdgeDetails({ payload, edge, onSelectNode, onClear, onAskSelection, selectionHref, onOpenSelection, loadProjectHistory }: InspectorProps & { edge: ReviewMapEdge }) {
  const byId = new Map(payload.graph.nodes.map((node) => [node.id, node]));
  const { from, to } = visualEnds(edge);
  const fromNode = byId.get(from);
  const toNode = byId.get(to);
  const kind = edgeKind(edge.kind);
  const fromName = fromNode ? nodeName(fromNode) : from;
  const toName = toNode ? nodeName(toNode) : to;
  const sentence = kind === "impacts"
    ? `${toName} changed in a way that reaches ${fromName}`
    : `${fromName} ${EDGE_KIND_LABEL[kind]} ${toName}`;
  const architectureLabel = edge.delta_status ? ` · architecture ${edge.delta_status.replace(/_/g, " ")}` : "";
  const label = `${fromName} → ${toName} · ${edge.kind}`;
  const evidence = edge.evidence ?? [];
  const names = edge.symbols ?? [];
  const reviewHref = edge.exactness === "snapshot" ? selectionHref?.(edge.selection) ?? null : null;
  const canOpenReviewTarget = edge.exactness === "snapshot" && Boolean(edge.selection.path);
  return (
    <>
      <div className="sticky top-0 z-10 border-b border-border/70 bg-background/95 px-4 pb-3.5 pt-4 backdrop-blur">
        <div className="flex items-start gap-2.5">
          <div className="mt-1"><KindSwatch kind={kind} /></div>
          <div className="min-w-0 flex-1">
            <h2 className="text-heading font-semibold leading-5 text-foreground">{sentence}</h2>
            <p className="mt-0.5 text-label tabular-nums text-muted-foreground">
              {plural(edge.weight || 1, "link site")}{edge.exactness === "snapshot" ? " · Snapshot fact" : edge.exactness === "current_index" ? " · current index" : " · exactness unavailable"}{architectureLabel}{edge.uncertain ? " · uncertain" : ""}
            </p>
          </div>
          <Button variant="ghost" size="icon" onClick={onClear} aria-label="Close map inspector"><X size={13} /></Button>
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {canOpenReviewTarget && (
            <Button variant="default" size="sm" onClick={() => onOpenSelection(edge.selection)} icon={<ArrowRight size={12} />}>
              Open Review target
            </Button>
          )}
          {reviewHref && (
            <Button asChild variant="ghost" size="sm" tooltip="Opens the changed Review file in a new tab">
              <a href={reviewHref} target="_blank" rel="noopener noreferrer" aria-label="Open Review target in a new tab">
                <ExternalLink size={12} aria-hidden="true" />New tab
              </a>
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={() => onAskSelection(edge.selection, label)} icon={<Sparkles size={12} />} aria-label="Ask about this link">
            Ask
          </Button>
        </div>
      </div>

      <Section title="Between">
        <div className="-mx-1.5 space-y-0.5">
          {[fromNode, toNode].map((node, index) => node && (
            <InspectorRow
              key={node.id}
              onClick={() => onSelectNode(node.id)}
              aria-label={`Select ${nodeName(node)}`}
            >
              <NodeGlyph node={node} />
              <span className="min-w-0 flex-1 truncate text-body text-neutral-200">{nodeName(node)}</span>
              {index === 0 && <ArrowRight size={12} className="shrink-0 text-muted-foreground" aria-hidden="true" />}
            </InspectorRow>
          ))}
        </div>
      </Section>

      {edge.delta_status && (edge.base_weight !== undefined || edge.snapshot_weight !== undefined) && (
        <Section title="Base → Snapshot">
          <p className="text-label tabular-nums text-neutral-400">
            Relationship weight {edge.base_weight ?? 0} → {edge.snapshot_weight ?? 0} · <span className="text-neutral-200">{edge.delta_status.replace(/_/g, " ")}</span>
          </p>
        </Section>
      )}

      {names.length > 0 && (
        <Section title={kind === "impacts" ? "Changed names it reaches" : "Names it lands on"}>
          <div className="flex flex-wrap gap-1.5">
            {names.map((item) => (
              <span key={item.name} className="rounded-md border border-border bg-muted/60 px-1.5 py-0.5 font-mono text-caption text-neutral-300">
                {item.name}{item.count > 1 ? <span className="ml-1 text-muted-foreground">×{item.count}</span> : null}
              </span>
            ))}
          </div>
        </Section>
      )}

      {evidence.length > 0 && (
        <Section title="Why connected" aside={evidence.length < (edge.weight || 0) ? `${evidence.length} of ${edge.weight}` : undefined}>
          <ul className="space-y-2">
            {evidence.map((item, index) => (
              <li key={item.path + ":" + item.line + ":" + index} className="text-label">
                <div className="flex items-center justify-between gap-3">
                  <span className="truncate font-mono text-caption text-neutral-300" title={item.path}>{item.path}{item.line > 0 ? ":" + item.line : ""}</span>
                  {item.uncertainty && <span className="shrink-0 text-caption text-amber-300">uncertain</span>}
                </div>
                <div className="mt-0.5 text-caption text-muted-foreground">
                  {item.kind.replace(/_/g, " ")}{item.symbol ? ` · ${item.symbol}` : ""}
                </div>
                {item.snippet && <code className="mt-1 block truncate rounded bg-muted/70 px-1.5 py-1 text-caption text-neutral-300">{item.snippet}</code>}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {edge.architecture_provenance && (
        <Section title="Architecture provenance">
          <p className="text-label leading-5 text-neutral-400">{edge.architecture_provenance.note}</p>
        </Section>
      )}
      <ExactnessNote exactness={edge.exactness} subject="link" deltaStatus={edge.delta_status} provenance={edge.architecture_provenance} />
      {loadProjectHistory && (
        <Section title="Evolution">
          <MapProjectHistory
            selectionKey={edge.id}
            load={() => loadProjectHistory(edgeHistorySelection(fromNode, toNode))}
          />
        </Section>
      )}
    </>
  );
}
