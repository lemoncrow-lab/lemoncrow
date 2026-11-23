// Human labels for internal identifiers the server sends as-is (tool names, enum statuses,
// snake_case ids). The raw value stays available to callers for a CopyId or a title attribute.

const TOOL_NAMES: Record<string, string> = {
  code_search: "Code search",
  read: "Read",
  edit: "Edit",
  write: "Write",
  bash: "Shell",
  web_fetch: "Web fetch",
  web_search: "Web search",
  grep: "Search",
  glob: "Find files",
  todowrite: "Todo list",
  unattributed: "Capture gap",
};

/** "read_db" → "Read db"; "IN_PROGRESS" → "In progress"; "swe.general" → "Swe general". */
export function humanize(value: string): string {
  const text = value
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/[_.\-:]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * A tool call's name for people: MCP wrappers (`mcp__lc__read`, `mcp__plugin_lemoncrow_lc__code_search`,
 * `mcp__github__create_issue`) lose their routing prefix, and known tools get their product name.
 */
export function toolLabel(name: string): string {
  if (!name) return "";
  const parts = name.startsWith("mcp__") ? name.split("__").filter(Boolean) : [name];
  const bare = parts[parts.length - 1] ?? name;
  return TOOL_NAMES[bare.toLowerCase()] ?? humanize(bare);
}

/** An enum status (`IN_PROGRESS`, `not_detected`, `needs-review`) in sentence case. */
export function statusLabel(status: string): string {
  return status ? humanize(status) : "";
}

// Domain bundle ids (src/lemoncrow/core/domains): "<area>.<focus>". The built-in
// bundle is swe.general; user bundles get the area's name and their focus humanized.
const DOMAIN_AREAS: Record<string, string> = { swe: "Software engineering" };

/** "swe.general" → "Software engineering"; "swe.frontend" → "Software engineering · Frontend". */
export function domainLabel(domain: string): string {
  if (!domain) return "";
  const [area, ...rest] = domain.split(".");
  const areaName = DOMAIN_AREAS[area.toLowerCase()];
  if (!areaName) return humanize(domain);
  const focus = rest.join(".");
  return !focus || focus.toLowerCase() === "general" ? areaName : `${areaName} · ${humanize(focus)}`;
}
