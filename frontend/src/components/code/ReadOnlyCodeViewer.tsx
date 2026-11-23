import { useEffect, useRef, useState } from "react";
import type * as MonacoApi from "monaco-editor/editor";

/** Uncommitted line ranges (new side), drawn as a gutter stripe. */
export interface ChangedLines {
  added: readonly (readonly [number, number])[];
  modified: readonly (readonly [number, number])[];
  /** Lines a removal sits before. */
  deleted: readonly number[];
}

interface ReadOnlyCodeViewerProps {
  path: string;
  value: string;
  theme: "light" | "dark";
  selectedRange?: readonly [number, number] | null;
  changedLines?: ChangedLines | null;
  onSelectionChange?: (selection: { start: number; end: number; text: string } | null) => void;
}

type Monaco = typeof MonacoApi;

type MonacoModule = typeof import("./monaco");
let monacoLoad: Promise<MonacoModule> | null = null;

/** Load the core once. Additional languages are only fetched for files that need them. */
function loadMonaco(path: string): Promise<Monaco> {
  monacoLoad ??= import("./monaco").catch((error: unknown) => {
    monacoLoad = null;
    throw error;
  });
  return monacoLoad.then(async (module) => {
    await module.ensureMonacoLanguage(path);
    return module.monaco;
  });
}

let modelSeq = 0;

const FONT_FAMILY = 'ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", monospace';

export default function ReadOnlyCodeViewer({
  path,
  value,
  theme,
  selectedRange = null,
  changedLines = null,
  onSelectionChange,
}: ReadOnlyCodeViewerProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const editorRef = useRef<MonacoApi.editor.IStandaloneCodeEditor | null>(null);
  const decorationsRef = useRef<MonacoApi.editor.IEditorDecorationsCollection | null>(null);
  const valueRef = useRef(value);
  valueRef.current = value;
  const [monaco, setMonaco] = useState<Monaco | null>(null);
  const [readyLanguagePath, setReadyLanguagePath] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const themeName = theme === "dark" ? "lc-dark" : "lc-light";
  // Callers build the range tuple inline; key effects on its numbers, not its identity.
  const rangeStart = selectedRange?.[0] ?? null;
  const rangeEnd = selectedRange?.[1] ?? null;

  useEffect(() => {
    let cancelled = false;
    setReadyLanguagePath(null);
    setLoadFailed(false);
    loadMonaco(path).then(
      (api) => {
        if (!cancelled) {
          setMonaco(api);
          setReadyLanguagePath(path);
        }
      },
      () => {
        if (!cancelled) setLoadFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [path]);

  // Monaco themes are global; set it before the editor is created so it opens in the right one.
  useEffect(() => {
    if (monaco) monaco.editor.setTheme(themeName);
  }, [monaco, themeName]);

  useEffect(() => {
    const host = hostRef.current;
    if (!monaco || !host) return;
    const editor = monaco.editor.create(host, {
      model: null,
      readOnly: true,
      domReadOnly: true,
      automaticLayout: true,
      wordWrap: "on",
      fontFamily: FONT_FAMILY,
      fontSize: 12,
      lineHeight: 19,
      padding: { top: 8, bottom: 18 },
      scrollBeyondLastLine: false,
      renderLineHighlight: "none",
      renderValidationDecorations: "off",
      minimap: { enabled: true, renderCharacters: false },
      stickyScroll: { enabled: true },
      fixedOverflowWidgets: true,
    });
    editorRef.current = editor;
    decorationsRef.current = editor.createDecorationsCollection();
    return () => {
      decorationsRef.current = null;
      editorRef.current = null;
      editor.dispose();
    };
  }, [monaco]);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || !onSelectionChange) return;
    const subscription = editor.onDidChangeCursorSelection(({ selection }) => {
      const model = editor.getModel();
      if (!model || selection.isEmpty()) {
        onSelectionChange(null);
        return;
      }
      const text = model.getValueInRange(selection);
      if (!text.trim()) {
        onSelectionChange(null);
        return;
      }
      const end = selection.endColumn === 1 && selection.endLineNumber > selection.startLineNumber
        ? selection.endLineNumber - 1
        : selection.endLineNumber;
      onSelectionChange({ start: selection.startLineNumber, end, text: text.slice(0, 8000) });
    });
    return () => subscription.dispose();
  }, [monaco, onSelectionChange]);

  // One model per file. The URI carries the path so Monaco picks the language from the
  // extension or a shebang; the sequence keeps URIs unique across remounts.
  useEffect(() => {
    const editor = editorRef.current;
    if (!monaco || !editor || readyLanguagePath !== path) return;
    modelSeq += 1;
    const uri = monaco.Uri.from({ scheme: "inmemory", path: `/${modelSeq}/${path.replace(/^\/+/, "")}` });
    const model = monaco.editor.createModel(valueRef.current, undefined, uri);
    editor.setModel(model);
    editor.updateOptions({ ariaLabel: `${path} (read-only)` });
    return () => model.dispose();
  }, [monaco, path, readyLanguagePath]);

  useEffect(() => {
    const model = editorRef.current?.getModel();
    if (model && model.getValue() !== value) model.setValue(value);
  }, [value]);

  useEffect(() => {
    const model = editorRef.current?.getModel();
    const decorations = decorationsRef.current;
    if (!monaco || !model || !decorations) return;
    const lastLine = model.getLineCount();
    const clamp = (line: number) => Math.max(1, Math.min(line, lastLine));
    const lines = (start: number, end: number) => new monaco.Range(clamp(start), 1, Math.max(clamp(start), clamp(end)), 1);
    const { OverviewRulerLane } = monaco.editor;
    const next: MonacoApi.editor.IModelDeltaDecoration[] = [];
    if (changedLines) {
      for (const [kind, ranges, color] of [
        ["added", changedLines.added, "rgba(52,211,153,.75)"],
        ["modified", changedLines.modified, "rgba(251,191,36,.8)"],
      ] as const) {
        for (const [start, end] of ranges) {
          if (start > lastLine) continue;
          next.push({
            range: lines(start, end),
            options: {
              isWholeLine: true,
              linesDecorationsClassName: `lc-code-${kind}-line`,
              overviewRuler: { color, position: OverviewRulerLane.Left },
            },
          });
        }
      }
      for (const line of changedLines.deleted) {
        if (line < 1) continue;
        next.push({
          range: lines(line, line),
          options: {
            isWholeLine: true,
            className: "lc-code-deleted-line",
            overviewRuler: { color: "rgba(251,113,133,.85)", position: OverviewRulerLane.Left },
          },
        });
      }
    }
    if (rangeStart !== null && rangeEnd !== null) {
      next.push({
        range: lines(rangeStart, rangeEnd),
        options: {
          isWholeLine: true,
          className: "lc-code-selected-line",
          overviewRuler: { color: "rgba(56,189,248,.7)", position: OverviewRulerLane.Center },
        },
      });
    }
    decorations.set(next);
  }, [monaco, path, value, changedLines, rangeStart, rangeEnd]);

  useEffect(() => {
    const editor = editorRef.current;
    const model = editor?.getModel();
    if (!monaco || !editor || !model || rangeStart === null) return;
    const lastLine = model.getLineCount();
    const start = Math.max(1, Math.min(rangeStart, lastLine));
    const end = Math.max(start, Math.min(rangeEnd ?? start, lastLine));
    editor.revealLinesInCenter(start, end, monaco.editor.ScrollType.Immediate);
  }, [monaco, path, rangeStart, rangeEnd]);

  if (loadFailed) {
    return (
      <pre className="absolute inset-0 overflow-auto p-3 font-mono text-caption text-foreground" aria-label="Code viewer">
        {value}
      </pre>
    );
  }
  // Monaco needs a sized box: fill the nearest positioned ancestor rather than rely on
  // a percentage height, which collapses to zero when the parent's height is indefinite.
  return <div ref={hostRef} className="absolute inset-0" aria-label="Code viewer" />;
}
