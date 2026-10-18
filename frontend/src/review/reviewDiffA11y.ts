/**
 * Accessibility for the real @pierre/diffs renderer (Phase 14 item 21).
 *
 * Pierre renders each diff into a shadow root as a CSS grid: per code column a
 * `[data-gutter]` of line-number cells and a `[data-content]` of `[data-line]`
 * rows carrying `data-line` (this side's number), `data-alt-line` (the other
 * side's number on unchanged rows) and `data-line-type`. It ships no ARIA, its
 * expand controls are `div[role=button]` without a tab stop, and in forced
 * colors its change bars (gradients) disappear. This decorator, run from
 * Pierre's `onPostRender`, adds:
 *
 * - list semantics per code column, one list item per line, with the state
 *   (added / deleted / unchanged) as the item's role description and the old
 *   and new line numbers as its description; the duplicate gutter number text
 *   is hidden from assistive tech;
 * - one roving tab stop per file: arrows move line by line, Home/End jump,
 *   Left/Right cross split columns; expand controls get a tab stop, a name and
 *   Enter/Space;
 * - text markers (+ / −) and border styles under forced colors and
 *   prefers-contrast, so state never depends on colour alone.
 *
 * Attributes are only written when they change, so a re-render costs one read
 * per row.
 */

export type DiffLineState = "added" | "deleted" | "unchanged";

export interface DiffLineSemantics {
  state: DiffLineState;
  oldLine: number | null;
  newLine: number | null;
}

const STYLE_ATTR = "data-lc-review-a11y-style";
const ROW_SELECTOR = "[data-line]";
const keyboardRoots = new WeakSet<Node>();

export const DIFF_A11Y_CSS = `
  [data-line][tabindex]:focus-visible,
  [data-expand-button][tabindex]:focus-visible {
    outline: 2px solid var(--diffs-modified-base, #69b1ff);
    outline-offset: -2px;
  }
  @media (forced-colors: active), (prefers-contrast: more) {
    [data-indicators] [data-line] { padding-inline-start: 2ch; }
    [data-indicators] [data-line-type="change-addition"][data-line]::before,
    [data-indicators] [data-line-type="change-deletion"][data-line]::before {
      position: absolute;
      top: 0;
      left: 0;
      width: 1.5ch;
      text-align: center;
      user-select: none;
      font-weight: 700;
    }
    [data-indicators] [data-line-type="change-addition"][data-line]::before { content: "+" / ""; }
    [data-indicators] [data-line-type="change-deletion"][data-line]::before { content: "\\2212" / ""; }
    [data-indicators] [data-line-type="change-addition"][data-line] { border-inline-start: 2px solid CanvasText; }
    [data-indicators] [data-line-type="change-deletion"][data-line] { border-inline-start: 2px dashed CanvasText; }
  }
  @media (forced-colors: active) {
    [data-line][tabindex]:focus-visible,
    [data-expand-button][tabindex]:focus-visible { outline-color: Highlight; }
  }
`;

function lineNumber(value: string | null): number | null {
  if (value == null || value === "") return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Old/new numbers and state for one Pierre `[data-line]` row. */
export function diffLineSemantics(row: Element): DiffLineSemantics | null {
  const type = row.getAttribute("data-line-type") ?? "";
  const line = lineNumber(row.getAttribute("data-line"));
  if (line == null) return null;
  if (type === "change-addition") return { state: "added", oldLine: null, newLine: line };
  if (type === "change-deletion") return { state: "deleted", oldLine: line, newLine: null };
  if (!type.startsWith("context")) return null;
  const alt = lineNumber(row.getAttribute("data-alt-line"));
  // The old column numbers its rows by the old file; the new and unified
  // columns by the new file, with the other side in data-alt-line.
  const oldColumn = Boolean(row.closest("[data-deletions]"));
  return oldColumn
    ? { state: "unchanged", oldLine: line, newLine: alt }
    : { state: "unchanged", oldLine: alt, newLine: line };
}

export function diffLineLabels(semantics: DiffLineSemantics): { roleDescription: string; description: string } {
  const numbers = [
    semantics.oldLine != null ? `old line ${semantics.oldLine}` : "",
    semantics.newLine != null ? `new line ${semantics.newLine}` : "",
  ].filter(Boolean).join(", ");
  const roleDescription = semantics.state === "added" ? "added line" : semantics.state === "deleted" ? "deleted line" : "unchanged line";
  return { roleDescription, description: numbers };
}

function setAttr(element: Element, name: string, value: string): void {
  if (element.getAttribute(name) !== value) element.setAttribute(name, value);
}

function columnLabel(code: Element, path: string): string {
  if (code.hasAttribute("data-deletions")) return `${path} · old version`;
  if (code.hasAttribute("data-additions")) return `${path} · new version`;
  return `${path} · changes`;
}

function rowsOf(list: Element): HTMLElement[] {
  return [...list.querySelectorAll<HTMLElement>(`:scope > ${ROW_SELECTOR}`)];
}

function splitIndex(row: Element): string {
  return (row.getAttribute("data-line-index") ?? "").split(",")[1] ?? "";
}

/**
 * What this file's keyboard user last focused, by position rather than by
 * element: Pierre replaces row and separator elements whenever the stream
 * re-renders (scrolling moves the active target), which would otherwise drop
 * focus to <body> mid-traversal.
 */
interface FocusMemory {
  /** Roving row position (`row:<column>:<line-index>:<type>`). */
  row: string;
  /** Element position that holds focus right now, or "" once focus left on purpose. */
  focused: string;
  /** After a keyboard expansion: focus the first row at this content slot. */
  expanded: { column: number; slot: number } | null;
}

const focusMemory = new WeakMap<Node, FocusMemory>();

function memoryFor(root: Node): FocusMemory {
  let memory = focusMemory.get(root);
  if (!memory) {
    memory = { row: "", focused: "", expanded: null };
    focusMemory.set(root, memory);
  }
  return memory;
}

function columnIndex(element: Element): number {
  const code = element.closest("[data-code]");
  const root = element.getRootNode() as ParentNode;
  return code ? [...root.querySelectorAll("[data-code]")].indexOf(code) : -1;
}

function expandDirection(button: Element): "up" | "down" | "both" {
  return button.hasAttribute("data-expand-up") ? "up" : button.hasAttribute("data-expand-down") ? "down" : "both";
}

function focusKey(element: Element): string {
  if (element.matches(ROW_SELECTOR)) {
    return `row:${columnIndex(element)}:${element.getAttribute("data-line-index") ?? ""}:${element.getAttribute("data-line-type") ?? ""}`;
  }
  if (element.hasAttribute("data-expand-button")) {
    const separator = element.closest("[data-separator]");
    return `expand:${separator?.getAttribute("data-expand-index") ?? ""}:${expandDirection(element)}`;
  }
  return "";
}

function findByKey(root: ParentNode, key: string): HTMLElement | null {
  if (key.startsWith("row:")) {
    const [, column, lineIndex, type] = key.split(":");
    // Pierre writes digits/commas and lowercase type names; anything else never matches.
    if (!/^[\d,]+$/.test(lineIndex ?? "") || !/^[a-z-]+$/.test(type ?? "")) return null;
    const code = root.querySelectorAll("[data-code]")[Number(column)];
    return code?.querySelector<HTMLElement>(
      `[data-content] > ${ROW_SELECTOR}[data-line-index="${lineIndex}"][data-line-type="${type}"]`,
    ) ?? null;
  }
  if (key.startsWith("expand:")) {
    for (const candidate of root.querySelectorAll<HTMLElement>("[data-expand-button][tabindex='0']")) {
      if (focusKey(candidate) === key) return candidate;
    }
  }
  return null;
}

function deepActiveElement(): Element | null {
  let active: Element | null = document.activeElement;
  while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
  return active;
}

function moveRovingRow(root: ParentNode, next: HTMLElement): void {
  for (const current of root.querySelectorAll<HTMLElement>(`${ROW_SELECTOR}[tabindex="0"]`)) {
    if (current !== next) current.setAttribute("tabindex", "-1");
  }
  next.setAttribute("tabindex", "0");
}

function attachKeyboard(root: ShadowRoot | HTMLElement): void {
  if (keyboardRoots.has(root)) return;
  keyboardRoots.add(root);
  const memory = memoryFor(root);
  root.addEventListener("keydown", (raw) => {
    const event = raw as KeyboardEvent;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const origin = event.composedPath()[0];
    if (!(origin instanceof HTMLElement)) return;
    if (origin.hasAttribute("data-expand-button")) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        const separator = origin.closest("[data-separator]");
        const parent = separator?.parentElement;
        // The gutter and content grids list the same slots in the same order.
        memory.expanded = separator && parent ? { column: columnIndex(separator), slot: [...parent.children].indexOf(separator) } : null;
        origin.click();
      }
      return;
    }
    if (!origin.matches(ROW_SELECTOR)) return;
    const list = origin.parentElement;
    if (!list) return;
    const rows = rowsOf(list);
    const index = rows.indexOf(origin);
    let next: HTMLElement | undefined;
    if (event.key === "ArrowDown") next = rows[index + 1];
    else if (event.key === "ArrowUp") next = rows[index - 1];
    else if (event.key === "Home") next = rows[0];
    else if (event.key === "End") next = rows[rows.length - 1];
    else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const lists = [...root.querySelectorAll<HTMLElement>("[data-content][role='list']")];
      const other = lists[lists.indexOf(list) + (event.key === "ArrowRight" ? 1 : -1)];
      if (!other) return;
      const peers = rowsOf(other);
      const wanted = Number.parseInt(splitIndex(origin), 10);
      // The same split row, or the nearest one when that side has a gap.
      next = peers.reduce<HTMLElement | undefined>((best, row) => {
        const distance = Math.abs(Number.parseInt(splitIndex(row), 10) - wanted);
        if (!Number.isFinite(distance)) return best;
        if (!best) return row;
        return distance < Math.abs(Number.parseInt(splitIndex(best), 10) - wanted) ? row : best;
      }, undefined);
    } else return;
    event.preventDefault();
    event.stopPropagation();
    if (!next) return;
    moveRovingRow(root, next);
    next.focus();
  });
  root.addEventListener("focusin", (raw) => {
    const origin = (raw as FocusEvent).composedPath()[0];
    if (!(origin instanceof HTMLElement)) return;
    memory.focused = focusKey(origin);
    if (origin.matches(ROW_SELECTOR)) {
      memory.row = memory.focused;
      if (origin.getAttribute("tabindex") !== "0") moveRovingRow(root, origin);
    }
  });
  root.addEventListener("focusout", (raw) => {
    const event = raw as FocusEvent;
    const origin = event.composedPath()[0];
    if (event.relatedTarget) {
      // Focus moved somewhere on purpose (Tab, a click on a control).
      memory.focused = "";
      return;
    }
    // No destination: a click on plain page, a window switch, or Pierre
    // removing the element. Only the last one is restored.
    queueMicrotask(() => {
      if (origin instanceof Node && origin.isConnected) memory.focused = "";
    });
  });
}

/** Put focus back where Pierre's re-render took it from, never stealing it from elsewhere. */
function restoreFocus(root: ShadowRoot | HTMLElement, lists: HTMLElement[]): void {
  const memory = memoryFor(root);
  if (memory.expanded) {
    const { column, slot } = memory.expanded;
    const list = lists.find((candidate) => columnIndex(candidate) === column);
    const after = list ? [...list.children].slice(slot).find((child) => child.matches(ROW_SELECTOR)) as HTMLElement | undefined : undefined;
    if (after) {
      memory.expanded = null;
      moveRovingRow(root, after);
      after.focus({ preventScroll: true });
      return;
    }
  }
  if (!memory.focused) return;
  const active = deepActiveElement();
  if (active && active !== document.body && active.isConnected) return;
  findByKey(root, memory.focused)?.focus({ preventScroll: true });
}

export function decorateDiffAccessibility(node: HTMLElement, path: string, phase: "mount" | "update" | "unmount"): void {
  if (phase === "unmount") return;
  const root = node.shadowRoot ?? node;
  if (root instanceof ShadowRoot && !root.querySelector(`style[${STYLE_ATTR}]`)) {
    const style = document.createElement("style");
    style.setAttribute(STYLE_ATTR, "true");
    style.textContent = DIFF_A11Y_CSS;
    root.appendChild(style);
  }

  const lists: HTMLElement[] = [];
  for (const code of root.querySelectorAll<HTMLElement>("[data-code]")) {
    for (const part of code.children) {
      if (part.hasAttribute("data-gutter")) {
        // Hide the number text, not the cell: Pierre places its hover comment
        // button inside the cell, and a hidden subtree must hold no control.
        for (const cell of part.children) {
          if (cell.hasAttribute("data-gutter-buffer")) setAttr(cell, "aria-hidden", "true");
          else if (cell.hasAttribute("data-column-number")) {
            const number = cell.querySelector(":scope > [data-line-number-content]");
            if (number) setAttr(number, "aria-hidden", "true");
          }
        }
      } else if (part.hasAttribute("data-content")) {
        lists.push(part as HTMLElement);
        setAttr(part, "role", "list");
        setAttr(part, "aria-label", columnLabel(code, path));
        for (const child of part.children) {
          if (child.matches(ROW_SELECTOR)) {
            const semantics = diffLineSemantics(child);
            setAttr(child, "role", "listitem");
            if (semantics) {
              const labels = diffLineLabels(semantics);
              setAttr(child, "aria-roledescription", labels.roleDescription);
              setAttr(child, "aria-description", labels.description);
            }
          } else if (child.hasAttribute("data-line-annotation")) {
            setAttr(child, "role", "listitem");
          } else {
            // Buffers are spacing; separator copies repeat the gutter's control.
            setAttr(child, "aria-hidden", "true");
          }
        }
      }
    }
  }

  // One roving tab stop per file, kept on the row the reviewer last focused.
  const rows = lists.flatMap((list) => rowsOf(list));
  const memory = memoryFor(root);
  const roving = findByKey(root, memory.row) ?? rows[0];
  for (const row of rows) setAttr(row, "tabindex", row === roving ? "0" : "-1");

  // Pierre repeats each separator per column; the first copy is the visible
  // control. It gets the tab stop and a name; later copies leave the a11y tree.
  const seen = new Set<string>();
  for (const button of root.querySelectorAll<HTMLElement>("[data-separator] [data-expand-button]")) {
    const separator = button.closest<HTMLElement>("[data-separator]")!;
    const direction = expandDirection(button);
    const key = `${separator.getAttribute("data-expand-index") ?? ""}:${direction}`;
    if (seen.has(key) || separator.closest("[data-content]")) {
      setAttr(separator, "aria-hidden", "true");
      if (button.hasAttribute("tabindex")) button.removeAttribute("tabindex");
      continue;
    }
    seen.add(key);
    const hidden = separator.querySelector("[data-unmodified-lines]")?.textContent?.trim() || "unchanged lines";
    const verb = direction === "up" ? "Show more above" : direction === "down" ? "Show more below" : "Show";
    setAttr(button, "tabindex", "0");
    setAttr(button, "aria-label", `${verb}: ${hidden}`);
  }

  attachKeyboard(root as ShadowRoot | HTMLElement);
  if (phase === "update") restoreFocus(root as ShadowRoot | HTMLElement, lists);
}
