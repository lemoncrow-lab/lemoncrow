import { useMemo } from "react";

import type { CsvPreview } from "./types";
import type { MarkdownRenderMode } from "./MarkdownReviewPreview";

const MAX_ROWS = 300;
const MAX_COLUMNS = 50;
const MAX_CELL_CHARS = 800;

export interface ParsedCsv {
  rows: string[][];
  totalRows: number;
  truncatedRows: number;
  truncatedColumns: boolean;
}

/** Small RFC-4180-style parser for preview rendering; permissive on malformed input. */
export function parseCsv(text: string): ParsedCsv {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  let totalRows = 0;
  let truncatedColumns = false;

  const pushCell = () => {
    if (row.length < MAX_COLUMNS) row.push(cell);
    else truncatedColumns = true;
    cell = "";
  };
  const pushRow = () => {
    pushCell();
    totalRows += 1;
    if (rows.length < MAX_ROWS) rows.push(row);
    row = [];
  };

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          cell += '"';
          index += 1;
        } else quoted = false;
      } else cell += char;
      continue;
    }
    if (char === '"' && cell.length === 0) {
      quoted = true;
    } else if (char === ",") {
      pushCell();
    } else if (char === "\n") {
      pushRow();
    } else if (char !== "\r") {
      cell += char;
    }
  }
  if (cell.length > 0 || row.length > 0 || text.endsWith(",")) pushRow();

  return {
    rows,
    totalRows,
    truncatedRows: Math.max(0, totalRows - rows.length),
    truncatedColumns,
  };
}

export type CsvRowMark =
  | { kind: "same" }
  | { kind: "changed"; partner: string[] }
  | { kind: "unpaired" }
  /** Counterpart may lie beyond the render limit; do not claim a change. */
  | { kind: "unknown" };

export interface CsvAlignment {
  old: CsvRowMark[];
  new: CsvRowMark[];
}

const rowKey = (row: string[]) => row.map((value) => value.trim()).join("\u0000");

/**
 * Bounded, deterministic row alignment for preview compare. Rows (already capped at MAX_ROWS)
 * are matched by LCS on normalized full-row values; unmatched rows between two anchors are paired
 * in order as "changed" rows, and any leftovers are pure insertions/deletions.
 */
export function alignCsvRows(oldParsed: ParsedCsv, newParsed: ParsedCsv): CsvAlignment {
  const oldBody = oldParsed.rows.slice(1);
  const newBody = newParsed.rows.slice(1);
  const oldKeys = oldBody.map(rowKey);
  const newKeys = newBody.map(rowKey);
  const n = oldKeys.length;
  const m = newKeys.length;
  const width = m + 1;
  const table = new Uint16Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        oldKeys[i] === newKeys[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }

  const oldMarks: CsvRowMark[] = new Array(n);
  const newMarks: CsvRowMark[] = new Array(m);
  const unknownTail = oldParsed.truncatedRows > 0 || newParsed.truncatedRows > 0;

  const flushGap = (oStart: number, oEnd: number, nStart: number, nEnd: number, isTail: boolean) => {
    const paired = Math.min(oEnd - oStart, nEnd - nStart);
    for (let k = 0; k < paired; k += 1) {
      oldMarks[oStart + k] = { kind: "changed", partner: newBody[nStart + k] };
      newMarks[nStart + k] = { kind: "changed", partner: oldBody[oStart + k] };
    }
    const leftover: CsvRowMark = isTail && unknownTail ? { kind: "unknown" } : { kind: "unpaired" };
    for (let i = oStart + paired; i < oEnd; i += 1) oldMarks[i] = leftover;
    for (let j = nStart + paired; j < nEnd; j += 1) newMarks[j] = leftover;
  };

  let i = 0;
  let j = 0;
  let gapO = 0;
  let gapN = 0;
  while (i < n && j < m) {
    if (oldKeys[i] === newKeys[j]) {
      flushGap(gapO, i, gapN, j, false);
      oldMarks[i] = { kind: "same" };
      newMarks[j] = { kind: "same" };
      i += 1;
      j += 1;
      gapO = i;
      gapN = j;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) i += 1;
    else j += 1;
  }
  flushGap(gapO, n, gapN, m, true);
  return { old: oldMarks, new: newMarks };
}

function cellText(value: string): { text: string; truncated: boolean } {
  if (value.length <= MAX_CELL_CHARS) return { text: value, truncated: false };
  return { text: `${value.slice(0, MAX_CELL_CHARS)}…`, truncated: true };
}

function TablePane({
  label,
  parsed,
  compareAgainst,
  marks,
}: {
  label: string;
  parsed: ParsedCsv;
  compareAgainst?: ParsedCsv;
  marks?: CsvRowMark[];
}) {
  const [header = [], ...body] = parsed.rows;
  const comparedRows = compareAgainst?.rows ?? [];
  if (parsed.rows.length === 0) {
    return (
      <section className="min-w-0 overflow-hidden rounded border border-border bg-background">
        <div className="border-b border-border px-3 py-2 text-caption font-medium text-neutral-400">{label}</div>
        <div className="p-6 text-center text-label text-muted-foreground">Empty CSV</div>
      </section>
    );
  }
  return (
    <section className="min-w-0 overflow-hidden rounded border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-caption">
        <span className="font-medium text-neutral-300">{label}</span>
        <span className="ml-auto font-mono text-muted-foreground">{parsed.totalRows} row{parsed.totalRows === 1 ? "" : "s"}</span>
      </div>
      <div className="max-h-[620px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 font-mono text-caption text-neutral-300">
          <thead className="sticky top-0 z-10 bg-muted">
            <tr>
              <th className="border-b border-r border-border px-2 py-1.5 text-right font-normal text-muted-foreground">#</th>
              {header.map((value, column) => {
                const cell = cellText(value);
                const changed = compareAgainst ? value !== (comparedRows[0]?.[column] ?? "") : false;
                return (
                  <th
                    key={column}
                    title={cell.truncated ? value : undefined}
                    className={`max-w-72 border-b border-r border-border px-2 py-1.5 text-left font-medium ${changed ? "bg-amber-950/20 text-amber-200" : "text-neutral-200"}`}
                  >
                    <span className="block max-w-72 truncate">{cell.text || `Column ${column + 1}`}</span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {body.map((row, rowIndex) => (
              <tr key={rowIndex} className="odd:bg-background even:bg-muted/25">
                <td className="border-b border-r border-neutral-900 px-2 py-1.5 text-right text-muted-foreground">{rowIndex + 2}</td>
                {header.map((_header, column) => {
                  const value = row[column] ?? "";
                  const cell = cellText(value);
                  const mark = marks?.[rowIndex];
                  const changed =
                    mark?.kind === "unpaired" ? true : mark?.kind === "changed" ? value !== (mark.partner[column] ?? "") : false;
                  return (
                    <td
                      key={column}
                      title={cell.truncated ? value : undefined}
                      className={`max-w-72 border-b border-r border-neutral-900 px-2 py-1.5 align-top ${changed ? "bg-amber-950/15 text-amber-100" : ""}`}
                    >
                      <span className="block max-w-72 whitespace-pre-wrap break-words">{cell.text}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(parsed.truncatedRows > 0 || parsed.truncatedColumns) && (
        <div className="border-t border-border px-3 py-2 text-micro text-muted-foreground">
          {parsed.truncatedRows > 0 ? `${parsed.truncatedRows} more row(s) not rendered` : ""}
          {parsed.truncatedRows > 0 && parsed.truncatedColumns ? " · " : ""}
          {parsed.truncatedColumns ? `only the first ${MAX_COLUMNS} columns are rendered` : ""}
        </div>
      )}
    </section>
  );
}

export default function CsvReviewPreview({
  preview,
  revisionId = "",
  status = "modified",
  mode = "preview",
}: {
  preview: CsvPreview;
  revisionId?: string;
  status?: string;
  mode?: MarkdownRenderMode;
}) {
  const oldCsv = useMemo(() => parseCsv(preview.old_content), [preview.old_content]);
  const newCsv = useMemo(() => parseCsv(preview.new_content), [preview.new_content]);
  const alignment = useMemo(() => alignCsvRows(oldCsv, newCsv), [oldCsv, newCsv]);
  const deleted = status === "deleted";
  const badge = revisionId ? (
    <div className="border-b border-border bg-background px-3 py-1.5 font-mono text-micro text-violet-300" title={revisionId}>
      Snapshot {revisionId} · rendered CSV
    </div>
  ) : null;

  if (mode === "preview") {
    return (
      <div data-testid="csv-review-preview" className="bg-background">
        {badge}
        <div className="p-3">
          <TablePane label={deleted ? "Before" : "After"} parsed={deleted ? oldCsv : newCsv} />
        </div>
      </div>
    );
  }

  const showOld = status !== "added";
  const showNew = status !== "deleted";
  return (
    <div data-testid="csv-review-preview" className="bg-background">
      {badge}
      <div className={`grid gap-3 p-3 ${showOld && showNew ? "xl:grid-cols-2" : ""}`}>
        {showOld && <TablePane label="Before" parsed={oldCsv} compareAgainst={showNew ? newCsv : undefined} marks={showNew ? alignment.old : undefined} />}
        {showNew && <TablePane label="After" parsed={newCsv} compareAgainst={showOld ? oldCsv : undefined} marks={showOld ? alignment.new : undefined} />}
      </div>
    </div>
  );
}
