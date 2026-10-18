/** Shared presentation helpers for Development surfaces (sheet and file drawer). */

const HOST_LABELS: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  opencode: "OpenCode",
  lemoncode: "LemonCode",
  cursor: "Cursor",
  copilot: "Copilot",
  gemini: "Gemini",
  hermes: "Hermes",
  antigravity: "Antigravity",
  pi: "Pi",
  human: "Human",
  ci: "CI",
};

export function hostLabel(host: string): string {
  const key = host.trim().toLowerCase();
  if (HOST_LABELS[key]) return HOST_LABELS[key];
  if (key.startsWith("lemoncrow")) return "LemonCrow";
  if (!key) return "Unknown";
  return key.charAt(0).toUpperCase() + key.slice(1);
}

export const ROLE_LABEL: Record<string, string> = {
  author: "author",
  verifier: "verifier",
  investigator: "supporting",
};

export function shortTime(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function statusTone(status: string): string {
  if (status === "pass") return "text-emerald-300";
  if (status === "fail") return "text-rose-300";
  if (status === "pending" || status === "open") return "text-amber-300";
  return "text-muted-foreground";
}

/** Repository-relative path equality that tolerates a leading ./ or a longer prefix. */
export function samePath(candidate: string, path: string): boolean {
  const normalized = candidate.replace(/\\/g, "/").replace(/^\.\//, "");
  return normalized === path || normalized.endsWith(`/${path}`);
}
