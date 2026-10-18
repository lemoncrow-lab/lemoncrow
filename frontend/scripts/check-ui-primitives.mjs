import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const root = path.resolve(process.cwd(), "src");
const uiRoot = path.join(root, "components", "ui") + path.sep;
const reviewRoot = path.join(root, "review") + path.sep;
const forbidden = ["button", "input", "textarea", "select", "option"];
const pattern = new RegExp(`<(?:${forbidden.join("|")})\\b`, "g");
const workbenchStyleRules = [
  { pattern: /rounded-(?:xl|2xl|3xl)/g, message: "Use rounded-lg panels or rounded-md controls; larger radii are Review/content-specific." },
  { pattern: /rounded-(?:sm|none)/g, message: "Use the Reader radius scale: rounded-lg panels and rounded-md controls." },
  { pattern: /\bp-(?:5|8|10|12|16|20|24)\b/g, message: "Use the Reader density scale (normally p-3/p-4; p-6 is reserved for compact empty states)." },
  { pattern: /(?:slate|gray|zinc|stone)-\d{2,3}/g, message: "Use neutral-* theme tokens so workbench chrome follows light/dark mode." },
  { pattern: /bg-\[#[0-9a-fA-F]{3,8}\]/g, message: "Use neutral/surface theme tokens instead of hard-coded background colors." },
];
// Apply everywhere outside components/ui (Review included): these are the shadcn-migration invariants.
const systemRules = [
  { pattern: /role="dialog"|aria-modal=/g, message: "Use Dialog/Sheet from components/ui; do not hand-roll modals." },
  { pattern: /data-review-dropdown/g, message: "Use DropdownMenu (actions) or Popover (content) from components/ui." },
  { pattern: /\btext-\[\d+(?:\.\d+)?px\]/g, message: "Use the type scale: text-micro/caption/label/body/heading/title." },
  { pattern: /\bz-\[\d+\]/g, message: "Overlays portal at z-50; use z-10/z-20 for in-flow stacking." },
  { pattern: /(?<!disabled:)\btext-neutral-(?:600|700)\b/g, message: "Below WCAG AA contrast for text; use text-muted-foreground." },
];
const violations = [];
const styleViolations = [];

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
