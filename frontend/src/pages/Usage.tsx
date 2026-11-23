import { Select, SelectItem } from "../components/ui/select";
import { Button as UiButton } from "../components/ui/button";
import { Suspense, lazy, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { api } from "../api";
import { EmptyState, PageFrame } from "../components/WorkbenchUI";
import { TimeWindowSelect } from "../components/TimeWindowSelect";
import { useProductCapabilities } from "../lib/RuntimeCapabilities";
import Analytics from "./Analytics";
import ReviewAnalytics from "../review/analytics/ReviewAnalytics";

// Inspector views render only once opened; load each on demand.
const Savings = lazy(() => import("./Savings"));
const Optimizations = lazy(() => import("./Optimizations"));
const Reports = lazy(() => import("./Reports"));

type InspectorView = "savings" | "advisor" | "benchmarks";

const INSPECTOR_LABELS: Record<InspectorView, string> = {
  savings: "Savings evidence",
  advisor: "Optimization advisor",
  benchmarks: "Benchmarks",
};

function isInspectorView(value: string | null): value is InspectorView {
  return value === "savings" || value === "advisor" || value === "benchmarks";
}

/**
 * Usage: the spend dashboard, and its evidence views (`?inspect=`). An evidence view is drawn in the page, not in a
 * drawer, so the window scrolls it; its toolbar (back, view, window) stays pinned under the top bar.
 */
export default function Usage() {
  const [searchParams, setSearchParams] = useSearchParams();
  // The advisor and benchmarks read local-runtime endpoints; a server without
  // runtime diagnostics (hosted) does not answer them, so do not offer them.
  const hasRuntimeInspectors = useProductCapabilities().features["runtime.diagnostics"];
  const requestedView = searchParams.get("inspect");
  const inspecting = isInspectorView(requestedView);
  const inspectorView: InspectorView =
    hasRuntimeInspectors && (requestedView === "advisor" || requestedView === "benchmarks") ? requestedView : "savings";
  const [reviewOpen, setReviewOpen] = useState(false);
  // Benchmarks matter only to people who run them: offer the view once a report exists
  // (or when a link opened it). Asked once, the first time an evidence view opens.
  const [hasReports, setHasReports] = useState<boolean | null>(null);
  useEffect(() => {
    if (!inspecting || !hasRuntimeInspectors || hasReports !== null) return;
    let cancelled = false;
    api
      .reports()
      .then((reports) => {
        if (!cancelled) setHasReports(reports.length > 0);
      })
      .catch(() => {
        if (!cancelled) setHasReports(false);
      });
    return () => {
      cancelled = true;
    };
  }, [hasReports, hasRuntimeInspectors, inspecting]);
  const offerBenchmarks = hasRuntimeInspectors && (hasReports === true || inspectorView === "benchmarks");

  // Opening and leaving an evidence view are history steps (Back returns to the dashboard); switching views is not.
  const showView = (view: InspectorView | null, replace: boolean) => {
    const next = new URLSearchParams(searchParams);
    if (view) next.set("inspect", view);
    else next.delete("inspect");
    setSearchParams(next, { replace });
  };

  // The window scrolls these views, so entering or leaving one starts at its top.
  useEffect(() => {
    if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  }, [inspecting]);

  if (inspecting) {
    return (
      <section aria-label="Usage inspector">
        <div
          data-testid="usage-inspector-toolbar"
          className="sticky top-[41px] z-20 border-b border-border bg-background/95 backdrop-blur"
        >
          <div className="mx-auto flex w-full max-w-[1600px] flex-wrap items-center gap-2 px-5 py-2 lg:px-6">
            <UiButton type="button" variant="ghost" size="sm" onClick={() => showView(null, false)}>
              <ArrowLeft size={14} aria-hidden="true" />
              Usage
            </UiButton>
            <h1 className="min-w-0 text-heading font-semibold text-foreground">{INSPECTOR_LABELS[inspectorView]}</h1>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <Select
                aria-label="Usage inspector view"
                value={inspectorView}
                onValueChange={(value) => showView(value as InspectorView, true)}
                uiSize="xs"
                className="min-w-44"
              >
                <SelectItem value="savings">Savings evidence</SelectItem>
                {hasRuntimeInspectors && <SelectItem value="advisor">Optimization advisor</SelectItem>}
                {offerBenchmarks && <SelectItem value="benchmarks">Benchmarks</SelectItem>}
              </Select>
              {inspectorView !== "benchmarks" && <TimeWindowSelect label="Usage time window" />}
            </div>
          </div>
        </div>
        <PageFrame>
          <Suspense fallback={<EmptyState title="Loading…" />}>
            {inspectorView === "savings" && <Savings />}
            {hasRuntimeInspectors && inspectorView === "advisor" && <Optimizations />}
            {hasRuntimeInspectors && inspectorView === "benchmarks" && <Reports />}
          </Suspense>
        </PageFrame>
      </section>
    );
  }

  return (
    <PageFrame className="space-y-4">
      <div className="flex justify-end">
        <TimeWindowSelect label="Usage time window" />
      </div>
      <Analytics onInspectSavings={() => showView("savings", false)} />
      {/* Review's tables run ~3,200px; they sit behind a disclosure and mount only when opened. */}
      <details
        className="rounded-md border border-border bg-surface"
        onToggle={(event) => setReviewOpen((event.currentTarget as HTMLDetailsElement).open)}
      >
        <summary className="cursor-pointer px-3 py-2 text-ui font-semibold text-foreground">Review analytics</summary>
        {reviewOpen && (
          <div className="border-t border-border p-3">
            <ReviewAnalytics />
          </div>
        )}
      </details>
    </PageFrame>
  );
}
