// Blind Spots (N05): what this Snapshot has NOT verified, as a bounded,
// deterministic projection over the InvestigationContext and the Review
// projection. Every item comes from a field the server already sent; a category
// whose data the projection does not carry is `not_measured` with its reason,
// never an empty "nothing found". No score, no percentage, no model call, and an
// item that is not proven to belong to one target stays at Review level.
import { readinessGaps } from "../investigation/impactReadiness";
import type { InvestigationContext } from "../investigation/investigationContext";
import type { ReviewProjection, ReviewProjectionDecision, ReviewTarget } from "../types";

export type BlindSpotState = "known" | "nothing_open" | "not_measured" | "not_applicable";

export type BlindSpotCategoryKey =
  | "unexamined_consumers"
  | "untested_hypotheses"
  | "missing_traces"
  | "external_handoffs"
  | "checks"
  | "index_coverage"
  | "uninspected_regions"
  | "unresolved_human"
  | "snapshot_reopened";

export type BlindSpotAction = "open_source" | "see_evidence" | "ask" | "record_decision";

export type BlindSpotAnchor =
  | { level: "target"; targetId: string; path: string; label: string }
  | { level: "review" };

export interface BlindSpotItem {
  /** Stable across renders of the same Snapshot: category, kind and the owning record's id. */
  id: string;
  category: BlindSpotCategoryKey;
  title: string;
  detail: string;
  anchor: BlindSpotAnchor;
  /** The decision this item can be settled through, when one exists. */
  decisionKey: string | null;
  actions: BlindSpotAction[];
}

export interface BlindSpotCategory {
  key: BlindSpotCategoryKey;
  label: string;
  state: BlindSpotState;
  /** What was measured, in words; empty when nothing was. */
  scope: string;
  /** Parts of this category the projection cannot measure, one line each. */
  notMeasured: string[];
  /** Why the category does not apply, when it does not. */
  notApplicable: string;
  items: BlindSpotItem[];
  /** Items beyond the per-category bound, counted, never silently dropped. */
  omitted: number;
}

export interface BlindSpotsModel {
  context: InvestigationContext;
  categories: BlindSpotCategory[];
  counts: { items: number; known: number; nothingOpen: number; notMeasured: number; notApplicable: number };
}

export interface BlindSpotCapabilities {
  openSource: boolean;
  evidence: boolean;
  ask: boolean;
  /** A human decision can be recorded here (a decision write path exists and the Snapshot is current). */
  decide: boolean;
}

export interface BlindSpotsInput {
  context: InvestigationContext;
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
  checkCounts?: { pass: number; fail: number; not_run: number; unknown: number } | null;
  capabilities: BlindSpotCapabilities;
}

// lc-debt: per-category item cap; page items by file when a category needs more than this on screen.
export const MAX_ITEMS_PER_CATEGORY = 200;

const LABEL: Record<BlindSpotCategoryKey, string> = {
  unexamined_consumers: "Unchanged consumers of changed code",
  untested_hypotheses: "Untested claims and hypotheses",
  missing_traces: "Changed code no recorded run reached",
  external_handoffs: "Uncertain external handoffs",
  checks: "Stale or failed checks",
  index_coverage: "Incomplete analysis coverage",
  uninspected_regions: "Important code not yet reviewed",
  unresolved_human: "Unresolved comments and decisions",
  snapshot_reopened: "Reopened by a new Snapshot",
};

const EVIDENCE_CATEGORIES: ReadonlySet<BlindSpotCategoryKey> = new Set([
  "untested_hypotheses",
  "missing_traces",
  "external_handoffs",
  "checks",
]);

const CLAIM_TEXT: Record<string, string> = {
  author_cited: "Cited by the author; no recorded run observed it",
  inferred: "Inferred; no recorded run observed it",
  inferred_failing: "Inferred, and a linked check failed",
  partial: "Only part of it is backed by evidence",
  contradictory: "Linked evidence disagrees",
  none: "No evidence linked",
};

const REOPENED_VALIDITY: ReadonlySet<string> = new Set(["implementation_changed_since_acceptance", "needs_reconsideration"]);

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

interface Draft {
  kind: string;
  ref: string;
  title: string;
  detail: string;
  targetIds?: readonly string[];
  decision?: ReviewProjectionDecision | null;
}

class Builder {
  private readonly byId: ReadonlyMap<string, ReviewTarget>;

  constructor(
    targets: readonly ReviewTarget[],
    private readonly capabilities: BlindSpotCapabilities,
  ) {
    this.byId = new Map(targets.map((target) => [target.target_id, target]));
  }

  category(
    key: BlindSpotCategoryKey,
    drafts: readonly Draft[],
    options: { scope?: string; notMeasured?: string[]; notApplicable?: string; extraOmitted?: number } = {},
  ): BlindSpotCategory {
    const seen = new Set<string>();
    const items: BlindSpotItem[] = [];
    for (const draft of drafts) {
      const id = `${key}:${draft.kind}:${draft.ref}`;
      if (seen.has(id)) continue;
      seen.add(id);
      items.push(this.item(key, id, draft));
    }
    const notMeasured = options.notMeasured ?? [];
    const state: BlindSpotState = items.length > 0
      ? "known"
      : options.notApplicable
        ? "not_applicable"
        : options.scope
          ? "nothing_open"
          : "not_measured";
    return {
      key,
      label: LABEL[key],
      state,
      scope: options.scope ?? "",
      notMeasured,
      notApplicable: options.notApplicable ?? "",
      items: items.slice(0, MAX_ITEMS_PER_CATEGORY),
      omitted: Math.max(0, items.length - MAX_ITEMS_PER_CATEGORY) + (options.extraOmitted ?? 0),
    };
  }

  private item(category: BlindSpotCategoryKey, id: string, draft: Draft): BlindSpotItem {
    // Only a target in this Snapshot's loaded list is an anchor; anything else stays Review-level.
    const target = (draft.targetIds ?? []).map((targetId) => this.byId.get(targetId)).find(Boolean);
    const anchor: BlindSpotAnchor = target
      ? { level: "target", targetId: target.target_id, path: target.path, label: target.symbol ? `${target.path} · ${target.symbol}` : target.path }
      : { level: "review" };
    const decision = draft.decision ?? null;
    const decidable = Boolean(
      decision?.state && !decision.state.withdrawn && (decision.actions ?? []).some((action) => action !== "ask"),
    );
    const actions: BlindSpotAction[] = [];
    if (anchor.level === "target" && this.capabilities.openSource) actions.push("open_source");
    if (EVIDENCE_CATEGORIES.has(category) && this.capabilities.evidence) actions.push("see_evidence");
    if (this.capabilities.ask) actions.push("ask");
    if (decidable && this.capabilities.decide) actions.push("record_decision");
    return {
      id,
      category,
      title: draft.title,
      detail: draft.detail,
      anchor,
      decisionKey: decision?.key ?? null,
      actions,
    };
  }
}

function isOpenDecision(decision: ReviewProjectionDecision): boolean {
  return !decision.state || (!decision.state.withdrawn && decision.state.outstanding);
}

function reopenedDecisions(projection: ReviewProjection): Map<string, ReviewProjectionDecision | null> {
  const out = new Map<string, ReviewProjectionDecision | null>();
  const byKey = new Map(projection.decisions.map((decision) => [decision.key, decision]));
  for (const key of projection.delta?.reopened_decisions ?? []) {
    const decision = byKey.get(key);
    if (!decision?.state?.withdrawn) out.set(key, decision ?? null);
  }
  for (const decision of projection.decisions) {
    if (decision.state?.withdrawn) continue;
    if (decision.stale || REOPENED_VALIDITY.has(decision.state?.validity ?? "")) out.set(decision.key, decision);
  }
  return out;
}

/** Derive the Blind Spots projection; pure, bounded and deterministic for identical inputs. */
export function blindSpotsModel(input: BlindSpotsInput): BlindSpotsModel {
  const { context, projection, targets, checkCounts } = input;
  const build = new Builder(targets, input.capabilities);
  const behavior = projection.behavior ?? null;
  const noTargets = targets.length === 0 && projection.coverage.target_count === 0;
  const reopened = reopenedDecisions(projection);
  const categories: BlindSpotCategory[] = [];

  // 1. Consumers: unchanged callers/consumers of changed targets nobody has reviewed yet.
  const impact = projection.context ?? null;
  if (noTargets) {
    categories.push(build.category("unexamined_consumers", [], { notApplicable: "No changed code targets." }));
  } else if (!impact) {
    categories.push(build.category("unexamined_consumers", [], {
      notMeasured: ["This Review projection carries no caller or consumer inventory for changed targets."],
    }));
  } else {
    const byId = new Map(targets.map((target) => [target.target_id, target]));
    const settled = (ids: readonly string[]) => ids.length > 0 && ids.every((id) => {
      const target = byId.get(id);
      return target?.state === "reviewed" && !target.changed_since_mark;
    });
    const consumers: Draft[] = [];
    let unchanged = 0;
    let omitted = 0;
    let unlisted = 0;
    for (const group of impact.consumers) {
      omitted += group.omitted;
      unlisted += group.unlisted_callers;
      for (const site of group.sites) {
        if (site.in_patch) continue;
        unchanged += 1;
        if (settled(group.target_ids)) continue;
        const where = site.line > 0 ? `${site.path}:${site.line}` : site.path;
        const what = group.symbol ? `${site.kind === "caller" ? "Calls" : "Uses"} ${group.symbol}` : "Depends on the change";
        consumers.push({
          kind: site.kind,
          ref: `${group.key}:${where}:${site.symbol || site.snippet}`,
          title: site.symbol ? `${where} · ${site.symbol}` : where,
          detail: `${what}${group.path ? ` in ${group.path}` : ""}; not changed by this PR${site.uncertainty ? ` · ${site.uncertainty}` : ""}`,
          // A file-level join is not proven to belong to one target, so it stays Review level.
          targetIds: group.join === "exact" || group.target_ids.length === 1 ? group.target_ids : [],
        });
      }
    }
    const gaps = readinessGaps(impact.readiness).filter((gap) => gap.hidesConsumers);
    const notMeasured = gaps.map((gap) => `${gap.title}: no known consumer is not the same as no consumer.`);
    if (unlisted > 0) notMeasured.push(`${plural(unlisted, "caller")} the index counted are not listed (in changed files, same-named, or past the search cap).`);
    if (impact.consumers_omitted > 0) notMeasured.push(`${plural(impact.consumers_omitted, "changed symbol")} with consumers not listed (past the bound).`);
    categories.push(build.category("unexamined_consumers", consumers, {
      scope: consumers.length > 0 || gaps.length === 0
        ? `${plural(unchanged, "unchanged consumer")} recorded by this Snapshot's impact pass`
        : undefined,
      notMeasured,
      extraOmitted: omitted,
    }));
  }

  // 2. Claims not observed, stated limits, and open questions or AI hypotheses.
  const hypotheses: Draft[] = [];
  for (const claim of projection.claims) {
    if (claim.summary === "observed") continue;
    hypotheses.push({
      kind: "claim",
      ref: claim.id,
      title: claim.statement || claim.id,
      detail: claim.summary_text || CLAIM_TEXT[claim.summary] || "Not observed by a recorded run",
      targetIds: claim.target_ids,
    });
  }
  let concernCount = 0;
  for (const item of projection.items) {
    item.concerns.forEach((concern, index) => {
      concernCount += 1;
      hypotheses.push({
        kind: "limit",
        ref: `${item.id}:${index}`,
        title: `Not exercised: ${concern.scope}`,
        detail: concern.reason,
        targetIds: item.primary_target_ids,
      });
    });
  }
  for (const question of projection.questions) {
    hypotheses.push({
      kind: "question",
      ref: question.id,
      title: question.title,
      detail: `${question.origin === "ai" ? "AI hypothesis, unverified" : "Open question"}${question.detail ? ` · ${question.detail}` : ""}`,
      targetIds: question.target_ids,
    });
  }
  categories.push(build.category("untested_hypotheses", hypotheses, {
    scope: `${plural(projection.claims.length, "claim")}, ${plural(concernCount, "stated limit")}, ${plural(projection.questions.length, "open question")}`,
    notMeasured: ["Branch coverage: runs record which targets executed, not which branches."],
  }));

  // 3. Changed code that no recorded run reached.
  if (!behavior) {
    categories.push(build.category("missing_traces", [], {
      notMeasured: ["No execution coverage is recorded for this Snapshot, so whether the change ran is unknown."],
    }));
  } else if (!behavior.execution_ran) {
    categories.push(build.category("missing_traces", [{
      kind: "no_run",
      ref: "snapshot",
      title: "No recorded run on this Snapshot",
      detail: "Nothing in this change was executed by a recorded run.",
    }], { scope: "Recorded runs on this Snapshot" }));
  } else if (!behavior.coverage) {
    categories.push(build.category("missing_traces", [], {
      notMeasured: ["Runs were recorded, but not which changed targets they reached."],
    }));
  } else {
    const coverage = behavior.coverage;
    categories.push(build.category("missing_traces", coverage.never_executed.map((row) => ({
      kind: "never_ran",
      ref: row.target_id || row.path,
      title: `Never executed: ${row.label || row.path}`,
      detail: `No recorded run reached ${row.path}.`,
      targetIds: [row.target_id],
    })), {
      scope: `${coverage.exercised} of ${plural(coverage.changed_targets, "changed target")} ran in ${plural(coverage.runs, "recorded run")}`,
      extraOmitted: Math.max(0, coverage.never_executed_count - coverage.never_executed.length),
    }));
  }

  // 4. External handoffs no evidence verified.
  if (!behavior) {
    categories.push(build.category("external_handoffs", [], {
      notMeasured: ["No behavior or integration evidence is recorded for this Snapshot."],
    }));
  } else {
    const handoffs: Draft[] = [];
    for (const row of behavior.targets) {
      if (row.unverified_integrations.length === 0) continue;
      handoffs.push({
        kind: "integration",
        ref: row.behavior_key,
        title: `${row.unverified_integrations.join(", ")}: not verified`,
        detail: row.title,
        targetIds: row.target_ids,
      });
    }
    for (const check of behavior.checklist?.items ?? []) {
      if (!check.required || check.result?.status === "PASS") continue;
      handoffs.push({
        kind: "manual",
        ref: check.item_id,
        title: check.result ? `Manual check ${check.result.status.toLowerCase()}: ${check.title}` : `Manual check not recorded: ${check.title}`,
        detail: check.integration,
      });
    }
    const current = projection.evaluation_timeline?.current;
    if (current?.revision_id === projection.revision_id) {
      for (const receipt of current.receipts) {
        if (!receipt.unverified_integrations || receipt.superseded_by_newer_result) continue;
        handoffs.push({
          kind: "receipt",
          ref: receipt.evidence_id || receipt.id,
          title: `${receipt.title}: ${plural(receipt.unverified_integrations, "integration")} not verified`,
          detail: "The run recorded integrations it could not verify.",
        });
      }
    }
    categories.push(build.category("external_handoffs", handoffs, {
      scope: `${plural(behavior.targets.length, "behavior change")} checked for unverified integrations`,
    }));
  }

  // 5. Failed, missing and superseded checks and receipts.
  const checks: Draft[] = [];
  const statusWords = { FAIL: "Check failed", NOT_RUN: "Check did not run", UNKNOWN: "No result recorded" } as const;
  const snapshotRefs = context.checks.filter((check) => check.origin !== "claim_evidence");
  for (const check of snapshotRefs) {
    if (check.status === "PASS") continue;
    checks.push({ kind: "check", ref: `${check.status}:${check.key}`, title: `${statusWords[check.status]}: ${check.key}`, detail: "Recorded on this Snapshot.", targetIds: check.target_ids });
  }
  const staleKeys = new Set<string>();
  for (const check of context.checks) {
    if (check.origin !== "claim_evidence") continue;
    if (!check.current) {
      staleKeys.add(check.key);
      checks.push({
        kind: "stale",
        ref: check.key,
        title: `Evidence from an earlier Snapshot: ${check.key}`,
        detail: check.snapshot_number !== null ? `Recorded on Snapshot ${check.snapshot_number}; not re-run on this one.` : "Not re-run on this Snapshot.",
        targetIds: check.target_ids,
      });
    } else if (check.status !== "PASS") {
      checks.push({ kind: "linked", ref: `${check.status}:${check.key}`, title: `${statusWords[check.status]}: ${check.key}`, detail: "Linked to a claim on this Snapshot.", targetIds: check.target_ids });
    }
  }
  for (const key of projection.delta?.stale_evidence ?? []) {
    if (staleKeys.has(key)) continue;
    checks.push({ kind: "stale", ref: key, title: `Evidence superseded by this Snapshot: ${key}`, detail: "Recorded on an earlier Snapshot." });
  }
  for (const action of projection.actions) {
    if (action.id.startsWith("decision:") || action.id.startsWith("behavior:")) continue;
    checks.push({ kind: "required", ref: action.id, title: action.title, detail: action.detail, targetIds: action.target_ids });
  }
  for (const target of targets) {
    if (target.verification.fail > 0) {
      checks.push({ kind: "target_fail", ref: target.target_id, title: `Linked check failed: ${target.symbol || target.path}`, detail: plural(target.verification.fail, "failing check"), targetIds: [target.target_id] });
    } else if (target.verification.unknown > 0) {
      checks.push({ kind: "target_unknown", ref: target.target_id, title: `Linked check has no result: ${target.symbol || target.path}`, detail: plural(target.verification.unknown, "check") + " without a result", targetIds: [target.target_id] });
    }
  }
  const timeline = projection.evaluation_timeline?.current;
  if (timeline?.revision_id === projection.revision_id) {
    for (const receipt of timeline.receipts) {
      if (receipt.superseded_by_newer_result) continue;
      const rejected = receipt.execution_provenance === "rejected" || receipt.attempt_lifecycle === "rejected";
      const missing = receipt.unavailable_artifact_roles ?? [];
      if (!rejected && missing.length === 0) continue;
      checks.push({
        kind: "receipt",
        ref: receipt.evidence_id || receipt.id,
        title: rejected ? `Run record rejected: ${receipt.title}` : `Run artifacts unavailable: ${receipt.title}`,
        detail: rejected ? "Its execution record failed verification." : `Missing: ${missing.join(", ")}`,
      });
    }
  }
  const snapshotChecksKnown = !context.unavailable.includes("snapshot_checks") || snapshotRefs.length > 0 || Boolean(checkCounts);
  const recordedRows = snapshotRefs.filter((check) => check.origin === "snapshot_check").length;
  const recorded = recordedRows || snapshotRefs.length || (checkCounts ? checkCounts.pass + checkCounts.fail + checkCounts.not_run + checkCounts.unknown : 0);
  if (snapshotChecksKnown && snapshotRefs.length === 0 && checkCounts) {
    // Counts without rows: name the totals at Review level.
    if (checkCounts.fail) checks.push({ kind: "count", ref: "fail", title: `${plural(checkCounts.fail, "check")} failed`, detail: "Recorded on this Snapshot." });
    if (checkCounts.not_run) checks.push({ kind: "count", ref: "not_run", title: `${plural(checkCounts.not_run, "check")} did not run`, detail: "Recorded on this Snapshot." });
    if (checkCounts.unknown) checks.push({ kind: "count", ref: "unknown", title: `${plural(checkCounts.unknown, "check")} without a result`, detail: "Recorded on this Snapshot." });
  }
  if (snapshotChecksKnown && recorded === 0) {
    checks.push({ kind: "none", ref: "snapshot", title: "No check recorded on this Snapshot", detail: "Nothing here is verified by a recorded run." });
  }
  categories.push(build.category("checks", checks, snapshotChecksKnown
    ? { scope: `${plural(recorded, "recorded check")} on this Snapshot` }
    : {
      scope: projection.claims.some((claim) => claim.evidence.length > 0) ? "Claim-linked evidence only" : undefined,
      notMeasured: ["Snapshot-wide check results are not loaded in this view."],
    }));

  // 6. Analysis that covered only part of the change.
  const index: Draft[] = [];
  const total = projection.coverage.target_count;
  if (targets.length < total) {
    index.push({ kind: "targets", ref: "loaded", title: `Only ${targets.length} of ${plural(total, "changed target")} are loaded`, detail: "The rest are not analyzed in this view." });
  }
  if (projection.coverage.unresolved_refs > 0) {
    index.push({ kind: "refs", ref: "unresolved", title: `${plural(projection.coverage.unresolved_refs, "author reference")} could not be matched`, detail: "They name code this Snapshot does not contain." });
  }
  const inventory = behavior?.static_inventory;
  for (const line of [...(behavior?.degraded ?? []), ...(inventory?.degraded ?? [])]) {
    index.push({ kind: "degraded", ref: line, title: line, detail: "Reported as degraded for this Snapshot." });
  }
  if (inventory) {
    const state = inventory.register.state ?? "";
    if (state === "truncated" || state === "changed_files_only") {
      index.push({
        kind: "register",
        ref: state,
        title: state === "truncated" ? "Side-effect register was truncated" : "Side-effect register covers changed files only",
        detail: inventory.register.note || `${plural(inventory.register.files_scanned ?? inventory.files_analyzed, "file")} scanned.`,
      });
    }
    if (["limited_by_budget", "not_available", "not_run", "pending"].includes(inventory.status)) {
      index.push({ kind: "static", ref: inventory.status, title: `Static side-effect scan: ${inventory.status.replaceAll("_", " ")}`, detail: inventory.reason });
    }
  }
  if (impact) {
    for (const gap of readinessGaps(impact.readiness)) {
      index.push({ kind: "readiness", ref: gap.key, title: gap.title, detail: gap.detail });
    }
  }
  categories.push(build.category("index_coverage", index, noTargets
    ? { notApplicable: "No changed code targets." }
    : {
      scope: impact
        ? `${targets.length} of ${plural(total, "changed target")} loaded; code index ${impact.readiness.index_status}`
        : `${targets.length} of ${plural(total, "changed target")} loaded`,
      notMeasured: impact ? [] : ["Code-graph index readiness is not carried by this projection; the Map reports it."],
    }));

  // 7. Review-first code nobody has reviewed yet.
  const important = targets.filter((target) => target.priority
    ? target.priority.attention === "review_first"
    : target.attention_level === "high");
  categories.push(build.category("uninspected_regions", important
    .filter((target) => target.state === "unreviewed" || target.state === "unknown")
    .map((target) => ({
      kind: "target",
      ref: target.target_id,
      title: target.symbol || target.path,
      detail: target.priority?.reasons[0]?.detail || "Ranked review first; not reviewed yet",
      targetIds: [target.target_id],
    })), noTargets
    ? { notApplicable: "No changed code targets." }
    : { scope: important.length > 0 ? `${plural(important.length, "review-first target")}` : "No target is ranked review first" }));

  // 8. Open comments and decisions (reopened decisions are listed under 9 instead).
  const human: Draft[] = [];
  for (const target of targets) {
    const counts = target.annotation_counts;
    const name = target.symbol || target.path;
    if (counts.open) human.push({ kind: "comments", ref: target.target_id, title: `${plural(counts.open, "open comment")} on ${name}`, detail: "Not resolved.", targetIds: [target.target_id] });
    if (counts.orphaned) human.push({ kind: "orphaned", ref: target.target_id, title: `${plural(counts.orphaned, "comment")} lost its line in ${name}`, detail: "The code it was written on changed.", targetIds: [target.target_id] });
    if (counts.addressed_needs_rereview) human.push({ kind: "rereview", ref: target.target_id, title: `${plural(counts.addressed_needs_rereview, "addressed comment")} to re-check on ${name}`, detail: "The author marked it addressed.", targetIds: [target.target_id] });
  }
  for (const decision of projection.decisions) {
    if (!isOpenDecision(decision) || reopened.has(decision.key)) continue;
    human.push({
      kind: "decision",
      ref: decision.key,
      title: decision.question || decision.key,
      detail: decision.state ? decision.state.detail || "Needs a human decision" : "Proposed by the author; no human decision recorded",
      targetIds: decision.target_ids,
      decision,
    });
  }
  categories.push(build.category("unresolved_human", human, {
    scope: `Comments on ${plural(targets.length, "target")} and ${plural(projection.decisions.length, "decision")}`,
  }));

  // 9. Judgments a newer Snapshot reopened; accepted, unaffected judgments stay out.
  const delta = projection.delta;
  if (delta === null || (delta === undefined && projection.snapshot_number === 0)) {
    categories.push(build.category("snapshot_reopened", [], { notApplicable: "First Snapshot: no earlier judgment to reopen." }));
  } else {
    const drafts: Draft[] = [...reopened].map(([key, decision]) => ({
      kind: "decision",
      ref: key,
      title: decision?.question || `Decision reopened: ${key}`,
      detail: decision?.state?.changed_inputs.length
        ? `Accepted earlier; changed: ${decision.state.changed_inputs.join(", ")}`
        : decision?.stale ? "Written for earlier content" : "Accepted on an earlier Snapshot; its inputs changed",
      targetIds: decision?.target_ids ?? [],
      decision,
    }));
    for (const target of targets) {
      if (target.state !== "changed_since_review" && !target.changed_since_mark) continue;
      drafts.push({ kind: "target", ref: target.target_id, title: `Changed since review: ${target.symbol || target.path}`, detail: "Your earlier judgment no longer covers this code.", targetIds: [target.target_id] });
    }
    for (const row of behavior?.targets ?? []) {
      if (row.state !== "changed_since_review") continue;
      drafts.push({ kind: "behavior", ref: row.behavior_key, title: `Behavior changed since review: ${row.title}`, detail: row.summary || row.kind_label, targetIds: row.target_ids });
    }
    categories.push(build.category("snapshot_reopened", drafts, delta
      ? { scope: `Compared with Snapshot ${delta.since_snapshot}` }
      : { notMeasured: ["This projection does not say what changed since the previous Snapshot."] }));
  }

  const count = (state: BlindSpotState) => categories.filter((category) => category.state === state).length;
  return {
    context,
    categories,
    counts: {
      items: categories.reduce((sum, category) => sum + category.items.length + category.omitted, 0),
      known: count("known"),
      nothingOpen: count("nothing_open"),
      notMeasured: count("not_measured"),
      notApplicable: count("not_applicable"),
    },
  };
}
