/** The Diff | Map | Preview tab bar of the All files centre toolbar (SPEC 4.6), shown on All files, Map and Preview, directly below the top bar.
 * Arrow keys move between tabs; Enter or Space opens one (a tab is a different screen, so moving focus does not navigate).
 */
import { useRef } from "react";
import { Button } from "../components/ui/button";
import { useReviewFlowNavigation } from "./useReviewFlowNavigation";

export type ViewTab = "diff" | "map" | "preview";
export function previewTabLabel(count: number | null): string {
  return count === null ? "Preview" : `Preview · ${count}`;
}
/** Diff, Map and Preview are always all listed (review-9.1): the Map says itself when nothing connected changed. */
export default function ReviewViewTabs({ reviewRef, current, previewCount }: { reviewRef: string; current: ViewTab; previewCount: number | null }) {
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const tabs: Array<{ id: ViewTab; label: string; open: () => void }> = [
    { id: "diff", label: "Diff", open: () => goTo({ kind: "all", part: "" }) },
    { id: "map", label: "Map", open: () => goTo({ kind: "map", lens: "data-path", view: "changes" }) },
    { id: "preview", label: previewTabLabel(previewCount), open: () => goTo({ kind: "preview", surface: "" }) },
  ];
  const onKeyDown = (event: React.KeyboardEvent, index: number) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : step ? (index + step + tabs.length) % tabs.length : -1;
    if (next < 0) return;
    event.preventDefault();
    refs.current[next]?.focus();
  };
  return <div role="tablist" aria-label="Views of this change" className="flex shrink-0 gap-1 border-b border-border px-3" data-testid="review-view-tabs">
    {tabs.map((tab, i) => <Button key={tab.id} ref={(el) => { refs.current[i] = el; }} unstyled role="tab" type="button"
      aria-selected={current === tab.id} tabIndex={current === tab.id ? 0 : -1} data-tab={tab.id}
      className={` border-b-2 px-3 text-label font-medium ${current === tab.id ? "border-foreground text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}
      onKeyDown={(event) => onKeyDown(event, i)} onClick={() => { if (current !== tab.id) tab.open(); }}>{tab.label}</Button>)}
  </div>;
}
