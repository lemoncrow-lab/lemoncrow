import { useEffect, useSyncExternalStore } from "react";

import { isHostedWorkspace } from "./hostedOnly";
import { requestBrowserJson } from "./reviewApi";
import { showFlowToast } from "./flowToast";

/**
 * The personal AI budget (SPEC 7.1 OWNER-1 to OWNER-3). There is no AI on/off switch: `intelligence.monthly_budget_usd`
 * is the control, and the server says this month's use and whether a model call may start. A $0 or used-up budget
 * hides nothing; it disables new AI actions and names why.
 */
export const AI_PREFERENCES_PATH = "/api/review-intelligence";
export const BUDGET_KEY = "intelligence.monthly_budget_usd";
/** On a hosted workspace the limit is the organization's monthly cost cap (SPEC 7.1 P9-5), read from here. */
export const ORG_CAP_PATH = "/v1/organization/ai-cap";

export interface AiBudget {
  /** null: no limit. */
  monthly_budget_usd: number | null;
  /** The cost that is known; a lower bound while `complete` is false. */
  used_usd: number;
  unreported_calls: number;
  complete: boolean;
  /** The first day of next UTC month, `YYYY-MM-DD`. */
  resets_on: string;
  may_run: boolean;
  /** "", "budget_zero", "budget_used_up", "budget_invalid" or "budget_unreadable"; "org_cap_zero" or "org_cap_used_up" for an organization's cap. */
  reason_code: string;
  reason: string;
  /** The organization's name when the limit is its cost cap and the deployment has one; "" otherwise. */
  organization?: string;
}

/** The limit is an organization's cost cap, not this person's own budget. */
export const isOrgCap = (budget: Pick<AiBudget, "reason_code">): boolean => budget.reason_code.startsWith("org_cap_");

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** The `ai_budget` block of `/api/review-intelligence`; null for anything else, so nothing is claimed. */
export function parseAiBudget(raw: unknown): AiBudget | null {
  if (!raw || typeof raw !== "object") return null;
  const block = (raw as { ai_budget?: unknown }).ai_budget;
  if (!block || typeof block !== "object") return null;
  const b = block as Record<string, unknown>;
  const used = num(b.used_usd);
  if (typeof b.may_run !== "boolean" || used === null) return null;
  return {
    monthly_budget_usd: num(b.monthly_budget_usd),
    used_usd: used,
    unreported_calls: Math.max(0, Math.trunc(num(b.unreported_calls) ?? 0)),
    complete: b.complete !== false,
    resets_on: typeof b.resets_on === "string" ? b.resets_on : "",
    may_run: b.may_run,
    reason_code: typeof b.reason_code === "string" ? b.reason_code : "",
    reason: typeof b.reason === "string" ? b.reason : "",
    organization: typeof b.organization === "string" ? b.organization : "",
  };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

/** `2026-12-01` as `Dec 1`. */
export function resetLabel(resetsOn: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(resetsOn);
  if (!match) return "";
  return `${MONTHS[Number(match[2]) - 1] ?? ""} ${Number(match[3])}`.trim();
}

/** Why a new AI action cannot start, or "" when it can. Unknown (no answer yet) is "": nothing is claimed. */
export function aiBlockedReason(budget: AiBudget | null): string {
  if (!budget || budget.may_run) return "";
  if (isOrgCap(budget)) return orgPausedText(budget.organization ?? "", budget.reason_code === "org_cap_zero" ? "" : budget.resets_on);
  if (budget.reason_code === "budget_used_up") return `AI budget used up until ${resetLabel(budget.resets_on)}`;
  if (budget.reason_code === "budget_zero") return "AI budget is $0";
  return budget.reason || "AI budget does not allow new AI runs";
}

/** `$5`, `$12.50`; used amounts keep cents (`$0.60`) and a nonzero amount under a cent reads `<$0.01`. */
export function usd(amount: number, opts: { cents?: boolean } = {}): string {
  if (amount > 0 && amount < 0.01) return "<$0.01";
  if (!opts.cents && Number.isInteger(amount)) return `$${amount}`;
  return `$${amount.toFixed(2)}`;
}

export const BUDGET_LABEL = "AI budget ($ per month)";

export function budgetStatusLine(limit: number | null): string {
  if (limit === null) return "No limit.";
  if (limit === 0) return "$0 · no new AI runs. AI results you already have stay.";
  return `AI stops at ${usd(limit)}/month. Reviews still work.`;
}

export function budgetUseLine(budget: Pick<AiBudget, "used_usd" | "unreported_calls">): string {
  const base = `${usd(budget.used_usd, { cents: true })} used this month`;
  const n = budget.unreported_calls;
  return n > 0 ? `${base} · ${n} ${n === 1 ? "call" : "calls"} with cost not reported` : base;
}

export function budgetSavedToast(limit: number | null): string {
  return limit === null ? "AI budget removed · no limit" : `AI budget saved · ${usd(limit)}/month`;
}

/** "AI hints paused for Acme until Dec 1" (SPEC 4.19); without a date it is a $0 cap, without a name "your organization". */
export function orgPausedText(organization: string, resetsOn: string): string {
  const base = `AI hints paused for ${organization || "your organization"}`;
  return resetsOn ? `${base} until ${resetLabel(resetsOn)}` : base;
}

export function orgBannerText(organization: string, resetsOn: string): string {
  return `${orgPausedText(organization, resetsOn)} · reviews still work`;
}

export function usedUpBannerText(resetsOn: string): string {
  return `AI budget used up until ${resetLabel(resetsOn)} · reviews and AI results you already have still work`;
}

/** The field's text to a budget: "" is no limit, else a number of dollars, 0 or more. `undefined` is not valid. */
export function parseBudgetInput(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  if (!/^\d+(\.\d+)?$/.test(trimmed)) return undefined;
  const value = Number(trimmed);
  return Number.isFinite(value) && value <= 1_000_000_000 ? value : undefined;
}

// --- one shared copy of the server's answer, so the banner and every disabled Ask agree -------------------------

let current: AiBudget | null = null;
let fetchedAt = 0;
let inflight: Promise<void> | null = null;
const listeners = new Set<() => void>();
const STALE_MS = 5_000;
/** Waits before asking again while there is no answer (the browser may not be paired with the server yet). */
const RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];

function emit(): void { listeners.forEach((listener) => listener()); }

/** Replace the shared answer (a Save just returned a fresh one). */
export function setAiBudget(next: AiBudget | null): void {
  current = next;
  fetchedAt = Date.now();
  emit();
}

type ReadOutcome = "answered" | "refused" | "failed";

/**
 * Read the server's answer now. "answered" is any answer that parses, including one with no budget block (an older or
 * hosted server: no budget is shown); "refused" is a 4xx other than 401 (asking again will not change it); "failed" is
 * anything else, 401 included (the browser may not be paired yet). A failed read keeps the last answer.
 */
function read(): Promise<ReadOutcome> {
  if (inflight) return inflight.then(() => lastOutcome);
  let request: Promise<unknown>;
  try {
    // A hosted workspace has no personal settings file: its limit is the organization's cap. A local install (and a
    // discovery that has not answered) asks for the personal budget, as before.
    request = isHostedWorkspace().then((hosted) => requestBrowserJson<unknown>(hosted ? ORG_CAP_PATH : AI_PREFERENCES_PATH));
  } catch {
    return Promise.resolve("failed"); // no way to ask: nothing is claimed
  }
  const run = request
    .then((result): ReadOutcome => {
      if (!result || typeof result !== "object") return "failed";
      const parsed = parseAiBudget(result);
      if (parsed) setAiBudget(parsed);
      else fetchedAt = Date.now(); // answered, with no budget to show: not asked again until stale
      return "answered";
    })
    .catch((error: unknown): ReadOutcome => {
      const status = (error as { status?: unknown } | null)?.status;
      return typeof status === "number" && status >= 400 && status < 500 && status !== 401 ? "refused" : "failed";
    })
    .then((outcome) => { lastOutcome = outcome; return outcome; });
  inflight = run.then(() => undefined).finally(() => { inflight = null; });
  return run;
}

let lastOutcome: ReadOutcome = "failed";

/** Read the server's answer now (for a Save or a focus). */
export function refreshAiBudget(): Promise<void> {
  return read().then(() => undefined);
}

// One schedule for every mounted hook and every remount: a server that never answers is asked about five times, not
// five times per screen. It ends on any answer and on a refusal, and starts over only after an answer.
let retryStep = 0;
let retryTimer: number | undefined;
let holders = 0;

function scheduleRetry(): void {
  if (retryTimer !== undefined || holders === 0 || retryStep >= RETRY_DELAYS_MS.length) return;
  retryTimer = window.setTimeout(() => {
    retryTimer = undefined;
    retryStep += 1;
    void read().then(afterRead);
  }, RETRY_DELAYS_MS[retryStep]);
}

function afterRead(outcome: ReadOutcome): void {
  if (outcome === "failed") scheduleRetry();
  else if (outcome === "answered") retryStep = 0;
  else retryStep = RETRY_DELAYS_MS.length; // refused: stop
}

/** The shared answer now, for tests. */
export function currentAiBudget(): AiBudget | null { return current; }

/** For tests. */
export function resetAiBudgetForTests(next: AiBudget | null = null): void {
  current = next;
  fetchedAt = next ? Date.now() : 0;
  inflight = null;
  lastOutcome = "failed";
  retryStep = 0;
  window.clearTimeout(retryTimer);
  retryTimer = undefined;
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** This machine's AI budget, or null until the server has answered (or when it cannot). Reads again when stale and on focus. */
export function useAiBudget(): AiBudget | null {
  const budget = useSyncExternalStore(subscribe, () => current, () => null);
  useEffect(() => {
    holders += 1;
    if (Date.now() - fetchedAt > STALE_MS) void read().then(afterRead);
    const onFocus = () => { void refreshAiBudget(); };
    window.addEventListener("focus", onFocus);
    return () => {
      holders -= 1;
      if (holders === 0) { window.clearTimeout(retryTimer); retryTimer = undefined; }
      window.removeEventListener("focus", onFocus);
    };
  }, []);
  return budget;
}

export interface AiGate {
  /** "" when new AI runs may start. */
  reason: string;
  blocked: boolean;
  /** Spread on a button: it stays visible and reads as disabled, naming why (a tap still answers with the reason). */
  attrs: { "aria-disabled"?: true; title?: string; "data-ai-blocked"?: "true" };
  /** Wrap an AI action: runs it, or toasts the reason when the budget does not allow it. */
  run: <A extends unknown[]>(action: ((...args: A) => void) | undefined) => ((...args: A) => void) | undefined;
}

/** Say the reason through the shared toast (what a key or a tap on a disabled AI action does). */
export function toastAiReason(reason: string): void {
  showFlowToast({ text: reason });
}

export function useAiGate(): AiGate {
  const reason = aiBlockedReason(useAiBudget());
  const blocked = reason !== "";
  return {
    reason,
    blocked,
    attrs: blocked ? { "aria-disabled": true, title: reason, "data-ai-blocked": "true" } : {},
    run: (action) => (action === undefined ? undefined : blocked ? () => toastAiReason(reason) : action),
  };
}
