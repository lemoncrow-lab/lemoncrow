/**
 * The organization's reviewer routing document as editable rows (hosted-31.3), and back to the same JSON the server
 * stores: `{automatic?, rules?: [{id, paths, users?, teams?, min_criticality?, automatic?}]}`. The checks mirror the
 * server's `parse_reviewer_rules` so a row says what is wrong before Save; the server still decides.
 */

export const CRITICALITIES = ["critical", "high", "normal", "low"] as const;
export type Criticality = (typeof CRITICALITIES)[number];

export interface RuleRow {
  /** Local row identity only; never saved. */
  key: number;
  id: string;
  /** Comma- or line-separated entries, as typed. */
  paths: string;
  users: string;
  teams: string;
  minCriticality: Criticality | "";
  automatic: boolean;
}

export interface RoutingDraft {
  automatic: boolean;
  /** Whether the stored document named `automatic`, so an unticked box saves `false` rather than dropping it. */
  automaticNamed: boolean;
  rules: RuleRow[];
}

export const MAX_RULES = 128;
const MAX_ENTRIES = 64;
const RULE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const TEAM = /^@?[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/;

let nextKey = 1;

export function emptyRule(existing: readonly RuleRow[] = []): RuleRow {
  const taken = new Set(existing.map((rule) => rule.id));
  let n = existing.length + 1;
  while (taken.has(`rule-${n}`)) n += 1;
  return { key: nextKey++, id: `rule-${n}`, paths: "", users: "", teams: "", minCriticality: "", automatic: false };
}

function entriesText(raw: unknown): string {
  if (typeof raw === "string") return raw;
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string").join(", ") : "";
}

export function entries(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map((item) => item.trim()).filter(Boolean))];
}

export function draftOf(document: Record<string, unknown> | null): RoutingDraft {
  const rules = Array.isArray(document?.rules) ? document.rules : [];
  return {
    automatic: document?.automatic === true,
    automaticNamed: typeof document?.automatic === "boolean",
    rules: rules
      .filter((rule): rule is Record<string, unknown> => Boolean(rule) && typeof rule === "object" && !Array.isArray(rule))
      .map((rule) => ({
        key: nextKey++,
        id: typeof rule.id === "string" ? rule.id : "",
        paths: entriesText(rule.paths),
        users: entriesText(rule.users),
        teams: entriesText(rule.teams),
        minCriticality: CRITICALITIES.includes(rule.min_criticality as Criticality) ? (rule.min_criticality as Criticality) : "",
        automatic: rule.automatic === true,
      })),
  };
}

/** The JSON to save, or `null` (no document) when nothing is set. */
export function documentOf(draft: RoutingDraft): Record<string, unknown> | null {
  if (draft.rules.length === 0 && !draft.automatic && !draft.automaticNamed) return null;
  const document: Record<string, unknown> = {};
  if (draft.automatic || draft.automaticNamed) document.automatic = draft.automatic;
  if (draft.rules.length > 0) {
    document.rules = draft.rules.map((rule) => {
      const out: Record<string, unknown> = { id: rule.id.trim(), paths: entries(rule.paths) };
      const users = entries(rule.users);
      const teams = entries(rule.teams);
      if (users.length > 0) out.users = users;
      if (teams.length > 0) out.teams = teams;
      if (rule.minCriticality) out.min_criticality = rule.minCriticality;
      if (rule.automatic) out.automatic = true;
      return out;
    });
  }
  return document;
}

/** What is wrong with one row, in the page's words; empty when it can be saved. */
export function ruleProblems(rule: RuleRow, all: readonly RuleRow[]): string[] {
  const problems: string[] = [];
  const id = rule.id.trim();
  if (!RULE_ID.test(id)) problems.push("Name: letters, digits, dots, dashes, colons or underscores, starting with a letter or digit (64 at most).");
  else if (all.some((other) => other.key !== rule.key && other.id.trim() === id)) problems.push("Name: another rule already has this name.");
  const paths = entries(rule.paths);
  const users = entries(rule.users);
  const teams = entries(rule.teams);
  if (paths.length === 0) problems.push("Paths: name at least one path or pattern.");
  if (users.length === 0 && teams.length === 0) problems.push("Reviewers: name at least one person or team.");
  if (teams.some((team) => !TEAM.test(team))) problems.push("Teams: write each team as org/team.");
  for (const [label, list] of [["Paths", paths], ["People", users], ["Teams", teams]] as const) {
    if (list.length > MAX_ENTRIES) problems.push(`${label}: ${MAX_ENTRIES} entries at most.`);
    if (list.some((item) => item.length > 256)) problems.push(`${label}: each entry is 256 characters at most.`);
  }
  return problems;
}

export function draftValid(draft: RoutingDraft): boolean {
  return draft.rules.length <= MAX_RULES && draft.rules.every((rule) => ruleProblems(rule, draft.rules).length === 0);
}
