import { afterEach, describe, expect, it, vi } from "vitest";

import { decorateDiffAccessibility, diffLineLabels, diffLineSemantics } from "./reviewDiffA11y";

// Markup shaped like @pierre/diffs 1.4 renders it in Chromium (the real renderer
// is exercised by e2e/review-p14-diff-a11y.spec.ts): per code column a gutter
// and a content grid listing the same slots in the same order.
const SPLIT = `
<pre data-diff data-indicators="bars" data-diff-type="split">
  <code data-code data-deletions>
    <div data-gutter>
      <div data-separator="line-info" data-expand-index="0" data-separator-first><div data-separator-wrapper><div role="button" data-expand-button data-expand-down></div><div data-separator-content><span data-unmodified-lines>3 unmodified lines</span></div></div></div>
      <div data-line-type="context" data-column-number="4" data-line-index="3,3"><span data-line-number-content>4</span></div>
      <div data-gutter-buffer="annotation" data-buffer-size="1"></div>
      <div data-line-type="change-deletion" data-column-number="7" data-line-index="7,7"><span data-line-number-content>7</span></div>
      <div data-line-type="context" data-column-number="8" data-line-index="13,12"><span data-line-number-content>8</span></div>
    </div>
    <div data-content>
      <div data-separator="line-info" data-expand-index="0" data-separator-first><div data-separator-wrapper><div role="button" data-expand-button data-expand-down></div></div></div>
      <div data-line="4" data-alt-line="4" data-line-type="context" data-line-index="3,3">def handler():</div>
      <div data-line-annotation="0,5"><div data-annotation-content></div></div>
      <div data-line="7" data-line-type="change-deletion" data-line-index="7,7">    limit = 100</div>
      <div data-content-buffer data-buffer-size="4"></div>
      <div data-line="8" data-alt-line="13" data-line-type="context" data-line-index="13,12">    if total &gt; limit:</div>
    </div>
  </code>
  <code data-code data-additions>
    <div data-gutter>
      <div data-separator="line-info" data-expand-index="0" data-separator-first><div data-separator-wrapper><div role="button" data-expand-button data-expand-down></div></div></div>
      <div data-line-type="context" data-column-number="4" data-line-index="3,3"><span data-line-number-content>4</span></div>
      <div data-line-type="change-addition" data-column-number="7" data-line-index="6,6"><span data-line-number-content>7</span></div>
      <div data-line-type="change-addition" data-column-number="8" data-line-index="8,7"><span data-line-number-content>8</span></div>
      <div data-line-type="context" data-column-number="13" data-line-index="13,12"><span data-line-number-content>13</span></div>
    </div>
    <div data-content>
      <div data-line="4" data-alt-line="4" data-line-type="context" data-line-index="3,3">def handler():</div>
      <div data-line="7" data-line-type="change-addition" data-line-index="6,6">    total += 1</div>
      <div data-line="8" data-line-type="change-addition" data-line-index="8,7">    limit = 101</div>
      <div data-line="13" data-alt-line="8" data-line-type="context" data-line-index="13,12">    if total &gt; limit:</div>
    </div>
  </code>
</pre>`;

const UNIFIED = `
<pre data-diff data-indicators="bars" data-diff-type="single">
  <code data-code data-unified>
    <div data-gutter>
      <div data-line-type="change-addition" data-column-number="7" data-line-index="6,6"></div>
      <div data-line-type="change-deletion" data-column-number="7" data-line-index="7,7"></div>
      <div data-line-type="context" data-column-number="13" data-line-index="13,12"></div>
    </div>
    <div data-content>
      <div data-line="7" data-line-type="change-addition" data-line-index="6,6">    total += 1</div>
      <div data-line="7" data-line-type="change-deletion" data-line-index="7,7">    limit = 100</div>
      <div data-line="13" data-alt-line="8" data-line-type="context" data-line-index="13,12">    if total &gt; limit:</div>
    </div>
  </code>
</pre>`;

function pierreHost(markup: string): { host: HTMLElement; root: ShadowRoot } {
  const host = document.createElement("diffs-container");
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = markup;
  document.body.appendChild(host);
  return { host, root };
}

function rows(root: ShadowRoot): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>("[data-line]")];
}

function press(target: HTMLElement, key: string): void {
  target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, composed: true, cancelable: true }));
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("diff accessibility on the Pierre renderer", () => {
  it("gives every split row its state and both line numbers, from the column it lives in", () => {
    const { host, root } = pierreHost(SPLIT);
    decorateDiffAccessibility(host, "src/a.py", "mount");
    const described = rows(root).map((row) => [row.getAttribute("role"), row.getAttribute("aria-roledescription"), row.getAttribute("aria-description")]);
    expect(described).toEqual([
      ["listitem", "unchanged line", "old line 4, new line 4"],
      ["listitem", "deleted line", "old line 7"],
      ["listitem", "unchanged line", "old line 8, new line 13"],
      ["listitem", "unchanged line", "old line 4, new line 4"],
      ["listitem", "added line", "new line 7"],
      ["listitem", "added line", "new line 8"],
      ["listitem", "unchanged line", "old line 8, new line 13"],
    ]);
    const lists = [...root.querySelectorAll("[data-content]")];
    expect(lists.map((list) => [list.getAttribute("role"), list.getAttribute("aria-label")])).toEqual([
      ["list", "src/a.py · old version"],
      ["list", "src/a.py · new version"],
    ]);
    // A list owns only list items: buffers and repeated separators leave the tree.
    for (const list of lists) {
      for (const child of list.children) {
        expect(child.getAttribute("role") === "listitem" || child.getAttribute("aria-hidden") === "true").toBe(true);
      }
    }
    // Gutter numbers are visual duplicates of the row description.
    for (const cell of root.querySelectorAll("[data-line-number-content], [data-gutter-buffer]")) expect(cell.getAttribute("aria-hidden")).toBe("true");
    // ...but not the cell, which hosts Pierre's hover comment button.
    for (const cell of root.querySelectorAll("[data-column-number]")) expect(cell.hasAttribute("aria-hidden")).toBe(false);
  });

  it("reads unified unchanged rows as new line with the old line from data-alt-line", () => {
    const { host, root } = pierreHost(UNIFIED);
    decorateDiffAccessibility(host, "src/a.py", "mount");
    expect(rows(root).map((row) => row.getAttribute("aria-description"))).toEqual(["new line 7", "old line 7", "old line 8, new line 13"]);
    expect(root.querySelector("[data-content]")!.getAttribute("aria-label")).toBe("src/a.py · changes");
  });

  it("maps Pierre line types to semantics and labels", () => {
    const row = document.createElement("div");
    row.setAttribute("data-line", "3");
    row.setAttribute("data-line-type", "context-expanded");
    row.setAttribute("data-alt-line", "2");
    expect(diffLineSemantics(row)).toEqual({ state: "unchanged", oldLine: 2, newLine: 3 });
    row.setAttribute("data-line-type", "something-else");
    expect(diffLineSemantics(row)).toBeNull();
    expect(diffLineLabels({ state: "deleted", oldLine: 9, newLine: null })).toEqual({ roleDescription: "deleted line", description: "old line 9" });
  });

  it("keeps one roving tab stop per file and moves line by line, across columns and to the ends", () => {
    const { host, root } = pierreHost(SPLIT);
    decorateDiffAccessibility(host, "src/a.py", "mount");
    const all = rows(root);
    expect(all.filter((row) => row.getAttribute("tabindex") === "0")).toEqual([all[0]]);
    expect(all.slice(1).every((row) => row.getAttribute("tabindex") === "-1")).toBe(true);

    all[0].focus();
    press(all[0], "ArrowDown");
    expect(root.activeElement).toBe(all[1]);
    expect(all[1].getAttribute("tabindex")).toBe("0");
    expect(all[0].getAttribute("tabindex")).toBe("-1");

    press(all[1], "End");
    expect(root.activeElement).toBe(all[2]);
    // Right: the new-side row on the same split row.
    press(all[2], "ArrowRight");
    expect(root.activeElement).toBe(all[6]);
    press(all[6], "ArrowUp");
    expect(root.activeElement).toBe(all[5]);
    press(all[5], "Home");
    expect(root.activeElement).toBe(all[3]);
    // Left from new line 7 (split row 6): nearest old-side row.
    press(all[4], "ArrowLeft");
    expect(root.activeElement).toBe(all[1]);
    expect(all.filter((row) => row.getAttribute("tabindex") === "0")).toEqual([all[1]]);

    // A re-render keeps the reviewer's row as the tab stop and writes nothing.
    const writes: MutationRecord[] = [];
    const observer = new MutationObserver((records) => writes.push(...records));
    observer.observe(root, { attributes: true, subtree: true, childList: true });
    decorateDiffAccessibility(host, "src/a.py", "update");
    observer.disconnect();
    expect(writes.concat(observer.takeRecords())).toEqual([]);
    expect(all.filter((row) => row.getAttribute("tabindex") === "0")).toEqual([all[1]]);
  });

  it("exposes one named, keyboard-operable expand control per gap", () => {
    const { host, root } = pierreHost(SPLIT);
    decorateDiffAccessibility(host, "src/a.py", "mount");
    const buttons = [...root.querySelectorAll<HTMLElement>("[data-expand-button]")];
    expect(buttons.map((button) => button.getAttribute("tabindex"))).toEqual(["0", null, null]);
    expect(buttons[0].getAttribute("aria-label")).toBe("Show more below: 3 unmodified lines");
    expect(buttons.slice(1).every((button) => button.closest("[data-separator]")!.getAttribute("aria-hidden") === "true")).toBe(true);

    const click = vi.fn();
    buttons[0].addEventListener("click", click);
    press(buttons[0], "Enter");
    press(buttons[0], " ");
    expect(click).toHaveBeenCalledTimes(2);
  });

  it("names an expand control by what it hides once the stream reworded the gap as an action", () => {
    const { host, root } = pierreHost(SPLIT);
    const label = root.querySelector<HTMLElement>("[data-unmodified-lines]")!;
    label.textContent = "Show 3 more lines";
    label.dataset.lcHiddenDescription = "3 lines unchanged";
    decorateDiffAccessibility(host, "src/a.py", "mount");
    const button = root.querySelector<HTMLElement>("[data-expand-button]")!;
    expect(button.getAttribute("aria-label")).toBe("Show more below: 3 lines unchanged");
  });

  it("puts focus back on the same line when Pierre replaces the rows, but never steals it", async () => {
    const { host, root } = pierreHost(SPLIT);
    decorateDiffAccessibility(host, "src/a.py", "mount");
    rows(root)[4].focus();
    expect(root.activeElement).toBe(rows(root)[4]);

    // Pierre re-renders: every row element is replaced.
    root.querySelector("pre")!.outerHTML = SPLIT;
    await Promise.resolve();
    expect(root.activeElement).toBeNull();
    decorateDiffAccessibility(host, "src/a.py", "update");
    expect(root.activeElement).toBe(rows(root)[4]);
    expect(rows(root)[4].getAttribute("aria-description")).toBe("new line 7");
    expect(rows(root).filter((row) => row.getAttribute("tabindex") === "0")).toEqual([rows(root)[4]]);

    // The reviewer moves on: a later re-render leaves their focus alone.
    const elsewhere = document.createElement("button");
    document.body.appendChild(elsewhere);
    elsewhere.focus();
    await Promise.resolve();
    root.querySelector("pre")!.outerHTML = SPLIT;
    decorateDiffAccessibility(host, "src/a.py", "update");
    expect(document.activeElement).toBe(elsewhere);
    // The roving stop still remembers their line for the next Tab.
    expect(rows(root).filter((row) => row.getAttribute("tabindex") === "0")).toEqual([rows(root)[4]]);
  });

  it("moves focus to the first revealed line after a keyboard expansion", () => {
    const { host, root } = pierreHost(SPLIT);
    decorateDiffAccessibility(host, "src/a.py", "mount");
    const button = root.querySelector<HTMLElement>("[data-expand-button][tabindex='0']")!;
    button.addEventListener("click", () => {
      // Pierre reveals line 1-3 where the separator was and drops the separator.
      const revealed = (line: number) => `<div data-line="${line}" data-alt-line="${line}" data-line-type="context-expanded" data-line-index="${line - 1},${line - 1}">line ${line}</div>`;
      for (const content of root.querySelectorAll("[data-content]")) {
        content.querySelector("[data-separator]")?.remove();
        content.insertAdjacentHTML("afterbegin", revealed(1) + revealed(2) + revealed(3));
      }
      for (const separator of root.querySelectorAll("[data-gutter] [data-separator]")) separator.remove();
    });
    button.focus();
    press(button, "Enter");
    decorateDiffAccessibility(host, "src/a.py", "update");
    const focused = root.activeElement as HTMLElement;
    expect(focused.getAttribute("data-line")).toBe("1");
    expect(focused.closest("[data-deletions]")).not.toBeNull();
    expect(focused.getAttribute("aria-description")).toBe("old line 1, new line 1");
  });

  it("injects the forced-colors / contrast markers once per shadow root", () => {
    const { host, root } = pierreHost(SPLIT);
    decorateDiffAccessibility(host, "src/a.py", "mount");
    decorateDiffAccessibility(host, "src/a.py", "update");
    const styles = root.querySelectorAll("style[data-lc-review-a11y-style]");
    expect(styles).toHaveLength(1);
    expect(styles[0].textContent).toContain("forced-colors: active");
    expect(styles[0].textContent).toContain('content: "+" / ""');
  });
});
