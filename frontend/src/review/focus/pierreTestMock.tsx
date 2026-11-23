/**
 * jsdom cannot run @pierre/diffs (constructable stylesheets, shadow DOM, Shiki). Tests mock it with this light DOM
 * stand-in: one `[data-line][data-line-type]` row per patch line, a gutter "+" per row, and the annotations drawn
 * under their line, so screens can be tested on the same contract the real renderer keeps.
 *
 *   vi.mock("@pierre/diffs", () => import("./pierreTestMock").then((m) => m.pierreCore));
 *   vi.mock("@pierre/diffs/react", () => import("./pierreTestMock").then((m) => m.pierreReact));
 */
import type { ReactNode } from "react";

import { Button } from "../../components/ui/button";

type MockFile = { name: string; patch: string };
type Row = { line: number; type: "change-addition" | "change-deletion" | "context"; side: "additions" | "deletions"; text: string };

function rowsOf(patch: string): Row[] {
  const rows: Row[] = [];
  let oldLine = 0;
  let newLine = 0;
  for (const raw of patch.split("\n")) {
    const header = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
    if (header) { oldLine = Number(header[1]); newLine = Number(header[2]); continue; }
    if (raw.startsWith("diff ") || raw.startsWith("---") || raw.startsWith("+++") || !raw) continue;
    if (raw.startsWith("+")) rows.push({ line: newLine++, type: "change-addition", side: "additions", text: raw.slice(1) });
    else if (raw.startsWith("-")) rows.push({ line: oldLine++, type: "change-deletion", side: "deletions", text: raw.slice(1) });
    else { rows.push({ line: newLine++, type: "context", side: "additions", text: raw.slice(1) }); oldLine++; }
  }
  return rows;
}

export const pierreCore = {
  parsePatchFiles: (patch: string, id: string) => [{ files: [{ name: id, patch } satisfies MockFile] }],
  registerCustomTheme: () => undefined,
  resolveTheme: async () => ({ name: "mock", type: "light", settings: [] }),
};

function FileDiff(props: {
  fileDiff: MockFile;
  options?: { diffStyle?: string; onGutterUtilityClick?: (range: { start: number; end: number; side: string; endSide: string }) => void };
  lineAnnotations?: { side: string; lineNumber: number; metadata?: unknown }[];
  renderAnnotation?: (annotation: { side: string; lineNumber: number; metadata?: unknown }) => ReactNode;
}) {
  const rows = rowsOf(props.fileDiff.patch);
  return <div data-testid="mock-pierre-diff" data-diff-style={props.options?.diffStyle}>
    {rows.map((row, index) => {
      const annotations = (props.lineAnnotations ?? []).filter((item) => item.side === row.side && item.lineNumber === row.line);
      return <div key={index}>
        <div data-line={row.line} data-line-type={row.type}>
          {props.options?.onGutterUtilityClick && <Button unstyled type="button" aria-label={`Comment on line ${row.line}`} onClick={() => props.options?.onGutterUtilityClick?.({ start: row.line, end: row.line, side: row.side, endSide: row.side })}>+</Button>}
          <span>{row.text}</span>
        </div>
        {annotations.map((annotation, at) => <div key={at}>{props.renderAnnotation?.(annotation)}</div>)}
      </div>;
    })}
  </div>;
}

export const pierreReact = { FileDiff };
