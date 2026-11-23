#!/usr/bin/env node
// Copy the public pages of docs/ into the Docusaurus content directory.
// No external dependencies: pure Node.js.
//
// docs/ also holds internal material (planning/, superpowers/, usability/,
// operations/, marketing/, reports/, status/). Only the pages listed in
// PUBLIC_DOCS are published. A new page stays private until it is added here
// (and to sidebars.ts). `node sync-docs.mjs --check` verifies the copy.
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "fs";
import { dirname, join, posix, relative, resolve, sep } from "path";
import { fileURLToPath } from "url";

const here = dirname(fileURLToPath(import.meta.url));

// Public pages, relative to docs/, without the .md extension.
export const PUBLIC_DOCS = [
  "README",
  "setup/installation",
  "setup/ai-review",
  "setup/workspaces-lite-quickstart",
  "setup/pro-hosted-runtime",
  "setup/github-action",
  "setup/web-qa",
  "setup/proof",
  "setup/production-watch",
  "setup/importing-review-rules",
  "setup/hosted-review",
  "setup/privacy",
  "setup/troubleshooting",
  "hosts/all-agent-clis",
  "hosts/host-capability-matrix",
  "hosts/claude-code-install",
  "hosts/codex-install",
  "hosts/codex-capabilities",
  "hosts/copilot-install",
  "hosts/antigravity-install",
  "hosts/opencode-install",
  "hosts/cursor-install",
  "hosts/hermes-install",
  "hosts/pi-install",
  "sdk/python",
  "sdk/mcp",
  "reference/cli",
  "reference/review",
  "reference/review-walkthrough",
  "reference/architecture",
  "reference/environment",
  "reference/support-matrix",
  "reference/review-preview-plugins",
  "architecture/runtime-lite",
  "architecture/oss-private-code-intelligence",
  "architecture/review-authority-map",
  "architecture/workspaces-and-review",
  "benchmarks/harbor-eval",
  "benchmarks/mini-eval",
  "benchmarks/results",
  "legal/licensing",
  "legal/licensing-report",
  "legal/dependency-licenses",
  // The two user-facing roadmaps and the self-hosting notes are public on
  // docs.lemoncrow.com (by link; they are not in the sidebar); the rest of
  // planning/ and operations/ is internal.
  // tests/test_docs_site_public_allowlist.py fails on any other internal page.
  "planning/roadmap",
  "planning/savings-optimization-roadmap",
  "operations/production-readiness",
];

function listFiles(dir) {
  if (!existsSync(dir)) return [];
  const out = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...listFiles(path));
    else out.push(path);
  }
  return out;
}

const REPO_BLOB = "https://github.com/lemoncrow-lab/lemoncrow/blob/main/";

// A link that leaves docs/ (../../LICENSE, ../../src/...) has no page on the
// site; point it at the file on GitHub instead.
export function rewriteRepoLinks(page, text) {
  return text.replace(/\]\((\.\.?\/[^)\s]*)\)/g, (match, target) => {
    const resolved = posix.normalize(posix.join("docs", posix.dirname(page), target));
    if (resolved.startsWith("docs/")) return match;
    return `](${REPO_BLOB}${resolved})`;
  });
}

export function sync(srcRoot, dstRoot) {
  // The destination is a generated, gitignored copy. Start it empty so a page that
  // moved out of docs/ cannot survive as a stale copy and hide a broken sidebar entry.
  rmSync(dstRoot, { recursive: true, force: true });
  for (const page of PUBLIC_DOCS) {
    const src = join(srcRoot, `${page}.md`);
    if (!existsSync(src)) throw new Error(`public page missing from docs/: ${page}.md`);
    const dst = join(dstRoot, `${page}.md`);
    mkdirSync(dirname(dst), { recursive: true });
    writeFileSync(dst, rewriteRepoLinks(page, readFileSync(src, "utf8")));
  }
}

// Return the problems with a synced copy: files outside the allowlist and
// sidebar ids that are not published.
export function check(dstRoot, sidebarsPath = join(here, "sidebars.ts")) {
  const problems = [];
  const allowed = new Set(PUBLIC_DOCS.map((page) => `${page}.md`));
  for (const file of listFiles(dstRoot)) {
    const rel = relative(dstRoot, file).split(sep).join("/");
    if (!allowed.has(rel)) problems.push(`not in the public allowlist: ${rel}`);
  }
  const sidebars = readFileSync(sidebarsPath, "utf8");
  for (const [, id] of sidebars.matchAll(/id:\s*"([^"]+)"/g)) {
    if (!PUBLIC_DOCS.includes(id)) problems.push(`sidebar id is not published: ${id}`);
  }
  return problems;
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const outIndex = args.indexOf("--out");
  const srcRoot = join(here, "..", "docs");
  const dstRoot = outIndex >= 0 ? resolve(args[outIndex + 1]) : join(here, "docs");
  console.log(`Syncing public docs from ${srcRoot} to ${dstRoot}`);
  sync(srcRoot, dstRoot);
  const problems = check(dstRoot);
  if (problems.length) {
    for (const problem of problems) console.error(problem);
    process.exit(1);
  }
  console.log(`Done: ${PUBLIC_DOCS.length} pages.`);
}
