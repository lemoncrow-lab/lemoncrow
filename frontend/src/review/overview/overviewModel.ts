/**
 * The Overview's rules (SPEC 4.2), pure: what the screen counts, lists and hides, from the data the Reader
 * already holds. Nothing here reads the DOM, storage or the network.
 *
 * Every fact is either checked in code (a server fact) or says "Not verified" with its reason. A row with no
 * server data behind it is not built: it is left out, never filled with a guess (SPEC 1).
 */

import { answerCounts, isJudgedState, targetAnswer } from "../answerModel";
import type { ReviewFlowRoute } from "../reviewRoutes";
import { readOrder, type HasProof } from "../sinceReview";
import type {
  AnswerCategory,
  ReviewConsumerGroup,
  ReviewJournalEntry,
  ReviewOverview,
  ReviewProjection,
  ReviewTarget,
} from "../types";
import type { ReadingView } from "./defaultView";

export function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

// ------------------------------------------------------------------ parts

function queueOrder(a: ReviewTarget, b: ReviewTarget): number {
  const left = a.priority?.position ?? a.attention_rank;
  const right = b.priority?.position ?? b.attention_rank;
  return left - right || a.path.localeCompare(b.path) || a.start_line - b.start_line;
}

/** The parts asked in one way, in the server's queue order. */
export function partsOf(targets: readonly ReviewTarget[], category: AnswerCategory): ReviewTarget[] {
  return targets.filter((target) => targetAnswer(target).category === category).sort(queueOrder);
}

export interface OverviewCounts {
  parts: number;
  files: number;
  decisions: number;
  quickChecks: number;
  acceptedByRule: number;
}

/** Decisions and quick checks are counted by the answer category the server computed; marks never change them. */
export function overviewCounts(targets: readonly ReviewTarget[]): OverviewCounts {
  const counts = answerCounts(targets);
  return {
    parts: targets.length,
    files: new Set(targets.map((target) => target.path)).size,
    decisions: counts.decision,
    quickChecks: counts.quick_check,
    acceptedByRule: counts.accepted_by_rule,
  };
}

/** "2 decisions · 1 quick check" for the Start bar. */
export function countsText(counts: Pick<OverviewCounts, "decisions" | "quickChecks">): string {
  const parts = [
    ...(counts.decisions > 0 || counts.quickChecks === 0 ? [plural(counts.decisions, "decision")] : []),
    ...(counts.quickChecks > 0 ? [plural(counts.quickChecks, "quick check")] : []),
  ];
  return parts.join(" · ");
}

interface SlugIndex {
  /** How many parts carry each (trimmed) symbol. */
  counts: Map<string, number>;
  /** The first part, in list order, whose link name is the key; and the first part by stable id. */
  bySlug: Map<string, number>;
  byId: Map<string, number>;
}

// One index per parts list (the lists are replaced, never edited in place), so a screen that names every part pays once, not once per part.
const SLUG_INDEXES = new WeakMap<readonly ReviewTarget[], SlugIndex>();

function slugIndex(targets: readonly ReviewTarget[]): SlugIndex {
  const cached = SLUG_INDEXES.get(targets);
  if (cached) return cached;
  const counts = new Map<string, number>();
  for (const target of targets) {
    const symbol = target.symbol.trim();
    if (symbol) counts.set(symbol, (counts.get(symbol) ?? 0) + 1);
  }
  const bySlug = new Map<string, number>();
  const byId = new Map<string, number>();
  targets.forEach((target, index) => {
    const symbol = target.symbol.trim();
    const slug = symbol && counts.get(symbol) === 1 ? symbol : target.target_id;
    if (!bySlug.has(slug)) bySlug.set(slug, index);
    if (!byId.has(target.target_id)) byId.set(target.target_id, index);
  });
  const index = { counts, bySlug, byId };
  SLUG_INDEXES.set(targets, index);
  return index;
}

/** The name of a part in a link: its symbol when that names exactly one part, else its stable id. */
export function partSlug(target: ReviewTarget, targets: readonly ReviewTarget[]): string {
  const symbol = target.symbol.trim();
  if (symbol && slugIndex(targets).counts.get(symbol) === 1) return symbol;
  return target.target_id;
}

/** The first part a link name or a stable id names, in list order (null when none does). */
export function partBySlug(targets: readonly ReviewTarget[], slug: string): ReviewTarget | null {
  const { bySlug, byId } = slugIndex(targets);
  const a = bySlug.get(slug);
  const b = byId.get(slug);
  const index = a === undefined ? b : b === undefined ? a : Math.min(a, b);
  return index === undefined ? null : targets[index];
}

export function partName(target: ReviewTarget): string {
  const symbol = target.symbol.trim();
  if (symbol) return symbol;
  const label = target.label.trim();
  // A part with no symbol is labelled by its raw anchor ("src/core/mod_01.py#0"): name the region in words instead.
  if (label && label !== target.path && !label.startsWith(`${target.path}#`)) return label;
  return regionName(target);
}

/** "Top of mod_01.py, lines 1–3" or "mod_01.py, lines 40–45"; the whole path when the server sent no lines. */
function regionName(target: Pick<ReviewTarget, "path" | "start_line" | "end_line">): string {
  const { start_line: start, end_line: end } = target;
  if (start <= 0) return target.path;
  const file = target.path.split("/").pop() || target.path;
  const lines = end > start ? `lines ${start}–${end}` : `line ${start}`;
  return start <= 1 ? `Top of ${file}, ${lines}` : `${file}, ${lines}`;
}

const OPEN_STATES = new Set(["unreviewed", "unknown", "changed_since_review", "later"]);

/** The first part that still needs a person's answer: decisions first, then quick checks. "" when none does. */
export function firstOpenPart(targets: readonly ReviewTarget[], hasProof?: HasProof): string {
  for (const category of ["decision", "quick_check"] as const) {
    const part = readOrder(partsOf(targets, category), hasProof).find((target) => OPEN_STATES.has(target.state) && !isJudgedState(target.state));
    if (part) return partSlug(part, targets);
  }
  return "";
}

/** Where Start goes: the remembered view (SPEC 3.4, 4.2). One at a time opens at the first part needing an answer. */
export function startRoute(view: ReadingView, targets: readonly ReviewTarget[], hasProof?: HasProof): ReviewFlowRoute {
  return view === "all" ? { kind: "all", part: "" } : { kind: "one", part: firstOpenPart(targets, hasProof) };
}

export function partRoute(target: ReviewTarget, targets: readonly ReviewTarget[]): ReviewFlowRoute {
  return { kind: "one", part: partSlug(target, targets) };
}

// ------------------------------------------------------------------- chips

export type EvidenceState = { state: "checked" } | { state: "unverified"; reason: string };

export const CHECKED: EvidenceState = { state: "checked" };
export function notVerified(reason: string): EvidenceState {
  return { state: "unverified", reason };
}

/** The chip text SPEC 5.1 allows: "Checked in code" or "Not verified · <reason>". */
export function evidenceText(evidence: EvidenceState): string {
  return evidence.state === "checked" ? "Checked in code" : `Not verified · ${evidence.reason}`;
}

export interface FactChip {
  label: string;
  tone: "p0" | "neutral";
  /** A repository policy asks for this part: the chip reads "Policy" and the rule's own words are here, said once per screen. */
  policy?: string;
  /** The label is the server's own sentence (it may hold a path): drawn as written, never uppercased. */
  plain?: boolean;
}

/**
 * Reasons the server read from the code or from a configured rule. Reasons an AI raised (author_observation_mismatch,
 * implementation_choice_unrecorded, structural_decision_question) are not here: they are hints, not facts.
 */
const FACT_REASONS: Readonly<Record<string, { label?: string; tone: "p0" | "neutral"; policy?: boolean }>> = {
  removed_safeguard: { label: "Safeguard removed", tone: "p0" },
  wide_reach: { tone: "p0" },
  sensitive_path_attention: { tone: "p0" },
  review_first_policy: { tone: "neutral", policy: true },
  criticality_policy: { tone: "neutral", policy: true },
  check_failed_linked: { tone: "p0" },
};

/** The one fact chip on a decision row: the strongest checked reason, or none. */
export function factChip(target: ReviewTarget): FactChip | null {
  for (const reason of target.priority?.reasons ?? []) {
    const known = FACT_REASONS[reason.code];
    if (!known) continue;
    const detail = reason.detail.trim();
    if (known.policy) {
      if (detail) return { label: "Policy", tone: known.tone, policy: detail };
      continue;
    }
    const label = known.label ?? detail;
    if (label) return known.label ? { label, tone: known.tone } : { label, tone: known.tone, plain: true };
  }
  return null;
}

/**
 * The policy sentences behind these parts' "Policy" chips, each once, in first-seen order: a list header states them a
 * single time instead of every row repeating "Critical · required by src/core/** policy".
 */
export function policyLines(targets: readonly ReviewTarget[]): string[] {
  const lines = new Set<string>();
  for (const target of targets) {
    const policy = factChip(target)?.policy;
    if (policy) lines.add(policy);
  }
  return [...lines];
}

/** A check is linked to this part and has no result: tests count as run only when a result exists (SPEC 5.1). */
export function testDidNotRun(target: ReviewTarget): boolean {
  const { pass, fail, unknown } = target.verification;
  return pass + fail === 0 && unknown > 0;
}

/** The question a decision asks, from the plan's decisions or its non-AI questions. "" when the server sent none. */
export function questionFor(target: ReviewTarget, projection: ReviewProjection | null): string {
  if (!projection) return "";
  const decision = projection.decisions.find((row) => row.question.trim() && row.target_ids.includes(target.target_id));
  if (decision) return decision.question.trim();
  const question = projection.questions.find((row) => row.origin !== "ai" && row.title.trim() && row.target_ids.includes(target.target_id));
  return question ? question.title.trim() : "";
}

// -------------------------------------------------------------- AI hints

/** A line range a hint rests on: "app/orders.py 4–6". */
export function linesLabel(target: Pick<ReviewTarget, "path" | "start_line" | "end_line">): string {
  const { path, start_line: start, end_line: end } = target;
  if (start <= 0) return path;
  return end > start ? `${path} ${start}–${end}` : `${path} ${start}`;
}

/** Starts with the visible text "based on <lines>" (WCAG 2.5.3). */
export function linesAria(target: Pick<ReviewTarget, "path" | "start_line" | "end_line">): string {
  const { start_line: start, end_line: end } = target;
  if (start <= 0) return `based on ${target.path}, open`;
  return `based on ${linesLabel(target)}, open ${end > start ? `lines ${start} to ${end}` : `line ${start}`}`;
}

export interface BeforeAfter {
  before: string;
  after: string;
}

/** The before → after line for a part: only an AI explanation of the changed code, and only with AI help available. */
export function beforeAfterFor(target: ReviewTarget, projection: ReviewProjection | null, aiAvailable: boolean): BeforeAfter | null {
  if (!aiAvailable || !projection) return null;
  const item = projection.items.find(
    (row) =>
      !row.stale &&
      row.rationale_origin === "inferred" &&
      row.before.trim() &&
      row.after.trim() &&
      row.primary_target_ids.includes(target.target_id),
  );
  return item ? { before: item.before.trim(), after: item.after.trim() } : null;
}

export interface AiHint {
  id: string;
  text: string;
  target: ReviewTarget;
}

/** AI-raised questions on parts this review has. Absent with AI help off, whatever the server holds. */
export function aiHints(projection: ReviewProjection | null, targets: readonly ReviewTarget[], aiAvailable: boolean): AiHint[] {
  if (!aiAvailable || !projection) return [];
  const byId = new Map(targets.map((target) => [target.target_id, target]));
  const hints: AiHint[] = [];
  for (const question of projection.questions) {
    if (question.origin !== "ai") continue;
    const target = question.target_ids.map((id) => byId.get(id)).find((row): row is ReviewTarget => Boolean(row));
    const text = (question.detail || question.title).trim();
    if (target && text) hints.push({ id: question.id, text, target });
  }
  return hints;
}

// --------------------------------------------------------------- headline

const HOST_WORDS: Readonly<Record<string, string>> = { claude_code: "Claude Code", claude: "Claude Code", codex: "Codex", cursor: "Cursor" };

/** The agent's name from the revision's recorded host; "The agent" when none was recorded. */
export function agentName(host: string): string {
  const raw = host.trim().toLowerCase();
  if (!raw) return "The agent";
  const known = HOST_WORDS[raw.replace(/[-\s]+/g, "_")];
  if (known) return known;
  return raw
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** The fixed headline template (SPEC 7.1 Q10): built from counted facts, never from a model. */
export function headline(host: string, counts: OverviewCounts): string {
  const base = `${agentName(host)} changed ${plural(counts.parts, "part")} in ${plural(counts.files, "file")}.`;
  if (counts.decisions === 0) return base;
  return `${base} ${counts.decisions === 1 ? "1 needs" : `${counts.decisions} need`} your decision.`;
}

// ----------------------------------------------------- asked, planned, did

export interface AskedPlanned {
  asked: string;
  planned: string[];
}

/** The task and the plan as the agent session recorded them. Empty when the session recorded none. */
export function askedPlanned(journal: readonly ReviewJournalEntry[] | undefined): AskedPlanned {
  const entries = journal ?? [];
  // Hidden HTML comments stay hidden until "Show hidden text": they are cut from what is shown here.
  const shown = (entry: ReviewJournalEntry) => visibleText(entry.summary);
  const asked = entries.find((entry) => entry.kind === "intent" && shown(entry));
  return {
    asked: asked ? shown(asked) : "",
    planned: entries.filter((entry) => entry.kind === "plan" && shown(entry)).map(shown),
  };
}

// ------------------------------------------------------------- checked rows

export interface CheckedRow {
  key: string;
  chip: FactChip;
  name: string;
  target: ReviewTarget;
  order: number;
}

/** "Checked in the code" rows: the fact chip of each decision, in queue order. */
export function checkedRows(targets: readonly ReviewTarget[]): CheckedRow[] {
  const rows: CheckedRow[] = [];
  partsOf(targets, "decision").forEach((target, index) => {
    const chip = factChip(target);
    if (chip) rows.push({ key: target.target_id, chip, name: partName(target), target, order: index + 1 });
  });
  return rows;
}

// --------------------------------------------------------------- who can reach

export interface ReachSite {
  label: string;
  evidence: EvidenceState;
}

export interface ReachRow {
  key: string;
  name: string;
  /** Callers found; or the honest absence. */
  outcome: "callers" | "none" | "untraced";
  sites: ReachSite[];
  evidence: EvidenceState;
}

const COULDNT_TRACE = "couldn't trace where it is called from";

/**
 * `byName`: the callers came from a search of the code by name, not from the index. An uncertain site found that way
 * is "matched by name only"; any other uncertain site could not be traced.
 */
function reachRow(group: ReviewConsumerGroup, ran: boolean, byName: boolean): ReachRow {
  const name = group.symbol.trim() || group.path;
  const sites: ReachSite[] = group.sites.map((site) => ({
    label: site.symbol ? `${site.path} · ${site.symbol}` : site.path,
    evidence: site.uncertainty.trim() ? notVerified(byName ? "matched by name only" : "couldn't trace") : CHECKED,
  }));
  if (sites.length > 0) return { key: group.key, name, outcome: "callers", sites, evidence: CHECKED };
  // "No caller found" is a search result that cannot see callers outside the repository or dynamic calls (SPEC 4.2).
  if (group.known_callers === 0 && ran) return { key: group.key, name, outcome: "none", sites, evidence: notVerified(COULDNT_TRACE) };
  return { key: group.key, name, outcome: "untraced", sites, evidence: notVerified(COULDNT_TRACE) };
}

export interface ReachModel {
  rows: ReachRow[];
  /** Other repositories were scanned and reached, so "callers outside this repository are not checked" would be false. */
  otherRepositoriesChecked: boolean;
}

export function reachModel(projection: ReviewProjection | null): ReachModel | null {
  const context = projection?.context;
  if (!context || context.consumers.length === 0) return null;
  const ran = context.readiness.impact_pass === "ran";
  const reached = projection?.workspace_impact && "reached" in projection.workspace_impact ? projection.workspace_impact.reached : [];
  // A caller listed under a changed function is not listed again under that function's file; a file row left with no
  // caller of its own is dropped (it had callers, all of them shown under its functions).
  const siteKey = (path: string, site: ReviewConsumerGroup["sites"][number]) => `${path}\u0000${site.path}\u0000${site.line}\u0000${site.symbol}`;
  const underSymbol = new Set(context.consumers.filter((group) => group.symbol.trim()).flatMap((group) => group.sites.map((site) => siteKey(group.path, site))));
  const groups = context.consumers.flatMap((group) => {
    if (group.symbol.trim() || group.sites.length === 0) return [group];
    const sites = group.sites.filter((site) => !underSymbol.has(siteKey(group.path, site)));
    return sites.length > 0 ? [{ ...group, sites }] : [];
  });
  return {
    rows: groups.map((group) => reachRow(group, ran, context.readiness.source === "checkout_search")),
    otherRepositoriesChecked: Array.isArray(reached) && reached.length > 0,
  };
}

// ---------------------------------------------------------------- connects

/** "Nothing new connected" only from a counted zero; any other count or none sent leaves the line out. */
export function connectsLine(projection: ReviewProjection | null): { text: string } | null {
  return projection?.triage?.dependency_changes === 0 ? { text: "Nothing new connected · no new dependencies" } : null;
}

// ------------------------------------------------------------- not checked

const NOT_CHECKED_LEAD = /^\s*Not checked:\s*/i;

/**
 * What the server says it did not check. Its text is "Not checked: a; b" (brief_summary.py): the lead is dropped
 * because the screen draws its own, and the items are split on "; ".
 */
export function notChecked(projection: ReviewProjection | null): string[] {
  return (projection?.summary?.facts ?? [])
    .filter((fact) => fact.kind === "unchecked")
    .flatMap((fact) => fact.text.replace(NOT_CHECKED_LEAD, "").split(";"))
    .map((item) => item.trim())
    .filter(Boolean);
}

/** The gap Who can reach this used to state on its own line; it now joins the one Not checked line (review-4.4). */
export const OTHER_REPOSITORIES_GAP = "callers in other repositories";

/**
 * review-4.4: Evidence says what was not checked once. The server's list, plus the other-repositories gap when Who can
 * reach this did not scan them (that section no longer repeats it), with any item the server already named kept once.
 */
export function notCheckedItems(unchecked: readonly string[], reach: ReachModel | null): string[] {
  const items = reach && !reach.otherRepositoriesChecked ? [...unchecked, OTHER_REPOSITORIES_GAP] : [...unchecked];
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = item.trim().toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// ------------------------------------------------------- untrusted / hidden

export interface HiddenText {
  where: string;
  text: string;
}

const HTML_COMMENT = /<!--([\s\S]*?)-->/g;
/** An unclosed `<!--` hides everything after it in a browser, so it is cut too. */
const HIDDEN_TAIL = /<!--[\s\S]*$/;

/** The text with every hidden HTML comment cut out. */
export function visibleText(text: string): string {
  return text.replace(HTML_COMMENT, "").replace(HIDDEN_TAIL, "").trim();
}

function comments(text: string, where: string, into: HiddenText[]): void {
  for (const match of text.matchAll(HTML_COMMENT)) {
    const found = match[0].trim();
    if (found) into.push({ where, text: found });
  }
}

/**
 * Hidden HTML comments in the text an agent or a pull request supplied. Found by reading the strings; the result is
 * only ever shown as inert text (SPEC 4.2 OV-4).
 */
export function hiddenText(overview: Pick<ReviewOverview, "journal" | "change"> | null | undefined): { scanned: boolean; found: HiddenText[] } {
  const found: HiddenText[] = [];
  let scanned = false;
  for (const entry of overview?.journal ?? []) {
    scanned = true;
    comments(entry.summary, "the agent session", found);
    comments(entry.detail, "the agent session", found);
  }
  for (const link of overview?.change?.scm ?? []) {
    scanned = true;
    comments(link.title ?? "", "the PR description", found);
    for (const value of Object.values(link.metadata ?? {})) {
      if (typeof value === "string") comments(value, "the PR description", found);
    }
  }
  return { scanned, found };
}

/** "1 hidden HTML comment found in the PR description"; "" when none was found. */
export function hiddenSummary(found: readonly HiddenText[]): string {
  if (found.length === 0) return "";
  const places = [...new Set(found.map((row) => row.where))];
  return `${plural(found.length, "hidden HTML comment")} found in ${places.join(" and ")}`;
}

// ---------------------------------------------------------------- evidence

export type EvidenceSectionId = "asked-planned-did" | "connects" | "asked-vs-did" | "reach" | "untrusted" | "not-checked";

export interface EvidenceSectionsInput {
  connects: boolean;
  reach: boolean;
  untrusted: boolean;
  notChecked: boolean;
}

/** The sections inside Evidence, in order. Its count is this list's length (OV-1). */
export function evidenceSections(input: EvidenceSectionsInput): EvidenceSectionId[] {
  return [
    "asked-planned-did",
    ...(input.connects ? (["connects"] as const) : []),
    "asked-vs-did",
    ...(input.reach ? (["reach"] as const) : []),
    ...(input.untrusted ? (["untrusted"] as const) : []),
    ...(input.notChecked ? (["not-checked"] as const) : []),
  ];
}

// -------------------------------------------------------------- preparing

export interface PreparationSource {
  state: "pending" | "running" | "ready" | "failed";
  completed: number;
  total: number;
}

/** "Checking 2 of 3…" while the server prepares the Brief; "Checking…" before it reports (SPEC 4.2 Preparing). */
export function preparingText(progress: PreparationSource | null): string {
  if (progress && progress.total > 0 && progress.state !== "ready") return `Checking ${Math.min(progress.completed, progress.total)} of ${progress.total}…`;
  return "Checking…";
}

// ------------------------------------------------------------ totals, ready

/** The server's +/- totals for the revision (packet stats); null when it sent none. */
export function changeTotals(stats: Record<string, number> | undefined): { additions: number; deletions: number } | null {
  if (!stats || typeof stats.additions !== "number" || typeof stats.deletions !== "number") return null;
  return { additions: stats.additions, deletions: stats.deletions };
}

export interface ReadinessInput {
  /** The review's parts are loaded. */
  structure: boolean;
  /** Every part is loaded: a startup slice (`complete === false`) must never be counted from. */
  partsComplete: boolean;
  /** The review.brief capability is on. */
  briefCapable: boolean;
  projectionState: "idle" | "loading" | "ready" | "failed";
  /** A projection for the revision on screen exists. Once it does, a refresh does not make the Overview wait again. */
  projectionCurrent: boolean;
}

/**
 * Start is enabled when the structure is loaded and complete and the facts have either arrived, failed (the screen then
 * shows what it has, honestly) or are not offered. It does not follow the Classic Reader's landing, which a stored view
 * can skip, and it does not drop back while a refresh runs (OV-8).
 */
export function overviewReady(input: ReadinessInput): boolean {
  if (!input.structure || !input.partsComplete) return false;
  if (!input.briefCapable) return true;
  // "ready" counts only for a plan of the revision on screen. "failed" counts because the screen then draws no plan at all.
  return input.projectionCurrent || input.projectionState === "failed";
}
