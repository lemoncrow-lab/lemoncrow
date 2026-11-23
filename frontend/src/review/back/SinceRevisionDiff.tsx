/**
 * The Revision N-1 → Revision N file patch on Welcome back, drawn by the same diff viewer (Pierre) as One at a time and
 * All files, in the reader's code colours (review-21.4). Read-only: no comments, no seen tracking. When the patch cannot be
 * parsed, the raw text is shown rather than nothing.
 */
import { useMemo, type CSSProperties } from "react";
import { parsePatchFiles } from "@pierre/diffs";
import { FileDiff } from "@pierre/diffs/react";

import { useCodeView } from "../codeView";
import { aaPierreTheme } from "../focus/aaCodeTheme";
import { reviewCodeThemeDefinition } from "../reviewCodeTheme";

const SURFACE_STYLE = { width: "100%", "--diffs-font-size": "13px", "--diffs-line-height": "21px" } as CSSProperties;

/** A bare hunk list gets file headers, so the parser reads it as one file instead of an unknown blob. */
function withFileHeader(path: string, patch: string): string {
  return /^(diff --git |--- )/m.test(patch) ? patch : `--- a/${path}\n+++ b/${path}\n${patch}`;
}

export function SinceRevisionDiff({ path, patch, label }: { path: string; patch: string; label: string }) {
  const view = useCodeView();
  const theme = reviewCodeThemeDefinition(view.codeTheme);
  const fileDiff = useMemo(() => {
    try {
      return parsePatchFiles(withFileHeader(path, patch), `lc:back:${path}`, true)[0]?.files[0] ?? null;
    } catch {
      return null;
    }
  }, [path, patch]);
  const options = useMemo(() => ({
    diffStyle: "unified" as const,
    overflow: view.overflow,
    theme: aaPierreTheme(theme.pierreTheme),
    themeType: theme.chromeTheme,
    hunkSeparators: "line-info" as const,
    disableFileHeader: true,
  }), [view.overflow, theme.pierreTheme, theme.chromeTheme]);
  if (!fileDiff) {
    return <pre tabIndex={0} role="region" aria-label={label} className="max-h-72 overflow-auto whitespace-pre-wrap break-words font-mono text-meta">
      {patch.slice(0, 9000)}{patch.length > 9000 ? "\n…diff truncated here" : ""}
    </pre>;
  }
  return <div tabIndex={0} role="region" aria-label={label} className="max-h-96 max-w-full overflow-auto rounded-md border border-border bg-card" data-testid="back-since-diff">
    <FileDiff fileDiff={fileDiff} options={options} style={SURFACE_STYLE} />
  </div>;
}
