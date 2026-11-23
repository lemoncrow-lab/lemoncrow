/**
 * One part's lines out of its file's unified patch (SPEC 4.3 Code): the whole function, one line-number column
 * (new numbers; old numbers on removed lines), a sign column, and unchanged runs collapsed to "N unchanged lines".
 * Pure. The patch is the server's (`GET /files/<path>/patch`); nothing here guesses a line it was not sent.
 */

export type PartRowKind = "add" | "del" | "ctx";

export interface PartRow {
  kind: PartRowKind;
  /** New-side number for added and unchanged lines, old-side number for removed ones. */
  num: number;
  text: string;
  /** Where on the new side a removed line sat: the number the next kept line has. 0 for add and ctx rows. */
  at: number;
}

export type PartItem = { type: "row"; row: PartRow } | { type: "gap"; lines: number };

/** The rows of a unified patch, hunk by hunk. File headers and `\ No newline` markers are skipped. */
export function parsePatchRows(patch: string): PartRow[] {
  const rows: PartRow[] = [];
  let oldNum = 0;
  let newNum = 0;
  let inHunk = false;
  const lines = patch.split("\n");
  // The final newline ends the last line; it does not start another.
  if (lines[lines.length - 1] === "") lines.pop();
  for (const line of lines) {
    if (line.startsWith("diff --git")) {
      inHunk = false;
      continue;
    }
    if (line.startsWith("@@")) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (match) {
        oldNum = Number(match[1]);
        newNum = Number(match[2]);
        inHunk = true;
      }
      continue;
    }
    if (!inHunk || line.startsWith("\\")) continue;
    if (line.startsWith("+")) {
      rows.push({ kind: "add", num: newNum, text: line.slice(1), at: 0 });
      newNum += 1;
    } else if (line.startsWith("-")) {
      rows.push({ kind: "del", num: oldNum, text: line.slice(1), at: newNum });
      oldNum += 1;
    } else if (line.startsWith(" ") || line === "") {
      // A blank line inside a hunk is an unchanged empty line whose leading space was trimmed.
      rows.push({ kind: "ctx", num: newNum, text: line.slice(1), at: 0 });
      newNum += 1;
      oldNum += 1;
    }
  }
  return rows;
}

/**
 * The part's lines. `start`/`end` are the part's new-side range; a part with none (a removed definition, a file-level
 * part) shows every row of the patch.
 */
export function partItems(rows: readonly PartRow[], start: number, end: number): PartItem[] {
  const ranged = start > 0 && end >= start;
  const kept = ranged
    ? rows.filter((row) => (row.kind === "del" ? row.at >= start && row.at <= end : row.num >= start && row.num <= end))
    : [...rows];
  const items: PartItem[] = [];
  let previousNew = ranged ? start - 1 : 0;
  kept.forEach((row, index) => {
    if (row.kind !== "del") {
      // Lines between two kept rows that the patch did not carry are unchanged: collapse them.
      const gap = row.num - previousNew - 1;
      if (gap > 0 && (ranged || index > 0)) items.push({ type: "gap", lines: gap });
      previousNew = row.num;
    }
    items.push({ type: "row", row });
  });
  if (ranged && previousNew < end) items.push({ type: "gap", lines: end - previousNew });
  return items;
}

/**
 * Fill unchanged lines only from the immutable source Snapshot, never from the current checkout.
 * Refuse an incomplete or inconsistent file: the caller then keeps the truthful patch-only view.
 * The cap bounds an exceptionally large function without silently truncating source evidence.
 */
export function fullFunctionItems(
  rows: readonly PartRow[], start: number, end: number, newContent: string | null,
  maxLines = 1200,
): PartItem[] | null {
  if (newContent === null || start <= 0 || end < start || end - start + 1 > maxLines) return null;
  const lines = newContent.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  if (end > lines.length) return null;
  const patched = new Map<number, PartRow>();
  const removed = new Map<number, PartRow[]>();
  for (const row of rows) {
    if (row.kind === "del") {
      if (row.at >= start && row.at <= end + 1) {
        const list = removed.get(row.at) ?? [];
        list.push(row);
        removed.set(row.at, list);
      }
      continue;
    }
    if (row.num < start || row.num > end) continue;
    // A supposedly frozen source disagreeing with its patch must never be drawn as a combined truth.
    if (row.text !== lines[row.num - 1] || patched.has(row.num)) return null;
    patched.set(row.num, row);
  }
  const output: PartItem[] = [];
  for (let line = start; line <= end + 1; line++) {
    for (const old of removed.get(line) ?? []) output.push({ type: "row", row: old });
    if (line > end) break;
    output.push({ type: "row", row: patched.get(line) ?? { kind: "ctx", num: line, text: lines[line - 1], at: 0 } });
  }
  return output;
}

/**
 * The part's rows as a unified patch for the Classic Reader's renderer (@pierre/diffs): one hunk per run between
 * gaps, so the diff shows exactly the part (and its whole function) and never the rest of the file. Old-side
 * numbers come from the patch (`oldNumbers`: new to old for patch context) or a removed row; a run with neither
 * starts at its new number, which only labels the hunk header.
 */
export function itemsToPatch(path: string, items: readonly PartItem[], oldNumbers: ReadonlyMap<number, number>): string {
  const runs: PartRow[][] = [[]];
  for (const item of items) {
    if (item.type === "gap") { if (runs[runs.length - 1].length) runs.push([]); continue; }
    runs[runs.length - 1].push(item.row);
  }
  let out = `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n`;
  for (const run of runs) {
    if (!run.length) continue;
    let oldBefore = 0;
    let oldStart = 0;
    for (const row of run) {
      const known = row.kind === "del" ? row.num : row.kind === "ctx" ? oldNumbers.get(row.num) : undefined;
      if (known !== undefined) { oldStart = known - oldBefore; break; }
      if (row.kind !== "add") oldBefore += 1;
    }
    const firstNew = run.find((row) => row.kind !== "del");
    const newStart = firstNew ? firstNew.num : Math.max(0, run[0].at - 1);
    if (oldStart <= 0) oldStart = Math.max(1, newStart);
    const oldCount = run.filter((row) => row.kind !== "add").length;
    const newCount = run.filter((row) => row.kind !== "del").length;
    out += `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@\n`;
    for (const row of run) out += `${row.kind === "add" ? "+" : row.kind === "del" ? "-" : " "}${row.text}\n`;
  }
  return out;
}

export interface WordChange {
  before: string;
  changed: string;
  after: string;
}

/** A lexical highlight only. It never claims semantic equivalence or test coverage. */
export function changedWordPieces(oldText: string, newText: string): { old: WordChange; new: WordChange } | null {
  if (oldText === newText) return null;
  let prefix = 0;
  while (prefix < oldText.length && prefix < newText.length && oldText[prefix] === newText[prefix]) prefix++;
  let suffix = 0;
  while (suffix < oldText.length - prefix && suffix < newText.length - prefix
    && oldText[oldText.length - suffix - 1] === newText[newText.length - suffix - 1]) suffix++;
  // Expand character-level differences to full word boundaries when both sides share a word prefix/suffix.
  const word = (value: string | undefined) => Boolean(value && /[\p{L}\p{N}_$]/u.test(value));
  while (prefix > 0 && word(oldText[prefix - 1]) && word(newText[prefix - 1])) prefix--;
  while (suffix > 0 && word(oldText[oldText.length - suffix]) && word(newText[newText.length - suffix])) suffix--;
  const split = (value: string): WordChange => ({
    before: value.slice(0, prefix),
    changed: value.slice(prefix, value.length - suffix),
    after: suffix ? value.slice(-suffix) : "",
  });
  return { old: split(oldText), new: split(newText) };
}

/** Pair only a solitary delete followed immediately by a solitary add; ambiguous multi-line edits stay whole-line only. */
export function wordChangeForItem(items: readonly PartItem[], index: number): WordChange | null {
  const item = items[index];
  if (!item || item.type !== "row") return null;
  const pairStart = item.row.kind === "del" ? index : item.row.kind === "add" ? index - 1 : -1;
  if (pairStart < 0) return null;
  const old = items[pairStart];
  const next = items[pairStart + 1];
  const prev = items[pairStart - 1];
  const after = items[pairStart + 2];
  if (old?.type !== "row" || old.row.kind !== "del" || next?.type !== "row" || next.row.kind !== "add") return null;
  if ((prev?.type === "row" && prev.row.kind === "del") || (after?.type === "row" && after.row.kind === "add")) return null;
  const pieces = changedWordPieces(old.row.text, next.row.text);
  return pieces ? (item.row.kind === "del" ? pieces.old : pieces.new) : null;
}

export function gapLabel(lines: number): string {
  return `${lines} unchanged ${lines === 1 ? "line" : "lines"}`;
}

export function signOf(kind: PartRowKind): string {
  return kind === "add" ? "+" : kind === "del" ? "−" : "";
}
