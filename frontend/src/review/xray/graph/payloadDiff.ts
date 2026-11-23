/**
 * A field-level diff of two JSON samples (a payload before and after), flattened into the rows the inspector draws.
 * Objects compare by key (After's key order, then keys only Before had), arrays by index. A changed scalar is one
 * amber row "old → new"; an unchanged object or array folds to one line.
 */

export type DiffKind = "same" | "added" | "removed" | "changed";

export interface DiffNode {
  kind: DiffKind;
  /** The key in its parent object, or the index in its parent array; null at the root. */
  key: string | number | null;
  before?: unknown;
  after?: unknown;
  /** Present when both sides are the same container type and were compared field by field. */
  children?: DiffNode[];
  container?: "object" | "array";
}

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const containerOf = (value: unknown): "object" | "array" | null => (Array.isArray(value) ? "array" : isObject(value) ? "object" : null);

export function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((item, i) => sameJson(item, b[i]));
  if (isObject(a) && isObject(b)) {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((key) => key in b && sameJson(a[key], b[key]));
  }
  return false;
}

/** `undefined` or `null` on a side means the sample is absent there (the contract's "null when absent on that side"). */
export function diffJson(before: unknown, after: unknown, key: string | number | null = null): DiffNode {
  const hasBefore = before !== undefined && before !== null;
  const hasAfter = after !== undefined && after !== null;
  if (key === null && !hasBefore && !hasAfter) return { kind: "same", key };
  if (key === null && !hasBefore && hasAfter) return { kind: "added", key, after, container: containerOf(after) ?? undefined, children: childrenOf(after, "added") };
  if (key === null && hasBefore && !hasAfter) return { kind: "removed", key, before, container: containerOf(before) ?? undefined, children: childrenOf(before, "removed") };
  if (sameJson(before, after)) return { kind: "same", key, before, after, container: containerOf(after) ?? undefined, children: childrenOf(after, "same") };
  const type = containerOf(before);
  if (type && type === containerOf(after)) {
    const children: DiffNode[] = [];
    if (type === "array") {
      const a = before as unknown[];
      const b = after as unknown[];
      for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
        if (i >= a.length) children.push(whole("added", i, b[i]));
        else if (i >= b.length) children.push(whole("removed", i, a[i]));
        else children.push(diffJson(a[i], b[i], i));
      }
    } else {
      const a = before as Record<string, unknown>;
      const b = after as Record<string, unknown>;
      for (const k of Object.keys(b)) children.push(k in a ? diffJson(a[k], b[k], k) : whole("added", k, b[k]));
      for (const k of Object.keys(a)) if (!(k in b)) children.push(whole("removed", k, a[k]));
    }
    return { kind: "changed", key, before, after, container: type, children };
  }
  return { kind: "changed", key, before, after };
}

function whole(kind: "added" | "removed", key: string | number, value: unknown): DiffNode {
  const node: DiffNode = kind === "added" ? { kind, key, after: value } : { kind, key, before: value };
  const container = containerOf(value);
  if (container) {
    node.container = container;
    node.children = childrenOf(value, kind);
  }
  return node;
}

function childrenOf(value: unknown, kind: DiffKind): DiffNode[] | undefined {
  if (Array.isArray(value)) return value.map((item, i) => (kind === "same" ? diffJson(item, item, i) : whole(kind as "added" | "removed", i, item)));
  if (isObject(value)) return Object.entries(value).map(([k, item]) => (kind === "same" ? diffJson(item, item, k) : whole(kind as "added" | "removed", k, item)));
  return undefined;
}

export interface DiffRow {
  kind: DiffKind;
  depth: number;
  /** `"name": `, `[1] ` or "" at the root. */
  key: string;
  /** The value text: a scalar, an opening bracket, a closing bracket, or a folded summary. */
  text: string;
  /** For a changed scalar: the Before text (drawn struck through) shown before `text`. */
  was?: string;
  /** A closing bracket row. */
  close?: boolean;
}

export function scalarText(value: unknown): string {
  if (value === undefined) return "—";
  if (typeof value === "string") return JSON.stringify(value);
  return JSON.stringify(value) ?? String(value);
}

/** A one-line form of a value; long ones fold to a count. */
export function inlineText(value: unknown, max = 56): string {
  const text = JSON.stringify(value) ?? String(value);
  if (text.length <= max) return text.replace(/,"/g, ', "').replace(/":/g, '": ');
  if (Array.isArray(value)) return `[… ${value.length} item${value.length === 1 ? "" : "s"}]`;
  if (isObject(value)) return `{… ${Object.keys(value).length} field${Object.keys(value).length === 1 ? "" : "s"}}`;
  return `${text.slice(0, max - 1)}…`;
}

function keyText(key: string | number | null): string {
  if (key === null) return "";
  return typeof key === "number" ? `[${key}] ` : `${JSON.stringify(key)}: `;
}

/** The rows of a diff, depth first. Unchanged containers below the root fold to one line. */
export function diffRows(node: DiffNode, depth = 0): DiffRow[] {
  const key = keyText(node.key);
  const value = node.kind === "removed" ? node.before : node.after;
  const expand = node.children && node.container && (depth === 0 || node.kind !== "same");
  if (!expand) {
    if (node.kind === "changed") return [{ kind: "changed", depth, key, text: inlineText(node.after), was: inlineText(node.before) }];
    return [{ kind: node.kind, depth, key, text: node.container ? inlineText(value) : scalarText(value) }];
  }
  const [open, close] = node.container === "array" ? ["[", "]"] : ["{", "}"];
  const rows: DiffRow[] = [{ kind: node.kind, depth, key, text: open }];
  for (const child of node.children!) rows.push(...diffRows(child, depth + 1));
  rows.push({ kind: node.kind, depth, key: "", text: close, close: true });
  return rows;
}

/** How many fields were added, removed or changed (leaves only). */
export function diffCounts(node: DiffNode): { added: number; removed: number; changed: number } {
  const counts = { added: 0, removed: 0, changed: 0 };
  const walk = (n: DiffNode) => {
    if (n.children && n.kind !== "same" && n.container && (n.kind === "changed" || n.key === null)) {
      n.children.forEach(walk);
      return;
    }
    if (n.kind === "added") counts.added += 1;
    else if (n.kind === "removed") counts.removed += 1;
    else if (n.kind === "changed") counts.changed += 1;
  };
  walk(node);
  return counts;
}
