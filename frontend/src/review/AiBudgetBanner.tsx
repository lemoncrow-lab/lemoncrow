import { Link } from "react-router-dom";

import { Button } from "../components/ui/button";
import { isOrgCap, orgBannerText, usedUpBannerText, useAiBudget } from "./aiBudget";

/** Where the budget is set: Settings, AI help. */
export const AI_BUDGET_SETTINGS_PATH = "/settings/review#ai-help";
/** Where an organization's cap is read (and, by an admin, set). */
export const ORG_CAP_SETTINGS_PATH = "/settings/org";

/**
 * SPEC 4.20 and 7.1 OWNER-3: the banner on Home and every review screen once this month's AI budget is used up.
 * Nothing is hidden behind it: reviews and the AI results already made still work. A $0 budget is not "used up"; it
 * shows no banner, only the disabled Ask and its reason. The text is static: the shared toast host is the only announcer (A11Y-3).
 */
export function AiBudgetBanner() {
  const budget = useAiBudget();
  if (!budget || (budget.reason_code !== "budget_used_up" && budget.reason_code !== "org_cap_used_up")) return null;
  const org = isOrgCap(budget);
  return (
    <p
      data-testid="ai-budget-banner"
      className="flex flex-wrap items-center gap-x-3 border-b border-p1-line bg-p1-bg px-4 py-2 text-label"
    >
      <span className="min-w-0 flex-1">{org ? orgBannerText(budget.organization ?? "", budget.resets_on) : usedUpBannerText(budget.resets_on)}</span>
      <Button asChild variant="link" size="compact">
        <Link to={org ? ORG_CAP_SETTINGS_PATH : AI_BUDGET_SETTINGS_PATH}>Settings</Link>
      </Button>
    </p>
  );
}
