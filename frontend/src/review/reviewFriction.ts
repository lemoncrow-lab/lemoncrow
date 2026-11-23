/**
 * Local-only Review friction metrics (Phase 14 item 2).
 *
 * Measures task friction, not usage: how long until the reviewer reaches code
 * and makes a first judgment, how often they switch or backtrack between
 * surfaces, when Guided review is used relative to change size, right-rail
 * mode switches, re-review time and reopened targets after a correction loop,
 * notice stacking, Ask default-vs-manual provider use and mobile abandonment.
 *
 * Privacy boundary: records live only in this browser's localStorage. There is
 * no network path in this module; it never calls the server, the product
 * telemetry endpoints (`/telemetry/*`) or the remote exporter, so the existing
 * opt-in remote telemetry consent is neither needed nor bypassed. Records hold
 * counters and durations only: no paths, symbols, comment or Ask text.
 *
 * Retention: at most REVIEW_FRICTION_MAX_SESSIONS sessions and nothing older
 * than REVIEW_FRICTION_RETENTION_MS; both are enforced on every write.
 * `clearReviewFriction()` removes everything.
 *
 * The four counts that do leave the browser live in `reviewFrictionCounts.ts`.
 */

import { useEffect } from "react";

export const REVIEW_FRICTION_STORAGE_KEY = "lc.review.friction.v1";
export const REVIEW_FRICTION_MAX_SESSIONS = 50;
export const REVIEW_FRICTION_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
/** Set by this browser's first human judgment: the "first useful Review" settings changes are counted against. */
export const REVIEW_FIRST_JUDGMENT_KEY = "lc.review.friction.first-judgment.v1";

export type FrictionSurface = "brief" | "tour" | "implementation" | "map";
export type FrictionRail = "context" | "ask" | null;
export type FrictionSizeBucket = "tiny" | "small" | "medium" | "large" | "unknown";

export interface ReviewFrictionSession {
  v: 1;
  id: string;
  /** Opaque Review id; never a path or title. */
  review: string;
  startedAt: number;
  updatedAt: number;
  /** Opened in the phone composition. */
  compact: boolean;
  size: FrictionSizeBucket;
  /** Open -> first Code view with an active target (ms). */
  firstTargetMs: number | null;
  /** Open -> first human judgment: mark, comment, decision or verdict (ms). */
  firstJudgmentMs: number | null;
  judgments: number;
  surfaceSwitches: number;
  /** A return to the surface visited just before, or an explicit in-Review Back. */
  backtracks: number;
  surfaces: FrictionSurface[];
  tourUsed: boolean;
  railOpens: number;
  /** Context <-> Ask without closing the rail. */
  railSwitches: number;
  /** Moments where more than one notice competed (blocking + context signals). */
  noticeStacks: number;
  noticePeak: number;
  askDefault: number;
  askManual: number;
  askProviderChanges: number;
  /** Set when this session opened on, or received, a newer Snapshot with reopened work. */
  rereview: { startMs: number; reopened: number; firstJudgmentMs: number | null } | null;
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function isSession(value: unknown): value is ReviewFrictionSession {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<ReviewFrictionSession>;
  return record.v === 1 && typeof record.id === "string" && typeof record.review === "string"
    && typeof record.startedAt === "number" && typeof record.updatedAt === "number";
}

export function readReviewFriction(): ReviewFrictionSession[] {
  const store = storage();
  if (!store) return [];
  try {
    const parsed: unknown = JSON.parse(store.getItem(REVIEW_FRICTION_STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter(isSession) : [];
  } catch {
    return [];
  }
}

export function clearReviewFriction(): void {
  try {
    storage()?.removeItem(REVIEW_FRICTION_STORAGE_KEY);
    storage()?.removeItem(REVIEW_FIRST_JUDGMENT_KEY);
  } catch {
    // Storage unavailable: nothing was kept.
  }
}

/** Bounded write: drops expired sessions, then keeps the newest MAX_SESSIONS. */
function writeSession(session: ReviewFrictionSession, now: number): void {
  const store = storage();
  if (!store) return;
  const kept = readReviewFriction()
    .filter((item) => item.id !== session.id && now - item.updatedAt <= REVIEW_FRICTION_RETENTION_MS);
  kept.push(session);
  kept.sort((left, right) => left.updatedAt - right.updatedAt);
  try {
    store.setItem(REVIEW_FRICTION_STORAGE_KEY, JSON.stringify(kept.slice(-REVIEW_FRICTION_MAX_SESSIONS)));
  } catch {
    // Quota or privacy mode: friction metrics are best-effort and never block review.
  }
}

export function frictionSizeBucket(targetCount: number | null | undefined): FrictionSizeBucket {
  if (targetCount == null || !Number.isFinite(targetCount) || targetCount <= 0) return "unknown";
  if (targetCount <= 3) return "tiny";
  if (targetCount <= 15) return "small";
  if (targetCount <= 60) return "medium";
  return "large";
}

function sessionId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }
}

/** One page-load of one Review. Every method is cheap, synchronous and never throws. */
export class ReviewFrictionRecorder {
  readonly session: ReviewFrictionSession;
  private readonly clock: () => number;
  private lastRail: FrictionRail = null;
  private backTo: FrictionSurface | null = null;
  private lastRevision: number | null = null;
  private pendingRereview = false;

  constructor(review: string, options: { compact?: boolean; clock?: () => number } = {}) {
    this.clock = options.clock ?? (() => Date.now());
    const now = this.clock();
    this.session = {
      v: 1,
      id: sessionId(),
      review,
      startedAt: now,
      updatedAt: now,
      compact: Boolean(options.compact),
      size: "unknown",
      firstTargetMs: null,
      firstJudgmentMs: null,
      judgments: 0,
      surfaceSwitches: 0,
      backtracks: 0,
      surfaces: [],
      tourUsed: false,
      railOpens: 0,
      railSwitches: 0,
      noticeStacks: 0,
      noticePeak: 0,
      askDefault: 0,
      askManual: 0,
      askProviderChanges: 0,
      rereview: null,
    };
  }

  private elapsed(): number {
    return Math.max(0, this.clock() - this.session.startedAt);
  }

  private save(): void {
    const now = this.clock();
    this.session.updatedAt = now;
    writeSession(this.session, now);
  }

  size(targetCount: number): void {
    const bucket = frictionSizeBucket(targetCount);
    if (bucket === this.session.size) return;
    this.session.size = bucket;
    this.save();
  }

  surface(view: FrictionSurface): void {
    const visited = this.session.surfaces;
    const current = visited[visited.length - 1];
    if (current === view) return;
    const counted = this.backTo === view;
    this.backTo = null;
    if (current !== undefined) {
      this.session.surfaceSwitches += 1;
      if (!counted && visited[visited.length - 2] === view) this.session.backtracks += 1;
    }
    // Bounded: the trail only needs the last two surfaces plus a short history.
    this.session.surfaces = [...visited, view].slice(-12);
    if (view === "tour") this.session.tourUsed = true;
    this.save();
  }

  /** An explicit in-Review Back (backtrail) to *to*; the surface change it causes is not counted twice. */
  back(to: FrictionSurface): void {
    this.session.backtracks += 1;
    const surfaces = this.session.surfaces;
    this.backTo = surfaces[surfaces.length - 1] === to ? null : to;
    this.save();
  }

  target(): void {
    if (this.session.firstTargetMs !== null) return;
    this.session.firstTargetMs = this.elapsed();
    this.save();
  }

  judgment(): void {
    const elapsed = this.elapsed();
    this.session.judgments += 1;
    if (this.session.firstJudgmentMs === null) this.session.firstJudgmentMs = elapsed;
    if (this.session.rereview && this.session.rereview.firstJudgmentMs === null) {
      this.session.rereview.firstJudgmentMs = Math.max(0, elapsed - this.session.rereview.startMs);
    }
    this.pendingRereview = false;
    try {
      storage()?.setItem(REVIEW_FIRST_JUDGMENT_KEY, "1");
    } catch {
      // Storage unavailable: settings changes keep counting as before the first Review.
    }
    this.save();
  }

  rail(mode: FrictionRail): void {
    const previous = this.lastRail;
    if (previous === mode) return;
    this.lastRail = mode;
    if (mode === null) return;
    if (previous === null) this.session.railOpens += 1;
    else this.session.railSwitches += 1;
    this.save();
  }

  /** The number of notices competing for the frame right now. */
  notices(count: number): void {
    if (count <= this.session.noticePeak && count <= 1) return;
    if (count > 1) this.session.noticeStacks += 1;
    this.session.noticePeak = Math.max(this.session.noticePeak, count);
    this.save();
  }

  ask(manualProvider: boolean): void {
    if (manualProvider) this.session.askManual += 1;
    else this.session.askDefault += 1;
    this.save();
  }

  askProviderChanged(): void {
    this.session.askProviderChanges += 1;
    this.save();
  }

  /**
   * The displayed Snapshot and how many of its targets changed since the
   * reviewer's marks. Opening on reopened work, or receiving a newer Snapshot
   * while open, starts a re-review measurement.
   */
  snapshot(revisionNumber: number, reopened: number): void {
    const previous = this.lastRevision;
    this.lastRevision = revisionNumber;
    if (previous !== null && revisionNumber > previous) {
      this.pendingRereview = true;
      this.session.rereview = { startMs: this.elapsed(), reopened, firstJudgmentMs: null };
      this.save();
      return;
    }
    if (reopened <= 0) return;
    if (!this.session.rereview) {
      // Opened on reopened work: the re-review starts with the session.
      if (this.session.judgments > 0) return;
      this.pendingRereview = true;
      this.session.rereview = { startMs: 0, reopened, firstJudgmentMs: null };
      this.save();
      return;
    }
    // Progress for the new Snapshot can land after the Snapshot itself.
    if (this.pendingRereview && reopened > this.session.rereview.reopened) {
      this.session.rereview.reopened = reopened;
      this.save();
    }
  }
}

let activeRecorder: ReviewFrictionRecorder | null = null;

/** The Reader that is showing a Review owns the recorder other Review surfaces report to. */
export function setActiveReviewFriction(recorder: ReviewFrictionRecorder | null): void {
  activeRecorder = recorder;
}

export function activeReviewFriction(): ReviewFrictionRecorder | null {
  return activeRecorder;
}

/**
 * Reports the Reader's derived shell state to the active recorder. Rendered
 * by the Reader once it shows a Review; renders nothing.
 */
export function ReviewFrictionProbe({
  view,
  onTarget,
  targetCount,
  rail,
  notices,
  revisionNumber,
  reopened,
}: {
  view: FrictionSurface;
  onTarget: boolean;
  targetCount: number;
  rail: FrictionRail;
  notices: number;
  revisionNumber: number;
  reopened: number;
}): null {
  useEffect(() => { activeReviewFriction()?.size(targetCount); }, [targetCount]);
  useEffect(() => { activeReviewFriction()?.surface(view); }, [view]);
  useEffect(() => { if (onTarget) activeReviewFriction()?.target(); }, [onTarget]);
  useEffect(() => { activeReviewFriction()?.rail(rail); }, [rail]);
  useEffect(() => { activeReviewFriction()?.notices(notices); }, [notices]);
  useEffect(() => { activeReviewFriction()?.snapshot(revisionNumber, reopened); }, [revisionNumber, reopened]);
  return null;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : Math.round((sorted[middle - 1] + sorted[middle]) / 2);
}

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  return Math.round((values.reduce((sum, value) => sum + value, 0) / values.length) * 100) / 100;
}

export interface ReviewFrictionSummary {
  sessions: number;
  medianOpenToFirstTargetMs: number | null;
  medianOpenToFirstJudgmentMs: number | null;
  meanSurfaceSwitches: number | null;
  meanBacktracks: number | null;
  /** Share of sessions per size bucket that entered Guided review. */
  tourUseBySize: Partial<Record<FrictionSizeBucket, { sessions: number; tour: number }>>;
  meanRailSwitches: number | null;
  medianRereviewMs: number | null;
  meanReopenedTargets: number | null;
  /** Share of sessions in which more than one notice competed. */
  noticeStackIncidence: number | null;
  askDefault: number;
  askManual: number;
  askProviderChanges: number;
  mobileSessions: number;
  /** Phone sessions that ended without any human judgment. */
  mobileAbandoned: number;
  /** Phone sessions without a judgment followed by a desktop session of the same Review. */
  mobileHandoffs: number;
}

export function summarizeReviewFriction(sessions: readonly ReviewFrictionSession[]): ReviewFrictionSummary {
  const tourUseBySize: ReviewFrictionSummary["tourUseBySize"] = {};
  for (const session of sessions) {
    const bucket = tourUseBySize[session.size] ?? { sessions: 0, tour: 0 };
    bucket.sessions += 1;
    if (session.tourUsed) bucket.tour += 1;
    tourUseBySize[session.size] = bucket;
  }
  const rereviews = sessions.map((session) => session.rereview).filter((value): value is NonNullable<typeof value> => value !== null);
  const mobile = sessions.filter((session) => session.compact);
  const abandoned = mobile.filter((session) => session.judgments === 0);
  const byStart = [...sessions].sort((left, right) => left.startedAt - right.startedAt);
  const handoffs = abandoned.filter((session) =>
    byStart.some((later) => !later.compact && later.review === session.review && later.startedAt > session.startedAt));
  return {
    sessions: sessions.length,
    medianOpenToFirstTargetMs: median(sessions.map((session) => session.firstTargetMs).filter((value): value is number => value !== null)),
    medianOpenToFirstJudgmentMs: median(sessions.map((session) => session.firstJudgmentMs).filter((value): value is number => value !== null)),
    meanSurfaceSwitches: mean(sessions.map((session) => session.surfaceSwitches)),
    meanBacktracks: mean(sessions.map((session) => session.backtracks)),
    tourUseBySize,
    meanRailSwitches: mean(sessions.map((session) => session.railSwitches)),
    medianRereviewMs: median(rereviews.map((item) => item.firstJudgmentMs).filter((value): value is number => value !== null)),
    meanReopenedTargets: mean(rereviews.map((item) => item.reopened)),
    noticeStackIncidence: sessions.length ? Math.round((sessions.filter((session) => session.noticeStacks > 0).length / sessions.length) * 100) / 100 : null,
    askDefault: sessions.reduce((sum, session) => sum + session.askDefault, 0),
    askManual: sessions.reduce((sum, session) => sum + session.askManual, 0),
    askProviderChanges: sessions.reduce((sum, session) => sum + session.askProviderChanges, 0),
    mobileSessions: mobile.length,
    mobileAbandoned: abandoned.length,
    mobileHandoffs: handoffs.length,
  };
}
