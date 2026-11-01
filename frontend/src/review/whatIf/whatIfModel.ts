// What-If Impact Explorer (N03): "if this changed target's contract changed in
// this way, which callers, consumers, tests and boundaries might be affected?"
// A read-only exploratory overlay over data the Reader already holds -- the
// projection's `context` consumer inventory (F7), the InvestigationContext's
// checks, and behavior integrations. Nothing here edits source, decisions or the
// graph, fetches, or asks a model. Reach is bounded and deterministic; what it
// cannot see is stated as unknown, never as "no callers".
import { consumersComplete, readinessGaps } from "../investigation/impactReadiness";
import type { InvestigationCheckStatus, InvestigationContext, RelationshipProof } from "../investigation/investigationContext";
import type { ReviewConsumerGroup, ReviewConsumerSite, ReviewProjection, ReviewTarget } from "../types";

export type WhatIfScenarioKey =
  | "returns_nullable"
  | "signature_changes"
  | "raises_error"
  | "becomes_async"
  | "field_removed"
  | "value_changes"
  | "removed";

export interface WhatIfScenario {
  key: WhatIfScenarioKey;
  label: string;
  /** What the hypothetical would ask of an unchanged caller, in words. */
  effect: string;
}

const SCENARIOS: Record<WhatIfScenarioKey, WhatIfScenario> = {
  returns_nullable: { key: "returns_nullable", label: "Return value becomes nullable", effect: "would need to handle a missing (null/None) result" },
  signature_changes: { key: "signature_changes", label: "A parameter is removed or the signature changes", effect: "would need its call updated to the new signature" },
  raises_error: { key: "raises_error", label: "It raises or throws a new error", effect: "would need to catch or propagate the new error" },
  becomes_async: { key: "becomes_async", label: "It becomes async", effect: "would need to await the result" },
  field_removed: { key: "field_removed", label: "A field or member is removed", effect: "would break where it reads that field" },
  value_changes: { key: "value_changes", label: "Its value or shape changes", effect: "would read a different value" },
  removed: { key: "removed", label: "It is removed", effect: "would no longer resolve" },
};

function lastSegment(symbol: string): string {
  const parts = symbol.split(/[.:#·]/).map((part) => part.trim()).filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

/** The fixed counterfactual menu for one target; depends only on its kind and name. */
export function scenariosFor(target: Pick<ReviewTarget, "kind" | "symbol">): WhatIfScenario[] {
  const keys: WhatIfScenarioKey[] = (() => {
    switch (target.kind) {
      case "symbol":
        // A capitalised name is a class or type by convention: its contract is its members.
        return /^[A-Z]/.test(lastSegment(target.symbol))
          ? ["field_removed", "signature_changes", "removed"]
          : ["returns_nullable", "signature_changes", "raises_error", "becomes_async", "removed"];
      case "document_section":
        return ["value_changes", "field_removed", "removed"];
      case "dependency":
        return ["signature_changes", "removed"];
      default:
        return ["signature_changes", "value_changes", "removed"];
    }
  })();
  return keys.map((key) => SCENARIOS[key]);
}

export type WhatIfRowKind = "caller" | "consumer" | "test";

/** `actual`: this PR's own change already touches this edge; `simulated`: the hypothetical's effect on unchanged code. */
export type WhatIfOrigin = "actual" | "simulated";

export interface WhatIfCoverage {
  key: string;
  status: InvestigationCheckStatus;
  /** `snapshot` when the check is linked to the target; `inferred` when matched by name only. */
  proof: RelationshipProof;
}

export interface WhatIfRow {
  id: string;
  kind: WhatIfRowKind;
  path: string;
  line: number;
  symbol: string;
  /** Hops from the selected target (1 = direct). */
  depth: number;
  origin: WhatIfOrigin;
  /** False when this PR also changed the consumer. */
  unchanged: boolean;
  exactness: string;
  /** Why this is a candidate rather than a fact; empty when it is a fact. */
  uncertainty: string;
  reason: string;
  /** The changed target covering this row, when it is in the Review; null for code outside the patch. */
  openTargetId: string | null;
  /** The changed target this edge starts from (always in the Review). */
  fromTargetId: string;
  coverage: WhatIfCoverage[];
}

export interface WhatIfGap {
  key: string;
  title: string;
  detail: string;
}

export interface WhatIfModel {
  snapshotIdentity: string;
  targetId: string;
  scenario: WhatIfScenario;
  /** False when the projection carries no consumer inventory at all. */
  measured: boolean;
  rows: WhatIfRow[];
  /** Reachable consumers not listed: past the visit bound, past the packet's per-group bound, or counted but unlisted. */
  omitted: number;
  /** Checks linked to the selected target itself. */
  targetChecks: WhatIfCoverage[];
  /** Explicit unknowns: what this overlay cannot see. */
  gaps: WhatIfGap[];
  /** "No known callers" only when the index is complete; otherwise the headline says the list may be partial. */
  complete: boolean;
}

export interface WhatIfInput {
  /** The Review-subject InvestigationContext: it carries target-linked and Review-level checks alike. */
  context: InvestigationContext;
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
  targetId: string;
  scenario: WhatIfScenarioKey;
}

// lc-debt: fixed fan-in bounds (depth 3, 200 visits); page reach by depth if a Reader needs a wider walk.
export const MAX_DEPTH = 3;
export const MAX_VISITS = 200;

const TEST_PATH = /(^|\/)(tests?|__tests__|spec|e2e)\/|(^|\/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$/;

function stem(path: string): string {
  const base = path.split("/").pop() ?? path;
  return base.replace(/\.[^.]+$/, "").replace(/^test_|_test$|\.(test|spec)$/g, "");
}

/** Groups whose changed symbol a caller site names: the next hop of the walk. */
function groupsCalledBy(site: ReviewConsumerSite, groups: readonly ReviewConsumerGroup[]): ReviewConsumerGroup[] {
  const name = lastSegment(site.symbol);
  if (!name) return [];
  return groups.filter((group) => group.path === site.path && group.symbol === name);
}

function groupsFor(targetId: string, groups: readonly ReviewConsumerGroup[]): ReviewConsumerGroup[] {
  return groups.filter((group) => group.target_ids.includes(targetId));
}

/** Derive the overlay; pure, bounded and deterministic for identical inputs. */
export function whatIfModel(input: WhatIfInput): WhatIfModel {
  const { context, projection, targets, targetId } = input;
  const scenario = SCENARIOS[input.scenario];
  const inventory = projection.context ?? null;
  const target = targets.find((row) => row.target_id === targetId) ?? null;
  const targetsByPath = new Map<string, ReviewTarget[]>();
  for (const row of targets) targetsByPath.set(row.path, [...(targetsByPath.get(row.path) ?? []), row]);

  const targetChecks: WhatIfCoverage[] = context.checks
    .filter((check) => check.target_ids.includes(targetId))
    .map((check) => ({ key: check.key, status: check.status, proof: check.proof }));
  // Review-level checks carry no target link; a name match is the only tie, so it is `inferred`.
  const reviewChecks = context.checks.filter((check) => check.target_ids.length === 0);

  const rows: WhatIfRow[] = [];
  let omitted = 0;
  let unlisted = 0;
  let currentIndexRows = 0;
  let uncertainRows = 0;
  const walked = new Set<string>();
  if (inventory && target) {
    // Breadth-first fan-in from the selected target; a caller that is itself a changed symbol continues the walk.
    let frontier: { group: ReviewConsumerGroup; depth: number }[] = groupsFor(targetId, inventory.consumers).map((group) => ({ group, depth: 1 }));
    const seen = new Set<string>();
    let visits = 0;
    while (frontier.length > 0) {
      const next: typeof frontier = [];
      for (const { group, depth } of frontier) {
        if (walked.has(group.key)) continue;
        walked.add(group.key);
        omitted += group.omitted;
        unlisted += group.unlisted_callers;
        for (const site of group.sites) {
          const caller = site.kind === "caller";
          const where = `${site.path}:${site.line}:${site.symbol || site.snippet}`;
          if (seen.has(where)) continue;
          seen.add(where);
          if (visits >= MAX_VISITS) {
            omitted += 1;
            continue;
          }
          visits += 1;
          const kind: WhatIfRowKind = TEST_PATH.test(site.path) ? "test" : caller ? "caller" : "consumer";
          const origin: WhatIfOrigin = site.in_patch || !caller ? "actual" : "simulated";
          const subject = group.symbol || target.symbol || target.path;
          const reason = origin === "simulated"
            ? `Calls ${subject}; if ${scenario.label.toLowerCase()}, it ${scenario.effect}.`
            : site.in_patch
              ? `Calls ${subject} and was changed in this PR too.`
              : `This PR's actual change reaches it (${site.impact_kind.replaceAll("_", " ")}).`;
          const owner = (targetsByPath.get(site.path) ?? []).find((row) => lastSegment(row.symbol) === lastSegment(site.symbol));
          const name = lastSegment(site.symbol).toLowerCase();
          const fileStem = stem(site.path).toLowerCase();
          const coverage: WhatIfCoverage[] = reviewChecks
            .filter((check) => {
              const key = check.key.toLowerCase();
              return (fileStem.length > 2 && key.includes(fileStem)) || (name.length > 2 && key.includes(name));
            })
            .map((check) => ({ key: check.key, status: check.status, proof: "inferred" as const }));
          if (site.exactness === "current_index") currentIndexRows += 1;
          const uncertainty = site.uncertainty
            || (group.join === "exact" ? "" : "Tied to the changed file, not proven to use this target.");
          if (uncertainty) uncertainRows += 1;
          rows.push({
            id: `${group.key}|${where}`,
            kind,
            path: site.path,
            line: site.line,
            symbol: site.symbol || site.snippet,
            depth,
            origin,
            unchanged: !site.in_patch,
            exactness: site.exactness,
            uncertainty,
            reason,
            openTargetId: owner?.target_id ?? null,
            fromTargetId: (group.join === "exact" && group.target_ids[0]) || target.target_id,
            coverage,
          });
          if (caller && depth < MAX_DEPTH) {
            for (const hop of groupsCalledBy(site, inventory.consumers)) next.push({ group: hop, depth: depth + 1 });
          } else if (caller && groupsCalledBy(site, inventory.consumers).some((hop) => !walked.has(hop.key))) {
            omitted += 1;
          }
        }
      }
      frontier = next;
    }
  }

  const gaps: WhatIfGap[] = [];
  if (!inventory) {
    gaps.push({ key: "inventory", title: "No caller or consumer inventory", detail: "This Review projection carries no impact data, so reach is unknown." });
  } else {
    for (const gap of readinessGaps(inventory.readiness)) {
      if (gap.hidesConsumers) gaps.push({ key: gap.key, title: gap.title, detail: `${gap.detail} Callers may exist that are not listed.` });
    }
  }
  if (unlisted > 0) {
    gaps.push({ key: "unlisted", title: `${unlisted} counted ${unlisted === 1 ? "caller is" : "callers are"} not listed`, detail: "The index counted them, but they are in changed files, share a name with another definition, or are past the search cap." });
  }
  if (uncertainRows > 0) {
    gaps.push({ key: "candidates", title: `${uncertainRows} ${uncertainRows === 1 ? "row is a candidate" : "rows are candidates"}, not a fact`, detail: "Matched by name or only by file; another definition may be the real target." });
  }
  if (currentIndexRows > 0) {
    gaps.push({ key: "current_index", title: `${currentIndexRows} ${currentIndexRows === 1 ? "row comes" : "rows come"} from the working index`, detail: "Not proven for this Snapshot." });
  }
  for (const row of projection.behavior?.targets ?? []) {
    if (!row.target_ids.includes(targetId) || row.unverified_integrations.length === 0) continue;
    gaps.push({ key: `network:${row.behavior_key}`, title: `Crosses ${row.unverified_integrations.join(", ")}`, detail: "Consumers on the other side of this boundary are not in the impact data." });
  }
  // Always true of the impact pass: it resolves callers by name and never through dispatch tables or reflection.
  gaps.push({ key: "dynamic", title: "Dynamic dispatch and reflection not traced", detail: "Callers reached through interfaces, callbacks, reflection or serialized names are unknown." });

  return {
    snapshotIdentity: context.snapshot_identity,
    targetId,
    scenario,
    measured: Boolean(inventory),
    rows,
    omitted,
    targetChecks,
    gaps,
    complete: Boolean(inventory && consumersComplete(inventory.readiness)) && unlisted === 0 && omitted === 0,
  };
}

/** Targets the Brief can offer What if on: changed code with a contract, in queue order. */
export function whatIfTargets(targets: readonly ReviewTarget[]): ReviewTarget[] {
  return targets.filter((target) => target.kind === "symbol" || target.kind === "document_section" || target.kind === "dependency");
}
