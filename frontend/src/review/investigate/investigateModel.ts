// Investigate (N06): one target's investigation, in task order -- X-Ray, its
// assumptions, What if, its blind spots, Challenge, the human decision -- as a
// pure, read-only projection over what the Reader already holds. It owns no
// state of its own: the held claim is the Review's one held challenge, each
// surface stays keyed by its Snapshot, and anything the inputs do not carry is
// said to be missing rather than shown as clear.
import { blindSpotsModel, type BlindSpotItem } from "../blindSpots/blindSpotsModel";
import { claimDigest, type ChallengeClaim } from "../challenge/challengeModel";
import type { HeldChallenge } from "../challenge/heldChallenge";
import { investigationContext, type InvestigationContext, type InvestigationInput } from "../investigation/investigationContext";
import type { ReviewMapLocation } from "../reviewMapLocation";
import type { EvidenceRow, ReviewMapGraph, ReviewProjection, ReviewProjectionDecision, ReviewTarget } from "../types";
import { whatIfTargets } from "../whatIf/whatIfModel";
import { encodeXRayLocation, graphSnapshot } from "../xray/xrayModel";

export type InvestigateStep = "xray" | "assumptions" | "what_if" | "blind_spots" | "challenge" | "decision";

export const INVESTIGATE_STEPS: readonly { key: InvestigateStep; label: string }[] = [
  { key: "xray", label: "X-Ray" },
  { key: "assumptions", label: "Assumptions" },
  { key: "what_if", label: "What if" },
  { key: "blind_spots", label: "Blind spots" },
  { key: "challenge", label: "Challenge" },
  { key: "decision", label: "Decision" },
];

export interface InvestigateFact {
  label: string;
  value: string;
}

export interface InvestigateInput {
  projection: ReviewProjection | null;
  targets: readonly ReviewTarget[];
  target: ReviewTarget;
  checks?: readonly EvidenceRow[] | null;
  revision?: InvestigationInput["revision"];
  /** The host can load this Snapshot's Map graph. */
  map: boolean;
}

export interface InvestigateModel {
  target: ReviewTarget;
  /** Recorded facts about the target itself; present with or without a projection. */
  facts: InvestigateFact[];
  /** False without a projection: only the source and its facts are shown. */
  analysed: boolean;
  /** The target-subject context of the Snapshot shown now; null without a projection. */
  context: InvestigationContext | null;
  /** The Review-subject context the Brief uses, so claims read here are the Brief's claims. */
  reviewContext: InvestigationContext | null;
  walk: { available: boolean; reason: string };
  whatIf: { available: boolean; targetId: string | null; reason: string };
  blindSpots: {
    items: BlindSpotItem[];
    /** Items past a category's display bound: they may include this target, so they are counted, not hidden. */
    omitted: number;
  };
  decisions: ReviewProjectionDecision[];
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

const STATE_LABEL: Record<string, string> = {
  unreviewed: "Not reviewed",
  reviewed: "Reviewed",
  needs_changes: "Needs changes",
};

export function targetFacts(target: ReviewTarget): InvestigateFact[] {
  const lines = target.start_line > 0
    ? target.end_line > target.start_line ? `${target.start_line}–${target.end_line}` : String(target.start_line)
    : "";
  const facts: InvestigateFact[] = [
    { label: "Source", value: lines ? `${target.path}:${lines}` : target.path },
    { label: "Kind", value: target.symbol ? `${target.kind} · ${target.symbol}` : target.kind },
    { label: "Changed", value: `+${target.additions} −${target.deletions}` },
    { label: "Your review", value: STATE_LABEL[target.state] ?? target.state },
  ];
  const { pass, fail, unknown } = target.verification;
  facts.push({
    label: "Checks on it",
    value: pass + fail + unknown > 0
      ? [fail ? `${fail} failed` : "", unknown ? `${unknown} without a result` : "", pass ? `${pass} passed` : ""].filter(Boolean).join(" · ")
      : "None recorded",
  });
  const open = target.annotation_counts.open + target.annotation_counts.orphaned;
  if (open > 0) facts.push({ label: "Open comments", value: String(open) });
  return facts;
}

/** The changed target What if starts on: this one, else the first contract in its file. */
export function whatIfTargetFor(targets: readonly ReviewTarget[], target: ReviewTarget): string | null {
  const candidates = whatIfTargets(targets);
  if (candidates.some((row) => row.target_id === target.target_id)) return target.target_id;
  return candidates.find((row) => row.path === target.path)?.target_id ?? null;
}

/** A decision a human still owes on this target; withdrawn and settled ones are not. */
export function openDecisionsFor(projection: ReviewProjection, targetId: string): ReviewProjectionDecision[] {
  return projection.decisions.filter((decision) =>
    decision.target_ids.includes(targetId)
    && !decision.state?.withdrawn
    && (!decision.state || decision.state.outstanding));
}

export function investigateModel(input: InvestigateInput): InvestigateModel {
  const { projection, targets, target, checks, revision } = input;
  const facts = targetFacts(target);
  const walk = input.map
    ? { available: true, reason: "" }
    : { available: false, reason: "This Snapshot has no Map graph to walk, so there is no X-Ray from here." };
  if (!projection) {
    return {
      target,
      facts,
      analysed: false,
      context: null,
      reviewContext: null,
      walk,
      whatIf: { available: false, targetId: null, reason: "The Review's analysis of this Snapshot is not loaded." },
      blindSpots: { items: [], omitted: 0 },
      decisions: [],
    };
  }
  const context = investigationContext({ projection, targets, checks, revision, subject: { kind: "target", id: target.target_id } });
  const reviewContext = investigationContext({ projection, targets, checks, revision });
  const whatIfTarget = whatIfTargetFor(targets, target);
  const whatIf = projection.context === undefined
    ? { available: false, targetId: null, reason: "What if needs this Snapshot's impact data, which this Review does not carry." }
    : whatIfTarget
      ? { available: true, targetId: whatIfTarget, reason: "" }
      : { available: true, targetId: null, reason: `No changed function, class, section or dependency in ${target.path} to start from; choose another changed target.` };
  // The Brief's own model, filtered: the same items, ids and claims, only this target's.
  // lc-debt: rebuilt over the whole Review per target; share one memoized model with the Brief if Reviews grow past the category bound often.
  const spots = blindSpotsModel({
    context: reviewContext,
    projection,
    targets,
    capabilities: { openSource: true, evidence: true, ask: false, decide: false },
  });
  const items = spots.categories.flatMap((category) =>
    category.state === "known"
      ? category.items.filter((item) => item.anchor.level === "target" && item.anchor.targetId === target.target_id)
      : []);
  const omitted = spots.categories.reduce((sum, category) => sum + (category.state === "known" ? category.omitted : 0), 0);
  return {
    target,
    facts,
    analysed: true,
    context,
    reviewContext,
    walk,
    whatIf,
    blindSpots: { items, omitted },
    decisions: openDecisionsFor(projection, target.target_id),
  };
}

/**
 * The Map location that opens the X-Ray rooted at *target*'s file on *graph*:
 * the Architecture Δ projection (it carries the base comparison), its Change
 * Story, and `map-xray` on the side the file exists in. Null when no node on
 * the graph is that file.
 */
export function xrayLocationFor(graph: ReviewMapGraph | null, target: Pick<ReviewTarget, "path">): ReviewMapLocation | null {
  if (!graph || !target.path) return null;
  const onPath = graph.nodes.filter((node) => node.path === target.path);
  const node = onPath.find((row) => row.kind === "file" && row.exactness === "snapshot")
    ?? onPath.find((row) => row.kind === "file")
    ?? onPath[0];
  if (!node) return null;
  const side = node.delta_status === "removed" ? "before" : "after";
  return {
    level: "architecture",
    projection: "architecture_delta",
    focus: "",
    node: node.id,
    edge: "",
    path: target.path,
    story: node.id,
    xray: encodeXRayLocation({ side, snapshot: graphSnapshot(graph), rootId: node.id }),
  };
}

/** Holding *claim*: the same claim on the same Snapshot keeps its run; anything else starts clean (the Brief's rule). */
export function holdClaim(held: HeldChallenge | null, claim: ChallengeClaim): HeldChallenge {
  const same = held
    && held.claim.snapshotIdentity === claim.snapshotIdentity
    && claimDigest(held.claim) === claimDigest(claim);
  return { claim, run: same ? held.run : null };
}

export function blindSpotNote(model: InvestigateModel): string {
  const { items, omitted } = model.blindSpots;
  const more = omitted > 0 ? ` ${plural(omitted, "more item")} past the display bound may include this code; the Brief's Blind spots counts them.` : "";
  return items.length === 0
    ? `No known blind spot is anchored on this code. That is not the same as verified: Review-level gaps and what was not measured are in the Brief's Blind spots.${more}`
    : more.trim();
}
