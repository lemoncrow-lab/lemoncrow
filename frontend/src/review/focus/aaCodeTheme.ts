/**
 * The Classic Reader's Pierre themes ship syntax colours below WCAG AA on white and on the diff tints (SPEC 5.3 measures
 * them). The flow renders the same themes, each token colour pushed toward the text colour until it reads at 4.6:1 on the
 * editor background, on the added/removed row tints and on the changed-word highlight inside those rows. Hue is kept;
 * only lightness moves.
 */
import { registerCustomTheme, resolveTheme } from "@pierre/diffs";

type Rgb = [number, number, number];
const MIN_RATIO = 4.6;
/** Pierre's row tints measure about 10% of the add/remove colour on light and 15% on dark; 30% leaves margin for hover and selection. */
const TINT = 0.3;
const ADDED: Rgb = [24, 164, 108];
const REMOVED: Rgb = [213, 44, 54];
/**
 * Pierre's own tints of the theme's git colour: a changed row is 20% of it on dark and 12% on light, and a changed word
 * lays another 20% or 15% of it on top of the row (`--diffs-bg-addition` and `--diffs-bg-addition-emphasis`).
 */
const ROW_TINT = { dark: 0.2, light: 0.12 };
const WORD_TINT = { dark: 0.2, light: 0.15 };

function parse(hex: string): Rgb | null {
  const match = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(hex.trim());
  if (!match) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}
const hex = (rgb: Rgb) => "#" + rgb.map((channel) => Math.round(channel).toString(16).padStart(2, "0")).join("");
const mix = (a: Rgb, b: Rgb, amount: number): Rgb => [0, 1, 2].map((i) => a[i] + (b[i] - a[i]) * amount) as Rgb;
function luminance(rgb: Rgb): number {
  const [r, g, b] = rgb.map((channel) => { const c = channel / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The nearest colour along the line to black (light theme) or white (dark theme) that passes on every background. */
export function readableOn(color: string, backgrounds: readonly Rgb[], dark: boolean): string {
  const rgb = parse(color);
  if (!rgb) return color;
  const target: Rgb = dark ? [255, 255, 255] : [0, 0, 0];
  for (let step = 0; step <= 20; step += 1) {
    const candidate = mix(rgb, target, step / 20);
    if (backgrounds.every((bg) => contrast(candidate, bg) >= MIN_RATIO)) return step === 0 ? color : hex(candidate);
  }
  return hex(target);
}

type Setting = { settings?: { foreground?: string } & Record<string, unknown> } & Record<string, unknown>;
type Resolved = Awaited<ReturnType<typeof resolveTheme>>;

export function aaTheme(base: Resolved, name: string): Resolved {
  const dark = base.type === "dark";
  const colors = { ...(base.colors ?? {}) } as Record<string, string>;
  const fallback: Rgb = dark ? [0, 0, 0] : [255, 255, 255];
  const bg = parse(colors["editor.background"] ?? base.bg ?? "") ?? fallback;
  // The row and changed-word tints Pierre draws from this theme's own git colours (getHighlighterThemeStyles).
  const tints = (key: string, ansi: string, fallbackColor: Rgb): Rgb[] => {
    const base = parse(colors[key] ?? colors[ansi] ?? "") ?? fallbackColor;
    const row = mix(bg, base, dark ? ROW_TINT.dark : ROW_TINT.light);
    return [row, mix(row, base, dark ? WORD_TINT.dark : WORD_TINT.light)];
  };
  const backgrounds: Rgb[] = [
    bg,
    mix(bg, ADDED, TINT),
    mix(bg, REMOVED, TINT),
    ...tints("gitDecoration.addedResourceForeground", "terminal.ansiGreen", ADDED),
    ...tints("gitDecoration.deletedResourceForeground", "terminal.ansiRed", REMOVED),
  ];
  const fix = (rule: Setting): Setting => rule.settings?.foreground
    ? { ...rule, settings: { ...rule.settings, foreground: readableOn(rule.settings.foreground, backgrounds, dark) } }
    : rule;
  for (const key of ["editor.foreground", "foreground", "editorLineNumber.foreground", "editorLineNumber.activeForeground"]) {
    if (colors[key]) colors[key] = readableOn(colors[key], backgrounds, dark);
  }
  return {
    ...base,
    name,
    colors,
    fg: base.fg ? readableOn(base.fg, backgrounds, dark) : base.fg,
    settings: (base.settings as Setting[] | undefined)?.map(fix) as Resolved["settings"],
    tokenColors: (base.tokenColors as Setting[] | undefined)?.map(fix) as Resolved["tokenColors"],
  };
}

const registered = new Set<string>();
/** The AA variant's name for a Pierre theme, registered once per page. */
export function aaPierreTheme(baseName: string): string {
  const name = `${baseName}-lc-aa`;
  if (!registered.has(name)) {
    registered.add(name);
    registerCustomTheme(name, async () => aaTheme(await resolveTheme(baseName as Parameters<typeof resolveTheme>[0]), name));
  }
  return name;
}
