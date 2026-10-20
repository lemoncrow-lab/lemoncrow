// Vendors the canonical design/ directory (repo root) into src/design/.
//
//   node scripts/sync-design.mjs          copy design/ -> src/design/
//   node scripts/sync-design.mjs --check  fail when src/design/ differs from design/
//
// The app imports its tokens, preset and fonts from src/design/, never from ../design:
// the Tailwind preset needs frontend/node_modules to resolve `tailwindcss`, the Vite dev
// servers of the app and of enterprise/frontend only serve files under src/, and the
// build staleness checks watch frontend/src. design/README.md has the full reasoning.
import { cp, mkdir, readdir, readFile, rm, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const canonical = path.resolve(frontendRoot, "..", "design");
const vendored = path.join(frontendRoot, "src", "design");
const SKIP = new Set(["README.md"]);

async function exists(target) {
  try {
    await stat(target);
    return true;
  } catch {
    return false;
  }
}

async function listFiles(root, dir = root) {
  const files = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (SKIP.has(entry.name) && dir === root) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) files.push(...(await listFiles(root, full)));
    else files.push(path.relative(root, full));
  }
  return files.sort();
}

/** Returns the drift between design/ and src/design/ as human-readable lines. */
export async function designDrift() {
  if (!(await exists(canonical))) return null; // built outside the repository: nothing to compare against
  const wanted = await listFiles(canonical);
  const have = (await exists(vendored)) ? await listFiles(vendored) : [];
  const drift = [];
  for (const file of wanted) {
    if (!have.includes(file)) drift.push(`missing in src/design: ${file}`);
    else if (!(await readFile(path.join(canonical, file))).equals(await readFile(path.join(vendored, file)))) drift.push(`differs: ${file}`);
  }
  for (const file of have) if (!wanted.includes(file)) drift.push(`not in design/: ${file}`);
  return drift;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes("--check")) {
    const drift = await designDrift();
    if (drift && drift.length) {
      console.error(`src/design/ drifted from the canonical design/ directory (run \`npm run sync:design\`):\n  ${drift.join("\n  ")}`);
      process.exit(1);
    }
    console.log(drift === null ? "design sync: no ../design directory, skipped." : "design sync: src/design matches design/.");
  } else {
    if (!(await exists(canonical))) {
      console.error(`${canonical} does not exist`);
      process.exit(1);
    }
    await rm(vendored, { recursive: true, force: true });
    await mkdir(vendored, { recursive: true });
    for (const file of await listFiles(canonical)) {
      await mkdir(path.dirname(path.join(vendored, file)), { recursive: true });
      await cp(path.join(canonical, file), path.join(vendored, file));
    }
    console.log(`design sync: copied ${canonical} -> ${vendored}`);
  }
}
