/**
 * Text widths from a fixed table, never from the font engine, so a layout is the same on every machine and in tests.
 * The table is Inter's advance widths in em, rounded up a little: a label measured here is never wider on screen
 * than the room the layout gave it. Mono text (paths, payload keys) is Geist Mono, a flat 0.6em.
 */

export type TextFont = "sans" | "sans-semibold" | "mono";

const SANS: Record<string, number> = {
  " ": 0.28, "!": 0.29, '"': 0.4, "#": 0.64, $: 0.6, "%": 0.82, "&": 0.68, "'": 0.23, "(": 0.35, ")": 0.35, "*": 0.47,
  "+": 0.6, ",": 0.27, "-": 0.42, ".": 0.27, "/": 0.4, ":": 0.28, ";": 0.28, "<": 0.6, "=": 0.6, ">": 0.6, "?": 0.5,
  "@": 0.96, "[": 0.34, "\\": 0.4, "]": 0.34, "^": 0.47, _: 0.5, "`": 0.32, "{": 0.37, "|": 0.28, "}": 0.37, "~": 0.6,
  "0": 0.6, "1": 0.44, "2": 0.58, "3": 0.6, "4": 0.61, "5": 0.58, "6": 0.6, "7": 0.54, "8": 0.6, "9": 0.6,
  A: 0.69, B: 0.66, C: 0.72, D: 0.72, E: 0.6, F: 0.58, G: 0.74, H: 0.74, I: 0.28, J: 0.52, K: 0.66, L: 0.56, M: 0.88,
  N: 0.75, O: 0.77, P: 0.64, Q: 0.77, R: 0.66, S: 0.64, T: 0.64, U: 0.73, V: 0.69, W: 0.97, X: 0.67, Y: 0.66, Z: 0.63,
  a: 0.57, b: 0.6, c: 0.55, d: 0.6, e: 0.57, f: 0.35, g: 0.6, h: 0.59, i: 0.26, j: 0.26, k: 0.54, l: 0.26, m: 0.88,
  n: 0.59, o: 0.59, p: 0.6, q: 0.6, r: 0.38, s: 0.52, t: 0.36, u: 0.59, v: 0.54, w: 0.79, x: 0.53, y: 0.54, z: 0.52,
  "×": 0.6, "·": 0.28, "→": 0.92, "←": 0.92, "…": 0.82, "−": 0.6, "—": 0.9, "–": 0.6, "€": 0.64, "£": 0.62,
};
const SANS_FALLBACK = 0.64;
const SEMIBOLD = 1.05;
const MONO = 0.61;

export function charWidth(char: string, size: number, font: TextFont = "sans"): number {
  if (font === "mono") return MONO * size;
  const em = SANS[char] ?? SANS_FALLBACK;
  return em * size * (font === "sans-semibold" ? SEMIBOLD : 1);
}

/** The width of `text` at `size` px, with optional letter spacing (px per character, as CSS `letter-spacing`). */
export function textWidth(text: string, size: number, font: TextFont = "sans", tracking = 0): number {
  let width = 0;
  for (const char of text) width += charWidth(char, size, font) + tracking;
  return Math.ceil(width);
}

/** `text` cut to fit `max` px, ending in an ellipsis when it was cut. */
export function fitText(text: string, max: number, size: number, font: TextFont = "sans", tracking = 0): string {
  if (textWidth(text, size, font, tracking) <= max) return text;
  const ellipsis = charWidth("…", size, font) + tracking;
  let width = 0;
  let out = "";
  for (const char of text) {
    const next = charWidth(char, size, font) + tracking;
    if (width + next + ellipsis > max) break;
    width += next;
    out += char;
  }
  return `${out.trimEnd()}…`;
}

/** Greedy word wrap to `max` px; a word longer than a line is cut. At most `lines` lines, the last one ellipsised. */
export function wrapText(text: string, max: number, size: number, lines = 3, font: TextFont = "sans"): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const out: string[] = [];
  let line = "";
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (textWidth(next, size, font) <= max) {
      line = next;
      continue;
    }
    if (line) out.push(line);
    line = textWidth(word, size, font) <= max ? word : fitText(word, max, size, font);
  }
  if (line) out.push(line);
  if (out.length <= lines) return out;
  const kept = out.slice(0, lines);
  kept[lines - 1] = fitText(`${kept[lines - 1]} ${out.slice(lines).join(" ")}`, max, size, font);
  return kept;
}
