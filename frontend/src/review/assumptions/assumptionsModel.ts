// Assumption Explorer (N02): which assumptions does this change rely on, and is
// any of them demonstrated? A pure, bounded, deterministic projection over data
// the Reader already holds -- the Review projection, its Snapshot targets and
// the InvestigationContext. It makes no model call and adds no analyzer: rows
// come from author claims and declared assumptions, stated limits, AI questions
// the server already validated, behavior changes and the consumer inventory.
// A row is `checked` only when a recorded, current, explicitly linked check
// passed for it; everything else is `unverified`, and the words of any record
// (code comments, model prose, claims) never change a row's state.
import { sameSnapshot, type InvestigationContext } from "../investigation/investigationContext";
import type {
  Annotation,
  ReviewConsumerGroup,
  ReviewProjection,
  ReviewProjectionEvidence,
  ReviewTarget,
} from "../types";

export type AssumptionKind =
  | "precondition"
  | "invariant"
  | "idempotency"
  | "concurrency"
  | "authorization"
  | "data_shape"
  | "failure_retry"
  | "side_effect";

export type AssumptionOrigin = "deterministic" | "author_claim" | "model_suggested";

/** `checked`: a current, explicitly linked check passed. `contradicted`: one failed. Otherwise `unverified`. */
export type AssumptionState = "unverified" | "checked" | "contradicted";

export const ASSUMPTION_KINDS: readonly AssumptionKind[] = [
  "precondition",
  "invariant",
  "idempotency",
  "concurrency",
  "authorization",
  "data_shape",
  "failure_retry",
  "side_effect",
];

export const KIND_LABEL: Record<AssumptionKind, string> = {
  precondition: "Preconditions",
  invariant: "Invariants",
  idempotency: "Idempotency",
  concurrency: "Concurrency",
  authorization: "Authorization",
  data_shape: "Data shape",
  failure_retry: "Failure and retry",
  side_effect: "Side effects",
};

/** What breaks, in general, when an assumption of this kind does not hold. A category note, not a finding. */
export const KIND_IF_FAILS: Record<AssumptionKind, string> = {
  precondition: "Callers reach the code with inputs or state it does not handle.",
  invariant: "State the code relies on becomes inconsistent.",
  idempotency: "A retry or replay repeats the effect.",
  concurrency: "Simultaneous work interleaves and corrupts or duplicates results.",
  authorization: "Someone can act or read without permission.",
  data_shape: "A reader or writer misreads the data.",
  failure_retry: "An error is retried, swallowed or surfaced wrongly.",
  side_effect: "Something outside the function changes unexpectedly.",
};

export const ORIGIN_LABEL: Record<AssumptionOrigin, string> = {
  deterministic: "Deterministic signal",
  author_claim: "Author claim",
  model_suggested: "AI suggestion",
};

export interface AssumptionSourceLink {
  target_id: string;
  path: string;
  symbol: string;
  start_line: number;
  end_line: number;
  /** The target changed after someone reviewed it, so an earlier reading of it no longer covers it. */
  changed_since_review: boolean;
  /** Links are valid only on the Snapshot they were built for. */
  snapshot_identity: string;
}

export interface AssumptionConsumerLink {
  path: string;
  line: number;
  symbol: string;
  kind: "caller" | "consumer";
  impact_kind: string;
  exactness: string;
  /** Why it is a candidate rather than a fact; empty when it is a fact. */
  uncertainty: string;
  /** The changed target it depends on, when one is loaded. */
  target_id: string | null;
}

/** A check explicitly linked to the row's own record (a claim's evidence, a decision's relied check). */
export interface AssumptionSupport {
  key: string;
  status: "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  /** Recorded on this Snapshot; false for evidence carried from an earlier one. */
  current: boolean;
  snapshot_number: number | null;
  /** `explicit`: the record names the check; `inferred`: relevance was guessed, so it neither supports nor contradicts. */
  mapping: "explicit" | "inferred";
  relation: "claim_evidence" | "relied_on";
}

/** A check that touches the same code but is not linked to this assumption: coverage, never proof. */
export interface AssumptionCoverage {
  label: string;
  status: "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  relation: "target_verification" | "claim_on_same_code";
}

export interface AssumptionRow {
  /** Stable for the same subject on the same Snapshot: kind plus a digest of the dedup key. */
  id: string;
  kind: AssumptionKind;
  hypothesis: string;
  detail: string;
  /** Where it came from; the first is the primary origin, later ones are merged duplicates. */
  origins: AssumptionOrigin[];
  /** The record that produced it, in words ("Author claim", "Static side-effect scan", ...). */
  signal: string;
  state: AssumptionState;
  checkedBy: string[];
  contradictedBy: string[];
  support: AssumptionSupport[];
  coverage: AssumptionCoverage[];
  targetIds: string[];
  sources: AssumptionSourceLink[];
  sourcesOmitted: number;
  /** Target ids the record names that are not in the loaded target list. */
  unresolvedTargets: number;
  consumers: AssumptionConsumerLink[];
  consumersOmitted: number;
  /** Records folded into this row because they named the same subject on the same code. */
  duplicates: number;
}

export interface AssumptionGroup {
  kind: AssumptionKind;
  label: string;
  ifFails: string;
  rows: AssumptionRow[];
  /** Rows past the per-kind bound, counted, never silently dropped. */
  omitted: number;
}

export interface AssumptionsScope {
  kind: "review" | "subject" | "finding";
  label: string;
  /** Null for the whole Review. */
  targetIds: string[] | null;
}

export interface AssumptionsModel {
  context: InvestigationContext;
  snapshotIdentity: string;
  scope: AssumptionsScope;
  groups: AssumptionGroup[];
  counts: { rows: number; unverified: number; checked: number; contradicted: number; duplicatesMerged: number };
  /** What was read, so an empty result says what was looked at. */
  looked: {
    claims: number;
    declaredAssumptions: number;
    statedLimits: number;
    aiQuestions: number;
    behaviorChanges: number;
    consumerSites: number;
  };
  notMeasured: string[];
}

export type AssumptionFinding = Pick<Annotation, "id" | "title" | "body" | "path" | "start_line" | "end_line">;

export interface AssumptionsInput {
  context: InvestigationContext;
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
  /** Start from a Review finding: only assumptions on the code it sits in. */
  finding?: AssumptionFinding | null;
}

// lc-debt: fixed caps (rows per kind, links per row, classified text length); page rows by file when a kind needs more on screen.
export const MAX_ROWS_PER_KIND = 60;
export const MAX_ROW_SOURCES = 6;
export const MAX_ROW_CONSUMERS = 6;
const MAX_CLASSIFIED_TEXT = 600;

// lc-debt: kind is assigned by a fixed keyword list over the record's own words; a naive heuristic that labels a row, never makes one true. Upgrade path: a typed `kind` on guidance assumptions and AI questions.
const KIND_PATTERNS: readonly [AssumptionKind, RegExp][] = [
  ["authorization", /\b(auth\w*|permissions?|privileges?|roles?|login|logout|sign[- ]?in|credentials?|tenants?|admin|csrf|acl|forbidden|unauthori[sz]ed|access control)\b/i],
  ["concurrency", /\b(concurren\w*|parallel|races?|racing|locks?|locking|locked|mutex|atomic\w*|simultaneous\w*|at once|threads?|deadlocks?|in[- ]flight|bursts?)\b/i],
  ["idempotency", /\b(idempoten\w*|double[- ]\w+|duplicat\w*|twice|exactly[- ]once|at[- ]most[- ]once|at[- ]least[- ]once|dedup\w*|replay\w*)\b/i],
  ["failure_retry", /\b(retr(y|ies|ied|ying)|timeouts?|timed? out|failures?|errors?|fallbacks?|backoff|exceptions?|raises?|crash\w*|rollbacks?|roll back)\b/i],
  ["data_shape", /\b(schemas?|shape|fields?|columns?|payloads?|serializ\w*|deserializ\w*|json|nullable|null|migrat\w*|encod\w*|response body)\b/i],
  ["side_effect", /\b(side[- ]effects?|emails?|webhooks?|publish\w*|notif\w*|enqueue\w*|persist\w*|writes? to|deletes?)\b/i],
  ["invariant", /\b(invariants?|always|never|unique|consisten\w*|monoton\w*)\b/i],
  ["precondition", /\b(preconditions?|requires?|expects?|assum\w*|must be|non[- ]?empty|validat\w*|guards?)\b/i],
];

export function classifyAssumption(text: string): AssumptionKind | null {
  const bounded = text.slice(0, MAX_CLASSIFIED_TEXT);
  for (const [kind, pattern] of KIND_PATTERNS) if (pattern.test(bounded)) return kind;
  return null;
}

function normalize(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/** Small, stable string digest for row ids (FNV-1a); identity, not security. */
function digest(text: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

function statusOf(value: string | null | undefined): AssumptionSupport["status"] {
  const upper = String(value ?? "").toUpperCase();
  return upper === "PASS" || upper === "FAIL" || upper === "NOT_RUN" ? upper : "UNKNOWN";
}

const ORIGIN_RANK: Record<AssumptionOrigin, number> = { deterministic: 0, author_claim: 1, model_suggested: 2 };
const STATE_RANK: Record<AssumptionState, number> = { contradicted: 0, unverified: 1, checked: 2 };

interface Draft {
  kind: AssumptionKind;
  hypothesis: string;
  detail: string;
  origin: AssumptionOrigin;
  signal: string;
  targetIds: readonly string[];
  support?: AssumptionSupport[];
  /** The server said a recorded run observed the claim; still needs a current explicit pass to count. */
  observed?: boolean;
  consumers?: AssumptionConsumerLink[];
  excludeClaimId?: string;
}

function supportFromEvidence(evidence: readonly ReviewProjectionEvidence[]): AssumptionSupport[] {
  return evidence.map((row) => ({
    key: row.key || row.title,
    status: statusOf(row.status),
    current: row.state === "current",
    snapshot_number: row.snapshot_number,
    mapping: row.mapping === "explicit" ? "explicit" : "inferred",
    relation: "claim_evidence" as const,
  }));
}

/** Only current, explicitly linked results decide a row; stale or inferred ones are shown, never counted. */
function stateOf(draft: Draft): { state: AssumptionState; checkedBy: string[]; contradictedBy: string[] } {
  const decisive = (draft.support ?? []).filter((row) => row.current && row.mapping === "explicit");
  const contradictedBy = decisive.filter((row) => row.status === "FAIL").map((row) => row.key);
  if (contradictedBy.length > 0) return { state: "contradicted", checkedBy: [], contradictedBy };
  // A generated suggestion is never shown as demonstrated, whatever passed nearby.
  if (draft.origin === "model_suggested") return { state: "unverified", checkedBy: [], contradictedBy: [] };
  const passed = decisive.filter((row) => row.status === "PASS").map((row) => row.key);
  const checked = passed.length > 0 && (draft.observed ?? true);
  return { state: checked ? "checked" : "unverified", checkedBy: checked ? passed : [], contradictedBy: [] };
}

function consumerLinks(groups: readonly ReviewConsumerGroup[], filter?: (impactKind: string) => boolean): AssumptionConsumerLink[] {
  const out: AssumptionConsumerLink[] = [];
  for (const group of groups) {
    for (const site of group.sites) {
      if (site.in_patch || (filter && !filter(site.impact_kind))) continue;
      out.push({
        path: site.path,
        line: site.line,
        symbol: site.symbol,
        kind: site.kind,
        impact_kind: site.impact_kind,
        exactness: String(site.exactness),
        uncertainty: site.uncertainty,
        target_id: group.target_ids.length === 1 ? group.target_ids[0] : null,
      });
    }
  }
  return out;
}

const CONSUMER_RULES: Record<string, { kind: AssumptionKind; text: (subject: string) => string } | undefined> = {
  signature_change: { kind: "precondition", text: (subject) => `Unchanged callers of ${subject} still pass what its new signature requires` },
  removed_symbol: { kind: "precondition", text: (subject) => `Nothing outside this change still uses the removed ${subject}` },
  decorator_contract: { kind: "precondition", text: (subject) => `Callers no longer need what a removed decorator on ${subject} provided` },
  contract_literal: { kind: "data_shape", text: (subject) => `Consumers of ${subject} still match its changed contract` },
};

function behaviorRule(kind: string, title: string): { kind: AssumptionKind; text: string } | null {
  if (kind === "side_effect") {
    if (/^(new )?permission or scope\b/i.test(title)) return { kind: "authorization", text: `Only intended callers gain or keep access after: ${title}` };
    if (/^(new )?(database (table|migration)|destructive database operation)\b/i.test(title)) {
      return { kind: "data_shape", text: `Stored data and its readers still agree after: ${title}` };
    }
    return { kind: "side_effect", text: `This side effect is intended where it now happens: ${title}` };
  }
  if (kind === "migration") return { kind: "data_shape", text: `Stored data and its readers still agree after: ${title}` };
  if (kind === "api_response" || kind === "compatibility") return { kind: "data_shape", text: `Consumers still accept the changed behavior: ${title}` };
  return null;
}

function collectDrafts(projection: ReviewProjection, context: InvestigationContext): { drafts: Draft[]; looked: AssumptionsModel["looked"] } {
  const drafts: Draft[] = [];
  const groupsByTarget = new Map<string, ReviewConsumerGroup[]>();
  for (const group of projection.context?.consumers ?? []) {
    for (const id of group.target_ids) groupsByTarget.set(id, [...(groupsByTarget.get(id) ?? []), group]);
  }
  const consumersFor = (ids: readonly string[]) => {
    const groups = [...new Set(ids.flatMap((id) => groupsByTarget.get(id) ?? []))];
    return consumerLinks(groups);
  };

  // Author claims: the PR's own statements, labelled by kind only when their words name one.
  for (const claim of projection.claims) {
    const kind = classifyAssumption(claim.statement);
    if (!kind) continue;
    drafts.push({
      kind,
      hypothesis: claim.statement,
      detail: claim.summary_text,
      origin: "author_claim",
      signal: claim.source ? `Claim by ${claim.source}` : "Author claim",
      targetIds: claim.target_ids,
      support: supportFromEvidence(claim.evidence),
      observed: claim.summary === "observed",
      consumers: consumersFor(claim.target_ids),
      excludeClaimId: claim.id,
    });
  }

  // Assumptions an author or agent declared on a decision (guidance `assumptions`), with the checks it relied on.
  let declared = 0;
  // Results recorded on this Snapshot, by name (a FAIL anywhere under that name wins).
  const checkStatus = new Map<string, AssumptionSupport["status"]>();
  for (const check of context.checks) {
    if (!check.current || checkStatus.get(check.key) === "FAIL") continue;
    checkStatus.set(check.key, check.status);
  }
  for (const decision of projection.decisions) {
    if (decision.state?.withdrawn) continue;
    for (const assumption of decision.assumptions) {
      declared += 1;
      const text = assumption.statement || `${assumption.key}: ${assumption.value}`;
      const kind = classifyAssumption(`${text} ${assumption.key}`);
      if (!kind) continue;
      drafts.push({
        kind,
        hypothesis: text,
        detail: decision.question ? `Declared for the decision: ${decision.question}` : "Declared by the author",
        origin: "author_claim",
        signal: `Assumption declared by ${decision.producer || "the author"}`,
        targetIds: decision.target_ids,
        // A relied-on check is the author's own link; it decides only when it is recorded on this Snapshot.
        support: decision.relied_checks.map((key) => ({
          key,
          status: checkStatus.get(key) ?? "UNKNOWN",
          current: checkStatus.has(key),
          snapshot_number: null,
          mapping: "explicit" as const,
          relation: "relied_on" as const,
        })),
        consumers: consumersFor(decision.target_ids),
      });
    }
  }

  // Limits the author stated: a scenario the change was not exercised under.
  let limits = 0;
  for (const item of projection.items) {
    for (const concern of item.concerns) {
      limits += 1;
      const kind = classifyAssumption(`${concern.scope} ${concern.reason}`);
      if (!kind) continue;
      drafts.push({
        kind,
        hypothesis: `Behaves correctly for: ${concern.scope}`,
        detail: concern.reason ? `Stated as not exercised: ${concern.reason}` : "Stated as not exercised",
        origin: item.producer === "lemoncrow" ? "deterministic" : "author_claim",
        signal: "Stated limit",
        targetIds: item.primary_target_ids,
        consumers: consumersFor(item.primary_target_ids),
      });
    }
  }

  // Questions a validated model synthesis raised: suggestions, never facts.
  const aiQuestions = projection.questions.filter((question) => question.origin === "ai");
  for (const question of aiQuestions) {
    const kind = classifyAssumption(`${question.title} ${question.detail}`);
    if (!kind) continue;
    drafts.push({
      kind,
      hypothesis: question.title,
      detail: question.detail,
      origin: "model_suggested",
      signal: "AI question from the Review synthesis",
      targetIds: question.target_ids,
      consumers: consumersFor(question.target_ids),
    });
  }

  // Deterministic signals: recorded behavior changes and unverified integrations.
  const behaviors = projection.behavior?.targets ?? [];
  for (const row of behaviors) {
    const rule = behaviorRule(row.kind, row.title);
    if (rule) {
      drafts.push({
        kind: rule.kind,
        hypothesis: rule.text,
        detail: row.summary || row.kind_label,
        origin: "deterministic",
        signal: row.kind === "side_effect" ? "Static side-effect scan" : `Behavior change: ${row.kind_label}`,
        targetIds: row.target_ids,
        consumers: consumersFor(row.target_ids),
      });
    }
    if (row.unverified_integrations.length > 0) {
      drafts.push({
        kind: "precondition",
        hypothesis: `${row.unverified_integrations.join(", ")} behaves as this change expects`,
        detail: `A recorded run could not verify it: ${row.title}`,
        origin: "deterministic",
        signal: "Unverified integration on a recorded run",
        targetIds: row.target_ids,
      });
    }
  }

  // Deterministic signals: consumers that depend on a changed contract (plain callers stay in Blind spots).
  let sites = 0;
  for (const group of projection.context?.consumers ?? []) {
    sites += group.sites.length;
    const byImpact = new Map<string, AssumptionConsumerLink[]>();
    for (const link of consumerLinks([group], (impact) => Boolean(CONSUMER_RULES[impact]))) {
      byImpact.set(link.impact_kind, [...(byImpact.get(link.impact_kind) ?? []), link]);
    }
    for (const [impact, links] of byImpact) {
      const rule = CONSUMER_RULES[impact]!;
      const subject = group.symbol || group.path || "the changed code";
      drafts.push({
        kind: rule.kind,
        hypothesis: rule.text(subject),
        detail: `${links.length} ${links.length === 1 ? "site" : "sites"} outside this change depend on it`,
        origin: "deterministic",
        signal: "Impact pass on this Snapshot",
        targetIds: group.target_ids,
        consumers: links,
      });
    }
  }

  return {
    drafts,
    looked: {
      claims: projection.claims.length,
      declaredAssumptions: declared,
      statedLimits: limits,
      aiQuestions: aiQuestions.length,
      behaviorChanges: behaviors.length,
      consumerSites: sites,
    },
  };
}

function overlaps(target: ReviewTarget, finding: AssumptionFinding): boolean {
  if (target.kind === "file" || finding.start_line <= 0) return true;
  const end = Math.max(finding.start_line, finding.end_line || finding.start_line);
  return target.start_line <= end && finding.start_line <= target.end_line;
}

function scopeFor(input: AssumptionsInput): AssumptionsScope {
  const { context, targets, finding } = input;
  if (finding) {
    const samePath = targets.filter((target) => target.path === finding.path);
    const hit = samePath.filter((target) => overlaps(target, finding));
    const label = finding.title || finding.body.slice(0, 80) || finding.id;
    return { kind: "finding", label: `Finding: ${label}`, targetIds: (hit.length > 0 ? hit : samePath).map((target) => target.target_id) };
  }
  if (context.subject.kind !== "review") {
    const ids = [...context.sources.map((source) => source.target_id), ...context.unresolved_target_ids];
    const first = context.sources[0];
    const label = first ? (first.symbol ? `${first.path} · ${first.symbol}` : first.path) : context.subject.id;
    return { kind: "subject", label, targetIds: ids };
  }
  return { kind: "review", label: "All changed code", targetIds: null };
}

/** Derive the Assumption Explorer projection; pure, bounded and deterministic for identical inputs. */
export function assumptionsModel(input: AssumptionsInput): AssumptionsModel {
  const { context, projection, targets } = input;
  const identity = context.snapshot_identity;
  const byId = new Map(targets.map((target) => [target.target_id, target]));
  const scope = scopeFor(input);
  const inScope = scope.targetIds ? new Set(scope.targetIds) : null;
  const { drafts, looked } = collectDrafts(projection, context);

  const claimChecks = context.checks.filter((check) => check.origin === "claim_evidence");
  const rows = new Map<string, AssumptionRow>();
  let duplicatesMerged = 0;

  for (const draft of drafts) {
    const targetIds = [...new Set(draft.targetIds.filter(Boolean))].sort();
    if (inScope && !targetIds.some((id) => inScope.has(id))) continue;
    const key = `${draft.kind}|${targetIds.join(",")}|${normalize(draft.hypothesis)}`;
    const existing = rows.get(key);
    if (existing) {
      // Same subject on the same code: one row, every origin kept, the strongest recorded result kept.
      duplicatesMerged += 1;
      const row = existing;
      row.duplicates += 1;
      if (!row.origins.includes(draft.origin)) {
        row.origins.push(draft.origin);
        row.origins.sort((a, b) => ORIGIN_RANK[a] - ORIGIN_RANK[b]);
      }
      // Each record is judged on its own links; a failure from any of them wins, then a pass.
      const next = stateOf(draft);
      row.contradictedBy = [...new Set([...row.contradictedBy, ...next.contradictedBy])];
      row.checkedBy = [...new Set([...row.checkedBy, ...next.checkedBy])];
      row.state = row.contradictedBy.length > 0 ? "contradicted" : row.checkedBy.length > 0 ? "checked" : "unverified";
      if (row.state === "contradicted") row.checkedBy = [];
      row.support.push(...(draft.support ?? []));
      continue;
    }

    const loaded = targetIds.map((id) => byId.get(id)).filter((target): target is ReviewTarget => Boolean(target));
    const sources = loaded.slice(0, MAX_ROW_SOURCES).map((target): AssumptionSourceLink => ({
      target_id: target.target_id,
      path: target.path,
      symbol: target.symbol,
      start_line: target.start_line,
      end_line: target.end_line,
      changed_since_review: target.state === "changed_since_review" || target.changed_since_mark,
      snapshot_identity: identity,
    }));
    const coverage: AssumptionCoverage[] = [];
    for (const target of loaded) {
      const { pass, fail, unknown } = target.verification;
      if (pass + fail + unknown === 0) continue;
      const parts = [fail ? `${fail} failing` : "", pass ? `${pass} passing` : "", unknown ? `${unknown} without a result` : ""].filter(Boolean);
      coverage.push({
        label: `Checks on ${target.symbol || target.path}: ${parts.join(", ")}`,
        status: fail ? "FAIL" : unknown ? "UNKNOWN" : "PASS",
        relation: "target_verification",
      });
    }
    const own = new Set((draft.support ?? []).map((row) => row.key));
    const seenCoverage = new Set<string>();
    for (const check of claimChecks) {
      if (own.has(check.key) || !check.current || !check.target_ids.some((id) => targetIds.includes(id))) continue;
      if (seenCoverage.has(check.key)) continue;
      seenCoverage.add(check.key);
      coverage.push({ label: `${check.key} (linked to another claim on this code)`, status: check.status, relation: "claim_on_same_code" });
    }
    const consumers = draft.consumers ?? [];
    const decided = stateOf(draft);
    const row: AssumptionRow = {
      id: `${draft.kind}:${digest(key)}`,
      kind: draft.kind,
      hypothesis: draft.hypothesis,
      detail: draft.detail,
      origins: [draft.origin],
      signal: draft.signal,
      ...decided,
      support: [...(draft.support ?? [])],
      coverage,
      targetIds,
      sources,
      sourcesOmitted: loaded.length - sources.length,
      unresolvedTargets: targetIds.length - loaded.length,
      consumers: consumers.slice(0, MAX_ROW_CONSUMERS),
      consumersOmitted: Math.max(0, consumers.length - MAX_ROW_CONSUMERS),
      duplicates: 0,
    };
    rows.set(key, row);
  }

  const groups: AssumptionGroup[] = [];
  for (const kind of ASSUMPTION_KINDS) {
    const all = [...rows.values()]
      .filter((row) => row.kind === kind)
      .map((row, index) => ({ row, index }))
      // Contradicted first, then by origin (deterministic, author, AI); insertion order otherwise.
      .sort((a, b) => STATE_RANK[a.row.state] - STATE_RANK[b.row.state]
        || ORIGIN_RANK[a.row.origins[0]] - ORIGIN_RANK[b.row.origins[0]]
        || a.index - b.index)
      .map(({ row }) => row);
    if (all.length === 0) continue;
    groups.push({
      kind,
      label: KIND_LABEL[kind],
      ifFails: KIND_IF_FAILS[kind],
      rows: all.slice(0, MAX_ROWS_PER_KIND),
      omitted: Math.max(0, all.length - MAX_ROWS_PER_KIND),
    });
  }

  const flat = [...rows.values()];
  const notMeasured = [
    "Implicit assumptions in the code itself: no pass in this view reads code for assumptions nobody stated.",
    "Supporting or contradicting code lines: rows link the changed code and its consumers, not the lines that uphold or break each hypothesis.",
  ];
  if (projection.context === undefined) notMeasured.push("Consumers: this projection carries no caller or consumer inventory.");
  if (!projection.behavior) notMeasured.push("Behavior and side effects: no behavior evidence is recorded for this Snapshot.");
  const synthesis = projection.synthesis?.status;
  if (synthesis !== "ready") {
    notMeasured.push(synthesis === "failed"
      ? "AI questions: the Review synthesis failed on this Snapshot, so no AI suggestion is listed."
      : "AI questions: the Review synthesis has not run on this Snapshot.");
  }
  if (scope.kind === "finding" && scope.targetIds?.length === 0) {
    notMeasured.push("The finding is not on a loaded changed target, so no assumption could be tied to it.");
  }

  return {
    context,
    snapshotIdentity: identity,
    scope,
    groups,
    counts: {
      rows: flat.length,
      unverified: flat.filter((row) => row.state === "unverified").length,
      checked: flat.filter((row) => row.state === "checked").length,
      contradicted: flat.filter((row) => row.state === "contradicted").length,
      duplicatesMerged,
    },
    looked,
    notMeasured,
  };
}

/** Whether *model* was built for the Snapshot *projection* now shows; links from another Snapshot are not followed. */
export function assumptionsCurrent(model: AssumptionsModel, projection: ReviewProjection | null): boolean {
  return sameSnapshot(model.context, projection);
}

/** The rows on one changed target, for the panel's focus control; null keeps every row. */
export function rowsForTarget(group: AssumptionGroup, targetId: string | null): AssumptionRow[] {
  return targetId ? group.rows.filter((row) => row.targetIds.includes(targetId)) : group.rows;
}
