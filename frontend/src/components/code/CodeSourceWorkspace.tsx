import { useEffect, useMemo, useRef, useState } from "react";
import { File } from "@pierre/diffs/react";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Copy,
  FileCode2,
  Folder,
  FolderOpen,
  Loader2,
  Network,
  Search,
  Send,
} from "lucide-react";

import {
  api,
  type CodeMapActivityEvent,
  type CodeMapActivityKind,
  type CodeMapAgentHandoffResponse,
  type CodeMapEditorCapability,
  type CodeMapEditorOpenResponse,
  type CodeMapNode,
  type CodeMapReference,
  type CodeMapReferences,
} from "../../api";

const ACTIVITY_TONE: Record<
  CodeMapActivityKind,
  { dot: string; text: string; label: string }
> = {
  search: { dot: "bg-violet-300", text: "text-violet-200", label: "Search" },
  read: { dot: "bg-cyan-300", text: "text-cyan-200", label: "Read" },
  edit: { dot: "bg-amber-300", text: "text-amber-200", label: "Edit" },
  verify: { dot: "bg-emerald-300", text: "text-emerald-200", label: "Verify" },
};

interface DirectoryNode {
  name: string;
  path: string;
  directories: Map<string, DirectoryNode>;
  files: CodeMapNode[];
}

function relativeTime(value: string): string {
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "now";
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 10) return "now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function absoluteProjectPath(projectRoot: string, path: string): string {
  if (path.startsWith("/")) return path;
  return `${projectRoot.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
}

function displayRange(range: readonly [number, number]): string {
  return range[0] === range[1] ? `L${range[0]}` : `L${range[0]}–L${range[1]}`;
}

function copyRange(path: string, range: readonly [number, number]): string {
  return range[0] === range[1]
    ? `${path}:L${range[0]}`
    : `${path}:L${range[0]}-L${range[1]}`;
}

function buildTree(files: CodeMapNode[]): DirectoryNode {
  const root: DirectoryNode = {
    name: "",
    path: "",
    directories: new Map(),
    files: [],
  };

  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    if (!parts.length) continue;
    let cursor = root;
    for (const part of parts.slice(0, -1)) {
      const path = cursor.path ? `${cursor.path}/${part}` : part;
      let directory = cursor.directories.get(part);
      if (!directory) {
        directory = {
          name: part,
          path,
          directories: new Map(),
          files: [],
        };
        cursor.directories.set(part, directory);
      }
      cursor = directory;
    }
    cursor.files.push(file);
  }

  return root;
}

function DirectoryBranch({
  directory,
  depth,
  selectedPath,
  selectedId,
  symbolsByPath,
  onSelect,
}: {
  directory: DirectoryNode;
  depth: number;
  selectedPath: string | null;
  selectedId: string | null;
  symbolsByPath: Map<string, CodeMapNode[]>;
  onSelect: (node: CodeMapNode) => void;
}) {
  const containsSelection =
    !!selectedPath &&
    (selectedPath === directory.path ||
      selectedPath.startsWith(`${directory.path}/`));
  const [open, setOpen] = useState(depth === 0 || containsSelection);

  useEffect(() => {
    if (containsSelection) setOpen(true);
  }, [containsSelection]);

  const directories = [...directory.directories.values()].sort((a, b) =>
    a.name.localeCompare(b.name)
  );
  const files = [...directory.files].sort((a, b) =>
    a.label.localeCompare(b.label)
  );

  return (
    <>
      {directory.path && (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex h-7 w-full items-center gap-1.5 truncate px-2 text-left text-[11px] text-neutral-400 transition hover:bg-neutral-900 hover:text-neutral-100"
          style={{ paddingLeft: 8 + depth * 12 }}
          aria-expanded={open}
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          {open ? (
            <FolderOpen size={13} className="shrink-0 text-neutral-500" />
          ) : (
            <Folder size={13} className="shrink-0 text-neutral-600" />
          )}
          <span className="truncate">{directory.name}</span>
        </button>
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
              onSelect={onSelect}
            />
          ))}
          {files.map((file) => {
            const pathActive = file.path === selectedPath;
            const fileActive = file.id === selectedId;
            const symbols = pathActive ? (symbolsByPath.get(file.path) ?? []) : [];
            return (
              <div key={file.id}>
                <button
                  type="button"
                  onClick={() => onSelect(file)}
                  title={file.path}
                  className={`flex h-7 w-full items-center gap-1.5 truncate border-l-2 pr-2 text-left text-[11px] transition ${
                    fileActive
                      ? "border-cyan-400 bg-cyan-500/10 text-neutral-100"
                      : pathActive
                        ? "border-cyan-500/30 bg-neutral-900/50 text-neutral-200"
                        : "border-transparent text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100"
                  }`}
                  style={{ paddingLeft: 24 + depth * 12 }}
                  aria-current={fileActive ? "true" : undefined}
                >
                  <FileCode2
                    size={12}
                    className={pathActive ? "text-cyan-300" : "text-neutral-600"}
                  />
                  <span className="truncate">{file.label}</span>
                  {symbols.length > 0 && (
                    <span className="ml-auto text-[9px] tabular-nums text-neutral-600">
                      {symbols.length}
                    </span>
                  )}
                </button>
                {symbols.map((symbol) => (
                  <button
                    key={symbol.id}
                    type="button"
                    onClick={() => onSelect(symbol)}
                    title={`${symbol.qualified_name} — ${symbol.path}:${symbol.line}`}
                    className={`flex h-7 w-full items-center gap-1.5 border-l-2 pr-2 text-left text-[10px] transition ${
                      symbol.id === selectedId
                        ? "border-cyan-400 bg-cyan-500/10 text-cyan-100"
                        : "border-transparent text-neutral-500 hover:bg-neutral-900 hover:text-neutral-200"
                    }`}
                    style={{ paddingLeft: 42 + depth * 12 }}
                    aria-current={symbol.id === selectedId ? "true" : undefined}
                  >
                    <CircleDot size={10} className="shrink-0 text-neutral-600" />
                    <span className="min-w-0 flex-1 truncate">{symbol.label}</span>
                    <span className="shrink-0 tabular-nums text-[9px] text-neutral-700">
                      {symbol.line}
                    </span>
                  </button>
                ))}
              </div>
            );
          })}
        </>
      )}
    </>
  );
}

function RelationshipList({
  title,
  nodes,
  direction,
  onSelect,
}: {
  title: string;
  nodes: CodeMapNode[];
  direction: "incoming" | "outgoing";
  onSelect: (node: CodeMapNode) => void;
}) {
  const Icon = direction === "incoming" ? ArrowDownLeft : ArrowUpRight;
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-neutral-500">
        <Icon
          size={11}
          className={direction === "incoming" ? "text-violet-300" : "text-cyan-300"}
        />
        {title} · {nodes.length}
      </div>
      {nodes.length ? (
        <div className="space-y-0.5">
          {nodes.slice(0, 30).map((node) => (
            <button
              key={node.id}
              type="button"
              onClick={() => onSelect(node)}
              className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-[11px] text-neutral-300 transition hover:bg-neutral-900 hover:text-white"
              title={`${node.qualified_name} — ${node.path}:${node.line}`}
            >
              <span className="min-w-0 flex-1 truncate">{node.label}</span>
              <span className="max-w-24 shrink-0 truncate text-[9px] text-neutral-600">
                {node.path.split("/").pop()}
              </span>
            </button>
          ))}
        </div>
      ) : (
        <p className="px-2 py-1 text-[11px] text-neutral-600">None in this graph.</p>
      )}
    </div>
  );
}

function ReferenceList({
  payload,
  loading,
  error,
  onOpen,
}: {
  payload: CodeMapReferences | null;
  loading: boolean;
  error: string | null;
  onOpen: (reference: CodeMapReference) => void;
}) {
  const shown = payload?.references ?? [];
  const total = payload?.reference_count ?? 0;
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-neutral-500">
        <Search size={11} className="text-amber-300" />
        References{payload ? ` · ${total}` : ""}
        {payload?.truncated && (
          <span className="ml-auto text-[9px] font-normal normal-case tracking-normal text-neutral-600">
            partial
          </span>
        )}
      </div>
      {loading ? (
        <div className="flex items-center gap-2 px-2 py-2 text-[10px] text-neutral-600">
          <Loader2 size={11} className="animate-spin" /> Finding usages…
        </div>
      ) : error ? (
        <p className="px-2 py-1 text-[10px] leading-4 text-red-300/70">{error}</p>
      ) : shown.length ? (
        <div className="space-y-0.5">
          {shown.slice(0, 40).map((reference, index) => (
            <button
              key={`${reference.path}:${reference.line}:${reference.column}:${index}`}
              type="button"
              onClick={() => onOpen(reference)}
              className="w-full border-l-2 border-transparent px-2 py-1.5 text-left transition hover:border-amber-400/50 hover:bg-neutral-900"
              title={`${reference.path}:${reference.line}`}
            >
              <div className="flex min-w-0 items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[10px] text-neutral-300">
                  {reference.path}
                </span>
                <span className="shrink-0 font-mono text-[9px] tabular-nums text-amber-300/70">
                  L{reference.line}
                </span>
              </div>
              {reference.caller && (
                <div className="mt-0.5 truncate text-[9px] text-neutral-600">
                  {reference.caller}
                </div>
              )}
              {reference.snippet && (
                <code className="mt-1 block truncate font-mono text-[9px] text-neutral-500">
                  {reference.snippet.trim()}
                </code>
              )}
            </button>
          ))}
        </div>
      ) : payload ? (
        <p className="px-2 py-1 text-[11px] text-neutral-600">No usages found.</p>
      ) : null}
    </div>
  );
}

function AgentHandoff({
  host,
  sessionId,
  supported,
  contextKey,
  onSend,
}: {
  host: string | null;
  sessionId: string | null;
  supported: boolean;
  contextKey: string;
  onSend: (message: string) => Promise<CodeMapAgentHandoffResponse>;
}) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<CodeMapAgentHandoffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setMessage("");
    setResult(null);
    setError(null);
  }, [contextKey]);

  const submit = async () => {
    const clean = message.trim();
    if (!clean || !sessionId || sending) return;
    setSending(true);
    setResult(null);
    setError(null);
    try {
      const response = await onSend(clean);
      setResult(response);
      if (response.state === "sent") setMessage("");
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : "Could not hand off to the agent.");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="border-b border-neutral-800 p-3">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-300">
        <Bot size={12} className="text-brand-300" /> Ask agent
        {sessionId && (
          <span className="ml-auto max-w-32 truncate text-[9px] font-normal normal-case tracking-normal text-neutral-600">
            {host || "agent"} · {sessionId.slice(0, 8)}
          </span>
        )}
      </div>
      {sessionId && supported ? (
        <>
          <textarea
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="Ask about this file, symbol, or exact line…"
            rows={3}
            className="mt-2 w-full resize-none border border-neutral-800 bg-neutral-900/60 px-2.5 py-2 text-[11px] leading-4 text-neutral-200 outline-none placeholder:text-neutral-600 focus:border-brand-500/60"
            aria-label="Ask agent about code"
          />
          <div className="mt-2 flex items-center justify-between gap-2">
            <p className="text-[9px] leading-4 text-neutral-600">
              Resumes this exact session with the current location attached.
            </p>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!message.trim() || sending}
              className="inline-flex h-7 shrink-0 items-center gap-1.5 border border-brand-500/40 bg-brand-500/10 px-2.5 text-[10px] font-medium text-brand-300 hover:bg-brand-500/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {sending ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
              Send
            </button>
          </div>
        </>
      ) : (
        <p className="mt-2 text-[10px] leading-4 text-neutral-600">
          {sessionId && host
            ? `Exact ${host} session found, but direct handoff is not supported.`
            : "No exact coding-agent session is available for this project yet."}
        </p>
      )}
      {result && (
        <p
          className={`mt-2 text-[10px] leading-4 ${
            result.state === "sent"
              ? "text-emerald-300/80"
              : result.state === "uncertain"
                ? "text-amber-300/80"
                : "text-red-300/80"
          }`}
        >
          {result.message || `Handoff ${result.state}.`}
        </p>
      )}
      {error && <p className="mt-2 text-[10px] leading-4 text-red-300/80">{error}</p>}
    </div>
  );
}

export default function CodeSourceWorkspace({
  projectRoot,
  allNodes,
  selectedNode,
  callers,
  callees,
  references,
  referencesLoading,
  referencesError,
  locationRange,
  events,
  liveEnabled,
  agentHost,
  agentSessionId,
  agentHandoffSupported,
  editorCapability,
  onSelect,
  onOpenMap,
  onOpenActivity,
  onOpenReference,
  onOpenLine,
  onOpenEditor,
  onAskAgent,
}: {
  projectRoot: string;
  allNodes: CodeMapNode[];
  selectedNode: CodeMapNode | null;
  callers: CodeMapNode[];
  callees: CodeMapNode[];
  references: CodeMapReferences | null;
  referencesLoading: boolean;
  referencesError: string | null;
  locationRange: readonly [number, number] | null;
  events: CodeMapActivityEvent[];
  liveEnabled: boolean;
  agentHost: string | null;
  agentSessionId: string | null;
  agentHandoffSupported: boolean;
  editorCapability: CodeMapEditorCapability | null;
  onSelect: (node: CodeMapNode) => void;
  onOpenMap: (node: CodeMapNode) => void;
  onOpenActivity: (event: CodeMapActivityEvent) => void;
  onOpenReference: (reference: CodeMapReference) => void;
  onOpenLine: (line: number) => void;
  onOpenEditor: () => Promise<CodeMapEditorOpenResponse>;
  onAskAgent: (message: string) => Promise<CodeMapAgentHandoffResponse>;
}) {
  const [fileFilter, setFileFilter] = useState("");
  const [source, setSource] = useState<string | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [copied, setCopied] = useState<"path" | "ref" | null>(null);
  const [lineInput, setLineInput] = useState("");
  const [editorOpening, setEditorOpening] = useState(false);
  const [editorError, setEditorError] = useState<string | null>(null);
  const sourceCache = useRef<Map<string, { editId: string; content: string }>>(new Map());
  const [chromeTheme, setChromeTheme] = useState<"light" | "dark">(() =>
    typeof document !== "undefined" && document.documentElement.classList.contains("dark")
      ? "dark"
      : "light"
  );

  const fileNodes = useMemo(
    () =>
      allNodes
        .filter((node) => node.node_type === "file")
        .sort((a, b) => a.path.localeCompare(b.path)),
    [allNodes]
  );
  const symbolsByPath = useMemo(() => {
    const byPath = new Map<string, CodeMapNode[]>();
    for (const node of allNodes) {
      if (node.node_type === "file" || !node.path) continue;
      const symbols = byPath.get(node.path);
      if (symbols) symbols.push(node);
      else byPath.set(node.path, [node]);
    }
    for (const symbols of byPath.values()) {
      symbols.sort((a, b) => a.line - b.line || a.label.localeCompare(b.label));
    }
    return byPath;
  }, [allNodes]);

  const filteredFiles = useMemo(() => {
    const needle = fileFilter.trim().toLowerCase();
    if (!needle) return fileNodes;
    return fileNodes.filter((node) => {
      if (`${node.label} ${node.path}`.toLowerCase().includes(needle)) {
        return true;
      }
      return (symbolsByPath.get(node.path) ?? []).some((symbol) =>
        `${symbol.label} ${symbol.qualified_name}`.toLowerCase().includes(needle)
      );
    });
  }, [fileFilter, fileNodes, symbolsByPath]);
  const tree = useMemo(() => buildTree(filteredFiles), [filteredFiles]);

  const selectedPath = selectedNode?.path ?? null;
  const absolutePath =
    selectedPath && projectRoot
      ? absoluteProjectPath(projectRoot, selectedPath)
      : null;
  const selectedEditId = useMemo(
    () =>
      selectedPath
        ? [...events]
            .reverse()
            .find((event) => event.kind === "edit" && event.path === selectedPath)?.id ?? ""
        : "",
    [events, selectedPath]
  );

  useEffect(() => {
    if (typeof document === "undefined" || typeof MutationObserver === "undefined") return;
    const root = document.documentElement;
    const syncTheme = () =>
      setChromeTheme(root.classList.contains("dark") ? "dark" : "light");
    syncTheme();
    const observer = new MutationObserver(syncTheme);
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!absolutePath) {
      setSource(null);
      setSourceError(null);
      setSourceLoading(false);
      return;
    }

    const cached = sourceCache.current.get(absolutePath);
    if (cached?.editId === selectedEditId) {
      setSource(cached.content);
      setSourceError(null);
      setSourceLoading(false);
      return;
    }

    let cancelled = false;
    setSourceLoading(true);
    setSourceError(null);
    void api
      .fileContent(absolutePath)
      .then((content) => {
        if (cancelled) return;
        const cache = sourceCache.current;
        cache.set(absolutePath, { editId: selectedEditId, content });
        if (cache.size > 24) {
          const oldest = cache.keys().next().value;
          if (oldest) cache.delete(oldest);
        }
        setSource(content);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setSource(null);
        setSourceError(
          caught instanceof Error ? caught.message : "Could not read this file."
        );
      })
      .finally(() => {
        if (!cancelled) setSourceLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [absolutePath, selectedEditId]);

  const selectedRange =
    locationRange ??
    (selectedNode && selectedNode.node_type !== "file"
      ? ([selectedNode.line, selectedNode.end_line] as const)
      : null);

  const copy = async (kind: "path" | "ref") => {
    if (!selectedNode) return;
    const text =
      kind === "path"
        ? selectedNode.path
        : selectedRange
          ? copyRange(selectedNode.path, selectedRange)
          : selectedNode.path;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(kind);
      window.setTimeout(() => setCopied(null), 1200);
    } catch {
      setCopied(null);
    }
  };
  useEffect(() => {
    setLineInput(locationRange ? String(locationRange[0]) : "");
    setEditorError(null);
  }, [locationRange, selectedPath]);

  const sourceOptions = useMemo(
    () => ({
      theme: chromeTheme === "dark" ? "pierre-dark" : "pierre-light",
      themeType: chromeTheme,
      disableFileHeader: true,
      overflow: "scroll" as const,
      stickyHeader: false,
      onPostRender: (
        node: HTMLElement,
        _instance: unknown,
        phase: "mount" | "update" | "unmount"
      ) => {
        if (phase === "unmount" || !selectedRange) return;
        const lineIndex = selectedRange[0] - 1;
        const target = node.shadowRoot?.querySelector<HTMLElement>(
          `[data-line][data-line-index="${lineIndex}"]`
        );
        target?.scrollIntoView?.({ block: "center" });
      },
    }),
    [chromeTheme, selectedRange]
  );

  return (
    <div className="grid min-h-[650px] grid-cols-1 xl:grid-cols-[270px_minmax(0,1fr)_310px]">
      <aside className="border-b border-neutral-800 bg-neutral-950/55 xl:border-b-0 xl:border-r">
        <div className="border-b border-neutral-800 p-3">
          <div className="flex items-center justify-between">
            <div className="text-[10px] font-semibold uppercase tracking-widest text-neutral-300">
              Files
            </div>
            <span className="text-[9px] tabular-nums text-neutral-600">
              {filteredFiles.length}/{fileNodes.length}
            </span>
          </div>
          <div className="relative mt-2">
            <Search
              size={12}
              className="pointer-events-none absolute left-2.5 top-2.5 text-neutral-600"
            />
            <input
              type="search"
              value={fileFilter}
              onChange={(event) => setFileFilter(event.target.value)}
              placeholder="Filter paths…"
              className="h-8 w-full border border-neutral-800 bg-neutral-900/60 pl-8 pr-2 text-[11px] text-neutral-200 outline-none placeholder:text-neutral-600 focus:border-brand-500/60"
              aria-label="Filter files"
            />
          </div>
        </div>
        <div className="max-h-[calc(100vh-245px)] min-h-[570px] overflow-y-auto py-1">
          {filteredFiles.length ? (
            <DirectoryBranch
              directory={tree}
              depth={0}
              selectedPath={selectedPath}
              selectedId={selectedNode?.id ?? null}
              symbolsByPath={symbolsByPath}
              onSelect={onSelect}
            />
          ) : (
            <p className="p-4 text-xs text-neutral-600">No matching files.</p>
          )}
        </div>
      </aside>

      <main className="min-w-0 bg-surface-code">
        {selectedNode ? (
          <div className="flex h-full min-h-[650px] flex-col">
            <div className="flex min-h-12 flex-wrap items-center gap-2 border-b border-neutral-800 bg-neutral-950/75 px-4 py-2">
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-1 text-[10px] text-neutral-600">
                  {selectedNode.path.split("/").map((part, index, parts) => (
                    <span
                      key={`${part}-${index}`}
                      className="flex min-w-0 items-center gap-1"
                    >
                      <span
                        className={
                          index === parts.length - 1
                            ? "truncate text-neutral-300"
                            : "truncate"
                        }
                      >
                        {part}
                      </span>
                      {index < parts.length - 1 && <span>/</span>}
                    </span>
                  ))}
                </div>
                {selectedNode.node_type !== "file" && (
                  <div className="mt-0.5 truncate text-xs font-medium text-neutral-200">
                    {selectedNode.qualified_name || selectedNode.label}
                  </div>
                )}
              </div>
              <form
                className="flex h-7 items-stretch border border-neutral-800 bg-neutral-900/60"
                onSubmit={(event) => {
                  event.preventDefault();
                  const line = Number(lineInput);
                  if (Number.isInteger(line) && line > 0) onOpenLine(line);
                }}
              >
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={lineInput}
                  onChange={(event) => setLineInput(event.target.value)}
                  placeholder="Line"
                  aria-label="Go to line"
                  className="w-16 bg-transparent px-2 text-[10px] tabular-nums text-neutral-300 outline-none placeholder:text-neutral-600"
                />
                <button
                  type="submit"
                  aria-label="Open source line"
                  className="border-l border-neutral-800 px-2 text-[10px] text-neutral-500 hover:text-neutral-100"
                >
                  Go
                </button>
              </form>
              {editorCapability?.available && editorCapability.preferred && (
                <button
                  type="button"
                  onClick={() => {
                    if (editorOpening) return;
                    setEditorOpening(true);
                    setEditorError(null);
                    void onOpenEditor()
                      .catch((caught: unknown) => {
                        setEditorError(
                          caught instanceof Error
                            ? caught.message
                            : "Could not open the local editor."
                        );
                      })
                      .finally(() => setEditorOpening(false));
                  }}
                  disabled={editorOpening}
                  aria-label={`Open in ${editorCapability.preferred.label}`}
                  className="inline-flex h-7 items-center gap-1.5 border border-neutral-700 bg-neutral-900 px-2 text-[10px] font-medium text-neutral-300 hover:border-neutral-600 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {editorOpening ? (
                    <Loader2 size={11} className="animate-spin" />
                  ) : (
                    <ArrowUpRight size={11} />
                  )}
                  {editorOpening ? "Opening…" : `Open in ${editorCapability.preferred.label}`}
                </button>
              )}
              <button
                type="button"
                onClick={() => void copy("path")}
                className="inline-flex h-7 items-center gap-1.5 border border-neutral-800 bg-neutral-900/60 px-2 text-[10px] text-neutral-400 hover:border-neutral-700 hover:text-neutral-100"
              >
                {copied === "path" ? <Check size={11} /> : <Copy size={11} />}
                Path
              </button>
              {selectedRange && (
                <button
                  type="button"
                  onClick={() => void copy("ref")}
                  title={displayRange(selectedRange)}
                  className="inline-flex h-7 items-center gap-1.5 border border-neutral-800 bg-neutral-900/60 px-2 text-[10px] text-neutral-400 hover:border-neutral-700 hover:text-neutral-100"
                >
                  {copied === "ref" ? <Check size={11} /> : <Copy size={11} />}
                  {selectedRange[0] === selectedRange[1] ? "Line" : "Range"}
                </button>
              )}
              {selectedNode.node_type !== "file" && (
                <button
                  type="button"
                  onClick={() => onOpenMap(selectedNode)}
                  className="inline-flex h-7 items-center gap-1.5 border border-neutral-700 bg-neutral-900 px-2 text-[10px] font-medium text-neutral-300 hover:border-neutral-600 hover:text-white"
                >
                  <Network size={11} /> Map relationships
                </button>
              )}
            </div>
            {editorError && (
              <div className="border-b border-red-900/50 bg-red-950/20 px-4 py-1.5 text-[10px] text-red-300/80">
                {editorError}
              </div>
            )}

            <div
              className="min-h-0 flex-1 overflow-auto"
              aria-label="Source viewer"
            >
              {sourceLoading ? (
                <div className="flex min-h-[520px] items-center justify-center gap-2 text-xs text-neutral-500">
                  <Loader2 size={14} className="animate-spin" /> Reading file…
                </div>
              ) : sourceError ? (
                <div className="m-5 border border-red-900/60 bg-red-950/20 p-4 text-xs text-red-200">
                  <div className="font-medium">
                    Could not open {selectedNode.path}
                  </div>
                  <div className="mt-1 text-red-300/70">{sourceError}</div>
                </div>
              ) : source !== null ? (
                <File
                  file={{ name: selectedNode.path, contents: source }}
                  options={sourceOptions}
                  selectedLines={
                    selectedRange
                      ? { start: selectedRange[0], end: selectedRange[1] }
                      : null
                  }
                  className="min-h-full"
                />
              ) : (
                <div className="flex min-h-[520px] items-center justify-center text-xs text-neutral-600">
                  Source is unavailable.
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="flex min-h-[650px] items-center justify-center p-8">
            <div className="max-w-xl text-center">
              <FileCode2 size={28} className="mx-auto text-cyan-300" />
              <h2 className="mt-4 text-base font-semibold text-neutral-100">
                Open a file or symbol
              </h2>
              <p className="mt-2 text-sm leading-6 text-neutral-400">
                Browse the repository on the left or use the global search above.
                Symbol selections open the full file and highlight the exact range.
              </p>
            </div>
          </div>
        )}
      </main>

      <aside className="border-t border-neutral-800 bg-neutral-950/60 xl:border-l xl:border-t-0">
        {selectedNode ? (
          <>
            <div className="border-b border-neutral-800 p-4">
              <div className="flex items-start gap-2.5">
                <div className="mt-0.5 border border-cyan-500/30 bg-cyan-500/10 p-1.5 text-cyan-200">
                  <CircleDot size={13} />
                </div>
                <div className="min-w-0">
                  <div className="text-[9px] font-semibold uppercase tracking-widest text-neutral-600">
                    {selectedNode.node_type || "symbol"} · {selectedNode.kind}
                  </div>
                  <div className="mt-1 break-words text-xs font-medium text-neutral-200">
                    {selectedNode.qualified_name || selectedNode.label}
                  </div>
                  <div className="mt-1 break-all text-[10px] leading-4 text-neutral-600">
                    {selectedNode.path}
                    {selectedRange
                      ? `:${displayRange(selectedRange)}`
                      : ""}
                  </div>
                </div>
              </div>
            </div>

            {selectedNode.node_type !== "file" && (
              <div className="space-y-4 border-b border-neutral-800 p-3">
                <RelationshipList
                  title="Callers"
                  nodes={callers}
                  direction="incoming"
                  onSelect={onSelect}
                />
                <RelationshipList
                  title="Callees"
                  nodes={callees}
                  direction="outgoing"
                  onSelect={onSelect}
                />
                <ReferenceList
                  payload={references}
                  loading={referencesLoading}
                  error={referencesError}
                  onOpen={onOpenReference}
                />
              </div>
            )}
          </>
        ) : (
          <div className="border-b border-neutral-800 p-4">
            <div className="text-[10px] font-semibold uppercase tracking-widest text-neutral-400">
              Inspector
            </div>
            <p className="mt-2 text-xs leading-5 text-neutral-600">
              Select a file or symbol to inspect its location and relationships.
            </p>
          </div>
        )}

        {selectedNode && (
          <AgentHandoff
            host={agentHost}
            sessionId={agentSessionId}
            supported={agentHandoffSupported}
            contextKey={`${agentSessionId ?? ""}:${selectedNode.id}:${selectedRange?.[0] ?? ""}:${selectedRange?.[1] ?? ""}`}
            onSend={onAskAgent}
          />
        )}

        <div>
          <div className="border-b border-neutral-800 p-3">
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-300">
              <Activity size={12} className="text-emerald-300" /> Agent activity
            </div>
            <p className="mt-1 text-[10px] leading-4 text-neutral-600">
              Click an event to jump to the file or exact indexed symbol.
            </p>
          </div>
          <div className="max-h-[360px] overflow-y-auto p-2">
            {events.length ? (
              <ol className="space-y-1" aria-label="Recent run activity">
                {[...events].reverse().map((event) => {
                  const tone = ACTIVITY_TONE[event.kind];
                  const actionable = !!event.path || !!event.symbol_ids?.length;
                  return (
                    <li key={event.id}>
                      <button
                        type="button"
                        disabled={!actionable}
                        onClick={() => onOpenActivity(event)}
                        className="w-full border border-neutral-800 bg-neutral-900/30 p-2 text-left transition enabled:hover:border-neutral-700 enabled:hover:bg-neutral-900/70 disabled:cursor-default"
                      >
                        <div className="flex items-center gap-2">
                          <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
                          <span
                            className={`text-[9px] font-semibold uppercase tracking-wider ${tone.text}`}
                          >
                            {tone.label}
                          </span>
                          <time
                            className="ml-auto text-[9px] text-neutral-600"
                            dateTime={event.at}
                          >
                            {relativeTime(event.at)}
                          </time>
                        </div>
                        <p className="mt-1.5 break-words text-[11px] leading-4 text-neutral-300">
                          {event.label}
                        </p>
                        {event.path && (
                          <p className="mt-1 truncate text-[9px] text-neutral-600">
                            {event.path}
                            {event.line ? `:${event.line}` : ""}
                          </p>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p className="p-2 text-xs leading-5 text-neutral-600">
                {liveEnabled
                  ? "Waiting for local activity."
                  : "Live follow is paused."}
              </p>
            )}
          </div>
        </div>
      </aside>
    </div>
  );
}
