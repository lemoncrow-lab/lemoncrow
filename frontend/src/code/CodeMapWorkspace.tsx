import { lazy, Suspense, useState } from "react";
import { Activity, ChevronLeft, ChevronRight, Loader2, SlidersHorizontal } from "lucide-react";
import type {
  CodeMapActivityEvent,
  CodeMapActivityKind,
  CodeMapFacet,
  CodeMapGraph,
  CodeMapNode,
  CodeMapSymbol,
} from "../api";
import CodeMapActivity from "./CodeMapActivity";
import CodeMapFilters from "./CodeMapFilters";
import CodeMapInspector from "./CodeMapInspector";

const CodeGraph = lazy(() => import("../components/CodeGraph"));
const CodeGraph3D = lazy(() => import("../components/CodeGraph3D"));

export default function CodeMapWorkspace({
  graph, groups, fileTypes, languages, selectedGroups, selectedFileTypes,
  selectedLanguage, dimension, selectedId, selectedNode, detail, callers, callees,
  incomingCount, outgoingCount, expandingId, activityByNode, followNodeId,
  liveEnabled, events, onToggleGroup, onToggleAllGroups, onToggleFileType,
  onToggleAllFileTypes, onLanguageChange, onSelect, onFocus, onPick, onOpenActivity,
}: {
  graph: CodeMapGraph;
  groups: CodeMapFacet[];
  fileTypes: CodeMapFacet[];
  languages: CodeMapFacet[];
  selectedGroups: ReadonlySet<string>;
  selectedFileTypes: ReadonlySet<string>;
  selectedLanguage: string;
  dimension: "2d" | "3d";
  selectedId: string | null;
  selectedNode: CodeMapNode | null;
  detail: CodeMapSymbol | null;
  callers: CodeMapNode[];
  callees: CodeMapNode[];
  incomingCount: number;
  outgoingCount: number;
  expandingId: string | null;
  activityByNode: Record<string, CodeMapActivityKind>;
  followNodeId: string | null;
  liveEnabled: boolean;
  events: CodeMapActivityEvent[];
  onToggleGroup(id: string): void;
  onToggleAllGroups(): void;
  onToggleFileType(id: string): void;
  onToggleAllFileTypes(): void;
  onLanguageChange(language: string): void;
  onSelect(nodeId: string | null): void;
  onFocus(nodeId: string): void;
  onPick(node: CodeMapNode): void;
  onOpenActivity(event: CodeMapActivityEvent): void;
}) {
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const activeFilters =
    (selectedGroups.size === groups.length ? 0 : Math.max(1, groups.length - selectedGroups.size)) +
    (selectedFileTypes.size === fileTypes.length ? 0 : Math.max(1, fileTypes.length - selectedFileTypes.size)) +
    (selectedLanguage === "all" ? 0 : 1);

  return (
    <div className="relative min-h-[650px] overflow-hidden bg-neutral-950">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_20%,rgba(56,189,248,0.055),transparent_35%),radial-gradient(circle_at_75%_75%,rgba(139,92,246,0.045),transparent_32%)]" />
      <div className={`relative grid min-h-[650px] transition-[grid-template-columns] duration-200 ${leftOpen && rightOpen ? "xl:grid-cols-[286px_minmax(0,1fr)_360px]" : leftOpen ? "xl:grid-cols-[286px_minmax(0,1fr)_0px]" : rightOpen ? "xl:grid-cols-[0px_minmax(0,1fr)_360px]" : "xl:grid-cols-[0px_minmax(0,1fr)_0px]"}`}>
        <aside className={`order-2 overflow-hidden border-t border-neutral-800/80 bg-neutral-950/88 backdrop-blur-xl transition-opacity xl:order-1 xl:border-r xl:border-t-0 ${leftOpen ? "opacity-100" : "pointer-events-none opacity-0"}`}>
          <div className="flex h-11 items-center justify-between border-b border-neutral-800/80 px-3">
            <div className="flex items-center gap-2 text-[11px] font-semibold text-neutral-200">
              <SlidersHorizontal size={13} className="text-cyan-300" /> Explore
              {activeFilters > 0 && <span className="rounded-full bg-cyan-400/10 px-1.5 py-0.5 text-[9px] text-cyan-200">{activeFilters}</span>}
            </div>
            <button type="button" onClick={() => setLeftOpen(false)} className="rounded-md p-1.5 text-neutral-500 hover:bg-neutral-800 hover:text-white" aria-label="Hide map filters"><ChevronLeft size={14} /></button>
          </div>
          <div className="max-h-[calc(100vh-300px)] overflow-y-auto">
            <CodeMapFilters groups={groups} fileTypes={fileTypes} languages={languages} selectedGroups={selectedGroups} selectedFileTypes={selectedFileTypes} selectedLanguage={selectedLanguage} onToggleGroup={onToggleGroup} onToggleAllGroups={onToggleAllGroups} onToggleFileType={onToggleFileType} onToggleAllFileTypes={onToggleAllFileTypes} onLanguageChange={onLanguageChange} />
            <CodeMapActivity events={events} liveEnabled={liveEnabled} onOpen={onOpenActivity} />
          </div>
        </aside>

        <main className="order-1 relative h-[68vh] min-h-[560px] xl:order-2 xl:h-[calc(100vh-214px)] xl:min-h-[650px]">
          <div className="absolute left-3 top-3 z-20 flex items-center gap-2">
            {!leftOpen && (
              <button type="button" onClick={() => setLeftOpen(true)} className="inline-flex h-8 items-center gap-2 rounded-lg border border-neutral-700/80 bg-neutral-950/90 px-2.5 text-[11px] text-neutral-300 shadow-xl backdrop-blur hover:border-neutral-600 hover:text-white">
                <SlidersHorizontal size={13} /> Explore
                {activeFilters > 0 && <span className="text-cyan-300">{activeFilters}</span>}
              </button>
            )}
            <div className="rounded-lg border border-neutral-800/80 bg-neutral-950/82 px-3 py-2 shadow-xl backdrop-blur">
              <div className="text-[10px] font-medium uppercase tracking-[0.16em] text-neutral-500">Code map</div>
              <div className="mt-0.5 flex items-center gap-2 text-[11px] text-neutral-300">
                <span>{graph.nodes.length.toLocaleString()} nodes</span><span className="text-neutral-700">•</span>
                <span>{graph.edges.length.toLocaleString()} relationships</span><span className="text-neutral-700">•</span>
                <span>{dimension.toUpperCase()}</span>
              </div>
            </div>
          </div>

          {!rightOpen && <button type="button" onClick={() => setRightOpen(true)} className="absolute right-3 top-3 z-20 inline-flex h-8 items-center gap-2 rounded-lg border border-neutral-700/80 bg-neutral-950/90 px-2.5 text-[11px] text-neutral-300 shadow-xl backdrop-blur hover:border-neutral-600 hover:text-white">Details <ChevronLeft size={13} /></button>}

          {!selectedNode && <div className="pointer-events-none absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded-lg border border-neutral-800/80 bg-neutral-950/78 px-3 py-2 text-[10px] text-neutral-400 shadow-xl backdrop-blur">Click to inspect · double-click to focus · drag to pan · scroll to zoom</div>}

          <Suspense fallback={<div className="flex h-full items-center justify-center gap-2 text-xs text-neutral-500"><Loader2 size={14} className="animate-spin" /> Loading map…</div>}>
            {dimension === "3d" ? (
              <CodeGraph3D nodes={graph.nodes} edges={graph.edges} selectedId={selectedId} activityByNode={activityByNode} followNodeId={liveEnabled ? followNodeId : null} onSelect={onSelect} onExpand={onFocus} />
            ) : (
              <CodeGraph nodes={graph.nodes} edges={graph.edges} selectedId={selectedId} activityByNode={activityByNode} followNodeId={liveEnabled ? followNodeId : null} onSelect={onSelect} onExpand={onFocus} />
            )}
          </Suspense>
        </main>

        <aside className={`order-3 overflow-hidden border-t border-neutral-800/80 bg-neutral-950/88 backdrop-blur-xl transition-opacity xl:border-l xl:border-t-0 ${rightOpen ? "opacity-100" : "pointer-events-none opacity-0"}`}>
          <div className="flex h-11 items-center justify-between border-b border-neutral-800/80 px-3">
            <div className="flex items-center gap-2 text-[11px] font-semibold text-neutral-200"><Activity size={13} className="text-violet-300" />{selectedNode ? "Selection" : "Inspector"}</div>
            <button type="button" onClick={() => setRightOpen(false)} className="rounded-md p-1.5 text-neutral-500 hover:bg-neutral-800 hover:text-white" aria-label="Hide map inspector"><ChevronRight size={14} /></button>
          </div>
          <div className="max-h-[calc(100vh-260px)] overflow-y-auto">
            <CodeMapInspector selectedNode={selectedNode} detail={detail} callers={callers} callees={callees} incomingCount={incomingCount} outgoingCount={outgoingCount} expandingId={expandingId} onPick={onPick} onFocus={onFocus} />
          </div>
        </aside>
      </div>
    </div>
  );
}
