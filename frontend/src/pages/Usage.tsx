import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { X } from "lucide-react";
import { PageFrame } from "../components/WorkbenchUI";
import Analytics from "./Analytics";
import Savings from "./Savings";
import Optimizations from "./Optimizations";
import Reports from "./Reports";

type InspectorView = "savings" | "advisor" | "benchmarks";

const INSPECTOR_LABELS: Record<InspectorView, string> = {
  savings: "Savings evidence",
  advisor: "Optimization advisor",
  benchmarks: "Benchmarks",
};

export default function Usage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedView = searchParams.get("inspect");
  const initialView: InspectorView =
    requestedView === "advisor" || requestedView === "benchmarks"
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
      </PageFrame>

      {inspectorOpen && (
        <>
          <button
            type="button"
            aria-label="Dismiss usage inspector"
            className="fixed inset-0 z-40 bg-black/35"
            onClick={closeInspector}
          />
          <aside
            aria-label="Usage inspector"
            className="fixed bottom-0 right-0 top-12 z-50 flex w-[min(900px,88vw)] flex-col border-l border-neutral-800 bg-neutral-950 shadow-2xl"
          >
            <div className="flex items-center gap-3 border-b border-neutral-800 px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                  Usage inspector
                </div>
                <div className="mt-0.5 text-sm font-semibold text-neutral-100">
                  {INSPECTOR_LABELS[inspectorView]}
                </div>
              </div>
              <select
                aria-label="Usage inspector view"
                value={inspectorView}
                onChange={(event) =>
                  selectInspectorView(event.target.value as InspectorView)
                }
                className="h-8 border border-neutral-800 bg-neutral-900 px-2 text-xs text-neutral-200 outline-none focus:border-neutral-600"
              >
                <option value="savings">Savings evidence</option>
                <option value="advisor">Optimization advisor</option>
                <option value="benchmarks">Benchmarks</option>
              </select>
              <button
                type="button"
                onClick={closeInspector}
                className="inline-flex h-8 w-8 items-center justify-center rounded-sm text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100"
                aria-label="Close usage inspector"
              >
                <X size={16} />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-4">
              {inspectorView === "savings" && <Savings />}
              {inspectorView === "advisor" && <Optimizations />}
              {inspectorView === "benchmarks" && <Reports />}
            </div>
          </aside>
        </>
      )}
    </>
  );
}
