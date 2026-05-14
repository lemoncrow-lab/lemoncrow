import { useEffect, useMemo, useState } from "react";
import {
  api,
  type Cluster,
  type WatchdogConfig,
  type WatchdogProfile,
  type PlanRecord,
  type Trace,
} from "../api";
import { Chip, MetricCard, SectionHeader } from "../components/WorkbenchUI";

function profilePayload(
  config: WatchdogConfig
): Record<string, Record<string, number>> {
  return Object.fromEntries(
    config.profiles.map((profile) => [profile.id, profile.weights])
  );
}

function configSignature(config: WatchdogConfig | null): string {
  if (!config) return "";
  return JSON.stringify({
    active_profile: config.active_profile,
    profiles: profilePayload(config),
  });
}

export default function Watchdogs() {
  const [savedConfig, setSavedConfig] = useState<WatchdogConfig | null>(null);
  const [draftConfig, setDraftConfig] = useState<WatchdogConfig | null>(null);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [plans, setPlans] = useState<PlanRecord[]>([]);
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<
    "idle" | "saving" | "saved" | "error"
  >("idle");

  useEffect(() => {
    Promise.allSettled([
      api.watchdogConfig(),
      api.traces(100, 0),
      api.plans(100),
      api.clusters(),
    ]).then(([configResult, tracesResult, plansResult, clustersResult]) => {
      if (configResult.status === "fulfilled") {
        setSavedConfig(configResult.value);
        setDraftConfig(configResult.value);
      } else {
        setError("Unable to load watchdog configuration.");
      }
      if (tracesResult.status === "fulfilled")
        setTraces(tracesResult.value.items);
      if (plansResult.status === "fulfilled") setPlans(plansResult.value);
      if (clustersResult.status === "fulfilled")
        setClusters(clustersResult.value);
    });
  }, []);

  const activeProfile = useMemo<WatchdogProfile | null>(() => {
    if (!draftConfig) return null;
    return (
      draftConfig.profiles.find(
        (item) => item.id === draftConfig.active_profile
      ) ??
      draftConfig.profiles[0] ??
      null
    );
  }, [draftConfig]);

  const currentWeights = activeProfile?.weights ?? {};
  const repeatedFailures = traces.reduce(
    (acc, trace) => acc + trace.repeated_failures.length,
    0
  );
  const failedValidations = traces.reduce(
    (acc, trace) =>
      acc + trace.validation_results.filter((result) => !result.passed).length,
    0
  );
  const blockedPlans = plans.filter((plan) => plan.status !== "success").length;
  const guardrailPressure = repeatedFailures + failedValidations + blockedPlans;
  const isDirty = configSignature(savedConfig) !== configSignature(draftConfig);

  const updateWeight = (monitorKey: string, value: number) => {
    setDraftConfig((prev) => {
      if (!prev) return prev;
      setSaveState("idle");
      return {
        ...prev,
        profiles: prev.profiles.map((profile) =>
          profile.id === prev.active_profile
            ? {
                ...profile,
                weights: {
                  ...profile.weights,
                  [monitorKey]: value,
                },
              }
            : profile
        ),
      };
    });
  };

  const persistConfig = async () => {
    if (!draftConfig) return;
    setSaveState("saving");
    setError(null);
    try {
      const saved = await api.updateWatchdogConfig({
        active_profile: draftConfig.active_profile,
        profiles: profilePayload(draftConfig),
      });
      setSavedConfig(saved);
      setDraftConfig(saved);
      setSaveState("saved");
    } catch {
      setSaveState("error");
      setError("Unable to save watchdog configuration.");
    }
  };

  return (
    <div className="space-y-6">
      <SectionHeader
        title="Watchdogs"
        description="Active execution pathology guards (loops, thrashing, repeated failures)."
        action={
          <div className="flex items-center gap-3">
            <span className="text-[10px] font-bold text-amber-500/60 border border-amber-500/30 px-1.5 py-0.5">
              DEV
            </span>
            <button
              type="button"
              disabled={!isDirty || saveState === "saving"}
              className="border border-purple-500/60 px-4 py-1 font-mono text-xs uppercase tracking-widest text-purple-400 hover:bg-purple-500/10 disabled:opacity-30"
              onClick={persistConfig}
            >
              {saveState === "saving" ? "Saving..." : "Save Configuration"}
            </button>
          </div>
        }
      />

      {error && <div className="text-sm text-red-400">{error}</div>}

      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Active profile"
          value={activeProfile?.label ?? "..."}
          tone="violet"
        />
        <MetricCard
          label="Runtime"
          value={draftConfig?.runtime_wired ? "live" : "..."}
          detail={
            draftConfig?.runtime_wired
              ? "Saved weights are read by new runtime sessions."
              : undefined
          }
          tone="emerald"
        />
        <MetricCard
          label="Observed sessions"
          value={String(traces.length)}
          tone="cyan"
        />
        <MetricCard
          label="Guardrail pressure"
          value={String(guardrailPressure)}
          detail={`${clusters.length} clusters · ${failedValidations} failed validations`}
          tone="amber"
        />
      </section>

      <section className="border border-neutral-800 bg-neutral-950/70 p-5">
        <SectionHeader
          title="Watchdog profile"
          description={activeProfile?.description}
          action={
            <div className="flex flex-wrap items-center gap-2">
              <select
                aria-label="Select watchdog profile"
                value={draftConfig?.active_profile ?? "coding"}
                onChange={(event) => {
                  const nextProfile = event.target.value;
                  setDraftConfig((prev) =>
                    prev
                      ? {
                          ...prev,
                          active_profile: nextProfile,
                        }
                      : prev
                  );
                  setSaveState("idle");
                }}
                className="border border-neutral-700 bg-transparent px-3 py-2 text-xs text-neutral-200"
              >
                {(draftConfig?.profiles ?? []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => {
                  if (savedConfig) {
                    setDraftConfig(savedConfig);
                    setSaveState("idle");
                  }
                }}
                disabled={!isDirty}
                className="border border-neutral-700 px-3 py-2 text-[10px] uppercase tracking-widest text-neutral-300 transition hover:border-neutral-500 hover:text-neutral-100 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Revert
              </button>
              <button
                type="button"
                onClick={() => {
                  void persistConfig();
                }}
                disabled={!isDirty || saveState === "saving"}
                className="border border-emerald-700 px-3 py-2 text-[10px] uppercase tracking-widest text-emerald-200 transition hover:border-emerald-500 hover:text-emerald-100 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saveState === "saving" ? "Saving" : "Save"}
              </button>
            </div>
          }
        />

        <div className="mt-4 flex flex-wrap items-center gap-2 text-xs">
          <Chip tone={isDirty ? "amber" : "emerald"}>
            {isDirty ? "unsaved changes" : "saved to runtime"}
          </Chip>
          {saveState === "saved" && <Chip tone="cyan">saved</Chip>}
          <Chip tone="neutral">
            {draftConfig?.library.length ?? 0} watchdog types
          </Chip>
        </div>

        <div className="mt-6 space-y-3">
          {(draftConfig?.library ?? []).map((monitor) => {
            const weight =
              currentWeights[monitor.key] ?? monitor.default_weight;
            return (
              <div
                key={monitor.key}
                className="border border-neutral-800 bg-neutral-950/80 p-4"
              >
                <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <div className="text-sm font-semibold text-neutral-100">
                        {monitor.title}
                      </div>
                      <Chip
                        tone={monitor.severity === "high" ? "amber" : "neutral"}
                      >
                        {monitor.severity}
                      </Chip>
                    </div>
                    <div className="mt-1 text-xs text-neutral-500">
                      {monitor.description}
                    </div>
                  </div>
                  <div className="min-w-[220px]">
                    <div className="flex items-center gap-3">
                      <input
                        type="range"
                        aria-label={`${monitor.title} weight`}
                        min="0"
                        max="1"
                        step="0.01"
                        value={weight}
                        onChange={(event) =>
                          updateWeight(monitor.key, Number(event.target.value))
                        }
                        className="w-full"
                      />
                      <span className="w-12 text-right text-xs text-neutral-300">
                        {weight === 0 ? "off" : weight.toFixed(2)}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
