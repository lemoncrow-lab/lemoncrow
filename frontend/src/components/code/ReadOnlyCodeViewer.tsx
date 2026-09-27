import { useEffect, useRef } from "react";
import { basicSetup } from "codemirror";
import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { EditorState, StateEffect } from "@codemirror/state";
import { Decoration, EditorView } from "@codemirror/view";

interface ReadOnlyCodeViewerProps {
  path: string;
  value: string;
  theme: "light" | "dark";
  selectedRange?: readonly [number, number] | null;
}

export default function ReadOnlyCodeViewer({
  path,
  value,
  theme,
  selectedRange = null,
}: ReadOnlyCodeViewerProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const selectedDecorations = (() => {
      if (!selectedRange) return Decoration.none;
      const doc = EditorState.create({ doc: value }).doc;
      const start = Math.max(1, Math.min(selectedRange[0], doc.lines));
      const end = Math.max(start, Math.min(selectedRange[1], doc.lines));
      const marks = [];
      for (let lineNumber = start; lineNumber <= end; lineNumber += 1) {
        marks.push(
          Decoration.line({
            attributes: { class: "lc-code-selected-line" },
          }).range(doc.line(lineNumber).from),
        );
      }
      return Decoration.set(marks, true);
    })();

    const chromeTheme = EditorView.theme(
      {
        "&": {
          height: "100%",
          backgroundColor: "transparent",
          color: theme === "dark" ? "#e5e5e5" : "#262626",
          fontSize: "12px",
        },
        ".cm-scroller": {
          overflow: "auto",
          fontFamily:
            'ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", monospace',
          lineHeight: "1.55",
        },
        ".cm-content": {
          padding: "8px 0 18px",
          caretColor: "transparent",
        },
        ".cm-line": {
          padding: "0 12px 0 6px",
        },
        ".cm-gutters": {
          borderRight:
            theme === "dark"
              ? "1px solid rgba(64,64,64,.72)"
              : "1px solid rgba(212,212,212,.9)",
          backgroundColor: "transparent",
          color: theme === "dark" ? "#737373" : "#a3a3a3",
        },
        ".cm-activeLine, .cm-activeLineGutter": {
          backgroundColor: "transparent",
        },
        ".lc-code-selected-line": {
          backgroundColor:
            theme === "dark"
              ? "rgba(14,165,233,.09)"
              : "rgba(14,165,233,.08)",
          boxShadow: "inset 2px 0 0 rgba(56,189,248,.7)",
        },
        ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
          backgroundColor:
            theme === "dark"
              ? "rgba(82,82,82,.65)"
              : "rgba(212,212,212,.85)",
        },
        "&.cm-focused": {
          outline: "none",
        },
      },
      { dark: theme === "dark" },
    );

    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.lineWrapping,
          chromeTheme,
          EditorView.decorations.of(selectedDecorations),
        ],
      }),
    });

    if (selectedRange) {
      const lineNumber = Math.max(
        1,
        Math.min(selectedRange[0], view.state.doc.lines),
      );
      window.requestAnimationFrame(() => {
        view.dispatch({
          effects: EditorView.scrollIntoView(
            view.state.doc.line(lineNumber).from,
            { y: "center" },
          ),
        });
      });
    }

    const language = LanguageDescription.matchFilename(languages, path);
    if (language) {
      void language
        .load()
        .then((support) => {
          if (!view.dom.isConnected) return;
          view.dispatch({
            effects: StateEffect.appendConfig.of(support),
          });
        })
        .catch(() => undefined);
    }

    return () => view.destroy();
  }, [path, selectedRange, theme, value]);

  return <div ref={hostRef} className="h-full min-h-0" aria-label="Code viewer" />;
}
