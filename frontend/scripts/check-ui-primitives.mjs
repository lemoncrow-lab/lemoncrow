import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import { designDrift } from "./sync-design.mjs";

const root = path.resolve(process.cwd(), "src");
const uiRoot = path.join(root, "components", "ui") + path.sep;
const reviewRoot = path.join(root, "review") + path.sep;
const forbidden = ["button", "input", "textarea", "select", "option"];
const pattern = new RegExp(`<(?:${forbidden.join("|")})\\b`, "g");
const workbenchStyleRules = [
  { pattern: /rounded-(?:xl|2xl|3xl)/g, message: "Use rounded-lg panels or rounded-md controls; larger radii are Review/content-specific." },
  { pattern: /rounded-none/g, message: "Use the radius scale: rounded-lg panels, rounded-md controls, rounded-sm badges and chips." },
  { pattern: /\bp-(?:5|8|10|12|16|20|24)\b/g, message: "Use the Reader density scale (normally p-3/p-4; p-6 is reserved for compact empty states)." },
  { pattern: /(?:slate|gray|zinc|stone)-\d{2,3}/g, message: "Use neutral-* theme tokens so workbench chrome follows light/dark mode." },
  { pattern: /bg-\[#[0-9a-fA-F]{3,8}\]/g, message: "Use neutral/surface theme tokens instead of hard-coded background colors." },
];
// Apply everywhere outside components/ui (Review included): these are the shadcn-migration invariants.
const systemRules = [
  { pattern: /role="dialog"|aria-modal=/g, message: "Use Dialog/Sheet from components/ui; do not hand-roll modals." },
  { pattern: /data-review-dropdown/g, message: "Use DropdownMenu (actions) or Popover (content) from components/ui." },
  { pattern: /\btext-\[\d+(?:\.\d+)?(?:px|rem|em)\]/g, message: "Use the type scale: text-micro/caption/label/body/heading/title (or tag/meta/ui/small/copy/lead/h3/h2/display/code)." },
  { pattern: /\btext-(?:xs|sm|base|lg|xl|[2-9]xl)\b/g, message: "Tailwind's default sizes are retired: use text-meta/ui/small/copy/lead or the app aliases text-label/body/heading/title." },
  { pattern: /\bz-\[\d+\]/g, message: "Overlays portal at z-50; use z-10/z-20 for in-flow stacking." },
  { pattern: /\bvariant="default"/g, message: "Button has no default variant any more: use brand (the one primary action of a view), solid, outline or ghost." },
  { pattern: /(?<!disabled:)\btext-neutral-(?:600|700)\b/g, message: "Below WCAG AA contrast for text; use text-muted-foreground." },
];
const violations = [];
const styleViolations = [];

// Design-token drift (design/README.md). These run over every .ts, .tsx and .css file outside src/design/ and
// tests: colours come from the tokens, not from hex or raw palette utilities, and the type, weight and
// elevation scales are closed.
const HUE_UTILITY = /\b(?:text|bg|border|ring|divide|fill|stroke|from|to|via|outline|accent|decoration|placeholder|caret|shadow)-(?:rose|sky|blue|teal|orange|yellow|green|indigo|fuchsia|slate|gray|zinc|stone|purple|pink|lime)-\d/;
const tokenRules = [
  { id: "hex", pattern: /(?<![\w&#-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b(?![\w-])/g, message: "Use a colour token (bg-card, text-subtle, text-p0, rgb(var(--border))), not a hex colour." },
  { id: "rgb", pattern: /\brgba?\(\s*\d/g, message: "Use a colour token (rgb(var(--border) / .5)), not a literal rgb() colour." },
  { id: "arbitrary", pattern: /\b(?:text|rounded|shadow|tracking)-\[/g, message: "No arbitrary text, radius, shadow or tracking values: use the scales (text-ui, rounded-lg, shadow-float, tracking-tag)." },
  { id: "shadow", pattern: /(?<![\w:-])shadow-(?:sm|md|lg|xl|2xl|inner|black)\b/g, message: "Two shadows exist: shadow-float (overlays) and shadow-frame (the hero artefact). Cards have none." },
  { id: "weight", pattern: /\bfont-(?:bold|extrabold|black)\b/g, message: "Weight 700 is retired: headings are font-semibold, labels font-medium." },
  { id: "neutral", pattern: /\b(?:text|bg|border|ring|divide|fill|stroke|from|to|via|outline)-neutral-\d/g, message: "Raw neutral-* is retired: use foreground, subtle, muted-foreground, border, input, muted, accent or background." },
  { id: "hue", pattern: HUE_UTILITY, message: "Only red, amber, cyan, emerald and violet ramps exist (P0, P1, P2, ok, ai); use those or the p0/p1/p2/ok/ai tokens." },
];
// One icon scale (spec 3.8): lucide icons are 14px (dense rows, inline with 12-13px text), 16px (the default) or 20px
// (empty states, feature headings); file-type glyphs are fixed at 14px. The 1.75 stroke is global (src/index.css).
const iconRules = [
  { id: "icon-size", pattern: /\bsize=\{(?!(?:14|16|20)\})\d+\}/g, message: "Icons use the three-step scale: size={14} (dense rows, inline with 12-13px text), size={16} (default) or size={20} (empty states, feature headings)." },
  { id: "icon-stroke", pattern: /\bsize=\{\d+\}[^\n]*\bstrokeWidth=\{|\bstrokeWidth=\{[^\n]*\bsize=\{\d+\}/g, message: "Stroke weight is global (1.75, src/index.css): do not set strokeWidth on an icon." },
];
// Content and theme files whose colours are data, not chrome: editor and markdown themes, graph canvases, chart
// series and the per-agent palette. Everything else must use tokens.
const tokenAllowlist = new Map([
  ["components/code/monaco.ts", ["hex"]],
  ["components/code/ReadOnlyCodeViewer.tsx", ["rgb"]],
  ["review/EditableCodeEditor.tsx", ["hex", "rgb"]],
  ["review/MarkdownReviewPreview.css", ["hex", "rgb"]],
  ["review/cssColorSwatches.ts", ["rgb"]],
  ["lib/graphColors.ts", ["hex"]],
  ["lib/agentColors.ts", ["hue"]],
  ["code/CodeGraph2D.tsx", ["hex", "rgb"]],
  ["code/CodeGraph3D.tsx", ["hex"]],
  ["code/CodeTopologyView.tsx", ["hex"]],
  ["pages/Telemetry.tsx", ["hex"]],
  ["pages/Savings.tsx", ["hex"]],
  // Hosted login: the GitHub and Google brand marks keep their own fills.
  ["../../enterprise/frontend/src/HostedApp.tsx", ["hex"]],
]);

async function walk(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      await walk(full);
      continue;
    }
    if (!entry.isFile() || !entry.name.endsWith(".tsx") || full.startsWith(uiRoot)) continue;
    const source = await readFile(full, "utf8");
    const lines = source.split("\n");
    lines.forEach((line, index) => {
      if (!entry.name.endsWith(".test.tsx") && pattern.test(line)) {
        violations.push(`${path.relative(process.cwd(), full)}:${index + 1}: ${line.trim()}`);
      }
      pattern.lastIndex = 0;

      if (!entry.name.endsWith(".test.tsx")) {
        for (const rule of systemRules) {
          if (rule.pattern.test(line)) {
            styleViolations.push(`${path.relative(process.cwd(), full)}:${index + 1}: ${rule.message}\n  ${line.trim()}`);
          }
          rule.pattern.lastIndex = 0;
        }
      }

      if (!full.startsWith(reviewRoot) && !entry.name.endsWith(".test.tsx")) {
        for (const rule of workbenchStyleRules) {
          if (rule.pattern.test(line)) {
            styleViolations.push(`${path.relative(process.cwd(), full)}:${index + 1}: ${rule.message}\n  ${line.trim()}`);
          }
          rule.pattern.lastIndex = 0;
        }
      }
    });
  }
}

await walk(root);

async function walkTokens(dir, rules = tokenRules) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "design" || entry.name === "__snapshots__") continue;
      await walkTokens(full, rules);
      continue;
    }
    if (!entry.isFile() || !/\.(?:tsx?|css)$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name) || entry.name.endsWith(".d.ts")) continue;
    const relative = path.relative(root, full).split(path.sep).join("/");
    const allowed = new Set(tokenAllowlist.get(relative) ?? []);
    const lines = (await readFile(full, "utf8")).split("\n");
    lines.forEach((line, index) => {
      if (/^\s*(?:\/\/|\*|\/\*)/.test(line)) return; // comments may name the retired values
      for (const rule of rules) {
        if (rule.id && allowed.has(rule.id)) continue;
        rule.pattern.lastIndex = 0;
        if (rule.pattern.test(line)) styleViolations.push(`${path.relative(process.cwd(), full)}:${index + 1}: ${rule.message}\n  ${line.trim()}`);
        rule.pattern.lastIndex = 0;
      }
    });
  }
}

await walkTokens(root, [...tokenRules, ...iconRules]);

// The hosted app (enterprise/frontend, private checkout only) renders these primitives and shares the theme, so
// its sources follow the same token, type and variant rules. Absent in the public tree: skipped.
const hostedRoot = path.resolve(root, "..", "..", "enterprise", "frontend", "src");
if (await stat(hostedRoot).then((info) => info.isDirectory(), () => false)) await walkTokens(hostedRoot, [...tokenRules, ...systemRules]);

// src/design/ is a vendored copy of the repository's design/ directory (design/README.md).
const drift = await designDrift();
if (drift && drift.length) {
  console.error(`src/design/ drifted from the canonical design/ directory (run \`npm run sync:design\`):\n  ${drift.join("\n  ")}`);
  process.exit(1);
}

if (violations.length) {
  console.error("Native interactive controls are only allowed in src/components/ui/*. Use LemonCrow UI primitives instead:\n");
  console.error(violations.join("\n"));
  process.exit(1);
}

if (styleViolations.length) {
  console.error("UI drifted from the design system (see DESIGN_GUIDE.md):\n");
  console.error(styleViolations.join("\n"));
  process.exit(1);
}

console.log("UI guard: shared controls are centralized and workbench styling follows the Review/Reader system.");
