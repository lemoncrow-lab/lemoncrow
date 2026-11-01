// InvestigationContext (N00 / A6): one read-only, versioned view of "what an
// investigation is about" over data the Reader already holds -- the Review
// projection, its Snapshot targets and the recorded checks. It is a projection,
// not a store: identity, source navigation, check status and Review authority
// stay with their existing owners, nothing here fetches or asks a model, and a
// field the inputs do not carry stays null and is named in `unavailable`.
import type {
  EvidenceRow,
  MarkState,
  ReviewProjection,
  ReviewTarget,
  RevisionInfo,
} from "../types";

export const INVESTIGATION_CONTEXT_VERSION = 1 as const;

/**
 * How a relationship is proven, in the Map's own vocabulary plus `observed`:
 * `snapshot` is exact for this Snapshot, `observed` came from a recorded run,
 * `current_index` is from the working index rather than this Snapshot,
 * `inferred` is a grouping or explanation nobody proved, `unknown` is unproven.
 */
export type RelationshipProof = "snapshot" | "observed" | "current_index" | "inferred" | "unknown";

export type InvestigationSubjectKind = "review" | "target" | "item" | "decision" | "claim";

export interface InvestigationSubject {
  kind: InvestigationSubjectKind;
  /** The Review id, a target id, a projection item id, a decision key or a claim id. */
  id: string;
}

export type InvestigationCheckStatus = "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";

export interface InvestigationSourceRef {
  target_id: string;
  path: string;
  symbol: string;
  kind: ReviewTarget["kind"];
  start_line: number;
  end_line: number;
  state: MarkState;
  proof: RelationshipProof;
}

export interface InvestigationCheckRef {
  /** The check's name or evidence key, as its owner reports it. */
  key: string;
  status: InvestigationCheckStatus;
  origin: "snapshot_check" | "claim_evidence" | "receipt";
  snapshot_number: number | null;
  /** Recorded for this Snapshot; false for evidence carried from an earlier one. */
  current: boolean;
  /** Targets the owner links it to; empty means it is Review-level only. */
  target_ids: string[];
  proof: RelationshipProof;
  evidence_id: string | null;
}

export interface InvestigationContext {
  version: typeof INVESTIGATION_CONTEXT_VERSION;
  review_id: string;
  revision_id: string;
  snapshot_number: number | null;
  /** `review_id@revision_id`: an investigation is only valid on the Snapshot it names. */
  snapshot_identity: string;
  historical: boolean;
  base_sha: string | null;
  head_sha: string | null;
  subject: InvestigationSubject;
  /** How the subject is tied to its sources; `unknown` when none of them is loaded. */
  relationship_proof: RelationshipProof;
  sources: InvestigationSourceRef[];
  sources_omitted: number;
  /** Ids the subject names that are not in the loaded target list (e.g. a partial list). */
  unresolved_target_ids: string[];
  checks: InvestigationCheckRef[];
  checks_omitted: number;
  /** Fields these inputs cannot supply, so they are null rather than guessed. */
  unavailable: string[];
}

export interface InvestigationInput {
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
  /** Recorded checks on this exact Snapshot; absent when the host did not load them. */
  checks?: readonly EvidenceRow[] | null;
  /** The Snapshot's revision when the host has it; ignored unless it is the projection's own revision. */
  revision?: Pick<RevisionInfo, "id" | "base_sha" | "head_sha"> | null;
  subject?: InvestigationSubject;
}

// lc-debt: fixed caps on carried refs; page refs by subject when an investigation needs more than this.
export const MAX_CONTEXT_SOURCES = 500;
export const MAX_CONTEXT_CHECKS = 500;

export function checkStatus(status: string | null | undefined): InvestigationCheckStatus {
  const value = String(status ?? "").toUpperCase();
  return value === "PASS" || value === "FAIL" || value === "NOT_RUN" ? value : "UNKNOWN";
}

export function snapshotIdentity(reviewId: string, revisionId: string): string {
  return `${reviewId}@${revisionId}`;
}

/** Whether *context* was built for the Snapshot *projection* now shows. */
export function sameSnapshot(context: InvestigationContext, projection: ReviewProjection | null): boolean {
  return Boolean(projection) && context.snapshot_identity === snapshotIdentity(projection!.review_id, projection!.revision_id);
}

function subjectTargets(projection: ReviewProjection, targets: readonly ReviewTarget[], subject: InvestigationSubject): {
  ids: string[];
  proof: RelationshipProof;
} {
  switch (subject.kind) {
    case "review":
      return { ids: targets.map((target) => target.target_id), proof: "snapshot" };
    case "target":
      return { ids: [subject.id], proof: "snapshot" };
    case "item": {
      const item = projection.items.find((row) => row.id === subject.id);
      if (!item) return { ids: [], proof: "unknown" };
      // A group explained only by an AI reading is a grouping nobody proved.
      return {
        ids: [...item.primary_target_ids, ...item.related_target_ids],
        proof: item.rationale_origin === "inferred" ? "inferred" : "snapshot",
      };
    }
    case "decision": {
      const decision = projection.decisions.find((row) => row.key === subject.id);
      return decision ? { ids: decision.target_ids, proof: "snapshot" } : { ids: [], proof: "unknown" };
    }
    case "claim": {
      const claim = projection.claims.find((row) => row.id === subject.id);
      if (!claim) return { ids: [], proof: "unknown" };
      const proof: RelationshipProof = claim.summary === "observed"
        ? "observed"
        : claim.summary === "inferred" || claim.summary === "inferred_failing" ? "inferred" : "snapshot";
      return { ids: claim.target_ids, proof };
    }
  }
}

function allChecks(projection: ReviewProjection, checks: readonly EvidenceRow[] | null | undefined): InvestigationCheckRef[] {
  const out: InvestigationCheckRef[] = [];
  for (const row of checks ?? []) {
    out.push({
      key: row.name,
      status: checkStatus(row.status),
      origin: "snapshot_check",
      snapshot_number: projection.snapshot_number,
      current: true,
      target_ids: [],
      proof: "snapshot",
      evidence_id: null,
    });
  }
  for (const claim of projection.claims) {
    for (const evidence of claim.evidence) {
      out.push({
        key: evidence.key || evidence.title,
        status: checkStatus(evidence.status),
        origin: "claim_evidence",
        snapshot_number: evidence.snapshot_number,
        current: evidence.state === "current",
        target_ids: [...claim.target_ids],
        proof: evidence.mapping === "explicit" ? "snapshot" : "inferred",
        evidence_id: null,
      });
    }
  }
  const current = projection.evaluation_timeline?.current;
  if (current && current.revision_id === projection.revision_id) {
    for (const receipt of current.receipts) {
      if (receipt.superseded_by_newer_result) continue;
      out.push({
        key: receipt.title,
        status: checkStatus(receipt.status),
        origin: "receipt",
        snapshot_number: current.snapshot_number,
        current: true,
        target_ids: [],
        proof: receipt.trust === "typed_observation" ? "observed" : "snapshot",
        evidence_id: receipt.evidence_id || null,
      });
    }
  }
  return out;
}

/** Derive the read-only investigation context; pure and deterministic for identical inputs. */
export function investigationContext(input: InvestigationInput): InvestigationContext {
  const { projection, targets } = input;
  const subject = input.subject ?? { kind: "review" as const, id: projection.review_id };
  const byId = new Map(targets.map((target) => [target.target_id, target]));
  const linked = subjectTargets(projection, targets, subject);
  const ids = [...new Set(linked.ids.filter(Boolean))];
  const known = ids.filter((id) => byId.has(id));
  const sources = known.slice(0, MAX_CONTEXT_SOURCES).map((id): InvestigationSourceRef => {
    const target = byId.get(id)!;
    return {
      target_id: target.target_id,
      path: target.path,
      symbol: target.symbol,
      kind: target.kind,
      start_line: target.start_line,
      end_line: target.end_line,
      state: target.state,
      proof: linked.proof,
    };
  });

  const scope = new Set(known);
  const scoped = allChecks(projection, input.checks).filter(
    // Review-level checks are not proven to touch one target, so only a Review subject carries them.
    (check) => subject.kind === "review" || check.target_ids.some((id) => scope.has(id)),
  );

  const revision = input.revision && input.revision.id === projection.revision_id ? input.revision : null;
  const timeline = projection.evaluation_timeline?.current;
  const head = revision?.head_sha
    || (timeline && timeline.revision_id === projection.revision_id ? timeline.head_sha : "")
    || null;
  const base = revision?.base_sha || null;
  const unavailable = [
    ...(base ? [] : ["base_sha"]),
    ...(head ? [] : ["head_sha"]),
    ...(input.checks ? [] : ["snapshot_checks"]),
  ];

  return {
    version: INVESTIGATION_CONTEXT_VERSION,
    review_id: projection.review_id,
    revision_id: projection.revision_id,
    snapshot_number: projection.snapshot_number,
    snapshot_identity: snapshotIdentity(projection.review_id, projection.revision_id),
    historical: projection.historical,
    base_sha: base,
    head_sha: head,
    subject,
    relationship_proof: known.length > 0 ? linked.proof : "unknown",
    sources,
    sources_omitted: known.length - sources.length,
    unresolved_target_ids: ids.filter((id) => !byId.has(id)),
    checks: scoped.slice(0, MAX_CONTEXT_CHECKS),
    checks_omitted: Math.max(0, scoped.length - MAX_CONTEXT_CHECKS),
    unavailable,
  };
}
