import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUpRight, Check, Copy, Eye, FileCode2, Loader2, Sparkles } from "lucide-react";

import FileTypeIcon from "../components/FileTypeIcon";
import FileContentViewer, { fileNeedsText, fileRenderKind } from "../components/code/FileContentViewer";
import type { ChangedLines } from "../components/code/ReadOnlyCodeViewer";
import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import {
  api,
  type CodeChangedFile,
  type CodeMapActivityEvent,
  type CodeMapEditorCapability,
  type CodeMapEditorOpenResponse,
  type CodeMapNode,
} from "../api";

export function displayRange(range: readonly [number, number]): string {
  return range[0] === range[1] ? `L${range[0]}` : `L${range[0]}–L${range[1]}`;
}

function copyRange(path: string, range: readonly [number, number]): string {
  return range[0] === range[1] ? `${path}:L${range[0]}` : `${path}:L${range[0]}-L${range[1]}`;
}

const CHANGE_WORD: Record<CodeChangedFile["status"], string> = { M: "Modified", A: "Added", D: "Deleted", R: "Renamed" };

export interface CodeSourceViewProps {
  projectRoot: string;
  selectedNode: CodeMapNode | null;
  locationRange: readonly [number, number] | null;
  events: readonly CodeMapActivityEvent[];
  change: CodeChangedFile | undefined;
  editorCapability: CodeMapEditorCapability | null;
  onOpenLine: (line: number) => void;
  onOpenEditor: () => Promise<CodeMapEditorOpenResponse>;
  /** Absent where Code Ask is not served. */
  onAskSelection?: (range: readonly [number, number], text: string) => void;
}

export default function CodeSourceView({
  projectRoot,
  selectedNode,
  locationRange,
  events,
  change,
  editorCapability,
  onOpenLine,
  onOpenEditor,
  onAskSelection,
}: CodeSourceViewProps) {
  const [source, setSource] = useState<string | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [viewerMode, setViewerMode] = useState<"source" | "preview">("source");
  const [copied, setCopied] = useState<"path" | "ref" | null>(null);
  const [lineInput, setLineInput] = useState("");
  const [textSelection, setTextSelection] = useState<{ start: number; end: number; text: string } | null>(null);
  const [editorOpening, setEditorOpening] = useState(false);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [changedLines, setChangedLines] = useState<ChangedLines | null>(null);
  const sourceCache = useRef<Map<string, { editId: string; content: string }>>(new Map());
  const [chromeTheme, setChromeTheme] = useState<"light" | "dark">(() =>
    typeof document !== "undefined" && document.documentElement.classList.contains("dark") ? "dark" : "light",
  );

  const selectedPath = selectedNode?.path ?? null;
  const renderKind = selectedPath ? fileRenderKind(selectedPath) : "code";
  const sourceUrl = selectedPath ? api.codeMapFileContentUrl(selectedPath, projectRoot) : "";
  const selectedEditId = useMemo(
    () => (selectedPath ? [...events].reverse().find((event) => event.kind === "edit" && event.path === selectedPath)?.id ?? "" : ""),
    [events, selectedPath],
  );

  useEffect(() => {
    if (!selectedPath) return;
    setViewerMode(fileRenderKind(selectedPath) === "code" ? "source" : "preview");
  }, [selectedPath]);

  useEffect(() => {
    if (typeof document === "undefined" || typeof MutationObserver === "undefined") return;
    const root = document.documentElement;
    const syncTheme = () => setChromeTheme(root.classList.contains("dark") ? "dark" : "light");
    syncTheme();
    const observer = new MutationObserver(syncTheme);
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!selectedPath || !projectRoot || !fileNeedsText(renderKind, viewerMode)) {
      setSource(null);
      setSourceError(null);
      setSourceLoading(false);
      return;
    }
    const cacheKey = `${projectRoot}:${selectedPath}`;
    const cached = sourceCache.current.get(cacheKey);
    if (cached?.editId === selectedEditId) {
      setSource(cached.content);
      setSourceError(null);
      setSourceLoading(false);
      return;
    }
    const controller = new AbortController();
    setSource(null);
    setSourceLoading(true);
    setSourceError(null);
    void api
      .codeMapFileContent(selectedPath, projectRoot, controller.signal)
      .then((content) => {
        const cache = sourceCache.current;
        cache.set(cacheKey, { editId: selectedEditId, content });
        if (cache.size > 32) {
          const oldest = cache.keys().next().value;
          if (oldest) cache.delete(oldest);
        }
        setSource(content);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setSource(null);
        setSourceError(caught instanceof Error ? caught.message : "Could not read this file.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setSourceLoading(false);
      });
    return () => controller.abort();
  }, [projectRoot, renderKind, selectedEditId, selectedPath, viewerMode]);

  // The change gutter follows the file's uncommitted state; it refreshes when
  // the file's diffstat or the latest agent edit to it changes.
  const changeKey = change ? `${change.status}:${change.additions}:${change.deletions}:${selectedEditId}` : "";
  useEffect(() => {
    if (!selectedPath || !projectRoot || !changeKey || change?.status === "D") {
      setChangedLines(null);
      return;
    }
    const controller = new AbortController();
    void api
      .codeMapFileChanges(selectedPath, projectRoot, controller.signal)
      .then((payload) => setChangedLines({ added: payload.added, modified: payload.modified, deleted: payload.deleted }))
      .catch(() => {
        if (!controller.signal.aborted) setChangedLines(null);
      });
    return () => controller.abort();
  }, [change?.status, changeKey, projectRoot, selectedPath]);

  const selectedRange =
    locationRange ?? (selectedNode && selectedNode.node_type !== "file" ? ([selectedNode.line, selectedNode.end_line] as const) : null);

  useEffect(() => {
    setLineInput(locationRange ? String(locationRange[0]) : "");
    setEditorError(null);
    setTextSelection(null);
  }, [locationRange, selectedPath]);

  const copy = async (kind: "path" | "ref") => {
    if (!selectedNode) return;
    const text = kind === "path" ? selectedNode.path : selectedRange ? copyRange(selectedNode.path, selectedRange) : selectedNode.path;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(kind);
      window.setTimeout(() => setCopied(null), 1200);
    } catch {
      setCopied(null);
    }
  };

  if (!selectedNode) {
    return (
      <main className="flex min-h-[560px] min-w-0 items-center justify-center bg-surface-code p-4 xl:min-h-0">
        <div className="max-w-xl text-center">
          <FileCode2 size={28} className="mx-auto text-cyan-300" />
          <h2 className="mt-4 text-base font-semibold text-foreground">Open a file or symbol</h2>
          <p className="mt-2 text-sm leading-6 text-neutral-400">
            Browse the repository on the left or use the search above. Symbol selections open the full file and highlight the exact range.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="flex min-h-[560px] min-w-0 flex-col bg-surface-code xl:min-h-0">
      <div className="flex h-9 shrink-0 items-center gap-1.5 overflow-x-auto whitespace-nowrap border-b border-border bg-background/75 px-3">
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <FileTypeIcon path={selectedNode.path} size={13} />
          <div className="flex min-w-0 items-center gap-1 text-caption text-muted-foreground">
            {selectedNode.path.split("/").map((part, index, parts) => (
              <span key={`${part}-${index}`} className="flex min-w-0 items-center gap-1">
                <span className={index === parts.length - 1 ? "truncate text-neutral-300" : "truncate"}>{part}</span>
                {index < parts.length - 1 && <span>/</span>}
              </span>
            ))}
          </div>
          <UiButton
            type="button"
            onClick={() => void copy("path")}
            size="icon"
            variant="ghost"
            className="h-6 w-6 shrink-0"
            aria-label={copied === "path" ? "Path copied" : "Copy file path"}
            tooltip={copied === "path" ? "Copied" : "Copy file path"}
          >
            {copied === "path" ? <Check size={12} /> : <Copy size={12} />}
          </UiButton>
          {selectedNode.node_type !== "file" && (
            <>
              <span className="shrink-0 text-muted-foreground">·</span>
              <span className="max-w-64 truncate text-caption font-medium text-neutral-300">{selectedNode.qualified_name || selectedNode.label}</span>
            </>
          )}
          {change && (
            <span
              className="shrink-0 rounded-md border border-amber-500/30 px-1.5 text-micro font-medium text-amber-200"
              title="Uncommitted in this worktree; the gutter marks changed lines."
            >
              {CHANGE_WORD[change.status]} · +{change.additions} −{change.deletions}
            </span>
          )}
        </div>
        {renderKind !== "code" && (
          <div className="flex h-7 shrink-0 overflow-hidden rounded-md border border-border/80 bg-background">
            <UiButton
              unstyled
              type="button"
              onClick={() => setViewerMode("preview")}
              aria-pressed={viewerMode === "preview"}
              className={`inline-flex h-full items-center gap-1 px-2 text-caption ${viewerMode === "preview" ? "bg-accent text-foreground" : "text-muted-foreground hover:text-neutral-200"}`}
            >
              <Eye size={10} /> Preview
            </UiButton>
            <UiButton
              unstyled
              type="button"
              onClick={() => setViewerMode("source")}
              aria-pressed={viewerMode === "source"}
              className={`inline-flex h-full items-center gap-1 border-l border-border px-2 text-caption ${viewerMode === "source" ? "bg-accent text-foreground" : "text-muted-foreground hover:text-neutral-200"}`}
            >
              <FileCode2 size={10} /> Source
            </UiButton>
          </div>
        )}
        <form
          className="flex h-7 shrink-0 items-stretch overflow-hidden rounded-md border border-border/80 bg-background"
          onSubmit={(event) => {
            event.preventDefault();
            const line = Number(lineInput);
            if (Number.isInteger(line) && line > 0) onOpenLine(line);
          }}
        >
          <UiInput
            unstyled
            type="number"
            min={1}
            step={1}
            value={lineInput}
            onChange={(event) => setLineInput(event.target.value)}
            placeholder="Line"
            aria-label="Go to line"
            className="w-14 bg-transparent px-2 text-caption tabular-nums text-neutral-300 outline-none placeholder:text-muted-foreground"
          />
          <UiButton unstyled type="submit" aria-label="Open source line" className="border-l border-border px-2 text-caption text-muted-foreground hover:text-foreground">
            Go
          </UiButton>
        </form>
        {editorCapability?.available && editorCapability.preferred && (
          <UiButton
            type="button"
            onClick={() => {
              if (editorOpening) return;
              setEditorOpening(true);
              setEditorError(null);
              void onOpenEditor()
                .catch((caught: unknown) => setEditorError(caught instanceof Error ? caught.message : "Could not open the local editor."))
                .finally(() => setEditorOpening(false));
            }}
            disabled={editorOpening}
            aria-label="Open in editor"
            size="xs"
            variant="outline"
            className="shrink-0"
          >
            {editorOpening ? <Loader2 size={10} className="animate-spin" /> : <ArrowUpRight size={10} />}
            {editorOpening ? "Opening…" : "Open in editor"}
          </UiButton>
        )}
        {textSelection && onAskSelection && (
          <UiButton type="button" onClick={() => onAskSelection([textSelection.start, textSelection.end], textSelection.text)} size="xs" variant="outline" className="shrink-0">
            <Sparkles size={10} /> Ask selection
          </UiButton>
        )}
        {selectedRange && (
          <UiButton type="button" onClick={() => void copy("ref")} title={displayRange(selectedRange)} size="xs" variant="ghost" className="shrink-0">
            {copied === "ref" ? <Check size={10} /> : <Copy size={10} />}
            {selectedRange[0] === selectedRange[1] ? "Line" : "Range"}
          </UiButton>
        )}
      </div>
      {editorError && <div className="border-b border-red-900/50 bg-red-950/20 px-4 py-1.5 text-caption text-red-300/80">{editorError}</div>}
      <div className="relative min-h-0 flex-1 overflow-hidden" aria-label="Source viewer">
        {sourceLoading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center gap-2 bg-background/70 text-xs text-muted-foreground backdrop-blur-[1px]">
            <Loader2 size={14} className="animate-spin" />
            Reading {selectedNode.path.split("/").pop()}…
          </div>
        )}
        {sourceError ? (
          <div className="m-4 rounded-lg border border-red-900/60 bg-red-950/20 p-4 text-xs text-red-200">
            <div className="font-medium">Could not open {selectedNode.path}</div>
            <div className="mt-1 text-red-300/70">{sourceError}</div>
          </div>
        ) : (
          <FileContentViewer
            path={selectedNode.path}
            source={source}
            sourceUrl={sourceUrl}
            kind={renderKind}
            mode={viewerMode}
            theme={chromeTheme}
            selectedRange={selectedRange}
            changedLines={changedLines}
            onSelectionChange={setTextSelection}
          />
        )}
        {!sourceLoading && !sourceError && source === null && fileNeedsText(renderKind, viewerMode) && (
          <div className="flex min-h-[520px] items-center justify-center text-xs text-muted-foreground">Source is unavailable.</div>
        )}
      </div>
    </main>
  );
}
