/**
 * P11-1: what the repository's own tests say about each changed part, one record per Snapshot.
 *
 * The server stores one outcome per (review, revision, changed file), pinned to the exact base and head commit,
 * and answers with a default plus the exceptions in ONE request per revision. Every screen reads that one record
 * (the Focus card, Quick check rows, All files and a reviewer's own Run test), so they cannot disagree. A part is
 * never blank: with no record it reads "Not run yet" or "Couldn't run" with the reason, and "Not tested" is its own
 * neutral outcome, never a pass. Reach is by the import graph at file level, never line or symbol coverage.
 *
 * Cost: one GET per revision, plus a bounded-backoff re-read per revision (never per row) while the record is still
 * being written; a bounded number of automatic runs per revision for the page session. A lookup is a map read, so
 * nothing here scales the DOM or the request count with the number of files.
 */
import { useEffect, useSyncExternalStore } from "react";
import { requestBrowserJson } from "./reviewApi";

export type ProofOutcome = "proven" | "exercised" | "fails" | "not_tested" | "couldnt_run" | "not_run_yet" | "over_budget";

export interface WireOutcome {
  o: ProofOutcome;
  /** For "couldnt_run": a key of `reasons`. */
  r?: string;
  /** Up to three test files that reach the part. */
  t?: string[];
  /** How many test files reach the part. */
  n?: number;
}

export interface PartProofProjection {
  revision_id: string;
  head_identity: string;
  base_identity: string;
  reach: "file";
  /** What every part without its own entry shows. */
  default: WireOutcome;
  files: Record<string, WireOutcome>;
  recorded: boolean;
  source: string;
  labels: Record<string, string>;
  details: Record<string, string>;
  reasons: Record<string, string>;
  /** Another request is already running this Snapshot's tests. */
  running?: boolean;
  /** The server finished its plan for this Snapshot. False: parts that tests reach may still be waiting. */
  complete?: boolean;
  /** Only on the client's own stand-in when the record could not be read: it says nothing about any test. */
  load_failed?: boolean;
}

export const OUTCOME_LABELS: Record<ProofOutcome, string> = {
  proven: "Proven",
  exercised: "Exercised",
  fails: "Fails",
  not_tested: "Not tested",
  couldnt_run: "Couldn't run",
  not_run_yet: "Not run yet",
  over_budget: "Not run: over this review's budget",
};

const OUTCOME_DETAILS: Record<ProofOutcome, string> = {
  proven: "A test that reaches this file fails on the base and passes here.",
  exercised: "A test that reaches this file passes. Reach is by the file's imports, not by line or symbol.",
  fails: "A test that reaches this file fails.",
  not_tested: "No test reaches this file through the import graph.",
  couldnt_run: "The tests could not run.",
  not_run_yet: "Tests reach this file but have not run yet.",
  over_budget: "Tests reach this file; they did not run because this review's minutes are spent.",
};

export type ProofTone = "success" | "warning" | "neutral";

export interface PartOutcomeView {
  outcome: ProofOutcome;
  /** Always a non-empty label from the fixed vocabulary. */
  label: string;
  /** One sentence for the title and the Focus card; always non-empty, always says reach is by imports. */
  text: string;
  tone: ProofTone;
  /** Test files that reach the part (at most three), and how many do. */
  tests: string[];
  reaching: number;
  /** The record could not be read at all: the label is neutral and makes no claim about tests. */
  unavailable: boolean;
  /**
   * Non-empty only when the repository's CI reported this part's result (a hosted review): "Reported by CI on <sha>, not
   * run by LemonCrow". A part CI said nothing about has no source line: there is no CI result to attribute.
   */
  ci: string;
  /** The repository declares no test command (`no_recipe`): nothing can run, so screens say it once, quietly. */
  noTestCommand: boolean;
}

/** The one quiet line a screen shows when the repository declares no test command. */
export const NO_TEST_COMMAND = "No test command set up";
const NO_RECIPE = "no_recipe";

/** The whole revision has no test command: its default outcome is "couldn't run" for want of a recipe. */
export function revisionHasNoTestCommand(proof: PartProofProjection | null | undefined): boolean {
  return Boolean(proof && !proof.load_failed && proof.default?.o === "couldnt_run" && proof.default.r === NO_RECIPE);
}

/** The projection's `source` for a result the repository's CI reported; LemonCrow ran no test for it. */
export const CI_SOURCE = "ci";

/** The exact line a CI-reported result carries on every screen. The sha is the Snapshot's head the report was pinned to. */
export function ciSourceLine(headIdentity: string): string {
  const sha = headIdentity.trim().slice(0, 7);
  return sha ? `Reported by CI on ${sha}, not run by LemonCrow` : "";
}

const OUTCOMES = new Set<string>(Object.keys(OUTCOME_LABELS));
const TONES: Record<ProofOutcome, ProofTone> = {
  proven: "success",
  exercised: "neutral",
  fails: "warning",
  not_tested: "neutral",
  couldnt_run: "warning",
  not_run_yet: "neutral",
  over_budget: "neutral",
};

/**
 * The reasons the server gives a part whose CI check it could not believe (P12-2): the check is not from a workflow
 * file the pull request leaves as the base has it, GitHub's records of it could not be read, GitHub's identity
 * token does not vouch for the report, or (P13-2) the report says the run was not in a sandbox.
 */
const UNTRUSTED_CI_REASONS = new Set<string>([
  "untrusted_workflow",
  "workflow_unreadable",
  "proof_report_missing",
  "proof_report_invalid",
  "proof_report_unattested",
  "proof_unsandboxed",
]);

/** The part's outcome in the shared vocabulary. With no record yet (or an unreadable one) it is "Not run yet", never blank. */
export function partOutcome(proof: PartProofProjection | null | undefined, path: string): PartOutcomeView {
  if (proof?.load_failed) {
    return {
      outcome: "couldnt_run",
      label: "Couldn't load",
      text: "The proof record could not be loaded. This says nothing about whether tests reach or pass this file.",
      tone: "neutral",
      tests: [],
      reaching: 0,
      unavailable: true,
      ci: "",
      noTestCommand: false,
    };
  }
  const raw = proof?.files?.[path] ?? proof?.default;
  const outcome: ProofOutcome = raw && OUTCOMES.has(raw.o) ? raw.o : "not_run_yet";
  const labels = proof?.labels ?? {};
  const label = labels[outcome]?.trim() || OUTCOME_LABELS[outcome];
  let text = proof?.details?.[outcome]?.trim() || OUTCOME_DETAILS[outcome];
  if (outcome === "couldnt_run") {
    const reason = raw?.r ? proof?.reasons?.[raw.r]?.trim() : "";
    text = reason ? `Couldn't run: ${reason}.` : "Couldn't run: the tests did not complete.";
  }
  // P12-2: a check that could not be believed is not a result CI reported, so it carries no "Reported by CI" line.
  const ci =
    proof?.source === CI_SOURCE && proof.files?.[path] && !(raw?.r && UNTRUSTED_CI_REASONS.has(raw.r))
      ? ciSourceLine(proof.head_identity ?? "")
      : "";
  const noTestCommand = outcome === "couldnt_run" && raw?.r === NO_RECIPE && !ci;
  return { outcome, label, text, tone: TONES[outcome], tests: raw?.t ?? [], reaching: raw?.n ?? 0, unavailable: false, ci, noTestCommand };
}

/** "Reached by tests/test_a.py and 2 more, by imports." (file-level reach; never a coverage claim). */
export function reachLine(view: PartOutcomeView): string {
  if (view.tests.length === 0) return "";
  const more = view.reaching > view.tests.length ? ` and ${view.reaching - view.tests.length} more` : "";
  return `Reached by ${view.tests.join(", ")}${more}, by imports (not line or symbol coverage).`;
}

// ------------------------------------------------------------------ the shared store

interface Entry {
  proof: PartProofProjection | null;
  /** A GET or this tab's run is in flight. */
  busy: boolean;
  /** The last read failed and no record is held: screens show a neutral "couldn't load", never "Not run yet". */
  failed: boolean;
}

/** Per revision, kept apart from what the screens read: the GET, the runs and the re-reads are tracked separately. */
interface Track {
  loading: Promise<void> | null;
  loaded: boolean;
  posting: boolean;
  runs: number;
  polls: number;
  watchers: number;
  timer: ReturnType<typeof setTimeout> | null;
  /** Requests are numbered when they start; a response older than one already applied is dropped. */
  issued: number;
  applied: number;
  want: { autoRun: boolean; inView: string[] };
}

/** Automatic runs per revision for the page session: the first, plus resumes of an interrupted run. */
export const MAX_AUTO_RUNS = 3;
/** Re-reads per revision while the record is incomplete or running, then the reader reloads. */
export const MAX_POLLS = 12;
const POLL_FIRST_MS = 2000;
const POLL_CAP_MS = 15000;

const entries = new Map<string, Entry>();
const tracks = new Map<string, Track>();
const listeners = new Set<() => void>();
const keyOf = (reviewId: string, revisionId: string) => `${reviewId}\u0000${revisionId}`;

function trackOf(key: string): Track {
  let track = tracks.get(key);
  if (!track) {
    track = { loading: null, loaded: false, posting: false, runs: 0, polls: 0, watchers: 0, timer: null, issued: 0, applied: 0, want: { autoRun: false, inView: [] } };
    tracks.set(key, track);
  }
  return track;
}

function put(key: string, entry: Entry) {
  entries.set(key, entry);
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** What a screen reads: the record, or (when it could not be read and none is held) a neutral stand-in. */
function visible(entry: Entry | undefined): PartProofProjection | null {
  if (!entry) return null;
  if (entry.proof) return entry.proof;
  return entry.failed ? LOAD_FAILED : null;
}

const LOAD_FAILED: PartProofProjection = {
  revision_id: "",
  head_identity: "",
  base_identity: "",
  reach: "file",
  default: { o: "couldnt_run", r: "load_failed" },
  files: {},
  recorded: false,
  source: "client",
  labels: {},
  details: {},
  reasons: {},
  running: false,
  complete: false,
  load_failed: true,
};

export function getPartProof(reviewId: string, revisionId: string): PartProofProjection | null {
  return visible(entries.get(keyOf(reviewId, revisionId)));
}

function base(reviewId: string): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/part-proof`;
}

/** A record for another revision is refused: a screen on revision B never shows revision A's outcomes. */
function accept(revisionId: string, proof: PartProofProjection): PartProofProjection | null {
  return proof && proof.revision_id === revisionId && OUTCOMES.has(proof.default?.o) ? proof : null;
}

export function fetchPartProof(reviewId: string, revisionId: string): Promise<PartProofProjection> {
  return requestBrowserJson<PartProofProjection>(`${base(reviewId)}?revision_id=${encodeURIComponent(revisionId)}`);
}

/** Run this Snapshot's tests that reach the changed parts, the parts in view first (the server bounds and meters it). */
export function postPartProofRun(reviewId: string, revisionId: string, inView: string[]): Promise<PartProofProjection> {
  return requestBrowserJson<PartProofProjection>(`${base(reviewId)}/run`, {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId, in_view: inView.slice(0, 200) }),
  });
}

/** The record still has parts to write: the server's run is underway or was interrupted. */
function pending(entry: Entry | undefined): boolean {
  if (!entry) return false;
  const proof = entry.proof;
  if (!proof) return entry.failed;
  if (proof.running) return true;
  return proof.recorded ? proof.complete === false : proof.default.o === "not_run_yet";
}

/** A run from this tab would help: nothing was recorded yet, or a recorded run stopped between batches. */
function needsRun(proof: PartProofProjection | null | undefined): boolean {
  if (!proof || proof.running) return false;
  return proof.recorded ? proof.complete === false : proof.default.o === "not_run_yet";
}

/** Apply a response unless a response to a later request is already in; false means it was stale and dropped. */
function settle(key: string, ticket: number, proof: PartProofProjection | null, failed: boolean): boolean {
  const track = trackOf(key);
  if (ticket < track.applied) return false;
  track.applied = ticket;
  const held = entries.get(key)?.proof ?? null;
  // A failed read keeps whatever record is held; only with none does the neutral stand-in show.
  const next = failed ? held : proof;
  put(key, { proof: next, busy: track.posting, failed: failed && !held });
  return true;
}

async function readInto(key: string, reviewId: string, revisionId: string): Promise<void> {
  const track = trackOf(key);
  const ticket = ++track.issued;
  try {
    const proof = accept(revisionId, await fetchPartProof(reviewId, revisionId));
    settle(key, ticket, proof, proof === null);
  } catch {
    settle(key, ticket, null, true);
  }
}

/** Re-read the record (after a reviewer's Run test wrote it). Resolves to the accepted record, or null. */
export async function refreshPartProof(reviewId: string, revisionId: string): Promise<PartProofProjection | null> {
  const key = keyOf(reviewId, revisionId);
  await readInto(key, reviewId, revisionId);
  schedulePoll(key, reviewId, revisionId);
  return entries.get(key)?.proof ?? null;
}

async function runInto(key: string, reviewId: string, revisionId: string): Promise<void> {
  const track = trackOf(key);
  if (track.posting || track.runs >= MAX_AUTO_RUNS) return;
  track.runs += 1;
  track.posting = true;
  const held = entries.get(key);
  put(key, { proof: held?.proof ?? null, busy: true, failed: held?.failed ?? false });
  try {
    const proof = accept(revisionId, await postPartProofRun(reviewId, revisionId, track.want.inView));
    track.posting = false;
    // The run's answer is the newest state at the moment it finished.
    if (proof) settle(key, ++track.issued, proof, false);
    else put(key, { ...(entries.get(key) ?? { proof: null, failed: false }), busy: false });
  } catch {
    track.posting = false;
    const now = entries.get(key);
    put(key, { proof: now?.proof ?? null, busy: false, failed: now?.failed ?? false });
  }
}

function stopPoll(track: Track) {
  if (track.timer !== null) clearTimeout(track.timer);
  track.timer = null;
}

/**
 * One timer per revision (not per row or per screen): re-read the record while it is running or incomplete, with a
 * growing delay and a hard cap, only while a screen is mounted. A tab allowed to write also resumes a run that stopped.
 */
function schedulePoll(key: string, reviewId: string, revisionId: string) {
  const track = trackOf(key);
  if (track.timer !== null || track.watchers === 0 || track.polls >= MAX_POLLS) return;
  if (!pending(entries.get(key))) return;
  const delay = Math.min(POLL_FIRST_MS * 2 ** track.polls, POLL_CAP_MS);
  track.timer = setTimeout(async () => {
    track.timer = null;
    if (track.watchers === 0) return;
    track.polls += 1;
    await readInto(key, reviewId, revisionId);
    if (track.want.autoRun && needsRun(entries.get(key)?.proof)) await runInto(key, reviewId, revisionId);
    schedulePoll(key, reviewId, revisionId);
  }, delay);
}

/**
 * Read the revision's record once. When the reader may write and the record is missing or stopped before its plan was
 * finished, start the automatic run (bounded per revision). A repeat call joins the first read; a later call with
 * `autoRun` now true (the reader's rights arrived after the first render) still starts the run.
 */
export async function loadPartProof(args: { reviewId: string; revisionId: string; inView: string[]; autoRun: boolean }): Promise<void> {
  const { reviewId, revisionId, inView, autoRun } = args;
  const key = keyOf(reviewId, revisionId);
  const track = trackOf(key);
  track.want = { autoRun: autoRun || track.want.autoRun, inView: track.loaded ? track.want.inView : inView };
  if (!track.loaded) {
    track.loaded = true;
    const held = entries.get(key);
    put(key, { proof: held?.proof ?? null, busy: true, failed: false });
    track.loading = readInto(key, reviewId, revisionId).finally(() => { track.loading = null; });
  }
  await track.loading;
  if (autoRun && needsRun(entries.get(key)?.proof)) await runInto(key, reviewId, revisionId);
  schedulePoll(key, reviewId, revisionId);
}

/**
 * The revision's shared record, loaded once per revision and kept fresh while it is incomplete. `inView` are the paths
 * on screen now (they run first on a large change); it is read once, when the revision first loads.
 */
export function usePartProof(reviewId: string, revisionId: string, options: { inView?: readonly string[]; autoRun?: boolean } = {}): PartProofProjection | null {
  const proof = useSyncExternalStore(subscribe, () => getPartProof(reviewId, revisionId), () => null);
  const { autoRun = false } = options;
  useEffect(() => {
    if (!reviewId || !revisionId) return;
    const key = keyOf(reviewId, revisionId);
    const track = trackOf(key);
    if (track.watchers === 0) track.polls = 0;
    track.watchers += 1;
    schedulePoll(key, reviewId, revisionId);
    return () => {
      track.watchers -= 1;
      if (track.watchers <= 0) {
        track.watchers = 0;
        stopPoll(track);
      }
    };
  }, [reviewId, revisionId]);
  useEffect(() => {
    if (!reviewId || !revisionId) return;
    void loadPartProof({ reviewId, revisionId, inView: [...(options.inView ?? [])], autoRun });
    // `inView` is deliberately read once per revision: it only orders the first run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewId, revisionId, autoRun]);
  return proof;
}

/**
 * The revision's record as it stands, with no request and no run: for ordering that only needs to know which files have
 * a passing test. A screen that wants the record loaded uses `usePartProof`.
 */
export function usePartProofRecord(reviewId: string, revisionId: string): PartProofProjection | null {
  return useSyncExternalStore(subscribe, () => getPartProof(reviewId, revisionId), () => null);
}

/** For tests only. */
export function resetPartProof() {
  for (const track of tracks.values()) stopPoll(track);
  tracks.clear();
  entries.clear();
  for (const listener of [...listeners]) listener();
}
