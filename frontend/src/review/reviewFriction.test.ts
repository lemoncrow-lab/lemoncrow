import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  REVIEW_FRICTION_MAX_SESSIONS,
  REVIEW_FRICTION_RETENTION_MS,
  REVIEW_FRICTION_STORAGE_KEY,
  ReviewFrictionRecorder,
  clearReviewFriction,
  frictionSizeBucket,
  readReviewFriction,
  summarizeReviewFriction,
  type ReviewFrictionSession,
} from "./reviewFriction";

function clockAt(start: number) {
  let now = start;
  return { clock: () => now, advance: (ms: number) => { now += ms; } };
}

describe("review friction metrics", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("measures open to first Code part and first judgment, once each", () => {
    const time = clockAt(1_000);
    const recorder = new ReviewFrictionRecorder("review-a", { clock: time.clock });
    recorder.surface("brief");
    time.advance(4_000);
    recorder.surface("implementation");
    recorder.target();
    time.advance(6_000);
    recorder.judgment();
    time.advance(5_000);
    recorder.target();
    recorder.judgment();

    const [session] = readReviewFriction();
    expect(session.firstTargetMs).toBe(4_000);
    expect(session.firstJudgmentMs).toBe(10_000);
    expect(session.judgments).toBe(2);
  });

  it("counts surface switches and backtracks without double-counting an explicit Back", () => {
    const recorder = new ReviewFrictionRecorder("review-a");
    recorder.surface("brief");
    recorder.surface("implementation");
    recorder.surface("brief");
    expect(recorder.session.surfaceSwitches).toBe(2);
    expect(recorder.session.backtracks).toBe(1);

    recorder.surface("map");
    recorder.back("brief");
    recorder.surface("brief");
    expect(recorder.session.backtracks).toBe(2);

    // A Back that stays on the same surface never suppresses a later real backtrack.
    recorder.back("brief");
    recorder.surface("implementation");
    recorder.surface("brief");
    expect(recorder.session.backtracks).toBe(4);
  });

  it("records Tour use against the change-size bucket", () => {
    const small = new ReviewFrictionRecorder("tiny-review");
    small.size(2);
    small.surface("implementation");
    const large = new ReviewFrictionRecorder("large-review");
    large.size(120);
    large.surface("brief");
    large.surface("tour");

    const summary = summarizeReviewFriction(readReviewFriction());
    expect(summary.tourUseBySize.tiny).toEqual({ sessions: 1, tour: 0 });
    expect(summary.tourUseBySize.large).toEqual({ sessions: 1, tour: 1 });
  });

  it("separates right-rail opens from Context <-> Ask switches", () => {
    const recorder = new ReviewFrictionRecorder("review-a");
    recorder.rail("context");
    recorder.rail("ask");
    recorder.rail("context");
    recorder.rail(null);
    recorder.rail("ask");
    expect(recorder.session.railOpens).toBe(2);
    expect(recorder.session.railSwitches).toBe(2);
  });

  it("measures re-review time and reopened parts after a newer revision", () => {
    const time = clockAt(0);
    const recorder = new ReviewFrictionRecorder("review-a", { clock: time.clock });
    recorder.snapshot(1, 0);
    time.advance(10_000);
    recorder.snapshot(2, 0);
    recorder.snapshot(2, 3);
    time.advance(7_000);
    recorder.judgment();
    expect(recorder.session.rereview).toEqual({ startMs: 10_000, reopened: 3, firstJudgmentMs: 7_000 });
  });

  it("treats opening on reopened work as a re-review from the start", () => {
    const time = clockAt(0);
    const recorder = new ReviewFrictionRecorder("review-a", { clock: time.clock });
    recorder.snapshot(3, 0);
    recorder.snapshot(3, 2);
    time.advance(2_000);
    recorder.judgment();
    expect(recorder.session.rereview).toEqual({ startMs: 0, reopened: 2, firstJudgmentMs: 2_000 });
  });

  it("counts stacked notices and Ask default vs manual provider use", () => {
    const recorder = new ReviewFrictionRecorder("review-a");
    recorder.notices(1);
    recorder.notices(3);
    recorder.notices(1);
    recorder.ask(false);
    recorder.ask(false);
    recorder.askProviderChanged();
    recorder.ask(true);
    expect(recorder.session.noticeStacks).toBe(1);
    expect(recorder.session.noticePeak).toBe(3);
    const summary = summarizeReviewFriction(readReviewFriction());
    expect(summary).toMatchObject({ askDefault: 2, askManual: 1, askProviderChanges: 1, noticeStackIncidence: 1 });
  });

  it("derives mobile abandonment and desktop handoff", () => {
    const time = clockAt(0);
    const phone = new ReviewFrictionRecorder("review-a", { compact: true, clock: time.clock });
    phone.surface("brief");
    const phoneOnly = new ReviewFrictionRecorder("review-b", { compact: true, clock: time.clock });
    phoneOnly.surface("brief");
    time.advance(60_000);
    const desktop = new ReviewFrictionRecorder("review-a", { clock: time.clock });
    desktop.surface("implementation");
    desktop.judgment();

    const summary = summarizeReviewFriction(readReviewFriction());
    expect(summary.mobileSessions).toBe(2);
    expect(summary.mobileAbandoned).toBe(2);
    expect(summary.mobileHandoffs).toBe(1);
  });

  it("keeps at most the newest sessions and drops expired ones on every write", () => {
    const time = clockAt(0);
    const old = new ReviewFrictionRecorder("old", { clock: time.clock });
    old.surface("brief");
    time.advance(REVIEW_FRICTION_RETENTION_MS + 1);
    for (let index = 0; index < REVIEW_FRICTION_MAX_SESSIONS + 5; index += 1) {
      new ReviewFrictionRecorder(`review-${index}`, { clock: time.clock }).surface("implementation");
      time.advance(1);
    }
    const sessions = readReviewFriction();
    expect(sessions).toHaveLength(REVIEW_FRICTION_MAX_SESSIONS);
    expect(sessions.some((session) => session.review === "old")).toBe(false);
    expect(sessions[sessions.length - 1].review).toBe(`review-${REVIEW_FRICTION_MAX_SESSIONS + 4}`);
    clearReviewFriction();
    expect(readReviewFriction()).toEqual([]);
  });

  it("keeps a session exactly 30 days old and drops one 30 days + 1 ms old on the next write", () => {
    const time = clockAt(0);
    new ReviewFrictionRecorder("past-bound", { clock: time.clock }).surface("brief");
    time.advance(1);
    new ReviewFrictionRecorder("at-bound", { clock: time.clock }).surface("brief");
    // Now the first session is RETENTION + 1 ms old and the second exactly RETENTION.
    time.advance(REVIEW_FRICTION_RETENTION_MS);
    new ReviewFrictionRecorder("fresh", { clock: time.clock }).surface("implementation");
    expect(readReviewFriction().map((session) => session.review)).toEqual(["at-bound", "fresh"]);
  });

  it("stores counters only and never touches the network", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const beacon = vi.fn(() => true);
    Object.defineProperty(navigator, "sendBeacon", { configurable: true, value: beacon });
    const recorder = new ReviewFrictionRecorder("review-a");
    recorder.surface("brief");
    recorder.surface("implementation");
    recorder.target();
    recorder.judgment();
    recorder.rail("ask");
    recorder.ask(true);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(beacon).not.toHaveBeenCalled();
    const raw = window.localStorage.getItem(REVIEW_FRICTION_STORAGE_KEY) ?? "";
    const [session] = JSON.parse(raw) as ReviewFrictionSession[];
    expect(Object.keys(session).sort()).toEqual([
      "askDefault", "askManual", "askProviderChanges", "backtracks", "compact", "firstJudgmentMs",
      "firstTargetMs", "id", "judgments", "noticePeak", "noticeStacks", "railOpens", "railSwitches",
      "rereview", "review", "size", "startedAt", "surfaceSwitches", "surfaces", "tourUsed", "updatedAt", "v",
    ]);
  });

  it("buckets change size", () => {
    expect([0, 1, 3, 4, 15, 16, 60, 61].map(frictionSizeBucket)).toEqual([
      "unknown", "tiny", "tiny", "small", "small", "medium", "medium", "large",
    ]);
  });
});
