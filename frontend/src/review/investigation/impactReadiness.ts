// What the Snapshot's impact pass could not see, in words, from the projection's
// `context.readiness` block (F7). Shared by Blind spots and What if so both say
// "index incomplete" the same way. Pure; no fetch, no model call.
import type { ReviewImpactReadiness } from "../types";

export interface ReadinessGap {
  /** The degradation name or readiness state it came from. */
  key: string;
  title: string;
  detail: string;
  /** The gap means callers or consumers may exist that are not listed. */
  hidesConsumers: boolean;
}

/** Impact-pass degradation names (review/impact.py) and what each one hides. */
const SIGNALS: Record<string, { title: string; detail: string; hidesConsumers: boolean }> = {
  symbol_relations: { title: "Caller relations unavailable", detail: "The code index could not answer who calls the changed symbols.", hidesConsumers: true },
  centrality: { title: "Symbol importance unavailable", detail: "The index could not rank how central the changed symbols are.", hidesConsumers: false },
  symbol_line_ranges: { title: "Index lines drifted in changed files", detail: "The index's symbol lines did not match this revision.", hidesConsumers: false },
  astgrep_unavailable: { title: "Structural matching fell back to text", detail: "ast-grep was unavailable, so contract, signature and removed-symbol consumers were matched by text, which can over-match.", hidesConsumers: false },
  ambiguous_symbol_counts: { title: "Ambiguous symbol names", detail: "Some callers could not be told apart from same-named definitions and were left out.", hidesConsumers: true },
  caller_line_ranges: { title: "Caller lines re-anchored", detail: "Some caller lines came from the working index and were re-located in this revision.", hidesConsumers: false },
  symbol_resolution_truncated: { title: "Symbol resolution was cut short", detail: "Not every changed symbol was resolved against the index.", hidesConsumers: true },
  caller_queries_truncated: { title: "Caller search was capped", detail: "Only the most-called changed symbols, and a bounded number of callers each, were expanded.", hidesConsumers: true },
  index_outline_incomplete: { title: "Index outline incomplete", detail: "The index had not walked every definition in the changed files.", hidesConsumers: true },
  cross_repository_sites: { title: "Other repositories not followed", detail: "Callers in a submodule or nested repository were dropped.", hidesConsumers: true },
  sites_absent_from_revision: { title: "Some sites missing from this revision", detail: "Sites the working tree has but this revision does not were dropped.", hidesConsumers: false },
  impact_sites_truncated: { title: "Impact sites were capped", detail: "More impact sites were found than the packet keeps.", hidesConsumers: true },
};

/** Recorded degradations that are not impact-pass names but still reach the Structure drawer. */
const OTHER_GAPS: Record<string, string> = {
  agent_reads_unrecorded: "The agent's file reads were not recorded, so what it looked at is unknown.",
  non_definition_symbols: "Some changed names are not definitions, so their callers were not searched.",
  test_evidence_unavailable: "No test results were recorded for this revision.",
};

/** One recorded degradation name in plain words, or null when it has none (the caller then keeps it behind a disclosure). */
export function degradedSentence(name: string): string | null {
  const signal = SIGNALS[name];
  if (signal) return `${signal.title}. ${signal.detail}`;
  return OTHER_GAPS[name] ?? null;
}

const STATUS: Record<string, ReadinessGap | null> = {
  fresh: null,
  stale: { key: "index:stale", title: "Code index is stale", detail: "The index answered, but it does not describe this revision exactly.", hidesConsumers: true },
  absent: { key: "index:absent", title: "No code index", detail: "Callers come only from structural search; call-graph callers are unknown.", hidesConsumers: true },
  unknown: { key: "index:unknown", title: "Index state unknown", detail: "This revision does not record whether a code index answered.", hidesConsumers: true },
  current: { key: "index:current", title: "Callers from the current code index", detail: "The capture had no index; callers come from the current code index, the one the Map reads, not this revision.", hidesConsumers: false },
};

/** Callers come from searching this revision's code by name (`pr_bot/callers.py`). */
const SEARCHED: ReadinessGap = {
  key: "index:checkout_search",
  title: "Callers found by searching the code",
  detail: "Callers come from a search of this revision's code by name and a parse of each file that names them, not a call graph.",
  hidesConsumers: false,
};

/** Every readiness gap, index state first, then degradation names in their recorded order. */
export function readinessGaps(readiness: ReviewImpactReadiness): ReadinessGap[] {
  if (readiness.source === "unavailable") {
    return [{ key: "packet:unavailable", title: "Impact data unavailable", detail: "This revision's change packet could not be read.", hidesConsumers: true }];
  }
  if (readiness.impact_pass === "not_run") {
    return [{ key: "impact:not_run", title: "Impact analysis did not run", detail: "No caller or consumer search ran for this revision.", hidesConsumers: true }];
  }
  if (readiness.impact_pass === "no_index") {
    return [{ key: "impact:no_index", title: "Callers not measured", detail: "No code index answered for this revision, so who calls the changed code is unknown.", hidesConsumers: true }];
  }
  const out: ReadinessGap[] = [];
  const status = readiness.source === "checkout_search"
    ? SEARCHED
    : readiness.index_status in STATUS ? STATUS[readiness.index_status] : STATUS.unknown;
  if (status) out.push(status);
  const unmeasured = readiness.unmeasured ?? [];
  if (unmeasured.length > 0) {
    const shown = unmeasured.slice(0, 3).map((item) => `${item.symbol} (${item.reason})`).join("; ");
    const more = unmeasured.length > 3 ? ` and ${unmeasured.length - 3} more` : "";
    out.push({ key: "callers:unmeasured", title: "Callers not measured for some changed code", detail: `The search could not finish for ${shown}${more}.`, hidesConsumers: true });
  }
  for (const name of readiness.degraded) {
    const signal = SIGNALS[name];
    if (signal) out.push({ key: name, ...signal });
  }
  return out;
}

/** The index answered for this Snapshot with nothing that hides consumers. */
export function consumersComplete(readiness: ReviewImpactReadiness): boolean {
  return readinessGaps(readiness).every((gap) => !gap.hidesConsumers);
}
