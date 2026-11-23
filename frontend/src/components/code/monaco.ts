// Monaco for the /code source viewer only, loaded on demand by ReadOnlyCodeViewer.
// Every other editor surface (Review proposals etc.) stays on CodeMirror 6.
import * as monaco from "monaco-editor/editor";
// The /code viewer is read-only: keep required navigation features and load
// each grammar on first use. Do not eagerly register all Monaco languages.
import "monaco-editor/features/codeEditor/register";
import "monaco-editor/features/find/register";
import "monaco-editor/features/folding/register";
import "monaco-editor/features/stickyScroll/register";
import "monaco-editor/features/hover/register";
import "monaco-editor/features/links/register";
import "monaco-editor/features/clipboard/register";
import "monaco-editor/features/contextmenu/register";
import "monaco-editor/features/bracketMatching/register";
import "monaco-editor/features/wordHighlighter/register";
import "monaco-editor/features/readOnlyMessage/register";
import { grammarForPath, type Grammar } from "./monacoLanguageRouter";
import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";
import JsonWorker from "monaco-editor/language/json/json.worker.js?worker";

import "./monacoViewer.css";

globalThis.MonacoEnvironment = {
  getWorker: (_workerId: string, label: string) => (label === "json" ? new JsonWorker() : new EditorWorker()),
};

// Monaco ships no TOML grammar; INI's `[section]` / `key = value` is close enough to read.
monaco.languages.register({ id: "ini", extensions: [".toml"] });

// Backgrounds match --surface-code in index.css so sticky scroll and the minimap blend in.
monaco.editor.defineTheme("lc-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#090909",
    "editorGutter.background": "#090909",
    "editorStickyScroll.background": "#090909",
    "editorLineNumber.foreground": "#737373",
    "editorLineNumber.activeForeground": "#a3a3a3",
    "editor.selectionBackground": "#525252a6",
    "editor.inactiveSelectionBackground": "#52525266",
  },
});
monaco.editor.defineTheme("lc-light", {
  base: "vs",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#ffffff",
    "editorGutter.background": "#ffffff",
    "editorStickyScroll.background": "#ffffff",
    "editorLineNumber.foreground": "#a3a3a3",
    "editorLineNumber.activeForeground": "#737373",
    "editor.selectionBackground": "#d4d4d4d9",
    "editor.inactiveSelectionBackground": "#d4d4d480",
  },
});

export { monaco };

// Each grammar is a separately cacheable chunk; uncommon languages preserve
// Monaco's complete syntax catalog on demand, not on the normal source path.
const grammarImports: Record<Grammar, () => Promise<unknown>> = {
  javascript: () => import("monaco-editor/languages/definitions/javascript/register"),
  typescript: () => import("monaco-editor/languages/definitions/typescript/register"),
  python: () => import("monaco-editor/languages/definitions/python/register"),
  go: () => import("monaco-editor/languages/definitions/go/register"),
  json: () => import("monaco-editor/languages/features/json/register.js"),
  yaml: () => import("monaco-editor/languages/definitions/yaml/register"),
  shell: () => import("monaco-editor/languages/definitions/shell/register"),
  css: () => import("monaco-editor/languages/definitions/css/register"),
  html: () => import("monaco-editor/languages/definitions/html/register"),
  markdown: () => import("monaco-editor/languages/definitions/markdown/register"),
  sql: () => import("monaco-editor/languages/definitions/sql/register"),
  cpp: () => import("monaco-editor/languages/definitions/cpp/register"),
  rust: () => import("monaco-editor/languages/definitions/rust/register"),
  ini: () => import("monaco-editor/languages/definitions/ini/register"),
  xml: () => import("monaco-editor/languages/definitions/xml/register"),
  dockerfile: () => import("monaco-editor/languages/definitions/dockerfile/register"),
  extended: () => import("monaco-editor/languages/definitions/register.all"),
};
const loadedGrammars = new Map<Grammar, Promise<unknown>>();
export async function ensureMonacoLanguage(path: string): Promise<void> {
  const language = grammarForPath(path);
  if (!language) return; // Monaco can still display unknown formats as plain text.
  let loading = loadedGrammars.get(language);
  if (!loading) {
    loading = grammarImports[language]().catch((error: unknown) => {
      loadedGrammars.delete(language);
      throw error;
    });
    loadedGrammars.set(language, loading);
  }
  await loading;
}
