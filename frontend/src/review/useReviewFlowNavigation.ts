import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { setDefaultView, type ReadingView } from "./overview/defaultView";
import { startRoute } from "./overview/overviewModel";
import { buildReviewFlowPath, type ReviewFlowRoute } from "./reviewRoutes";
import { useSeenCoverage } from "./seen/SeenCoverageProvider";
import type { ReviewTarget } from "./types";

export interface FlowGoOptions {
  replace?: boolean;
  /**
   * Set only by the W key and the Wrap up buttons (SPEC 4.6): they show the coverage dialog first when a risky
   * part is unseen. Every other move to Wrap ("Back to Wrap up" after Change, a redirect) goes straight there.
   */
  wrapUp?: boolean;
}

/**
 * How a flow screen moves to another one. A move to a screen or a part is a new
 * history entry, so Back and Forward step through them (NAV-1). `replace` is for
 * the moves that must not leave an entry behind (a redirect, a first load).
 */
export function useReviewFlowNavigation(reviewRef: string) {
  const navigate = useNavigate();
  const seen = useSeenCoverage();
  const goTo = useCallback(
    (route: ReviewFlowRoute, options: FlowGoOptions = {}) => {
      if (route.kind === "wrap" && options.wrapUp === true && seen.enabled) { seen.goToWrap(); return; }
      navigate(buildReviewFlowPath(reviewRef, route), { replace: options.replace === true });
    },
    [navigate, reviewRef, seen.enabled, seen.goToWrap],
  );
  return { goTo };
}

/**
 * The top bar's One at a time | All files switch on a screen that is neither (Map, Brief, X-Ray, Wrap up; review-0.4):
 * no side is pressed, and a choice is remembered as the default view and opens that view where the Overview's Start would.
 */
export function useReadingViewSwitch(reviewRef: string, targets: readonly ReviewTarget[]): { current: null; onChange: (view: ReadingView) => void } {
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const onChange = useCallback((view: ReadingView) => {
    setDefaultView(view);
    goTo(startRoute(view, targets));
  }, [goTo, targets]);
  return { current: null, onChange };
}
