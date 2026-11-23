import { useEffect } from "react";

/**
 * One document title per screen of the redesigned flow (SPEC 5.3, 4.6): "<screen> · <repo> · LemonCrow", set by the
 * shared top bar. The Reader and the app shell write their own generic titles ("Review: <id>", "Review") on mount and
 * on every route change; while a flow screen holds a title they leave it alone (`flowDocumentTitle`).
 */
let current = "";

export function flowDocumentTitle(): string {
  return current;
}

export function composeFlowTitle(screen: string, repo: string): string {
  return [screen, repo, "LemonCrow"].filter(Boolean).join(" · ");
}

export function useFlowDocumentTitle(screen: string, repo: string): void {
  const title = composeFlowTitle(screen, repo);
  useEffect(() => {
    current = title;
    document.title = title;
  }, [title]);
  useEffect(() => () => { current = ""; }, []);
}
