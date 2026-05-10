import { spawnSync } from "node:child_process";

const forwarded = process.argv
    .slice(2)
    .filter((arg) => arg !== "--watchAll=false" && arg !== "--watchAll" && arg !== "false");

const hasParallelismFlag = forwarded.some(
    (arg) => arg === "--no-file-parallelism" || arg === "--fileParallelism",
);

const args = ["vitest", "run", ...(hasParallelismFlag ? [] : ["--no-file-parallelism"]), ...forwarded];
const result = spawnSync("npx", args, { stdio: "inherit" });
process.exit(result.status ?? 1);
