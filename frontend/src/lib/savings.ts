import type { SavingsSummaryV2 } from "../api";

/**
 * The one "saved" figure every surface shows (Home, Usage, the Savings sheet), read from
 * `/v1/savings/summary` only. Saved is the ledger total (the statusline and `lc savings` figure);
 * cost reduction is saved ÷ (cost + saved), the share of what the window would have cost.
 */
export const SAVED_LABEL = "Saved";
export const REDUCTION_LABEL = "Cost reduction";
export const REDUCTION_HINT = "Saved ÷ (cost + saved): the share of what this window would have cost without LemonCrow.";

export interface SavingsHeadline {
  savedUsd: number;
  costUsd: number;
  baselineUsd: number;
  reductionPct: number;
}

export function costReductionPct(costUsd: number, savedUsd: number): number {
  const baseline = costUsd + savedUsd;
  return baseline > 0 && savedUsd > 0 ? (savedUsd / baseline) * 100 : 0;
}

export function savingsHeadline(summary: SavingsSummaryV2): SavingsHeadline {
  const savedUsd = Math.max(0, summary.total_saved_usd ?? (summary.saved_usd ?? 0) + (summary.carry_usd ?? 0));
  const costUsd = Math.max(0, summary.actually_cost_usd ?? summary.tracked_actual_cost_usd ?? 0);
  return {
    savedUsd,
    costUsd,
    baselineUsd: costUsd + savedUsd,
    reductionPct: costReductionPct(costUsd, savedUsd),
  };
}
