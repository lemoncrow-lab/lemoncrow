import type { ReviewConsumerGroup, ReviewConsumerSite, ReviewProjection, ReviewTarget } from "../types";

/**
 * Only groups explicitly attached to this part or file can appear in its Structure drawer. A call site is listed once:
 * where two groups name the same `path:line`, the checked-in-code reading is kept and the other dropped, and a group
 * left with no site is dropped when an earlier group already names the same symbol and file.
 */
export function structureGroups(target: ReviewTarget, projection: ReviewProjection | null): ReviewConsumerGroup[] {
  const groups = (projection?.context?.consumers ?? []).filter((group) =>
    group.target_ids.includes(target.target_id) || (group.join === "file" && group.path === target.path),
  );
  const siteKey = (site: ReviewConsumerSite) => `${site.path}:${site.line}`;
  const best = new Map<string, { group: number; site: number; checked: boolean }>();
  groups.forEach((group, g) => group.sites.forEach((site, s) => {
    const key = siteKey(site);
    const checked = siteProvenance(site).checked;
    const held = best.get(key);
    if (!held || (checked && !held.checked)) best.set(key, { group: g, site: s, checked });
  }));
  const seenHeads = new Set<string>();
  const out: ReviewConsumerGroup[] = [];
  groups.forEach((group, g) => {
    const sites = group.sites.filter((site, s) => {
      const held = best.get(siteKey(site));
      return held?.group === g && held.site === s;
    });
    const head = `${group.symbol}\u0000${group.path}`;
    const dropped = sites.length < group.sites.length;
    if (dropped && sites.length === 0 && seenHeads.has(head)) return;
    seenHeads.add(head);
    out.push(dropped ? { ...group, sites } : group);
  });
  return out;
}

/**
 * Provenance never upgrades a current-index or a name-match candidate into frozen code evidence. There are two
 * chips and no third kind: "Checked in code" or "Not verified · <reason>" (SPEC 5.1).
 */
export function siteProvenance(site: ReviewConsumerSite): { label: string; checked: boolean } {
  if (site.uncertainty?.trim()) return { label: `Not verified · ${site.uncertainty}`, checked: false };
  if (site.exactness === "snapshot") return { label: "Checked in code", checked: true };
  if (site.exactness === "current_index") return { label: "Not verified · read from the current index, not this revision", checked: false };
  return { label: "Not verified · evidence origin unknown", checked: false };
}

export function callerCoverage(group: ReviewConsumerGroup): string {
  if (group.known_callers === null || group.known_callers === undefined) return "Not verified · caller count unavailable";
  if (group.known_callers === 0) return "No caller found in this repository. Callers outside this repository are not checked.";
  return `${group.known_callers} ${group.known_callers === 1 ? "caller" : "callers"} · checked in code`;
}

export function structureLimitations(group: ReviewConsumerGroup): string[] {
  const items: string[] = [];
  if (group.omitted > 0) items.push(`${group.omitted} more sites not shown`);
  if (group.unlisted_callers > 0) items.push(`${group.unlisted_callers} callers counted but not listed`);
  return items;
}
