#!/usr/bin/env node
// Portable script to sync docs/ content into Docusaurus docs directory.
// No external dependencies — pure Node.js.
import { copyFileSync, existsSync, mkdirSync, readdirSync, statSync } from "fs";
import { join, relative } from "path";

const srcRoot = new URL("../docs", import.meta.url).pathname;
const dstRoot = new URL("docs", import.meta.url).pathname;

const extensions = new Set([".md", ".png", ".svg", ".jpg", ".jpeg"]);

function shouldCopy(name) {
  const ext = name.slice(name.lastIndexOf(".")).toLowerCase();
  return extensions.has(ext);
}

function walk(dir) {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir)) {
    const src = join(dir, entry);
    const stat = statSync(src);
    if (stat.isDirectory()) {
      walk(src);
    } else if (shouldCopy(entry)) {
      const rel = relative(srcRoot, src);
      const dst = join(dstRoot, rel);
      mkdirSync(new URL(".", new URL(dst, "file://")).pathname, { recursive: true });
      copyFileSync(src, dst);
    }
  }
}

console.log(`Syncing docs from ${srcRoot} → ${dstRoot}`);
walk(srcRoot);
console.log("Done.");
