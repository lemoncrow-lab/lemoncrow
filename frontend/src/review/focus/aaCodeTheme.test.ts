import { describe, expect, it } from "vitest";

import { aaTheme, contrast, readableOn } from "./aaCodeTheme";

const rgb = (hex: string): [number, number, number] => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as [number, number, number];

describe("AA code themes", () => {
  it("darkens the Pierre light yellow, green and cyan until they read at AA on white and the row tints, keeping passing colours", () => {
    const backgrounds = [rgb("#ffffff"), rgb("#e7f4ec"), rgb("#ffe8e4")];
    for (const color of ["#d5a910", "#199f43", "#1ca1c7", "#d32a61", "#737373"]) {
      const fixed = readableOn(color, backgrounds, false);
      for (const bg of backgrounds) expect(contrast(rgb(fixed), bg)).toBeGreaterThanOrEqual(4.5);
    }
    expect(readableOn("#0a0a0a", backgrounds, false)).toBe("#0a0a0a");
  });

  it("e2e-back-phone: a changed word's highlight reads at AA in both themes, measured where axe found 3.7:1", () => {
    const dark = aaTheme({ name: "pierre-dark", type: "dark", colors: { "editor.background": "#0a0a0a", "gitDecoration.addedResourceForeground": "#07c480", "gitDecoration.deletedResourceForeground": "#ff2e3f" }, settings: [{ settings: { foreground: "#9d9d9d" } }] } as never, "pierre-dark-aa");
    const darkFg = rgb((dark.settings[0].settings as { foreground: string }).foreground);
    // #154b35 is the rendered added-word highlight on pierre-dark; #5e1a20 stands for the removed one.
    for (const bg of ["#154b35", "#5e1a20", "#0a0a0a"]) expect(contrast(darkFg, rgb(bg))).toBeGreaterThanOrEqual(4.5);
    const light = aaTheme({ name: "pierre-light", type: "light", colors: { "editor.background": "#ffffff", "gitDecoration.addedResourceForeground": "#18a46c", "gitDecoration.deletedResourceForeground": "#d52c36" }, settings: [{ settings: { foreground: "#8e8e8e" } }] } as never, "pierre-light-aa");
    const lightFg = rgb((light.settings[0].settings as { foreground: string }).foreground);
    for (const bg of ["#c5e8da", "#f4cacc", "#ffffff"]) expect(contrast(lightFg, rgb(bg))).toBeGreaterThanOrEqual(4.5);
  });

  it("lightens on a dark theme and rewrites every token rule under the new name", () => {
    const theme = aaTheme({ name: "base", type: "dark", colors: { "editor.background": "#0d1117" }, settings: [{ settings: { foreground: "#3b4048" } }, { settings: {} }] } as never, "base-aa");
    expect(theme.name).toBe("base-aa");
    const fg = (theme.settings[0].settings as { foreground: string }).foreground;
    expect(contrast(rgb(fg), rgb("#0d1117"))).toBeGreaterThanOrEqual(4.6);
    expect(theme.settings[1].settings).toEqual({});
  });
});
