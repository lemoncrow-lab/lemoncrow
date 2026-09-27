import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Crosshair, ExternalLink, Loader2, MessageCircle, Network, Search, ShieldCheck, X } from "lucide-react";

import type { CodeMapEdge, CodeMapNode } from "../api";
import { trapModalTab } from "./focusTrap";
import { fetchHistoricalReviewMap } from "./reviewApi";
import type {
  ReviewMapEdge,
  ReviewMapNode,
  ReviewMapPayload,
  ReviewMapSelection,
  ReviewMapStatus,
} from "./types";

const CodeGraph = lazy(() => import("../components/CodeGraph"));

interface ReviewMapSheetProps {
  reviewId: string;
  revisionNumber: number;
  historical: boolean;
  initialSelection?: ReviewMapSelection | null;
  onClose(): void;
  onOpenSelection(selection: ReviewMapSelection): void;
  onAskSelection?(selection: ReviewMapSelection, label: string): void;
}

const STATUS_COLOR: Record<ReviewMapStatus, string> = {
  changed: "#f59e0b",
  new: "#34d399",
  impacted: "#22d3ee",
  context: "#737373",
};

const STATUS_FILTERS: Array<{ value: ReviewMapStatus | "all"; label: string }> = [
  { value: "all", label: "All" },
  { value: "changed", label: "Changed" },
  { value: "new", label: "New" },
  { value: "impacted", label: "Impacted" },
  { value: "context", label: "Context" },
];

function mapGraph(
  payload: ReviewMapPayload,
  nodes: ReviewMapNode[] = payload.graph.nodes,
  edges: ReviewMapEdge[] = payload.graph.edges,
): { nodes: CodeMapNode[]; edges: CodeMapEdge[] } {
  const degree = new Map<string, number>();
  for (const edge of edges) {
    const weight = Math.max(1, edge.weight || 1);
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + weight);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + weight);
  }
  return {
    nodes: nodes.map((node) => ({
      id: node.id,
      label: node.label,
      qualified_name: node.qualified_name || node.label,
      path: node.path,
      kind: node.kind,
      line: node.line,
      end_line: node.end_line,
      degree: degree.get(node.id) ?? 0,
      focus: payload.graph.focus === node.id,
      node_type: payload.level === "architecture"
        ? node.kind === "file" ? "file" : "community"
        : "symbol",
      community: payload.level === "architecture"
        ? node.component || node.label
        : node.path.split("/").slice(0, 2).join("/"),
      color: STATUS_COLOR[node.status],
      provenance: node.exactness,
    })),
    edges: edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      kind: edge.kind,
      depth: 1,
      weight: edge.weight,
      color:
        edge.kind === "impacts"
          ? edge.uncertain
            ? "#f59e0b"
            : "#22d3ee"
          : edge.kind === "calls"
            ? "#a78bfa"
            : edge.kind === "references"
              ? "#60a5fa"
              : undefined,
    })),
  };
}

function exactnessLabel(node: ReviewMapNode): string {
  return node.exactness === "snapshot" ? "Snapshot exact" : "Current index context";
}

function statusLabel(status: ReviewMapStatus): string {
  if (status === "new") return "New";
  if (status === "changed") return "Changed";
  if (status === "impacted") return "Impacted · unchanged";
  return "Surrounding context";
}

function statusPillClass(status: ReviewMapStatus): string {
  if (status === "new") return "border-emerald-900/60 bg-emerald-950/25 text-emerald-300";
  if (status === "changed") return "border-amber-900/60 bg-amber-950/25 text-amber-300";
  if (status === "impacted") return "border-cyan-900/60 bg-cyan-950/25 text-cyan-300";
  return "border-neutral-800 bg-neutral-900/55 text-neutral-400";
}

function NodeInspector({
  node,
  payload,
  onDrill,
  onOpenSelection,
  onAskSelection,
  onClose,
}: {
  node: ReviewMapNode;
  payload: ReviewMapPayload;
  onDrill(nodeId: string): void;
  onOpenSelection(selection: ReviewMapSelection): void;
  onAskSelection(selection: ReviewMapSelection, label: string): void;
  onClose(): void;
}) {
  const exact = node.exactness === "snapshot";
  return (
    <aside className="absolute inset-y-0 right-0 z-30 w-[min(380px,92vw)] overflow-y-auto border-l border-neutral-800/80 bg-neutral-950/96 shadow-[-24px_0_56px_rgba(0,0,0,0.34)] backdrop-blur-xl" aria-label="Review map inspector">
      <div className="sticky top-0 z-10 border-b border-neutral-900 bg-neutral-950/95 px-4 py-3 backdrop-blur-xl">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-neutral-800 bg-neutral-900/70">
            <Crosshair size={14} className={node.status === "changed" ? "text-amber-300" : node.status === "new" ? "text-emerald-300" : node.status === "impacted" ? "text-cyan-300" : "text-neutral-400"} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold text-neutral-100" title={node.label}>{node.label}</div>
            {node.path && <div className="mt-1 truncate font-mono text-[10px] text-neutral-500" title={node.path}>{node.path}{node.line > 0 ? ":" + node.line : ""}</div>}
          </div>
          <button type="button" className="review-icon-button -mr-1 -mt-1" onClick={onClose} aria-label="Close map inspector"><X size={13} /></button>
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5 text-[10px]">
          <span className={"rounded-md border px-2 py-1 font-medium " + statusPillClass(node.status)}>{statusLabel(node.status)}</span>
          <span className={"rounded-md border px-2 py-1 " + (exact ? "border-violet-900/60 bg-violet-950/35 text-violet-200" : "border-neutral-800 bg-neutral-900/50 text-neutral-500")}>{exactnessLabel(node)}</span>
        </div>
      </div>

      <div className="p-4">
        {(node.important_public_contract || node.boundaries.length > 0 || node.uncertain) && (
          <section className="rounded-lg border border-neutral-800/80 bg-neutral-900/25 p-3">
            <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-neutral-500">Signals</div>
            <div className="flex flex-wrap gap-1.5 text-[10px]">
              {node.important_public_contract && <span className="rounded-md border border-amber-900/60 bg-amber-950/15 px-2 py-1 text-amber-300">{payload.level === "symbol" ? "Public-contract file" : "Public contract"}</span>}
              {node.boundaries.map((boundary) => <span key={boundary} className="rounded-md border border-neutral-800 px-2 py-1 text-neutral-400">{boundary} boundary signal · path</span>)}
              {node.uncertain && <span className="rounded-md border border-amber-900/60 px-2 py-1 text-amber-300">Uncertain impact</span>}
            </div>
          </section>
        )}

        {payload.level === "architecture" ? (
          <>
            {node.component && (
              <div className="mb-3 flex items-center gap-2 text-[10px]">
                <span className="text-neutral-600">Component</span>
                <span className="rounded-md border border-neutral-800 bg-neutral-900/60 px-2 py-1 font-mono text-neutral-300">{node.component}</span>
              </div>
            )}

            {node.kind === "file" ? (
              <>
                <section className="grid grid-cols-2 gap-2">
                  <div className="rounded-lg border border-neutral-800/80 bg-neutral-900/30 p-3">
                    <div className="font-mono text-lg font-semibold tracking-tight text-neutral-100">{node.symbols?.length ?? 0}</div>
                    <div className="mt-0.5 text-[10px] uppercase tracking-wide text-neutral-500">changed symbols</div>
                  </div>
                  <div className="rounded-lg border border-neutral-800/80 bg-neutral-900/30 p-3">
                    <div className="font-mono text-lg font-semibold tracking-tight text-neutral-100">{node.relationship_count ?? 0}</div>
                    <div className="mt-0.5 text-[10px] uppercase tracking-wide text-neutral-500">relationships</div>
                  </div>
                </section>

                {(node.symbols?.length ?? 0) > 0 ? (
                  <section className="mt-5">
                    <div className="mb-2 flex items-center justify-between">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-neutral-500">Changed symbols</div>
                      <span className="text-[10px] text-violet-300/80">Snapshot</span>
                    </div>
                    <div className="overflow-hidden rounded-lg border border-neutral-800/80 bg-neutral-900/20">
                      {node.symbols?.map((symbol, index) => (
                        <button
                          key={symbol.id}
                          type="button"
                          onClick={() => onDrill(symbol.id)}
                          className={"group flex w-full min-w-0 items-center gap-2.5 px-3 py-2.5 text-left transition hover:bg-neutral-900/80 " + (index ? "border-t border-neutral-900" : "")}
                          aria-label={"Drill into " + symbol.label}
                        >
                          <Crosshair size={11} className="shrink-0 text-violet-300 transition group-hover:scale-110" />
                          <span className="min-w-0 flex-1 truncate text-[11px] text-neutral-300">{symbol.label}</span>
                          {symbol.caller_count >= 0 && <span className="shrink-0 font-mono text-[10px] text-neutral-500">{symbol.caller_count} callers</span>}
                        </button>
                      ))}
                    </div>
                    {node.symbols_truncated && <div className="mt-1.5 text-[10px] text-neutral-500">Additional symbols omitted from this bounded view.</div>}
                  </section>
                ) : node.status === "changed" || node.status === "new" ? (
                  <div className="mt-4 rounded-lg border border-neutral-800/70 bg-neutral-900/20 p-3 text-[11px] leading-5 text-neutral-500">
                    This file changed, but no stable changed-symbol boundary was captured for this Snapshot.
                  </div>
                ) : null}
              </>
            ) : (
              <section className="grid grid-cols-2 gap-2">
                <div className="rounded-lg border border-neutral-800/80 bg-neutral-900/30 p-3">
                  <div className="font-mono text-lg font-semibold tracking-tight text-neutral-100">{node.changed_file_count ?? 0}</div>
                  <div className="mt-0.5 text-[10px] uppercase tracking-wide text-neutral-500">changed files</div>
                </div>
                <div className="rounded-lg border border-neutral-800/80 bg-neutral-900/30 p-3">
                  <div className="font-mono text-lg font-semibold tracking-tight text-neutral-100">{node.impacted_file_count ?? 0}</div>
                  <div className="mt-0.5 text-[10px] uppercase tracking-wide text-neutral-500">impacted files</div>
                </div>
              </section>
            )}
          </>
        ) : (
          <section className="mt-4 rounded-lg border border-neutral-800/70 bg-neutral-900/20 p-3 text-[10px] text-neutral-500">
            {exact && (
              <div className="grid grid-cols-2 gap-3">
                {typeof node.caller_count === "number" && node.caller_count >= 0 && <div><span className="block font-mono text-base text-neutral-100">{node.caller_count}</span><span className="text-[9px] uppercase tracking-wide">frozen callers</span></div>}
                {typeof node.centrality_rank === "number" && <div><span className="block font-mono text-base text-neutral-100">#{node.centrality_rank}</span><span className="text-[9px] uppercase tracking-wide">frozen centrality</span></div>}
              </div>
            )}
            {!exact && <div className="leading-4">This node comes from the current Code Map index and is surrounding context, not a claim about the reviewed Snapshot.</div>}
            {node.uncertainty && <div className="mt-2 border-t border-neutral-800 pt-2 leading-4 text-amber-300/80">{node.uncertainty}</div>}
          </section>
        )}

        {exact && node.provenance?.host && (
          <section className="mt-5">
            <div className="mb-2 text-[9px] font-semibold uppercase tracking-[0.12em] text-neutral-500">Provenance</div>
            <div className="rounded-lg border border-neutral-800/70 bg-neutral-900/20 px-3 py-2 text-[9px] text-neutral-500">
              <span className="text-neutral-300">{node.provenance.host}</span>
              {node.provenance.model ? " · " + node.provenance.model : ""}
              {node.provenance.certainty ? " · " + node.provenance.certainty : ""}
            </div>
          </section>
        )}

        <div className="mt-5 grid gap-2">
          <button type="button" onClick={() => onAskSelection(node.selection, node.label)} className="review-toolbar-button-primary h-8 w-full justify-center">
            <MessageCircle size={12} /> Ask about this node
          </button>
          {exact && node.path && (
            <button type="button" onClick={() => onOpenSelection(node.selection)} className="review-toolbar-button h-8 w-full justify-center">
              <ExternalLink size={12} /> Open Snapshot source
            </button>
          )}
        </div>
      </div>
    </aside>
  );
}

function EdgeInspector({
  edge,
  payload,
  onAskSelection,
  onClose,
}: {
  edge: ReviewMapEdge;
  payload: ReviewMapPayload;
  onAskSelection(selection: ReviewMapSelection, label: string): void;
  onClose(): void;
}) {
  const source = payload.graph.nodes.find((node) => node.id === edge.source);
  const target = payload.graph.nodes.find((node) => node.id === edge.target);
  const label = (source?.label || edge.source) + " → " + (target?.label || edge.target) + " · " + edge.kind;
  return (
    <aside className="absolute inset-y-0 right-0 z-30 w-[min(380px,92vw)] overflow-y-auto border-l border-neutral-800/80 bg-neutral-950/96 shadow-[-24px_0_56px_rgba(0,0,0,0.34)] backdrop-blur-xl" aria-label="Review map relationship inspector">
      <div className="border-b border-neutral-900 px-4 py-3">
        <div className="flex items-center justify-between">
          <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-neutral-500">Relationship</div>
          <button type="button" className="review-icon-button -mr-1 -mt-1" onClick={onClose} aria-label="Close map relationship inspector"><X size={13} /></button>
        </div>
        <div className="mt-3 rounded-lg border border-neutral-800/80 bg-neutral-900/30 p-3">
          <div className="truncate text-[12px] font-medium text-neutral-100">{source?.label || edge.source}</div>
          <div className="my-2 flex items-center gap-2 text-[10px] uppercase tracking-wider text-neutral-500">
            <span className="h-px flex-1 bg-neutral-800" />{edge.kind}<span className="h-px flex-1 bg-neutral-800" />
          </div>
          <div className="truncate text-[12px] font-medium text-neutral-100">{target?.label || edge.target}</div>
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5 text-[10px]">
          <span className={"rounded-md border px-2 py-1 " + (edge.exactness === "snapshot" ? "border-violet-900/60 bg-violet-950/35 text-violet-200" : "border-neutral-800 bg-neutral-900/50 text-neutral-500")}>
            {edge.exactness === "snapshot" ? "Snapshot exact" : "Current index context"}
          </span>
          <span className="rounded-md border border-neutral-800 bg-neutral-900/50 px-2 py-1 text-neutral-400">
            {edge.weight} {edge.weight === 1 ? "link" : "links"}
          </span>
          {edge.uncertain && <span className="rounded-md border border-amber-900/60 px-2 py-1 text-amber-300">Uncertain</span>}
        </div>
      </div>
      <div className="p-4">
        {edge.exactness === "current_index" && (
          <p className="rounded-lg border border-neutral-800/70 bg-neutral-900/20 p-3 text-[11px] leading-5 text-neutral-400">
            Aggregated from the current code-intelligence graph. It provides navigation context, not a claim about the immutable Snapshot.
          </p>
        )}

        {(edge.evidence?.length ?? 0) > 0 && (
          <section className="mt-4">
            <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-neutral-500">Why connected</div>
            <div className="overflow-hidden rounded-lg border border-neutral-800/80 bg-neutral-900/20">
              {edge.evidence?.map((evidence, index) => (
                <div key={evidence.path + ":" + evidence.line + ":" + index} className={"px-3 py-2.5 " + (index ? "border-t border-neutral-900" : "")}>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-[10px] font-medium text-neutral-300">{evidence.kind}</span>
                    {evidence.uncertainty && <span className="text-[9px] text-amber-300/80">uncertain</span>}
                  </div>
                  <div className="mt-1 truncate font-mono text-[10px] text-neutral-500" title={evidence.path}>
                    {evidence.path}{evidence.line > 0 ? ":" + evidence.line : ""}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        <button type="button" onClick={() => onAskSelection(edge.selection, label)} className="review-toolbar-button-primary mt-4 h-8 w-full justify-center">
          <MessageCircle size={12} /> Ask about this relationship
        </button>
      </div>
    </aside>
  );
}

export default function ReviewMapSheet({
  reviewId,
  revisionNumber,
  historical,
  initialSelection,
  onClose,
  onOpenSelection,
  onAskSelection = () => {},
}: ReviewMapSheetProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const requestGeneration = useRef(0);
  const [payload, setPayload] = useState<ReviewMapPayload | null>(null);
  const [architecture, setArchitecture] = useState<ReviewMapPayload | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<ReviewMapStatus | "all">("all");
  const [snapshotOnly, setSnapshotOnly] = useState(false);
  const [componentFilter, setComponentFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    closeRef.current?.focus();
    const generation = ++requestGeneration.current;
    const isCurrent = () => requestGeneration.current === generation;
    setLoading(true);
    setPayload(null);
    setArchitecture(null);
    setError("");
    const load = async () => {
      const base = await fetchHistoricalReviewMap(reviewId, revisionNumber);
      if (!isCurrent()) return;
      let next = base;
      if (initialSelection?.level === "symbol") {
        const focus = initialSelection.focus_node_id
          || (initialSelection.kind === "review_map_node" && initialSelection.exactness === "snapshot" ? initialSelection.node_id : "");
        if (focus) next = await fetchHistoricalReviewMap(reviewId, revisionNumber, "symbol", focus);
      }
      if (!isCurrent()) return;
      setArchitecture(base);
      setPayload(next);
      setQuery("");
      setStatusFilter("all");
      setSnapshotOnly(false);
      setComponentFilter("all");
      if (initialSelection?.kind === "review_map_edge" && next.graph.edges.some((edge) => edge.id === initialSelection.edge_id)) {
        setSelectedId(null);
        setSelectedEdgeId(initialSelection.edge_id ?? null);
      } else {
        const directInitialNode = initialSelection?.kind === "review_map_node"
          ? next.graph.nodes.find((node) => node.id === initialSelection.node_id)
          : undefined;
        const pathRecoveredNode = !directInitialNode && initialSelection?.kind === "review_map_node" && initialSelection.path
          ? next.graph.nodes.find((node) => node.path === initialSelection.path)
          : undefined;
        const initialNode = directInitialNode?.id
          ?? pathRecoveredNode?.id
          ?? (next.level === "architecture" ? null : next.graph.focus ?? next.graph.nodes[0]?.id ?? null);
        setSelectedEdgeId(null);
        setSelectedId(initialNode);
      }
    };
    void load().catch((reason: unknown) => {
      if (isCurrent()) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (isCurrent()) setLoading(false);
    });
    return () => { requestGeneration.current += 1; };
  }, [historical, initialSelection, reviewId, revisionNumber]);

  const statusCounts = useMemo(() => {
    const counts: Record<ReviewMapStatus, number> = { changed: 0, new: 0, impacted: 0, context: 0 };
    for (const node of payload?.graph.nodes ?? []) counts[node.status] += 1;
    return counts;
  }, [payload]);

  const visibleNodes = useMemo(() => {
    if (!payload) return [];
    return payload.graph.nodes.filter((node) =>
      (statusFilter === "all" || node.status === statusFilter)
      && (!snapshotOnly || node.exactness === "snapshot")
      && (componentFilter === "all" || node.component === componentFilter)
    );
  }, [componentFilter, payload, snapshotOnly, statusFilter]);

  const visibleIds = useMemo(() => new Set(visibleNodes.map((node) => node.id)), [visibleNodes]);
  const visibleEdges = useMemo(
    () => (payload?.graph.edges ?? []).filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target)),
    [payload, visibleIds],
  );
  const graph = useMemo(
    () => payload ? mapGraph(payload, visibleNodes, visibleEdges) : { nodes: [], edges: [] },
    [payload, visibleEdges, visibleNodes],
  );
  const selected = visibleNodes.find((node) => node.id === selectedId) ?? null;
  const selectedEdge = visibleEdges.some((edge) => edge.id === selectedEdgeId)
    ? payload?.graph.edges.find((edge) => edge.id === selectedEdgeId) ?? null
    : null;

  const components = useMemo(
    () => [...new Set((payload?.graph.nodes ?? []).map((node) => node.component).filter((value): value is string => Boolean(value)))].sort(),
    [payload],
  );

  const searchResults = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!payload || !normalized) return [];
    return payload.graph.nodes
      .filter((node) => (
        node.label + " " + (node.qualified_name ?? "") + " " + node.path + " " + (node.component ?? "")
      ).toLowerCase().includes(normalized))
      .slice(0, 8);
  }, [payload, query]);

  const graphReadiness = payload?.context.readiness?.graph;
  const surroundingGraphLabel = payload?.context.source !== "current_index"
    ? "unavailable"
    : graphReadiness?.state && graphReadiness.state !== "ready"
      ? "current index · graph " + graphReadiness.state
      : "current index";

  const drill = async (nodeId: string) => {
    const generation = ++requestGeneration.current;
    const isCurrent = () => requestGeneration.current === generation;
    setLoading(true);
    try {
      const next = await fetchHistoricalReviewMap(reviewId, revisionNumber, "symbol", nodeId);
      if (!isCurrent()) return;
      setPayload(next);
      setQuery("");
      setStatusFilter("all");
      setSnapshotOnly(false);
      setComponentFilter("all");
      setSelectedEdgeId(null);
      setSelectedId(next.graph.focus ?? next.graph.nodes[0]?.id ?? null);
      setError("");
    } catch (reason) {
      if (isCurrent()) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  };

  const restoreArchitecture = () => {
    if (!architecture) return;
    requestGeneration.current += 1;
    setLoading(false);
    setError("");
    setPayload(architecture);
    setQuery("");
    setStatusFilter("all");
    setSnapshotOnly(false);
    setComponentFilter("all");
    setSelectedEdgeId(null);
    setSelectedId(null);
  };

  return (
    <div
      className="review-sheet-backdrop absolute inset-0 z-[90] items-stretch justify-stretch p-3"
      role="presentation"
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") { event.stopPropagation(); onClose(); }
      }}
    >
      <section role="dialog" aria-modal="true" aria-labelledby="review-map-title" className="review-sheet flex h-full min-h-0 w-full flex-col overflow-hidden rounded-xl border border-neutral-800/80 shadow-2xl">
        <header className="flex shrink-0 items-center gap-3 border-b border-neutral-800/80 bg-neutral-950/90 px-4 py-3 backdrop-blur-xl">
          {payload?.level === "symbol" ? (
            <button type="button" onClick={restoreArchitecture} className="review-icon-button" aria-label="Back to architecture map"><ArrowLeft size={13} /></button>
          ) : (
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-violet-900/50 bg-violet-950/30 text-violet-200"><Network size={15} /></div>
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h2 id="review-map-title" className="text-sm font-semibold tracking-tight text-neutral-100">Review Map</h2>
              {payload?.level === "symbol"
                ? <><span className="text-neutral-700">/</span><span className="truncate text-[11px] text-neutral-400">Symbol neighborhood</span></>
                : payload
                  ? <><span className="text-neutral-700">/</span><span className="truncate text-[11px] text-neutral-400">Files + relationships</span></>
                  : null}
            </div>
            <div className="mt-0.5 text-[10px] text-neutral-500">Snapshot {revisionNumber} · immutable review context</div>
          </div>
          {payload && (
            <div className="hidden items-center gap-2 lg:flex">
              <span className="inline-flex items-center gap-1.5 rounded-md border border-violet-900/50 bg-violet-950/25 px-2 py-1 text-[10px] text-violet-200"><ShieldCheck size={11} /> Snapshot facts exact</span>
              <span className="rounded-md border border-neutral-800 bg-neutral-900/45 px-2 py-1 text-[10px] text-neutral-400">Surrounding graph · {surroundingGraphLabel}</span>
            </div>
          )}
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close review map" className="review-icon-button"><X size={14} /></button>
        </header>

        {payload && (
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-neutral-900 bg-neutral-950/70 px-3 py-2">
            <div className="relative w-[300px] max-w-full">
              <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-neutral-600" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.stopPropagation();
                    setQuery("");
                    return;
                  }
                  if (event.key === "Enter" && searchResults[0]) {
                    event.preventDefault();
                    const node = searchResults[0];
                    setStatusFilter("all");
                    setSnapshotOnly(false);
                    setComponentFilter("all");
                    setQuery("");
                    setSelectedEdgeId(null);
                    setSelectedId(node.id);
                  }
                }}
                placeholder={payload.level === "architecture" ? "Find component or file…" : "Find symbol or path…"}
                className="review-field h-8 w-full rounded-md pl-7 pr-7 text-[11px]"
                aria-label="Search review map"
              />
              {query && <button type="button" onClick={() => setQuery("")} className="absolute right-2 top-1/2 -translate-y-1/2 text-neutral-600 hover:text-neutral-300" aria-label="Clear map search"><X size={11} /></button>}
              {query.trim() && (
                <div className="absolute left-0 top-[34px] z-50 w-[340px] max-w-[calc(100vw-48px)] overflow-hidden rounded-lg border border-neutral-800 bg-neutral-950 shadow-2xl">
                  {searchResults.length ? searchResults.map((node) => (
                    <button
                      key={node.id}
                      type="button"
                      aria-label={"Select " + node.label + " on map"}
                      className="flex w-full items-center gap-2.5 border-b border-neutral-900 px-3 py-2 text-left last:border-b-0 hover:bg-neutral-900/80"
                      onClick={() => {
                        setStatusFilter("all");
                        setSnapshotOnly(false);
                        setComponentFilter("all");
                        setQuery("");
                        setSelectedEdgeId(null);
                        setSelectedId(node.id);
                      }}
                    >
                      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: STATUS_COLOR[node.status] }} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[11px] font-medium text-neutral-200">{node.label}</span>
                        <span className="mt-0.5 block truncate font-mono text-[9px] text-neutral-500" title={node.path}>{node.path}</span>
                      </span>
                      <span className="shrink-0 text-[9px] uppercase tracking-wide text-neutral-500">{node.status}</span>
                    </button>
                  )) : <div className="px-3 py-3 text-[10px] text-neutral-600">No nodes match “{query.trim()}”.</div>}
                </div>
              )}
            </div>

            <div className="flex items-center gap-1 rounded-lg border border-neutral-800/80 bg-neutral-900/35 p-0.5" aria-label="Filter review map by status">
              {STATUS_FILTERS.map((filter) => {
                const count = filter.value === "all" ? payload.graph.nodes.length : statusCounts[filter.value];
                return (
                  <button
                    key={filter.value}
                    type="button"
                    onClick={() => setStatusFilter(filter.value)}
                    className={"flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[10px] font-medium transition " + (statusFilter === filter.value ? "bg-neutral-800 text-neutral-100 shadow-sm" : "text-neutral-500 hover:bg-neutral-900 hover:text-neutral-300")}
                    aria-pressed={statusFilter === filter.value}
                  >
                    {filter.value !== "all" && <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: STATUS_COLOR[filter.value] }} />}
                    {filter.label}<span className="font-mono text-[9px] text-neutral-600">({count})</span>
                  </button>
                );
              })}
            </div>

            <button
              type="button"
              onClick={() => setSnapshotOnly((value) => !value)}
              aria-pressed={snapshotOnly}
              className={"flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-[10px] font-medium transition " + (snapshotOnly ? "border-violet-800/70 bg-violet-950/35 text-violet-200" : "border-neutral-800 bg-neutral-900/25 text-neutral-500 hover:text-neutral-300")}
            >
              <ShieldCheck size={10} /> Snapshot only
            </button>

            {payload.level === "architecture" && components.length > 1 && (
              <select
                value={componentFilter}
                onChange={(event) => {
                  setComponentFilter(event.target.value);
                  setSelectedId(null);
                  setSelectedEdgeId(null);
                }}
                className="review-field h-8 min-w-[150px] py-0 text-[10px]"
                aria-label="Filter review map by component"
              >
                <option value="all">All components</option>
                {components.map((component) => <option key={component} value={component}>{component}</option>)}
              </select>
            )}

            <div className="ml-auto hidden items-center gap-2 text-[10px] text-neutral-500 md:flex">
              <span className="font-mono text-neutral-400">{visibleNodes.length + " / " + payload.graph.nodes.length + " nodes"}</span>
              <span className="text-neutral-800">·</span>
              <span className="font-mono text-neutral-400">{visibleEdges.length + " relations"}</span>
            </div>
          </div>
        )}

        {payload?.context.note && <div className="shrink-0 border-b border-neutral-900 bg-neutral-950/60 px-4 py-2 text-[10px] leading-4 text-neutral-500">{payload.context.note}</div>}
        {(payload?.snapshot.degraded.length ?? 0) > 0 && <div className="shrink-0 border-b border-amber-950/70 bg-amber-950/10 px-4 py-2 text-[10px] text-amber-300/85">Degraded Snapshot signals · {payload?.snapshot.degraded.join(" · ")}</div>}
        {error && <div role="alert" className="shrink-0 border-b border-rose-900/60 bg-rose-950/20 px-4 py-2 text-[10px] text-rose-200">{error}</div>}

        <div className="relative flex min-h-0 flex-1">
          <div className="relative min-w-0 flex-1">
            {payload && graph.nodes.length > 0 ? (
              <Suspense fallback={<div className="flex h-full items-center justify-center text-[10px] text-neutral-600">Loading shared Code Map canvas…</div>}>
                <CodeGraph
                  nodes={graph.nodes}
                  edges={graph.edges}
                  selectedId={selected?.id ?? null}
                  activityByNode={{}}
                  followNodeId={null}
                  selectedEdgeId={selectedEdge?.id ?? null}
                  edgeVisibility="all"
                  directedKinds={["calls", "impacts", "references"]}
                  presentation="compact"
                  forceLabels={graph.nodes.length <= 18}
                  dimUnrelatedSelection={false}
                  onSelect={(nodeId) => { setSelectedEdgeId(null); setSelectedId(nodeId); }}
                  onSelectEdge={(edgeId) => { setSelectedId(null); setSelectedEdgeId(edgeId); }}
                  onExpand={(nodeId) => {
                    const node = payload.graph.nodes.find((item) => item.id === nodeId);
                    if (payload.level === "architecture" && node?.symbols?.length === 1) {
                      void drill(node.symbols[0].id);
                    } else if (payload.level === "architecture") {
                      setSelectedEdgeId(null);
                      setSelectedId(nodeId);
                    }
                  }}
                />
              </Suspense>
            ) : !loading ? (
              <div className="flex h-full flex-col items-center justify-center px-6 text-center">
                <Network size={22} className="mb-3 text-neutral-700" />
                <div className="text-[12px] font-medium text-neutral-300">{payload?.graph.nodes.length ? "No nodes match the current filters" : "No map facts are available for this Snapshot."}</div>
                <div className="mt-1.5 max-w-sm text-[10px] leading-5 text-neutral-500">{payload?.graph.reason || (payload?.graph.nodes.length ? "Reset the status or Snapshot filter to bring nodes back into view." : "")}</div>
                {payload?.graph.nodes.length ? <button type="button" className="review-toolbar-button mt-3 h-7" onClick={() => { setStatusFilter("all"); setSnapshotOnly(false); setComponentFilter("all"); }}>Reset filters</button> : null}
              </div>
            ) : null}

            {payload && graph.nodes.length > 0 && (
              <>
                <div className="pointer-events-none absolute left-3 top-3 z-20 max-w-[calc(100%-24px)] rounded-lg border border-neutral-800/80 bg-neutral-950/82 px-3 py-2 text-[10px] text-neutral-400 shadow-lg backdrop-blur-md">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                    <span><span className="text-amber-300">●</span> changed</span>
                    <span><span className="text-emerald-300">●</span> new</span>
                    <span><span className="text-cyan-300">●</span> impacted</span>
                    <span><span className="text-neutral-500">●</span> context</span>
                    <span className="h-3 w-px bg-neutral-800" />
                    <span><span className="text-cyan-300">→</span> impact</span>
                    <span><span className="text-violet-300">→</span> call</span>
                    <span><span className="text-blue-300">→</span> reference</span>
                  </div>
                  <div className="mt-1 text-[9px] text-neutral-600">Click to inspect · drag to pan · scroll to zoom · double-click to drill</div>
                </div>
                <div className="pointer-events-none absolute right-3 top-3 z-20 hidden rounded-lg border border-neutral-800/80 bg-neutral-950/82 px-3 py-2 text-[10px] text-neutral-500 shadow-lg backdrop-blur-md xl:block">
                  Verification <span className="text-emerald-300">{payload.snapshot.verification.pass} pass</span> · <span className={payload.snapshot.verification.fail ? "text-rose-300" : "text-neutral-500"}>{payload.snapshot.verification.fail} fail</span> · {payload.snapshot.verification.not_run + payload.snapshot.verification.unknown} unresolved
                </div>
              </>
            )}

            {loading && (
              <div className="absolute inset-0 z-30 flex items-center justify-center bg-neutral-950/72 backdrop-blur-[2px]">
                <div className="flex items-center gap-2 rounded-lg border border-neutral-800 bg-neutral-950/95 px-3 py-2 text-[10px] text-neutral-400 shadow-xl">
                  <Loader2 size={13} className="animate-spin text-violet-300" /> Building Review Map…
                </div>
              </div>
            )}
            {payload?.graph.truncated && <div className="absolute bottom-3 right-3 z-20 rounded-lg border border-neutral-800 bg-neutral-950/85 px-2.5 py-1.5 text-[8px] text-neutral-500 shadow-lg backdrop-blur-md">Bounded view · {payload.graph.omitted_node_count ?? "additional"} nodes omitted</div>}
          </div>

          {!loading && selected && (
            <NodeInspector
              node={selected}
              payload={payload!}
              onDrill={(id) => void drill(id)}
              onOpenSelection={onOpenSelection}
              onAskSelection={onAskSelection}
              onClose={() => setSelectedId(null)}
            />
          )}
          {!loading && selectedEdge && (
            <EdgeInspector
              edge={selectedEdge}
              payload={payload!}
              onAskSelection={onAskSelection}
              onClose={() => setSelectedEdgeId(null)}
            />
          )}
        </div>
      </section>
    </div>
  );
}
