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

function cellText(value: string): { text: string; truncated: boolean } {
  if (value.length <= MAX_CELL_CHARS) return { text: value, truncated: false };
  return { text: `${value.slice(0, MAX_CELL_CHARS)}…`, truncated: true };
}

function TablePane({
  label,
  parsed,
  compareAgainst,
}: {
  label: string;
  parsed: ParsedCsv;
  compareAgainst?: ParsedCsv;
}) {
  const [header = [], ...body] = parsed.rows;
  const comparedRows = compareAgainst?.rows ?? [];
  if (parsed.rows.length === 0) {
    return (
      <section className="min-w-0 overflow-hidden rounded border border-neutral-800 bg-neutral-950">
        <div className="border-b border-neutral-800 px-3 py-2 text-[10px] font-medium text-neutral-400">{label}</div>
        <div className="p-6 text-center text-[11px] text-neutral-600">Empty CSV</div>
      </section>
    );
  }
  return (
    <section className="min-w-0 overflow-hidden rounded border border-neutral-800 bg-neutral-950">
      <div className="flex items-center gap-2 border-b border-neutral-800 px-3 py-2 text-[10px]">
        <span className="font-medium text-neutral-300">{label}</span>
        <span className="ml-auto font-mono text-neutral-600">{parsed.totalRows} row{parsed.totalRows === 1 ? "" : "s"}</span>
      </div>
      <div className="max-h-[620px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 font-mono text-[10px] text-neutral-300">
          <thead className="sticky top-0 z-10 bg-neutral-900">
            <tr>
              <th className="border-b border-r border-neutral-800 px-2 py-1.5 text-right font-normal text-neutral-600">#</th>
              {header.map((value, column) => {
                const cell = cellText(value);
                const changed = compareAgainst ? value !== (comparedRows[0]?.[column] ?? "") : false;
                return (
                  <th
                    key={column}
                    title={cell.truncated ? value : undefined}
                    className={`max-w-72 border-b border-r border-neutral-800 px-2 py-1.5 text-left font-medium ${changed ? "bg-amber-950/20 text-amber-200" : "text-neutral-200"}`}
                  >
                    <span className="block max-w-72 truncate">{cell.text || `Column ${column + 1}`}</span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {body.map((row, rowIndex) => (
              <tr key={rowIndex} className="odd:bg-neutral-950 even:bg-neutral-900/25">
                <td className="border-b border-r border-neutral-900 px-2 py-1.5 text-right text-neutral-700">{rowIndex + 2}</td>
                {header.map((_header, column) => {
                  const value = row[column] ?? "";
                  const cell = cellText(value);
                  const changed = compareAgainst ? value !== (comparedRows[rowIndex + 1]?.[column] ?? "") : false;
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
        <div className="border-t border-neutral-800 px-3 py-2 text-[9px] text-neutral-600">
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
  const deleted = status === "deleted";
  const badge = revisionId ? (
    <div className="border-b border-neutral-800 bg-neutral-950 px-3 py-1.5 font-mono text-[9px] text-violet-300" title={revisionId}>
      Snapshot {revisionId} · rendered CSV
    </div>
  ) : null;

  if (mode === "preview") {
    return (
      <div data-testid="csv-review-preview" className="bg-neutral-950">
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
    <div data-testid="csv-review-preview" className="bg-neutral-950">
      {badge}
      <div className={`grid gap-3 p-3 ${showOld && showNew ? "xl:grid-cols-2" : ""}`}>
        {showOld && <TablePane label="Before" parsed={oldCsv} compareAgainst={showNew ? newCsv : undefined} />}
        {showNew && <TablePane label="After" parsed={newCsv} compareAgainst={showOld ? oldCsv : undefined} />}
      </div>
    </div>
  );
}
