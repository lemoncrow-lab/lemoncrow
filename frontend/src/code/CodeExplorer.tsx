import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { ChevronDown, ChevronRight, CircleDot, Folder, FolderOpen, ListFilter, Search, X } from "lucide-react";

import FileTypeIcon from "../components/FileTypeIcon";
import { Button as UiButton } from "../components/ui/button";
import { PanelResizeHandle } from "../components/ui/panel-resize-handle";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../components/ui/disclosure-card";
import { Input as UiInput } from "../components/ui/input";
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from "../components/ui/dropdown-menu";
import type { CodeChangedFile, CodeChanges, CodeHotspots, CodeMapActivityKind, CodeMapActivitySession, CodeMapNode } from "../api";
import { folderRollups, sessionLabel, windowPhrase, type FolderRollup } from "./codeModel";

interface DirectoryNode {
  name: string;
  path: string;
  directories: Map<string, DirectoryNode>;
  files: CodeMapNode[];
}

function buildTree(files: readonly CodeMapNode[]): DirectoryNode {
  const root: DirectoryNode = { name: "", path: "", directories: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    if (!parts.length) continue;
    let cursor = root;
    for (const part of parts.slice(0, -1)) {
      const path = cursor.path ? `${cursor.path}/${part}` : part;
      let directory = cursor.directories.get(part);
      if (!directory) {
        directory = { name: part, path, directories: new Map(), files: [] };
        cursor.directories.set(part, directory);
      }
      cursor = directory;
    }
    cursor.files.push(file);
  }
  return root;
}

const CHANGE_TONE: Record<CodeChangedFile["status"], string> = {
  M: "text-amber-300",
  A: "text-emerald-300",
  D: "text-rose-300",
  R: "text-sky-300",
};

const LIVE_TONE: Record<CodeMapActivityKind, string> = {
  search: "bg-violet-300",
  read: "bg-cyan-300",
  edit: "bg-amber-300",
  verify: "bg-emerald-300",
};

function LiveDot({ kind, label }: { kind: CodeMapActivityKind; label: string }) {
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${LIVE_TONE[kind]}`} role="img" aria-label={label} title={label} />;
}

interface BranchProps {
  directory: DirectoryNode;
  depth: number;
  selectedPath: string | null;
  selectedId: string | null;
  symbolsByPath: ReadonlyMap<string, CodeMapNode[]>;
  changed: ReadonlyMap<string, CodeChangedFile>;
  live: ReadonlyMap<string, CodeMapActivityKind>;
  rollups: ReadonlyMap<string, FolderRollup>;
  forceOpen: boolean;
  onSelect: (node: CodeMapNode) => void;
}

function DirectoryBranch({ directory, depth, selectedPath, selectedId, symbolsByPath, changed, live, rollups, forceOpen, onSelect }: BranchProps) {
  const containsSelection = !!selectedPath && (selectedPath === directory.path || selectedPath.startsWith(`${directory.path}/`));
  const [open, setOpen] = useState(depth === 0 || containsSelection || forceOpen);
  useEffect(() => {
    if (containsSelection || forceOpen) setOpen(true);
  }, [containsSelection, forceOpen]);

  const directories = [...directory.directories.values()].sort((a, b) => a.name.localeCompare(b.name));
  const files = [...directory.files].sort((a, b) => a.label.localeCompare(b.label));
  const rollup = rollups.get(directory.path);

  return (
    <>
      {directory.path && (
        <UiButton
          unstyled
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex h-7 w-full items-center gap-1.5 truncate px-2 text-left text-label text-neutral-400 transition hover:bg-muted hover:text-foreground"
          style={{ paddingLeft: 8 + depth * 12 }}
          aria-expanded={open}
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          {open ? <FolderOpen size={13} className="shrink-0 text-muted-foreground" /> : <Folder size={13} className="shrink-0 text-muted-foreground" />}
          <span className="truncate">{directory.name}</span>
          <span className="ml-auto flex shrink-0 items-center gap-1.5">
            {rollup?.live && <LiveDot kind="edit" label="An agent is working in this folder" />}
            {rollup?.changed ? (
              <span className="text-micro tabular-nums text-amber-300/80" title={`${rollup.changed} uncommitted`}>
                {rollup.changed}
              </span>
            ) : null}
          </span>
        </UiButton>
      )}
      {(open || !directory.path) && (
        <>
          {directories.map((child) => (
            <DirectoryBranch
              key={child.path}
              directory={child}
              depth={directory.path ? depth + 1 : depth}
              selectedPath={selectedPath}
              selectedId={selectedId}
              symbolsByPath={symbolsByPath}
              changed={changed}
              live={live}
              rollups={rollups}
              forceOpen={forceOpen}
              onSelect={onSelect}
            />
          ))}
          {files.map((file) => {
            const pathActive = file.path === selectedPath;
            const fileActive = file.id === selectedId;
            const symbols = pathActive ? symbolsByPath.get(file.path) ?? [] : [];
            const change = changed.get(file.path);
            const liveKind = live.get(file.path);
            return (
              <div key={file.id}>
                <UiButton
                  unstyled
                  type="button"
                  onClick={() => onSelect(file)}
                  title={file.path}
                  className={`flex h-7 w-full items-center gap-1.5 truncate border-l-2 pr-2 text-left text-label transition ${
                    fileActive
                      ? "border-cyan-400/50 bg-cyan-500/10 text-foreground"
                      : pathActive
                        ? "border-cyan-500/30 bg-muted/50 text-neutral-200"
                        : file.ignored
                          ? "border-transparent text-muted-foreground/70 hover:bg-muted hover:text-foreground"
                          : "border-transparent text-neutral-400 hover:bg-muted hover:text-foreground"
                  }`}
                  style={{ paddingLeft: 24 + depth * 12 }}
                  aria-current={fileActive ? "true" : undefined}
                >
                  <FileTypeIcon path={file.path} size={13} />
                  <span className={`truncate ${change?.status === "D" ? "line-through opacity-70" : ""}`}>{file.label}</span>
                  <span className="ml-auto flex shrink-0 items-center gap-1.5">
                    {liveKind && <LiveDot kind={liveKind} label={`Agent ${liveKind} now`} />}
                    {file.ignored && <span className="text-micro text-muted-foreground" title="Ignored by Git">ignored</span>}
                    {change && (
                      <span className={`font-mono text-micro font-semibold ${CHANGE_TONE[change.status]}`} title={`Uncommitted: ${change.status}${change.shared ? " · shared by several sessions" : ""}`}>
                        {change.status}
                      </span>
                    )}
                    {symbols.length > 0 && <span className="text-micro tabular-nums text-muted-foreground">{symbols.length}</span>}
                  </span>
                </UiButton>
                {symbols.map((symbol) => (
                  <UiButton
                    unstyled
                    key={symbol.id}
                    type="button"
                    onClick={() => onSelect(symbol)}
                    title={`${symbol.qualified_name} — ${symbol.path}:${symbol.line}`}
                    className={`flex h-7 w-full items-center gap-1.5 border-l-2 pr-2 text-left text-caption transition ${
                      symbol.id === selectedId
                        ? "border-cyan-400/50 bg-cyan-500/10 text-cyan-100"
                        : "border-transparent text-muted-foreground hover:bg-muted hover:text-neutral-200"
                    }`}
                    style={{ paddingLeft: 42 + depth * 12 }}
                    aria-current={symbol.id === selectedId ? "true" : undefined}
                  >
                    <CircleDot size={10} className="shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">{symbol.label}</span>
                    <span className="shrink-0 text-micro tabular-nums text-muted-foreground">{symbol.line}</span>
                  </UiButton>
                ))}
              </div>
            );
          })}
        </>
      )}
    </>
  );
}

export interface CodeExplorerProps {
  files: readonly CodeMapNode[];
  symbolsByPath: ReadonlyMap<string, CodeMapNode[]>;
  selectedNode: CodeMapNode | null;
  changes: CodeChanges | null;
  changed: ReadonlyMap<string, CodeChangedFile>;
  showIgnored: boolean;
  onShowIgnored: (value: boolean) => void;
  ignoredTruncated: boolean;
  live: ReadonlyMap<string, CodeMapActivityKind>;
  activitySessions: readonly CodeMapActivitySession[];
  marks: ReadonlyMap<string, string>;
  /** Session filter: a session id, "unattributed", or null. */
  sessionFilter: string | null;
  onSessionFilter: (session: string | null) => void;
  onSelect: (node: CodeMapNode) => void;
  /** Files agents re-edited in the recent window, from recorded edits. */
  hotspots?: CodeHotspots | null;
  /** False where agent sessions and edit ledgers are developer-local and never reach this surface. */
  liveWorkspace?: boolean;
  onResizeWidth?: (width: number) => void;
}

const HOTSPOT_ROWS = 10;
const SECONDARY_DEFAULT_HEIGHT = 314; // Header, handle, and about ten 28px rows.
const SECONDARY_MIN_HEIGHT = 90;
const PROJECT_MIN_HEIGHT = 160;
type ResizableSection = "sessions" | "hotspots";

export default function CodeExplorer({
  files,
  symbolsByPath,
  selectedNode,
  changes,
  changed,
  showIgnored,
  onShowIgnored,
  ignoredTruncated,
  live,
  activitySessions,
  marks,
  sessionFilter,
  onSessionFilter,
  onSelect,
  hotspots = null,
  liveWorkspace = true,
  onResizeWidth,
}: CodeExplorerProps) {
  const [fileFilter, setFileFilter] = useState("");
  const [changedOnly, setChangedOnly] = useState(false);
  const [folderFilter, setFolderFilter] = useState("all");
  const [fileTypeFilter, setFileTypeFilter] = useState("all");
  const [filesOpen, setFilesOpen] = useState(true);
  const [sessionsOpen, setSessionsOpen] = useState(true);
  const [hotspotsOpen, setHotspotsOpen] = useState(true);
  const [sessionsHeight, setSessionsHeight] = useState<number | null>(null);
  const [hotspotsHeight, setHotspotsHeight] = useState<number | null>(null);
  const explorerRef = useRef<HTMLElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const sessionsRef = useRef<HTMLElement>(null);
  const hotspotsRef = useRef<HTMLElement>(null);
  const dragRef = useRef<{ section: ResizableSection; pointerId: number; startY: number; startHeight: number } | null>(null);
  const folders = useMemo(() => [...new Set(files.map((node) => node.path.includes("/") ? node.path.split("/")[0] : ""))].sort(), [files]);
  const fileTypes = useMemo(() => [...new Set(files.map((node) => node.file_type || node.kind))].sort(), [files]);
  const filesByPath = useMemo(() => new Map(files.map((node) => [node.path, node])), [files]);
  const hotspotRows = (hotspots?.files ?? []).filter((row) => filesByPath.has(row.path)).slice(0, HOTSPOT_ROWS);
  const sessionFiles = useMemo(() => {
    if (!sessionFilter || !changes) return null;
    if (sessionFilter === "unattributed") return new Set(changes.unattributed);
    return new Set(changes.sessions.find((row) => row.id === sessionFilter)?.files ?? []);
  }, [changes, sessionFilter]);

  const filtered = useMemo(() => {
    const needle = fileFilter.trim().toLowerCase();
    return files.filter((node) => {
      if (sessionFiles && !sessionFiles.has(node.path)) return false;
      if (changedOnly && !changed.has(node.path)) return false;
      if (folderFilter !== "all" && (node.path.includes("/") ? node.path.split("/")[0] : "") !== folderFilter) return false;
      if (fileTypeFilter !== "all" && (node.file_type || node.kind) !== fileTypeFilter) return false;
      if (!needle) return true;
      if (`${node.label} ${node.path}`.toLowerCase().includes(needle)) return true;
      return (symbolsByPath.get(node.path) ?? []).some((symbol) => `${symbol.label} ${symbol.qualified_name}`.toLowerCase().includes(needle));
    });
  }, [changed, changedOnly, fileFilter, fileTypeFilter, files, folderFilter, sessionFiles, symbolsByPath]);
  const tree = useMemo(() => buildTree(filtered), [filtered]);
  const rollups = useMemo(() => folderRollups(changed, live), [changed, live]);
  const liveSessions = new Set(activitySessions.filter((session) => session.live).map((session) => session.id));
  const sessions = changes?.sessions ?? [];
  const idle = activitySessions.filter((session) => !sessions.some((row) => row.id === session.id));

  const resizeSection = (section: ResizableSection, height: number) => {
    const available = (explorerRef.current?.getBoundingClientRect().height ?? 0) - (toolbarRef.current?.getBoundingClientRect().height ?? 0);
    const other = section === "sessions" ? hotspotsRef.current : sessionsRef.current;
    const otherHeight = other?.getBoundingClientRect().height ?? 0;
    const maximum = Math.max(SECONDARY_MIN_HEIGHT, available - otherHeight - PROJECT_MIN_HEIGHT);
    const next = Math.round(Math.max(SECONDARY_MIN_HEIGHT, Math.min(maximum, height)));
    if (section === "sessions") setSessionsHeight(next);
    else setHotspotsHeight(next);
  };

  const startResize = (section: ResizableSection, event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    const pane = section === "sessions" ? sessionsRef.current : hotspotsRef.current;
    if (!pane) return;
    dragRef.current = { section, pointerId: event.pointerId, startY: event.clientY, startHeight: pane.getBoundingClientRect().height };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };
  const moveResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (drag && drag.pointerId === event.pointerId) resizeSection(drag.section, drag.startHeight + drag.startY - event.clientY);
  };
  const stopResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!dragRef.current || dragRef.current.pointerId !== event.pointerId) return;
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const resizeWithKeyboard = (section: ResizableSection, event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const pane = section === "sessions" ? sessionsRef.current : hotspotsRef.current;
    resizeSection(section, (pane?.getBoundingClientRect().height ?? SECONDARY_DEFAULT_HEIGHT) + (event.key === "ArrowUp" ? 28 : -28));
  };

  return (
    <aside ref={explorerRef} className="relative flex min-h-0 flex-col overflow-hidden border-b border-border bg-background/55 xl:border-b-0 xl:border-r" aria-label="Explorer">
      {onResizeWidth && <PanelResizeHandle label="Resize code explorer" onResize={onResizeWidth} className="hidden xl:flex" />}
      <div ref={toolbarRef} className="border-b border-border p-2">
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-micro tabular-nums text-muted-foreground">
            {filtered.length}/{files.length}
          </span>
          <div className="relative min-w-0 flex-1">
            <Search size={11} className="pointer-events-none absolute left-2 top-2.5 text-muted-foreground" />
            <UiInput
              type="search"
              value={fileFilter}
              onChange={(event) => setFileFilter(event.target.value)}
              placeholder="Filter paths…"
              uiSize="xs"
              className="w-full pl-7 pr-2"
              aria-label="Filter files"
            />
          </div>
          <UiButton type="button" size="icon" variant={changedOnly ? "default" : "ghost"} onClick={() => setChangedOnly((value) => !value)} disabled={!changes} aria-label="Changed since HEAD" aria-pressed={changedOnly} tooltip="Show only files changed since HEAD" className="h-7 w-7 shrink-0">
            <span className="font-mono text-micro font-semibold">{changes?.total ?? "…"}</span>
          </UiButton>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <UiButton type="button" size="icon" variant={showIgnored || folderFilter !== "all" || fileTypeFilter !== "all" ? "outline" : "ghost"} aria-label="Explorer filters" className="h-7 w-7 shrink-0"><ListFilter size={13} /></UiButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56" aria-label="Explorer filters">
              <DropdownMenuCheckboxItem checked={changedOnly} onCheckedChange={(checked) => setChangedOnly(Boolean(checked))} disabled={!changes}>Changed since HEAD</DropdownMenuCheckboxItem>
              <DropdownMenuCheckboxItem checked={showIgnored} onCheckedChange={(checked) => onShowIgnored(Boolean(checked))}>Show Git ignored files</DropdownMenuCheckboxItem>
              {showIgnored && ignoredTruncated && <div className="px-2 py-1 text-caption text-muted-foreground">Ignored listing limited to 2,000 additional files.</div>}
              <DropdownMenuSeparator />
              <DropdownMenuLabel>Folder in this project</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={folderFilter} onValueChange={setFolderFilter}>
                <DropdownMenuRadioItem value="all">All folders</DropdownMenuRadioItem>
                {folders.map((folder) => <DropdownMenuRadioItem key={folder} value={folder}>{folder || "Root files"}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>File type</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={fileTypeFilter} onValueChange={setFileTypeFilter}>
                <DropdownMenuRadioItem value="all">All types</DropdownMenuRadioItem>
                {fileTypes.map((type) => <DropdownMenuRadioItem key={type} value={type}>{type[0].toUpperCase() + type.slice(1)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        {sessionFilter && (
          <div className="mt-2 flex items-center gap-1.5 rounded-md border border-cyan-500/30 bg-cyan-500/5 px-2 py-1 text-caption text-cyan-100">
            <span className="truncate">
              {sessionFilter === "unattributed"
                ? changes?.sessions.length
                  ? "Unattributed changes"
                  : "Uncommitted changes"
                : `Session ${marks.get(sessionFilter) ?? ""} · ${sessionLabel(sessions.find((row) => row.id === sessionFilter) ?? { host: "", session_id: sessionFilter })}`}
            </span>
            <UiButton unstyled type="button" className="ml-auto shrink-0 text-cyan-200 hover:text-neutral-50" onClick={() => onSessionFilter(null)} aria-label="Show all files">
              <X size={11} />
            </UiButton>
          </div>
        )}
      </div>
      <Collapsible asChild open={filesOpen} onOpenChange={setFilesOpen}>
        <section className={`flex min-h-0 flex-col border-b border-border ${filesOpen ? "flex-1" : "shrink-0"}`} aria-label="Project files">
          <CollapsibleTrigger asChild>
            <UiButton unstyled type="button" className="flex h-7 w-full shrink-0 items-center gap-1.5 px-3 text-left text-micro font-semibold uppercase tracking-widest text-muted-foreground transition hover:bg-muted hover:text-foreground">
              {filesOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              Project files
              <span className="ml-auto tabular-nums">{filtered.length}</span>
            </UiButton>
          </CollapsibleTrigger>
          <CollapsibleContent className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-1">
            {filtered.length ? (
              <DirectoryBranch
                directory={tree}
                depth={0}
                selectedPath={selectedNode?.path ?? null}
                selectedId={selectedNode?.id ?? null}
                symbolsByPath={symbolsByPath}
                changed={changed}
                live={live}
                rollups={rollups}
                forceOpen={Boolean(sessionFiles) || changedOnly || folderFilter !== "all" || fileTypeFilter !== "all" || Boolean(fileFilter.trim())}
                onSelect={onSelect}
              />
            ) : (
              <p className="p-4 text-xs text-muted-foreground">No matching files.</p>
            )}
          </CollapsibleContent>
        </section>
      </Collapsible>
      <Collapsible asChild open={sessionsOpen} onOpenChange={setSessionsOpen}>
        <section ref={sessionsRef} className="flex min-h-0 shrink-0 flex-col border-b border-border" style={{ height: sessionsOpen ? sessionsHeight ?? `min(${SECONDARY_DEFAULT_HEIGHT}px, 28%)` : undefined }} aria-label="Sessions">
          {sessionsOpen && (
            <UiButton unstyled type="button" aria-label="Resize Sessions" onPointerDown={(event) => startResize("sessions", event)} onPointerMove={moveResize} onPointerUp={stopResize} onPointerCancel={stopResize} onKeyDown={(event) => resizeWithKeyboard("sessions", event)} className="flex h-2 w-full shrink-0 cursor-row-resize touch-none items-center justify-center hover:bg-muted focus-visible:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring pointer-coarse:h-4">
              <span className="h-px w-8 bg-border" />
            </UiButton>
          )}
          <CollapsibleTrigger asChild>
            <UiButton unstyled type="button" className="flex h-7 w-full shrink-0 items-center gap-1.5 px-3 text-left text-micro font-semibold uppercase tracking-widest text-muted-foreground transition hover:bg-muted hover:text-foreground">
              {sessionsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              Sessions
              {liveWorkspace && <span className="ml-auto tabular-nums">{sessions.length + idle.length + (changes?.unattributed.length ? 1 : 0)}</span>}
            </UiButton>
          </CollapsibleTrigger>
          <CollapsibleContent className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-1">
            {!liveWorkspace ? (
              <p className="px-3 py-1 text-caption text-muted-foreground">Agent sessions are recorded on developer machines and are not available here.</p>
            ) : !sessions.length && !idle.length && !changes?.unattributed.length && <p className="px-3 py-1 text-caption text-muted-foreground">No recent sessions.</p>}
            {sessions.map((session) => (
            <UiButton
              unstyled
              key={session.id}
              type="button"
              onClick={() => onSessionFilter(sessionFilter === session.id ? null : session.id)}
              aria-pressed={sessionFilter === session.id}
              className={`flex w-full items-center gap-2 px-3 py-1 text-left text-label transition hover:bg-muted ${sessionFilter === session.id ? "bg-cyan-500/10 text-foreground" : "text-neutral-300"}`}
              title={`${session.host} · ${session.session_id}${session.model ? ` · ${session.model}` : ""}`}
            >
              <span className="inline-grid h-4 min-w-4 shrink-0 place-items-center rounded-full bg-cyan-400 px-1 text-micro font-bold text-neutral-950">{marks.get(session.id)}</span>
              <span className="min-w-0 flex-1 truncate">{sessionLabel(session)}</span>
              {liveSessions.has(session.id) && <LiveDot kind="edit" label="Working now" />}
              <span className="shrink-0 text-micro tabular-nums text-muted-foreground" title={session.shared_files.length ? `${session.shared_files.length} shared with another session` : undefined}>
                {session.files.length}
              </span>
            </UiButton>
          ))}
          {idle.map((session) => (
            <div key={session.id} className="flex items-center gap-2 px-3 py-1 text-label text-muted-foreground" title={`${session.host} · ${session.session_id}`}>
              <span className="inline-grid h-4 min-w-4 shrink-0 place-items-center rounded-full border border-neutral-700 px-1 text-micro font-bold">{marks.get(session.id)}</span>
              <span className="min-w-0 flex-1 truncate">
                {session.host} {session.session_id.slice(0, 8)}
              </span>
              {session.live && <LiveDot kind="read" label="Working now, no uncommitted edits recorded" />}
              <span className="shrink-0 text-micro text-muted-foreground">0</span>
            </div>
          ))}
          {changes && changes.unattributed.length > 0 && (
            <UiButton
              unstyled
              type="button"
              onClick={() => onSessionFilter(sessionFilter === "unattributed" ? null : "unattributed")}
              aria-pressed={sessionFilter === "unattributed"}
              className={`flex w-full items-center gap-2 px-3 py-1 text-left text-label transition hover:bg-muted ${sessionFilter === "unattributed" ? "bg-cyan-500/10 text-foreground" : "text-neutral-400"}`}
              title="Uncommitted files no session recorded editing: your own edits, hosts that record nothing, or edits made before capture began."
            >
              <span className="inline-grid h-4 min-w-4 shrink-0 place-items-center rounded-full border border-dashed border-neutral-600 px-1 text-micro">?</span>
              <span className="min-w-0 flex-1 truncate">{sessions.length ? "Unattributed" : "Uncommitted changes"}</span>
              <span className="shrink-0 text-micro tabular-nums text-muted-foreground">{changes.unattributed.length}</span>
            </UiButton>
          )}
          </CollapsibleContent>
        </section>
      </Collapsible>
      <Collapsible asChild open={hotspotsOpen} onOpenChange={setHotspotsOpen}>
        <section ref={hotspotsRef} className="flex min-h-0 shrink-0 flex-col" style={{ height: hotspotsOpen ? hotspotsHeight ?? `min(${SECONDARY_DEFAULT_HEIGHT}px, 28%)` : undefined }} aria-label={`Re-edited ${windowPhrase(hotspots?.days ?? 7)}`}>
          {hotspotsOpen && (
            <UiButton unstyled type="button" aria-label="Resize Re-edited files" onPointerDown={(event) => startResize("hotspots", event)} onPointerMove={moveResize} onPointerUp={stopResize} onPointerCancel={stopResize} onKeyDown={(event) => resizeWithKeyboard("hotspots", event)} className="flex h-2 w-full shrink-0 cursor-row-resize touch-none items-center justify-center hover:bg-muted focus-visible:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring pointer-coarse:h-4">
              <span className="h-px w-8 bg-border" />
            </UiButton>
          )}
          <CollapsibleTrigger asChild>
            <UiButton
              unstyled
              type="button"
              className="flex h-7 w-full shrink-0 items-center gap-1.5 px-3 text-left text-micro font-semibold uppercase tracking-widest text-muted-foreground transition hover:bg-muted hover:text-foreground"
              title={`Files agents edited more than once ${windowPhrase(hotspots?.days ?? 7)}, counted from the edits their sessions recorded.`}
            >
              {hotspotsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              Re-edited {windowPhrase(hotspots?.days ?? 7)}
              {liveWorkspace && <span className="ml-auto tabular-nums">{hotspotRows.length}</span>}
            </UiButton>
          </CollapsibleTrigger>
          <CollapsibleContent className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-1">
            {!liveWorkspace ? (
              <p className="px-3 py-1 text-caption text-muted-foreground">Edit history is recorded on developer machines and is not available here.</p>
            ) : !hotspotRows.length && <p className="px-3 py-1 text-caption text-muted-foreground">No re-edited files.</p>}
            {hotspotRows.map((row) => {
            const selected = selectedNode?.path === row.path;
            const sessionsText = `${row.sessions.length} ${row.sessions.length === 1 ? "session" : "sessions"}`;
            return (
              <UiButton
                unstyled
                key={row.path}
                type="button"
                onClick={() => {
                  const node = filesByPath.get(row.path);
                  if (node) onSelect(node);
                }}
                aria-current={selected ? "true" : undefined}
                aria-label={`${row.path}: ${row.edits} edits by ${sessionsText}`}
                title={`${row.path}\n${row.edits} edits by ${sessionsText}`}
                className={`flex w-full items-center gap-2 px-3 py-1 text-left text-label transition hover:bg-muted ${selected ? "bg-cyan-500/10 text-foreground" : "text-neutral-300"}`}
              >
                <FileTypeIcon path={row.path} size={12} />
                <span className="min-w-0 flex-1 truncate">{row.path.split("/").pop()}</span>
                <span className="shrink-0 text-micro tabular-nums text-muted-foreground">{row.edits}×</span>
              </UiButton>
            );
          })}
          </CollapsibleContent>
        </section>
      </Collapsible>
    </aside>
  );
}
