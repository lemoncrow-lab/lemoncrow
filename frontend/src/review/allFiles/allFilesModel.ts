/**
 * All files grouping and unaffected source context. Only the Review projection
 * may classify a file as generated; a filename heuristic marks known lockfile
 * formats, never a safety verdict. Caller sites do not grant review decisions.
 */
import type { ReviewConsumerSite, ReviewMarkEvent, ReviewProjection, ReviewTarget } from "../types";

const KNOWN_LOCKFILE = /(^|\/)(?:package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|bun\.lockb?|poetry\.lock|uv\.lock|Pipfile\.lock|composer\.lock|Podfile\.lock)$/i;
const MINIFIED = /\.min\.(?:js|css)$/i;

export function generatedPaths(targets: readonly ReviewTarget[], projection: ReviewProjection | null): Set<string> {
  const files = new Map((projection?.material?.files ?? []).map((file) => [file.path, file]));
  const protectedPaths = new Set(targets.filter((target) => target.answer?.category === "decision" || target.priority?.review_first).map((target) => target.path));
  return new Set(targets.filter((target) => {
    if (protectedPaths.has(target.path)) return false; // Never hide a human decision by default.
    const file = files.get(target.path);
    return file
      ? file.role === "generated" || (file.collapsed_default && (KNOWN_LOCKFILE.test(target.path) || MINIFIED.test(target.path)))
      : KNOWN_LOCKFILE.test(target.path) || MINIFIED.test(target.path);
  }).map((target) => target.path));
}

export type AllFilesGroupLabel = "Decisions" | "Quick check" | "Accepted by rule";

/** The group that starts folded: parts a repository rule accepted are not work, so they never sit among the ones that are. */
export const FOLDED_GROUP: AllFilesGroupLabel = "Accepted by rule";

/**
 * The groups of the Files list and the stream. Every part the server accepted by rule (generated files, lockfiles) is in
 * the one folded "Accepted by rule" group, whatever its name. Nothing else is folded: a quick check the server asks is never
 * hidden by a file name (a lockfile or minified path is a guess, not a verdict), so it cannot be left unanswered out of sight.
 */
export function allFilesGroups(
  decisions: readonly ReviewTarget[], quick: readonly ReviewTarget[], accepted: readonly ReviewTarget[],
): readonly (readonly [AllFilesGroupLabel, readonly ReviewTarget[]])[] {
  return [
    ["Decisions", decisions],
    ["Quick check", quick],
    ["Accepted by rule", accepted],
  ];
}

/** "Accepted by rule · 12 files": the folded group counts files, as a rule accepts a file. */
export function foldedGroupLabel(parts: readonly Pick<ReviewTarget, "path">[]): string {
  const files = new Set(parts.map((part) => part.path)).size;
  return `Accepted by rule · ${files} ${files === 1 ? "file" : "files"}`;
}

export function materialLabel(path: string, projection: ReviewProjection | null): string {
  const file = projection?.material?.files.find((item) => item.path === path);
  if (file?.role === "generated") return "Generated file · classified by Review";
  if (KNOWN_LOCKFILE.test(path)) return "Lockfile · identified by filename";
  if (MINIFIED.test(path)) return "Minified asset · identified by filename";
  return file?.role ? file.role.replaceAll("_", " ") : "";
}

export interface AffectedSite {
  key: string;
  path: string;
  line: number;
  symbol: string;
  snippet: string;
  kind: ReviewConsumerSite["kind"];
  exactness: string;
  uncertainty: string;
  inspectedByAgent: boolean | null;
  changedTargetId: string | null;
  changedTargetLabel: string;
}

/** Read-only, unchanged affected sites. This is not a zero/complete coverage estimate. */
export function affectedSites(targets: readonly ReviewTarget[], projection: ReviewProjection | null): AffectedSite[] {
  const changed = new Set(targets.map((target) => target.path));
  const seen = new Set<string>();
  const output: AffectedSite[] = [];
  for (const group of projection?.context?.consumers ?? []) {
    const owner = targets.find((target) => group.target_ids.includes(target.target_id))
      ?? targets.find((target) => target.path === group.path && (!group.symbol || target.symbol === group.symbol));
    for (const site of group.sites) {
      if (site.in_patch || changed.has(site.path) || !site.path) continue;
      const key = `${site.path}\u0000${site.line}\u0000${site.symbol}\u0000${site.kind}`;
      if (seen.has(key)) continue;
      seen.add(key);
      output.push({
        key, path: site.path, line: site.line, symbol: site.symbol,
        snippet: site.snippet, kind: site.kind, exactness: site.exactness,
        uncertainty: site.uncertainty, inspectedByAgent: site.inspected_by_agent ?? null,
        changedTargetId: owner?.target_id ?? null,
        changedTargetLabel: owner?.symbol || owner?.label || group.symbol || group.path,
      });
    }
  }
  return output.sort((a, b) => a.path.localeCompare(b.path) || a.line - b.line || a.key.localeCompare(b.key));
}

export function verifiedSite(site: Pick<AffectedSite, "exactness" | "uncertainty">): boolean {
  return site.exactness === "snapshot" && site.uncertainty === "";
}

/**
 * The one evidence chip a site may carry (SPEC 2): "Checked in code", or "Not verified · <reason>".
 * The reason is the server's own limitation when it gave one, else the plain fact about where the site came from.
 */
export function siteEvidenceLabel(site: Pick<AffectedSite, "exactness" | "uncertainty">): string {
  if (verifiedSite(site)) return "Checked in code";
  const reason = site.uncertainty.trim()
    || (site.exactness === "current_index" ? "found in the current code index, not in this revision" : "the location was not confirmed");
  return `Not verified · ${reason}`;
}

/** Callers the server lists inside files this change touches: not shown as unchanged code, so they are counted. */
export function callersInChangedFiles(targets: readonly ReviewTarget[], projection: ReviewProjection | null): number {
  const changed = new Set(targets.map((target) => target.path));
  const found = new Set<string>();
  for (const group of projection?.context?.consumers ?? []) {
    for (const site of group.sites) {
      if (site.path && (site.in_patch || changed.has(site.path))) found.add(`${site.path}\u0000${site.line}\u0000${site.symbol}\u0000${site.kind}`);
    }
  }
  return found.size;
}

const ANSWER_WORDS: Readonly<Record<string, string>> = {
  reviewed: "Looks good",
  needs_changes: "Needs a change",
  later: "Left for later",
};
const answerWord = (state: string) => ANSWER_WORDS[state] ?? "No answer";

/** One plain sentence for a stored answer event; never the stored event kind or state name. */
export function markEventSentence(event: Pick<ReviewMarkEvent, "event_kind" | "from_state" | "to_state">): string {
  switch (event.event_kind) {
    case "judgment":
      return event.from_state && event.from_state !== event.to_state
        ? `Answer changed from ${answerWord(event.from_state)} to ${answerWord(event.to_state)}`
        : `Answered ${answerWord(event.to_state)}`;
    case "reconciled":
      return `Answer ${answerWord(event.to_state)} carried over to a newer revision`;
    case "discarded":
      return "Answer dropped because the code changed";
    case "migrated":
      return "Answer moved with the part";
    default:
      return "Answer updated";
  }
}

const NOTE_STATE_WORDS: Readonly<Record<string, string>> = {
  open: "still open",
  resolved: "resolved",
  orphaned: "its lines moved or went away",
  obsolete: "no longer applies",
};
export const noteStateWord = (state: string): string => NOTE_STATE_WORDS[state] ?? "state not recorded";
