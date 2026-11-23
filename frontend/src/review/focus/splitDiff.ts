/** Two truthful columns over the same revision-pinned rows used by FocusCode. */
import type { PartItem, PartRow } from "./partDiff";

export type SplitSide = { row: PartRow; index: number };
export type SplitPair =
  | { kind: "gap"; lines: number }
  | { kind: "pair"; before: SplitSide | null; after: SplitSide | null };

/** Align contiguous deleted/added blocks. Context is shown on both sides. */
export function alignSplitRows(items: readonly PartItem[]): SplitPair[] {
  const result: SplitPair[] = [];
  for (let i = 0; i < items.length;) {
    const item = items[i];
    if (item.type === "gap") { result.push({ kind: "gap", lines: item.lines }); i++; continue; }
    if (item.row.kind === "ctx") {
      const side = { row: item.row, index: i };
      result.push({ kind: "pair", before: side, after: side });
      i++; continue;
    }
    const deletions: SplitSide[] = [];
    const additions: SplitSide[] = [];
    while (i < items.length) {
      const next = items[i];
      if (next.type !== "row" || next.row.kind === "ctx") break;
      (next.row.kind === "del" ? deletions : additions).push({ row: next.row, index: i });
      i++;
    }
    for (let n = 0; n < Math.max(deletions.length, additions.length); n++) {
      result.push({ kind: "pair", before: deletions[n] ?? null, after: additions[n] ?? null });
    }
  }
  return result;
}

/**
 * Parsed patch context has distinct old/new line numbers. Synthetic lines
 * reconstructed from the frozen new source do not carry old-side coordinates:
 * return undefined rather than inventing a number after a deletion or insertion.
 */
export function patchOldLineNumbers(patch: string): ReadonlyMap<number, number> {
  const result = new Map<number, number>();
  let oldLine = 0, newLine = 0, inHunk = false;
  for (const raw of patch.split("\n")) {
    if (raw.startsWith("diff --git")) { inHunk = false; continue; }
    if (raw.startsWith("@@")) {
      const parsed = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      if (parsed) { oldLine = Number(parsed[1]); newLine = Number(parsed[2]); inHunk = true; }
      continue;
    }
    if (!inHunk || raw.startsWith("\\")) continue;
    if (raw.startsWith("+")) newLine++;
    else if (raw.startsWith("-")) oldLine++;
    else if (raw.startsWith(" ") || raw === "") { result.set(newLine, oldLine); oldLine++; newLine++; }
  }
  return result;
}
