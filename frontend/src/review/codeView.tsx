import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { Button } from "../components/ui/button";
import { getInitialDiffStylePreference, persistDiffStylePreference, type DiffOverflow, type DiffStyle, type DiffStylePreference } from "./diffModel";
import { getInitialReviewCodeTheme, type ReviewCodeTheme } from "./reviewCodeTheme";

/** The Classic Reader's code-view choices (theme, diff style, line overflow), shared by every flow screen. */
export const DIFF_OVERFLOW_KEY = "lemoncrow.review.reader.diffOverflow";

export interface CodeViewState {
  codeTheme: ReviewCodeTheme;
  setCodeTheme(theme: ReviewCodeTheme): void;
  diffStyle: DiffStylePreference;
  setDiffStyle(style: DiffStylePreference): void;
  overflow: DiffOverflow;
  setOverflow(overflow: DiffOverflow): void;
  /** The layout the code is drawn in, as the code view resolved "Auto" for its width; null until one is drawn. */
  shown: DiffStyle | null;
  setShown(style: DiffStyle): void;
}

function initialOverflow(): DiffOverflow {
  try {
    const stored = window.localStorage.getItem(DIFF_OVERFLOW_KEY);
    if (stored === "wrap" || stored === "scroll") return stored;
  } catch { /* storage unavailable */ }
  // The owner asked for wrap by default: no sideways scroll unless the reviewer chooses one line per row.
  return "wrap";
}

function initialStyle(): DiffStylePreference {
  try { return getInitialDiffStylePreference(); } catch { return "auto"; }
}

function useCodeViewState(): CodeViewState {
  const [codeTheme, setCodeTheme] = useState<ReviewCodeTheme>(() => getInitialReviewCodeTheme());
  const [diffStyle, setStyle] = useState<DiffStylePreference>(initialStyle);
  const [overflow, setOverflowState] = useState<DiffOverflow>(initialOverflow);
  const [shown, setShown] = useState<DiffStyle | null>(null);
  const setDiffStyle = useCallback((style: DiffStylePreference) => {
    setStyle(style);
    try { persistDiffStylePreference(style); } catch { /* storage unavailable */ }
  }, []);
  const setOverflow = useCallback((next: DiffOverflow) => {
    setOverflowState(next);
    try { window.localStorage.setItem(DIFF_OVERFLOW_KEY, next); } catch { /* storage unavailable */ }
  }, []);
  return useMemo(() => ({ codeTheme, setCodeTheme, diffStyle, setDiffStyle, overflow, setOverflow, shown, setShown }), [codeTheme, diffStyle, overflow, setDiffStyle, setOverflow, shown]);
}

const CodeViewContext = createContext<CodeViewState | null>(null);

export function CodeViewProvider({ children }: { children: ReactNode }) {
  const state = useCodeViewState();
  return <CodeViewContext.Provider value={state}>{children}</CodeViewContext.Provider>;
}

/** The shared choices; a screen rendered without the Reader (tests) keeps its own. */
export function useCodeView(): CodeViewState {
  const shared = useContext(CodeViewContext);
  const local = useCodeViewState();
  return shared ?? local;
}

const STYLE_LABEL: Record<DiffStylePreference, string> = { auto: "Auto", split: "Split", unified: "Unified" };

/** Unified/Split and Wrap/One line, next to the code they change. The code colours are a Settings › Look choice. */
export function CodeViewControls({ className = "", splitAvailable = true }: { className?: string; splitAvailable?: boolean }) {
  const view = useCodeView();
  return (
    <div className={`flex flex-wrap items-center gap-1 ${className}`} data-testid="code-view-controls">
      <div role="group" aria-label="Diff layout" className="inline-flex rounded-md border border-border p-0.5 max-sm:hidden">
        {(["unified", "split"] as const).map((style) => {
          // On Auto, the layout actually drawn reads as the selected one: the toggle always shows which one you are looking at.
          const autoShown = !splitAvailable ? "unified" : view.shown;
          const active = view.diffStyle === style || (view.diffStyle === "auto" && autoShown === style);
          return <Button key={style} variant="ghost" size="compact" aria-pressed={active} disabled={style === "split" && !splitAvailable} className={`h-7 px-2 text-meta ${active ? "bg-accent text-foreground" : "text-muted-foreground"}`} onClick={() => view.setDiffStyle(view.diffStyle === style ? "auto" : style)}>{STYLE_LABEL[style]}</Button>;
        })}
      </div>
      <Button variant="ghost" size="compact" className="h-8 px-2 text-meta text-muted-foreground" aria-pressed={view.overflow === "wrap"} onClick={() => view.setOverflow(view.overflow === "wrap" ? "scroll" : "wrap")}>{view.overflow === "wrap" ? "Wrap lines" : "One line"}</Button>
    </div>
  );
}
