import { useCallback, useMemo, useState } from "react";
import { ChevronRight, Crosshair, FileCode2, Loader2, RotateCw } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import type { CodeFocusPayload, CodeModulesOverview } from "../api";
import ColumnCanvas, { type ColumnActivity } from "../map/ColumnCanvas";
import { buildColumnModel } from "../map/columnModel";
import MapCanvas from "../map/MapCanvas";
import { buildMapModel, type MapLane } from "../map/mapModel";
import type { MapMemberSymbol, MapNode } from "../map/types";
import { indexNote } from "./CodeInspector";
import { moduleOfPath, overlayFocus, type LiveMarks, type MapSpec } from "./codeModel";

const MAX_OVERVIEW_CARDS = 40;

const LANE_TITLES: Record<"module" | "file" | "symbol", Partial<Record<MapLane, string>>> = {
  module: { used_by: "Used by", subject: "Module", uses: "Uses" },
  file: { used_by: "Used by", subject: "File", uses: "Uses" },
  symbol: { used_by: "Callers", subject: "Symbol", uses: "Calls" },
};

export interface Crumb {
  label: string;
  spec: MapSpec;
  /** Where "Open in Source" goes for this step. */
  source?: { path: string; symbolId?: string; line?: number };
}

export interface CodeMapViewProps {
  spec: MapSpec;
  crumbs: Crumb[];
  overview: CodeModulesOverview | null;
  overviewLoading: boolean;
  overviewError: string | null;
  focus: CodeFocusPayload | null;
  focusLoading: boolean;
  focusError: string | null;
  selectedModuleId: string | null;
  selectedFocusId: string | null;
  live: ReadonlyMap<string, LiveMarks>;
  follow: boolean;
  followAvailable: boolean;
  onFollow: (follow: boolean) => void;
  onRetry: () => void;
  onSpec: (spec: MapSpec) => void;
  onSelectModule: (id: string | null) => void;
  onSelectFocusNode: (node: MapNode | null) => void;
  onOpenFocusNode: (node: MapNode) => void;
  onOpenSymbol: (symbol: MapMemberSymbol) => void;
  onOpenSource: (target: { path: string; symbolId?: string; line?: number }) => void;
  onInteract: () => void;
}

function Status({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full min-h-[420px] items-center justify-center gap-2 p-6 text-center text-xs text-muted-foreground">{children}</div>;
}

export default function CodeMapView({
  spec,
  crumbs,
  overview,
  overviewLoading,
  overviewError,
  focus,
  focusLoading,
  focusError,
  selectedModuleId,
  selectedFocusId,
  live,
  follow,
  followAvailable,
  onFollow,
  onRetry,
  onSpec,
  onSelectModule,
  onSelectFocusNode,
  onOpenFocusNode,
  onOpenSymbol,
  onOpenSource,
  onInteract,
}: CodeMapViewProps) {
  const [width, setWidth] = useState(1100);
  const [expandedFolds, setExpandedFolds] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const onWidth = useCallback((next: number) => setWidth((current) => (Math.abs(current - next) > 4 ? next : current)), []);

  const columnModel = useMemo(() => {
    if (spec || !overview) return null;
    return buildColumnModel(
      overview.modules.map((module) => ({
        id: module.id,
        label: module.label,
        parent: module.parent,
        files: module.files,
        symbols: module.symbols,
        changed: module.changed,
        purpose: module.purpose,
        purposeGenerated: module.purpose_kind === "generated",
      })),
      overview.edges,
      { width, maxCards: MAX_OVERVIEW_CARDS, expanded: expandedFolds },
    );
  }, [expandedFolds, overview, spec, width]);

  const columnActivity = useMemo(() => {
    const out = new Map<string, ColumnActivity>();
    if (!columnModel || !overview) return out;
    for (const [path, marks] of live) {
      const module = moduleOfPath(path, overview.modules);
      if (module === null) continue;
      const card = columnModel.cardOf.get(module);
      if (!card) continue;
      const prior = out.get(card);
      out.set(card, { kind: prior?.kind === "edit" ? "edit" : marks.kind, sessions: [...new Set([...(prior?.sessions ?? []), ...marks.sessions])] });
    }
    return out;
  }, [columnModel, live, overview]);

  const focusPayload = useMemo(() => (focus ? overlayFocus(focus, live) : null), [focus, live]);
  const focusModel = useMemo(
    () => (spec && focusPayload ? buildMapModel(focusPayload, { width, linkMode: "key", hideTests: false, expanded: expandedGroups }) : null),
    [expandedGroups, focusPayload, spec, width],
  );
  const focusNodes = useMemo(() => new Map((focus?.graph.nodes ?? []).map((node) => [node.id, node])), [focus]);

  const toggle = (setter: React.Dispatch<React.SetStateAction<Set<string>>>) => (key: string) =>
    setter((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const context = spec ? focus?.context : overview?.context;
  const last = crumbs[crumbs.length - 1];
  const filteredCount = spec
    ? focus?.graph.stats?.filtered_relationships ?? 0
    : Object.values(overview?.filtered ?? {}).reduce((sum, value) => sum + value, 0);

  let body: React.ReactNode;
  if (!spec) {
    if (overviewError) {
      body = (
        <Status>
          <span className="text-red-300/80">{overviewError}</span>
          <UiButton type="button" size="xs" variant="outline" onClick={onRetry}>
            <RotateCw size={10} /> Retry
          </UiButton>
        </Status>
      );
    } else if (!columnModel) {
      body = (
        <Status>
          <Loader2 size={13} className="animate-spin" /> Grouping the repository into modules…
        </Status>
      );
    } else if (!columnModel.cards.length) {
      body = <Status>No files to map yet.</Status>;
    } else {
      body = (
        <ColumnCanvas
          model={columnModel}
          selectedId={selectedModuleId ? columnModel.cardOf.get(selectedModuleId) ?? selectedModuleId : null}
          activity={columnActivity}
          onSelect={onSelectModule}
          onOpen={(id) => onSpec({ level: "module", id })}
          onToggleFold={toggle(setExpandedFolds)}
          onWidth={onWidth}
        />
      );
    }
  } else if (focusError) {
    body = (
      <Status>
        <span className="text-red-300/80">{focusError}</span>
        <UiButton type="button" size="xs" variant="outline" onClick={onRetry}>
          <RotateCw size={10} /> Retry
        </UiButton>
      </Status>
    );
  } else if (!focusModel || (focusLoading && !focus)) {
    body = (
      <Status>
        <Loader2 size={13} className="animate-spin" /> Reading what uses it and what it uses…
      </Status>
    );
  } else {
    body = (
      <MapCanvas
        model={focusModel}
        selectedId={selectedFocusId}
        selectedEdgeId={null}
        laneTitles={LANE_TITLES[spec.level]}
        ariaLabel="Code map"
        onSelect={(id) => onSelectFocusNode(id ? focusNodes.get(id) ?? null : null)}
        onSelectEdge={() => undefined}
        onOpen={(id) => {
          const node = focusNodes.get(id);
          if (node) onOpenFocusNode(node);
        }}
        onOpenSymbol={onOpenSymbol}
        onToggleGroup={toggle(setExpandedGroups)}
        onWidth={onWidth}
      />
    );
  }

  return (
    <main className="flex min-h-[560px] min-w-0 flex-col bg-surface-sunken" onPointerDown={onInteract} onKeyDown={onInteract} onWheel={onInteract}>
      <div className="flex h-9 shrink-0 items-center gap-1 overflow-x-auto whitespace-nowrap border-b border-border bg-background/75 px-3 text-label">
        <nav aria-label="Map location" className="flex min-w-0 items-center gap-1">
          {crumbs.map((crumb, index) => (
            <span key={`${crumb.label}-${index}`} className="flex min-w-0 items-center gap-1">
              {index > 0 && <ChevronRight size={11} className="shrink-0 text-muted-foreground" />}
              {index === crumbs.length - 1 ? (
                <span className="truncate font-medium text-neutral-200" aria-current="location">
                  {crumb.label}
                </span>
              ) : (
                <UiButton unstyled type="button" className="truncate text-muted-foreground hover:text-foreground" onClick={() => onSpec(crumb.spec)}>
                  {crumb.label}
                </UiButton>
              )}
            </span>
          ))}
        </nav>
        <span className="ml-auto flex shrink-0 items-center gap-2 pl-3">
          {(spec ? focusLoading && focus : overviewLoading && overview) && <Loader2 size={11} className="animate-spin text-muted-foreground" aria-label="Refreshing" />}
          {context && <span className="text-caption text-muted-foreground" title={context.readiness?.reason || undefined}>{indexNote(context)}</span>}
          {filteredCount > 0 && (
            <span className="text-caption text-muted-foreground" title="Name matches across languages or from shipping code into tests are not drawn.">
              {filteredCount} name {filteredCount === 1 ? "match" : "matches"} left out
            </span>
          )}
          {(spec ? focus?.graph.truncated : overview?.links_truncated) && <span className="text-caption text-amber-300/80">bounded</span>}
          {last?.source && (
            <UiButton type="button" size="xs" variant="ghost" onClick={() => onOpenSource(last.source!)}>
              <FileCode2 size={10} /> Open in Source
            </UiButton>
          )}
          <UiButton
            type="button"
            size="xs"
            variant="outline"
            active={follow}
            disabled={!followAvailable}
            aria-pressed={follow}
            onClick={() => onFollow(!follow)}
            title={followAvailable ? "Keep the map on the file the followed session is editing, except while you are using the map." : "No agent session to follow."}
          >
            <Crosshair size={10} /> Follow
          </UiButton>
        </span>
      </div>
      <div className="relative min-h-0 flex-1">{body}</div>
    </main>
  );
}
