import { Select, SelectItem } from "../components/ui/select";
import { Button as UiButton } from "../components/ui/button";
import {
  Sheet,
  SheetBody,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "../components/ui/sheet";
import { Suspense, lazy, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { X } from "lucide-react";
import { PageFrame } from "../components/WorkbenchUI";
import { useProductCapabilities } from "../lib/RuntimeCapabilities";
import Analytics from "./Analytics";
import ReviewAnalytics from "../review/analytics/ReviewAnalytics";

// Inspector views render only inside the opened sheet; load each on demand.
const Savings = lazy(() => import("./Savings"));
const Optimizations = lazy(() => import("./Optimizations"));
const Reports = lazy(() => import("./Reports"));

type InspectorView = "savings" | "advisor" | "benchmarks";

const INSPECTOR_LABELS: Record<InspectorView, string> = {
  savings: "Savings evidence",
  advisor: "Optimization advisor",
  benchmarks: "Benchmarks",
};

export default function Usage() {
  const [searchParams, setSearchParams] = useSearchParams();
  // The advisor and benchmarks read local-runtime endpoints; a server without
  // runtime diagnostics (hosted) does not answer them, so do not offer them.
  const hasRuntimeInspectors = useProductCapabilities().features["runtime.diagnostics"];
  const requestedView = searchParams.get("inspect");
  const initialView: InspectorView =
    hasRuntimeInspectors && (requestedView === "advisor" || requestedView === "benchmarks")
      ? requestedView
      : "savings";
  const [inspectorOpen, setInspectorOpen] = useState(
    requestedView === "savings" ||
      requestedView === "advisor" ||
      requestedView === "benchmarks"
  );
  const [inspectorView, setInspectorView] = useState<InspectorView>(initialView);

  const openInspector = (view = inspectorView) => {
    setInspectorView(view);
    setInspectorOpen(true);
    const next = new URLSearchParams(searchParams);
    next.set("inspect", view);
    setSearchParams(next, { replace: true });
  };

  const closeInspector = () => {
    setInspectorOpen(false);
    const next = new URLSearchParams(searchParams);
    next.delete("inspect");
    setSearchParams(next, { replace: true });
  };

  const selectInspectorView = (view: InspectorView) => {
    setInspectorView(view);
    const next = new URLSearchParams(searchParams);
    next.set("inspect", view);
    setSearchParams(next, { replace: true });
  };

  return (
    <>
      <PageFrame className="space-y-4">
        <Analytics onInspectSavings={() => openInspector("savings")} />
        <ReviewAnalytics />
      </PageFrame>

      <Sheet
        open={inspectorOpen}
        onOpenChange={(open) => {
          if (!open) closeInspector();
        }}
      >
        <SheetContent
          side="right"
          className="w-[min(900px,88vw)]"
          aria-describedby={undefined}
        >
          <SheetHeader className="items-center">
            <div className="min-w-0 flex-1">
              <SheetTitle className="text-caption font-mono font-normal uppercase tracking-widest text-muted-foreground">
                Usage inspector
              </SheetTitle>
              <div className="mt-0.5 text-sm font-semibold text-foreground">
                {INSPECTOR_LABELS[inspectorView]}
              </div>
            </div>
            <Select
              aria-label="Usage inspector view"
              value={inspectorView}
              onValueChange={(value) =>
                selectInspectorView(value as InspectorView)
              }
              uiSize="xs"
              className="min-w-44"
            >
              <SelectItem value="savings">Savings evidence</SelectItem>
              {hasRuntimeInspectors && <SelectItem value="advisor">Optimization advisor</SelectItem>}
              {hasRuntimeInspectors && <SelectItem value="benchmarks">Benchmarks</SelectItem>}
            </Select>
            <SheetClose asChild>
              <UiButton
                type="button"
                size="icon"
                variant="ghost"
                aria-label="Close usage inspector"
              >
                <X size={16} />
              </UiButton>
            </SheetClose>
          </SheetHeader>

          <SheetBody>
            <Suspense fallback={null}>
              {inspectorView === "savings" && <Savings />}
              {hasRuntimeInspectors && inspectorView === "advisor" && <Optimizations />}
              {hasRuntimeInspectors && inspectorView === "benchmarks" && <Reports />}
            </Suspense>
          </SheetBody>
        </SheetContent>
      </Sheet>
    </>
  );
}
