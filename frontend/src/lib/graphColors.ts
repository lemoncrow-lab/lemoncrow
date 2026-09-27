import type { CodeMapActivityKind } from "../api";

// Single vivid color per symbol kind (used when the server sends no color).
export const KIND_COLORS: Record<string, string> = {
  class: "#fbbf24",
  method: "#67e8f9",
  function: "#67e8f9",
  async_function: "#a5b4fc",
  interface: "#c4b5fd",
  type: "#c4b5fd",
  module: "#86efac",
  reference: "#a3a3a3",
};

// [light, dark] accent per live-activity kind.
export const ACTIVITY_COLORS: Record<CodeMapActivityKind, [string, string]> = {
  search: ["#c4b5fd", "#7c3aed"],
  read: ["#67e8f9", "#0e7490"],
  edit: ["#fbbf24", "#b45309"],
  verify: ["#4ade80", "#15803d"],
};

export function stableUnit(value: string): number {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

// Punch up the server/palette colors: saturate hard and pin lightness so nodes
// read as vivid, high-contrast dots instead of washed-out pastels. Near-grey
// colors are left alone so "other" nodes don't turn into a random hue.
export function vivid(hex: string, light: boolean): string {
  const match = /^#?([\da-f]{6})$/i.exec(hex.trim());
  if (!match) return hex;
  const int = parseInt(match[1], 16);
  const r = ((int >> 16) & 255) / 255;
  const g = ((int >> 8) & 255) / 255;
  const b = (int & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  const lightness = (max + min) / 2;
  if (delta < 0.05) return hex; // greyscale -> keep neutral
  let hue = 0;
  if (max === r) hue = ((g - b) / delta) % 6;
  else if (max === g) hue = (b - r) / delta + 2;
  else hue = (r - g) / delta + 4;
  hue = (hue * 60 + 360) % 360;
  const sat = 0.9;
  const lum = light
    ? Math.min(0.5, Math.max(0.4, lightness))
    : Math.min(0.64, Math.max(0.54, lightness));
  const c = (1 - Math.abs(2 * lum - 1)) * sat;
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = lum - c / 2;
  let rr = 0;
  let gg = 0;
  let bb = 0;
  if (hue < 60) [rr, gg, bb] = [c, x, 0];
  else if (hue < 120) [rr, gg, bb] = [x, c, 0];
  else if (hue < 180) [rr, gg, bb] = [0, c, x];
  else if (hue < 240) [rr, gg, bb] = [0, x, c];
  else if (hue < 300) [rr, gg, bb] = [x, 0, c];
  else [rr, gg, bb] = [c, 0, x];
  const channel = (value: number) =>
    Math.round((value + m) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${channel(rr)}${channel(gg)}${channel(bb)}`;
}
