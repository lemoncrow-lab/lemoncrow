/** Small, DOM-free code-file language router; no Monaco bundle in this module. */
export type Grammar =
  | "javascript" | "typescript" | "python" | "go" | "json" | "yaml" | "shell"
  | "css" | "html" | "markdown" | "sql" | "cpp" | "rust" | "ini" | "xml" | "dockerfile"
  | "extended";

const COMMON: Readonly<Record<string, Grammar>> = {
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  py: "python", pyi: "python", go: "go", json: "json", jsonc: "json", yaml: "yaml", yml: "yaml",
  sh: "shell", bash: "shell", zsh: "shell", fish: "shell", css: "css", html: "html", htm: "html",
  md: "markdown", markdown: "markdown", sql: "sql", c: "cpp", h: "cpp", cc: "cpp", cpp: "cpp",
  cxx: "cpp", hpp: "cpp", hxx: "cpp", rs: "rust", toml: "ini", ini: "ini", xml: "xml", svg: "xml",
};
const EXTENDED = new Set([
  "rb", "rake", "php", "java", "kt", "kts", "scala", "sc", "clj", "cljs", "ex", "exs", "erl",
  "hs", "swift", "dart", "lua", "pl", "pm", "r", "jl", "cs", "fs", "fsx", "fsproj",
  "scss", "less", "graphql", "gql", "proto", "tf", "tfvars", "hcl", "vue", "svelte",
  "ps1", "bat", "cmd", "sqlx", "coffee", "cob", "v", "vhd", "vhdl", "vb", "sol",
  "mdx", "rst", "ex", "razor", "pug", "liquid", "psm1", "ipynb", "dockerignore",
]);

export function grammarForPath(path: string): Grammar | null {
  const name = path.replaceAll("\\", "/").split("/").pop()?.toLowerCase() ?? "";
  if (name === "dockerfile" || name.endsWith(".dockerfile")) return "dockerfile";
  if (name === "makefile" || name === "gemfile" || name === "rakefile") return "extended";
  const ext = name.split(".").pop() ?? "";
  return COMMON[ext] ?? (EXTENDED.has(ext) ? "extended" : null);
}
