import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { HelpCircle } from "lucide-react";
import {
  api,
  type OptimizationAdvisorCandidate,
  type OptimizationRecommendation,
  type OptimizationRecommendationSession,
  type OptimizationsSummary,
  type OptimizationAdvisorPolicy,
  type OptimizationAdvisorCompactionPolicy,
  type OptimizationAdvisorHistoryEntry,
  type OptimizationImpactValidation,
  type OutcomesSummary,
} from "../api";
import {
  Chip,
  MetricCard,
  SectionHeader,
  Slider,
  SnippetCard,
  Switch,
  cx,
} from "../components/WorkbenchUI";
import { useTimeRange } from "../lib/TimeRangeContext";
import { fmtPct, fmtTok, fmtUsd } from "../lib/format";

const COMPACTION_LABELS: Record<string, string> = {
  prompt_cache_reorder: "Prompt-cache reorder",
  dedup: "Dedup compaction",
  retrieval_filter: "Retrieval filter",
  lossy_summary: "Lossy summary",
};

type SessionEvidence = OptimizationRecommendationSession & {
  recommendationId: string;
  recommendationTitle: string;
  recommendationSeverity: string;
  recommendationAction: string;
  estimatedUsdSaved: number;
  estimatedTokensSaved: number;
};

function fmtDeltaPercent(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${fmtPct(value * 100)}`;
}

function fmtPercent(value: number): string {
  return fmtPct(value * 100);
}

function fmtSignedUsd(value: number): string {
  if (value === 0) return "$0.00";
  return `${value > 0 ? "+" : "-"}${fmtUsd(Math.abs(value))}`;
}

function fmtPolicyLabel(value: string): string {
  return value.replaceAll("_", " ").replaceAll("-", " ");
}

function confidenceTone(value: string): "emerald" | "amber" | "red" {
  if (value === "high") return "emerald";
  if (value === "medium") return "amber";
  return "red";
}

function severityTone(value: string): "emerald" | "amber" | "red" | "neutral" {
  if (value === "high") return "red";
  if (value === "medium") return "amber";
  if (value === "low") return "emerald";
  return "neutral";
}

function qualityTone(score: number): "emerald" | "cyan" | "amber" | "red" {
  if (score >= 0.98) return "emerald";
  if (score >= 0.95) return "cyan";
  if (score >= 0.92) return "amber";
  return "red";
}

function deltaTone(
  value: number,
  lowerIsBetter: boolean
): "emerald" | "red" | "neutral" {
  if (value === 0) return "neutral";
  const improved = lowerIsBetter ? value < 0 : value > 0;
  return improved ? "emerald" : "red";
}

function collectSessionEvidence(
  recommendations: OptimizationRecommendation[],
  highExtraReadSessionIds: string[] = []
): SessionEvidence[] {
  const byTrace = new Map<string, SessionEvidence>();
  recommendations.forEach((recommendation) => {
    recommendation.sessions.forEach((session) => {
      if (!byTrace.has(session.trace_id)) {
        byTrace.set(session.trace_id, {
          ...session,
          recommendationId: recommendation.id,
          recommendationTitle: recommendation.title,
          recommendationSeverity: recommendation.severity,
          recommendationAction: recommendation.action,
          estimatedUsdSaved: recommendation.estimated_usd_saved,
          estimatedTokensSaved: recommendation.estimated_tokens_saved,
        });
      }
    });
  });
  // Outcomes sessions with elevated post-compaction re-read rates join this
  // list too — they're evidence the current policy is losing context, same
  // as the token/cost-driven recommendations above.
  highExtraReadSessionIds.forEach((sessionId) => {
    if (byTrace.has(sessionId)) return;
    byTrace.set(sessionId, {
      trace_id: sessionId,
      reason: "Elevated re-read rate after compaction (outcomes tracker).",
      recommendationId: "outcomes-high-extra-reads",
      recommendationTitle: "High extra reads after compaction",
      recommendationSeverity: "medium",
      recommendationAction: "Review compaction retention for this session.",
      estimatedUsdSaved: 0,
      estimatedTokensSaved: 0,
    });
  });
  return Array.from(byTrace.values());
}

export function candidateOrder(
  candidate: OptimizationAdvisorCandidate,
  currentCandidateId: string,
  recommendedCandidateId: string | null
): number {
  if (candidate.id === currentCandidateId) return 0;
  if (candidate.id === recommendedCandidateId) return 1;
  return 2;
}

function Hint({ text }: { text: string }) {
  return (
    <span
      title={text}
      className="inline-flex h-5 w-5 cursor-help items-center justify-center border border-neutral-700 text-neutral-400"
    >
      <HelpCircle size={12} />
    </span>
  );
}

export function CandidateSummaryCard({
  label,
  candidate,
  currentCandidate,
  badge,
  note,
}: {
  label: string;
  candidate: OptimizationAdvisorCandidate;
  currentCandidate: OptimizationAdvisorCandidate;
  badge?: "Current" | "Recommended";
  note: string;
}) {
  const costDelta =
    candidate.weekly_cost_usd - currentCandidate.weekly_cost_usd;
  const qualityDelta =
    candidate.estimated_quality - currentCandidate.estimated_quality;

  return (
    <article className="border border-neutral-800 bg-neutral-950/60 p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-neutral-400">
            {label}
          </div>
          <div className="mt-2 flex items-center gap-2">
            <h2 className="text-xl font-semibold text-neutral-100">
              {candidate.policy.name}
            </h2>
            <Hint text={note} />
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <Chip tone="neutral">
              {fmtPolicyLabel(candidate.policy.preset)}
            </Chip>
            <Chip tone={qualityTone(candidate.estimated_quality)}>
              quality floor {fmtPercent(candidate.policy.quality_floor)}
            </Chip>
            {badge && <Chip tone="amber">{badge}</Chip>}
          </div>
        </div>
        <div className="text-right">
          <div className="text-2xl font-semibold text-neutral-100">
            {fmtUsd(candidate.weekly_cost_usd)}
          </div>
          <div className="mt-1 text-xs text-neutral-400">weekly</div>
        </div>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Quality"
          value={fmtPercent(candidate.estimated_quality)}
          detail={fmtDeltaPercent(qualityDelta)}
          tone={qualityTone(candidate.estimated_quality)}
        />
        <MetricCard
          label="Cost delta"
          value={fmtSignedUsd(-costDelta)}
          detail="vs current"
          tone={deltaTone(costDelta, true)}
        />
        <MetricCard
          label="Latency"
          value={`${candidate.latency_mult.toFixed(2)}x`}
          detail="relative"
          tone={deltaTone(
            candidate.latency_mult - currentCandidate.latency_mult,
            true
          )}
        />
        <MetricCard
          label="Escalation"
          value={fmtPercent(candidate.escalation_rate)}
          detail="escalates"
          tone={deltaTone(
            candidate.escalation_rate - currentCandidate.escalation_rate,
            true
          )}
        />
      </div>

      <div className="mt-5 grid gap-4 xl:grid-cols-2">
        <div className="border border-neutral-800 bg-black/20 p-4">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
            Routing
          </div>
          <div className="mt-3 grid gap-2 text-sm text-neutral-300">
            <div>Simple: {candidate.policy.routing.simple}</div>
            <div>Medium: {candidate.policy.routing.medium}</div>
            <div>Hard: {candidate.policy.routing.hard}</div>
            <div
              className="text-neutral-400"
              title={candidate.policy.routing.escalate_on.join(", ")}
            >
              Escalation triggers
            </div>
          </div>
        </div>
        <div
          className="border border-neutral-800 bg-black/20 p-4"
          title={`Preserve: ${candidate.policy.compaction.preserve.join(", ")}`}
        >
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
            Compaction
          </div>
          <div className="mt-3 grid gap-2 text-sm text-neutral-300">
            <div>
              Trigger:{" "}
              {fmtPercent(
                candidate.policy.compaction.trigger_at_context_fraction
              )}
            </div>
            <div>Dedup: {candidate.policy.compaction.dedup ? "on" : "off"}</div>
            <div>
              Filter:{" "}
              {candidate.policy.compaction.retrieval_filter ? "on" : "off"}
            </div>
            <div>
              Lossy: {candidate.policy.compaction.lossy_summary ? "on" : "off"}
            </div>
          </div>
        </div>
      </div>
    </article>
  );
}

export function CandidateOptions({
  candidates,
  currentCandidateId,
  recommendedCandidateId,
  selectedCandidateId,
  onSelect,
}: {
  candidates: OptimizationAdvisorCandidate[];
  currentCandidateId: string;
  recommendedCandidateId: string | null;
  selectedCandidateId: string;
  onSelect: (candidateId: string) => void;
}) {
  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Optimization Frontier"
        title="Select configuration"
        action={
          <Hint text="Current, recommended, and alternative policy points from the advisor frontier." />
        }
      />

      <div className="grid gap-4 xl:grid-cols-5">
        {candidates.map((candidate) => {
          const selected = candidate.id === selectedCandidateId;
          const isCurrent = candidate.id === currentCandidateId;
          const isRecommended = candidate.id === recommendedCandidateId;
          return (
            <button
              key={candidate.id}
              type="button"
              onClick={() => onSelect(candidate.id)}
              className={cx(
                "border p-4 text-left transition",
                selected
                  ? "border-amber-500/60 bg-amber-950/15"
                  : "border-neutral-800 bg-neutral-950/60 hover:border-neutral-700"
              )}
            >
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-sm font-semibold text-neutral-100">
                  {candidate.policy.name}
                </div>
                {isCurrent && <Chip tone="neutral">Current</Chip>}
                {isRecommended && <Chip tone="amber">Recommended</Chip>}
              </div>
              <div className="mt-2 text-xs uppercase tracking-widest text-neutral-400">
                {fmtPolicyLabel(candidate.policy.preset)}
              </div>
              <div className="mt-4 text-2xl font-semibold text-neutral-100">
                {fmtUsd(candidate.weekly_cost_usd)}
              </div>
              <div className="mt-1 text-xs text-neutral-400">per week</div>
              <div className="mt-4 space-y-2 text-sm text-neutral-300">
                <div>Quality {fmtPercent(candidate.estimated_quality)}</div>
                <div>Latency {candidate.latency_mult.toFixed(2)}x</div>
                <div>Escalation {fmtPercent(candidate.escalation_rate)}</div>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function CandidateDetail({
  candidate,
  currentCandidate,
  headerOverride,
}: {
  candidate: OptimizationAdvisorCandidate;
  currentCandidate: OptimizationAdvisorCandidate;
  headerOverride?: string;
}) {
  const compactionRows = Object.entries(candidate.compaction_breakdown);
  const routingRows = Object.entries(candidate.routing_breakdown).sort(
    ([left], [right]) => left.localeCompare(right)
  );
  const costDelta =
    candidate.weekly_cost_usd - currentCandidate.weekly_cost_usd;
  const qualityDelta =
    candidate.estimated_quality - currentCandidate.estimated_quality;

  return (
    <section className="grid gap-4 xl:grid-cols-[0.88fr_1.12fr]">
      <article className="border border-neutral-800 bg-neutral-950/60 p-5">
        <SectionHeader
          eyebrow="Advanced details"
          title={headerOverride ?? candidate.policy.name}
          action={
            <Hint text="Inspect the exact routing and compaction mix for the selected option." />
          }
        />
        <div className="mt-5 grid gap-3 md:grid-cols-2">
          <MetricCard
            label="Cost vs current"
            value={fmtSignedUsd(-costDelta)}
            detail={`${fmtUsd(candidate.weekly_cost_usd)} / week`}
            tone={deltaTone(costDelta, true)}
          />
          <MetricCard
            label="Quality vs current"
            value={fmtDeltaPercent(qualityDelta)}
            detail={fmtPercent(candidate.estimated_quality)}
            tone={deltaTone(qualityDelta, false)}
          />
        </div>
        <div
          className="mt-5 border border-neutral-800 bg-black/20 p-4"
          title={`Preserve: ${candidate.policy.compaction.preserve.join(", ")}`}
        >
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
            Policy details
          </div>
          <div className="mt-3 grid gap-2 text-sm text-neutral-300">
            <div>
              Routing mode: {fmtPolicyLabel(candidate.policy.routing.policy)}
            </div>
            <div>
              Context trigger:{" "}
              {fmtPercent(
                candidate.policy.compaction.trigger_at_context_fraction
              )}
            </div>
            <div>
              Confidence required: {candidate.policy.confidence_required}
            </div>
          </div>
        </div>
      </article>

      <div className="grid gap-4">
        <article className="border border-neutral-800 bg-neutral-950/60 p-5">
          <SectionHeader
            eyebrow="Compaction breakdown"
            title="Compaction"
            action={
              <Hint text="Split savings by compaction type so users can see what is free vs riskier." />
            }
          />
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-400">
                  <th className="py-3 pr-4">Compaction type</th>
                  <th className="py-3 pr-4">Enabled</th>
                  <th className="py-3 text-right">Weekly savings</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-900">
                {compactionRows.map(([key, value]) => {
                  const enabled =
                    key === "prompt_cache_reorder"
                      ? candidate.policy.compaction.prompt_cache_reorder
                      : key === "dedup"
                        ? candidate.policy.compaction.dedup
                        : key === "retrieval_filter"
                          ? candidate.policy.compaction.retrieval_filter
                          : candidate.policy.compaction.lossy_summary;
                  return (
                    <tr key={key}>
                      <td className="py-3 pr-4 text-neutral-200">
                        {COMPACTION_LABELS[key] ?? fmtPolicyLabel(key)}
                      </td>
                      <td className="py-3 pr-4">
                        <Chip tone={enabled ? "emerald" : "neutral"}>
                          {enabled ? "On" : "Off"}
                        </Chip>
                      </td>
                      <td className="py-3 text-right font-mono text-neutral-300">
                        {fmtUsd(value)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </article>

        <article className="border border-neutral-800 bg-neutral-950/60 p-5">
          <SectionHeader
            eyebrow="Routing breakdown"
            title="Routing"
            action={
              <Hint text="Share of turns routed to cheap, medium, and expensive tiers." />
            }
          />
          <div className="mt-4 grid gap-3 md:grid-cols-3">
            {routingRows.length === 0 ? (
              <div className="border border-neutral-800 bg-black/20 p-4 text-sm text-neutral-400 md:col-span-3">
                No routing distribution was available for this option.
              </div>
            ) : (
              routingRows.map(([tier, share]) => (
                <div
                  key={tier}
                  className="border border-neutral-800 bg-black/20 p-4"
                >
                  <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
                    {tier}
                  </div>
                  <div className="mt-2 text-xl font-semibold text-neutral-100">
                    {fmtPercent(share)}
                  </div>
                  <div className="mt-2 text-xs text-neutral-400">of turns</div>
                </div>
              ))
            )}
          </div>
        </article>
      </div>
    </section>
  );
}

function SessionsBehindRecommendation({
  evidence,
}: {
  evidence: SessionEvidence[];
}) {
  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Sessions"
        title="Sessions"
        action={
          <Hint text="Concrete traces behind the recommendation. Open one in Sessions for the full context." />
        }
      />

      {evidence.length === 0 ? (
        <section className="border border-neutral-800 bg-neutral-950/60 p-5 text-sm text-neutral-400">
          No concrete session examples were available in this window.
        </section>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          {evidence.slice(0, 6).map((session) => (
            <article
              key={`${session.recommendationId}-${session.trace_id}`}
              className="border border-neutral-800 bg-neutral-950/60 p-4"
              title={[
                session.reason,
                session.tools?.length
                  ? `Tools: ${session.tools.join(", ")}`
                  : "",
                `Suggested action: ${session.recommendationAction}`,
              ]
                .filter(Boolean)
                .join("\n")}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-neutral-100">
                    {session.recommendationTitle}
                  </div>
                  <div className="mt-1 font-mono text-xs text-neutral-400">
                    {session.trace_id}
                  </div>
                </div>
                <Chip tone={severityTone(session.recommendationSeverity)}>
                  {session.recommendationSeverity}
                </Chip>
              </div>

              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <MetricCard
                  label="Estimated savings"
                  value={fmtUsd(session.estimatedUsdSaved)}
                  detail={fmtTok(session.estimatedTokensSaved)}
                  tone="emerald"
                />
                <MetricCard
                  label="Trace cost"
                  value={
                    typeof session.cost_usd === "number"
                      ? fmtUsd(session.cost_usd)
                      : "unknown"
                  }
                  detail={
                    typeof session.multiple === "number"
                      ? `${session.multiple.toFixed(1)}x peer average`
                      : session.host || session.project || "session"
                  }
                  tone="neutral"
                />
              </div>

              <div className="mt-4 space-y-1 text-sm text-neutral-300">
                <div>
                  {session.project || session.host || "session"}
                  {typeof session.input_output_ratio === "number"
                    ? ` • ${session.input_output_ratio.toFixed(1)}:1 input/output`
                    : ""}
                </div>
                <div className="text-xs text-neutral-400">
                  Hover for details
                </div>
              </div>

              <Link
                to={`/sessions?trace=${encodeURIComponent(session.trace_id)}`}
                className="mt-4 inline-flex border border-neutral-700 px-3 py-1.5 text-[10px] font-mono uppercase tracking-widest text-neutral-300 transition hover:border-amber-500/50 hover:text-amber-300"
              >
                Open in Sessions
              </Link>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function NextActions({
  summary,
  selectedCandidate,
}: {
  summary: OptimizationsSummary;
  selectedCandidate: OptimizationAdvisorCandidate;
}) {
  const applyCommand = summary.advisor.has_recommendation
    ? "atelier optimize apply --recommended"
    : "atelier optimize apply --preset balanced";
  const shadowPolicy =
    summary.advisor.has_recommendation &&
    summary.advisor.recommended_candidate_id
      ? "recommended"
      : selectedCandidate.policy.preset;
  const benchmarkCommand = `atelier optimize apply --preset ${selectedCandidate.policy.preset} && atelier benchmark run`;

  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Active operations"
        title="Actions"
        action={
          <Hint text="Commands to modify runtime policy or execute long-running validation suites (may incur LLM costs)." />
        }
      />

      <div className="grid gap-4 xl:grid-cols-3">
        <SnippetCard
          title="Apply"
          body={applyCommand}
          caption="Update local configuration"
        />
        <SnippetCard
          title="Shadow"
          body={`atelier optimize shadow --policy ${shadowPolicy} --days 7`}
          caption="Test policy in parallel"
        />
        <SnippetCard
          title="Benchmark"
          body={benchmarkCommand}
          caption="Run performance suite"
        />
      </div>
    </section>
  );
}

function PolicySandbox({
  initialPolicy,
}: {
  initialPolicy: OptimizationAdvisorPolicy;
}) {
  const [policy, setPolicy] = useState(initialPolicy);

  const updateCompaction = (
    key: keyof OptimizationAdvisorCompactionPolicy,
    val: any
  ) => {
    setPolicy((prev: OptimizationAdvisorPolicy) => ({
      ...prev,
      compaction: { ...prev.compaction, [key]: val },
    }));
  };

  const yamlConfig = useMemo(() => {
    const config = {
      name: "sandbox-policy",
      preset: "custom",
      quality_floor: policy.quality_floor,
      confidence_required: policy.confidence_required,
      routing: policy.routing,
      compaction: policy.compaction,
    };
    return `name: ${config.name}\npreset: custom\nquality_floor: ${config.quality_floor}\nconfidence_required: ${config.confidence_required}\nrouting:\n  policy: ${config.routing.policy}\n  simple: ${config.routing.simple}\n  medium: ${config.routing.medium}\n  hard: ${config.routing.hard}\ncompaction:\n  trigger_at_context_fraction: ${config.compaction.trigger_at_context_fraction}\n  prompt_cache_reorder: ${config.compaction.prompt_cache_reorder}\n  dedup: ${config.compaction.dedup}\n  retrieval_filter: ${config.compaction.retrieval_filter}\n  lossy_summary: ${config.compaction.lossy_summary}`;
  }, [policy]);

  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Interactive"
        title="Policy Sandbox"
        action={
          <Hint text="Tune optimization parameters in real-time. Copy the resulting YAML to apply a custom policy." />
        }
      />

      <div className="grid gap-6 xl:grid-cols-[1fr_0.7fr]">
        <div className="grid gap-6 border border-neutral-800 bg-neutral-950/60 p-6 md:grid-cols-2">
          <div className="space-y-8">
            <Slider
              label="Quality floor"
              value={policy.quality_floor}
              min={0.8}
              max={1.0}
              step={0.01}
              formatValue={fmtPercent}
              onChange={(v) =>
                setPolicy((p: OptimizationAdvisorPolicy) => ({
                  ...p,
                  quality_floor: v,
                }))
              }
            />
            <Slider
              label="Context trigger"
              value={policy.compaction.trigger_at_context_fraction}
              min={0.1}
              max={1.0}
              step={0.05}
              formatValue={fmtPercent}
              onChange={(v) =>
                updateCompaction("trigger_at_context_fraction", v)
              }
            />
            <div className="space-y-3">
              <label className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
                Confidence required
              </label>
              <div className="flex gap-2">
                {["low", "medium", "high"].map((level) => (
                  <button
                    key={level}
                    type="button"
                    onClick={() =>
                      setPolicy((p: OptimizationAdvisorPolicy) => ({
                        ...p,
                        confidence_required: level as any,
                      }))
                    }
                    className={cx(
                      "flex-1 border py-1.5 text-[10px] font-mono uppercase tracking-widest transition",
                      policy.confidence_required === level
                        ? "border-amber-500 bg-amber-950/20 text-amber-200"
                        : "border-neutral-800 bg-black/20 text-neutral-400 hover:border-neutral-700"
                    )}
                  >
                    {level}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="space-y-6">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
              Compaction features
            </div>
            <div className="grid gap-4">
              <Switch
                label="Prompt-cache reorder"
                checked={policy.compaction.prompt_cache_reorder}
                onChange={(v) => updateCompaction("prompt_cache_reorder", v)}
              />
              <Switch
                label="Dedup compaction"
                checked={policy.compaction.dedup}
                onChange={(v) => updateCompaction("dedup", v)}
              />
              <Switch
                label="Retrieval filter"
                checked={policy.compaction.retrieval_filter}
                onChange={(v) => updateCompaction("retrieval_filter", v)}
              />
              <Switch
                label="Lossy summary"
                checked={policy.compaction.lossy_summary}
                onChange={(v) => updateCompaction("lossy_summary", v)}
              />
            </div>
          </div>
        </div>

        <SnippetCard
          title="Custom Policy YAML"
          body={yamlConfig}
          caption="Save as policy.yaml and apply with --custom"
        />
      </div>
    </section>
  );
}

function OptimizationHistory({
  history,
}: {
  history: OptimizationAdvisorHistoryEntry[];
}) {
  if (!history || history.length === 0) return null;

  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Past recommendations"
        title="History"
        action={
          <Hint text="Historical advisor snapshots and their recorded impact." />
        }
      />
      <div className="overflow-x-auto border border-neutral-800 bg-neutral-950/60">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-400">
              <th className="px-5 py-3">Date</th>
              <th className="px-5 py-3">Confidence</th>
              <th className="px-5 py-3">Sessions</th>
              <th className="px-5 py-3">Quality</th>
              <th className="px-5 py-3 text-right">Potential savings</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-900">
            {history.slice(0, 10).map((item, idx) => (
              <tr key={idx} className="hover:bg-white/[0.02]">
                <td className="px-5 py-3 text-neutral-400">
                  {new Date(item.recorded_at).toLocaleDateString()}
                </td>
                <td className="px-5 py-3">
                  <Chip tone={confidenceTone(item.confidence)}>
                    {fmtPolicyLabel(item.confidence)}
                  </Chip>
                </td>
                <td className="px-5 py-3 text-neutral-300">
                  {item.sessions_analysed.toLocaleString()}
                </td>
                <td className="px-5 py-3">
                  <span
                    className={cx(
                      qualityTone(item.quality_delta + 0.95),
                      "font-mono"
                    )}
                  >
                    {fmtDeltaPercent(item.quality_delta)}
                  </span>
                </td>
                <td className="px-5 py-3 text-right font-mono text-emerald-300">
                  {fmtUsd(item.weekly_savings_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function OptimizationComparison({
  impact,
}: {
  impact: OptimizationImpactValidation;
}) {
  const deltas = [
    { label: "Tokens", value: impact.deltas.tokens_pct, lowerIsBetter: true },
    { label: "Cost", value: impact.deltas.cost_pct, lowerIsBetter: true },
    {
      label: "Cache leverage",
      value: impact.deltas.cache_leverage_pct,
      lowerIsBetter: false,
    },
    {
      label: "Saved tokens",
      value: impact.deltas.saved_tokens_pct,
      lowerIsBetter: false,
    },
  ];

  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Shadow vs Live"
        title="Comparison"
        action={
          <Hint text="Direct comparison of performance before and after applying the current policy (or during a shadow run)." />
        }
      />

      <div className="grid gap-4 md:grid-cols-4">
        {deltas.map((d) => (
          <MetricCard
            key={d.label}
            label={d.label}
            value={fmtDeltaPercent(d.value)}
            detail="vs baseline"
            tone={deltaTone(d.value, d.lowerIsBetter)}
          />
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <article className="border border-neutral-800 bg-neutral-950/60 p-5">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
            Baseline (Before)
          </div>
          <div className="mt-4 grid grid-cols-2 gap-4">
            <div>
              <div className="text-xs text-neutral-400">Traces</div>
              <div className="text-xl font-semibold text-neutral-100">
                {impact.before.trace_count}
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">Avg Cost</div>
              <div className="text-xl font-semibold text-neutral-100">
                {fmtUsd(impact.before.avg_cost_usd)}
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">Avg Tokens</div>
              <div className="text-xl font-semibold text-neutral-100">
                {fmtTok(impact.before.avg_tokens)}
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">Cache leverage</div>
              <div className="text-xl font-semibold text-neutral-100">
                {fmtPercent(impact.before.avg_cache_leverage)}
              </div>
            </div>
          </div>
        </article>

        <article className="border border-neutral-800 bg-neutral-950/60 p-5">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
            Current / Shadow (After)
          </div>
          <div className="mt-4 grid grid-cols-2 gap-4">
            <div>
              <div className="text-xs text-neutral-400">Traces</div>
              <div className="text-xl font-semibold text-neutral-100">
                {impact.after.trace_count}
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">Avg Cost</div>
              <div className="text-xl font-semibold text-neutral-100">
                {fmtUsd(impact.after.avg_cost_usd)}
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">Avg Tokens</div>
              <div className="text-xl font-semibold text-neutral-100">
                {fmtTok(impact.after.avg_tokens)}
              </div>
            </div>
            <div>
              <div className="text-xs text-neutral-400">Cache leverage</div>
              <div className="text-xl font-semibold text-neutral-100">
                {fmtPercent(impact.after.avg_cache_leverage)}
              </div>
            </div>
          </div>
        </article>
      </div>
    </section>
  );
}

function SupportingEvidence({
  summary,
  outcomes,
}: {
  summary: OptimizationsSummary;
  outcomes: OutcomesSummary | null;
}) {
  return (
    <section className="space-y-4">
      <SectionHeader eyebrow="Supporting evidence" title="Evidence" />

      <div className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Route decisions"
          value={outcomes ? outcomes.route_decisions.toLocaleString() : "—"}
          detail={
            outcomes && outcomes.route_decisions > 0
              ? `avg score ${outcomes.route_avg_score.toFixed(3)}`
              : "no routing decisions observed"
          }
          tone="cyan"
        />
        <MetricCard
          label="Route avg score"
          value={
            outcomes && outcomes.route_decisions > 0
              ? outcomes.route_avg_score.toFixed(3)
              : "—"
          }
          tone={qualityTone(outcomes?.route_avg_score ?? 0)}
        />
        <MetricCard
          label="Compact events"
          value={outcomes ? outcomes.compact_events.toLocaleString() : "—"}
          detail={
            outcomes && outcomes.compact_events > 0
              ? `avg score ${outcomes.compact_avg_score.toFixed(3)}`
              : "no compaction events observed"
          }
          tone="violet"
        />
        <MetricCard
          label="Compact avg score"
          value={
            outcomes && outcomes.compact_events > 0
              ? outcomes.compact_avg_score.toFixed(3)
              : "—"
          }
          tone={qualityTone(outcomes?.compact_avg_score ?? 0)}
        />
      </div>

      <div className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Quality score"
          value={`${summary.quality_score.score}/100`}
          detail={`${summary.quality_score.grade} grade`}
          tone={qualityTone(summary.quality_score.score / 100)}
        />
        <div
          title={
            summary.advisor.golden.failures.join("\n") || "No corpus issues"
          }
        >
          <MetricCard
            label="Golden failures"
            value={summary.advisor.golden.failures.length.toLocaleString()}
            detail={
              summary.advisor.golden.failures.length === 0
                ? `${summary.advisor.golden.passed.toLocaleString()} / ${summary.advisor.golden.total.toLocaleString()}`
                : "hover"
            }
            tone={
              summary.advisor.golden.failures.length === 0 ? "emerald" : "red"
            }
          />
        </div>
        <MetricCard
          label="Reread savings"
          value={fmtTok(summary.reread_telemetry.total_tokens_saved)}
          detail={fmtUsd(summary.reread_telemetry.total_cost_saved_usd)}
          tone="cyan"
        />
        <MetricCard
          label="Routine reroute"
          value={fmtUsd(
            summary.model_routing_simulation.estimated_cost_saved_usd
          )}
          detail={`${summary.model_routing_simulation.candidate_count} candidate traces`}
          tone="violet"
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <article className="border border-neutral-800 bg-neutral-950/60 p-5">
          <SectionHeader
            eyebrow="Coverage"
            title="Task mix"
            action={
              <Hint
                text={[
                  summary.advisor.confidence_reason,
                  summary.advisor.message,
                ]
                  .filter(Boolean)
                  .join("\n")}
              />
            }
          />
          <div className="mt-4 grid gap-3 md:grid-cols-3">
            {Object.entries(summary.advisor.bucket_counts).map(
              ([bucket, count]) => (
                <div
                  key={bucket}
                  className="border border-neutral-800 bg-black/20 p-3"
                >
                  <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
                    {bucket}
                  </div>
                  <div className="mt-2 text-xl font-semibold text-neutral-100">
                    {count.toLocaleString()}
                  </div>
                </div>
              )
            )}
          </div>
        </article>

        <article className="border border-neutral-800 bg-neutral-950/60 p-5">
          <SectionHeader eyebrow="Observed wins" title="Top levers" />
          <div className="mt-4 grid gap-3 md:grid-cols-3">
            {summary.auto_optimizations.slice(0, 3).map((item) => (
              <div
                key={item.id}
                className="border border-neutral-800 bg-black/20 p-3"
                title={`${fmtTok(item.tokens_saved)} saved across ${item.session_count.toLocaleString()} sessions`}
              >
                <div className="text-sm font-semibold text-neutral-100">
                  {item.title}
                </div>
                <div className="mt-2 text-xs text-neutral-400">
                  {fmtUsd(item.cost_saved_usd)}
                </div>
              </div>
            ))}
          </div>
        </article>
      </div>

      {summary.external_optimizations?.payload?.overview != null && (
        <article className="border border-neutral-800 bg-neutral-950/60 p-5">
          <SectionHeader eyebrow="External analyzer" title="Codeburn" />
          <div className="mt-4 grid gap-4 md:grid-cols-4">
            <MetricCard
              label="Sessions"
              value={summary.external_optimizations.payload.overview.sessions.toString()}
              tone="neutral"
            />
            <MetricCard
              label="Health"
              value={
                summary.external_optimizations.payload.overview.health_grade
              }
              detail={`${summary.external_optimizations.payload.overview.health_score}/100`}
              tone="cyan"
            />
            <MetricCard
              label="External savings"
              value={fmtUsd(
                summary.external_optimizations.payload.overview
                  .estimated_usd_saved
              )}
              detail={fmtTok(
                summary.external_optimizations.payload.overview
                  .estimated_tokens_saved
              )}
              tone="emerald"
            />
            <MetricCard
              label="Issues"
              value={summary.external_optimizations.payload.overview.issue_count.toString()}
              detail={summary.external_optimizations.period}
              tone="amber"
            />
          </div>
        </article>
      )}
    </section>
  );
}

export default function Optimizations() {
  const [summary, setSummary] = useState<OptimizationsSummary | null>(null);
  const [outcomes, setOutcomes] = useState<OutcomesSummary | null>(null);
  const { days, range } = useTimeRange();
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string>("");
  const [showSandbox, setShowSandbox] = useState(false);

  useEffect(() => {
    setLoading(true);
    api
      .optimizationsSummary(days)
      .then((payload) => {
        setSummary(payload);
        setErr(null);
      })
      .catch((error) => setErr(String(error)))
      .finally(() => setLoading(false));
  }, [days]);

  useEffect(() => {
    api
      .outcomesSummary(range)
      .then(setOutcomes)
      .catch(() => setOutcomes(null));
  }, [range]);

  useEffect(() => {
    if (!summary) return;
    const preferred =
      summary.advisor.recommended_candidate_id ??
      summary.advisor.current_candidate_id;
    setSelectedCandidateId(preferred);
  }, [summary]);

  const orderedCandidates = useMemo(() => {
    if (!summary) return [];
    const presetOrder = [
      "conservative",
      "balanced",
      "economy",
      "maximum_saving",
    ];
    const baseCandidates = summary.advisor.candidates.filter(
      (c) => presetOrder.includes(c.policy.preset) && c.id !== "current"
    );
    return baseCandidates.sort((a, b) => {
      return (
        presetOrder.indexOf(a.policy.preset) -
        presetOrder.indexOf(b.policy.preset)
      );
    });
  }, [summary]);

  const currentCandidate = useMemo(() => {
    if (!summary) return null;
    return (
      summary.advisor.candidates.find(
        (candidate) => candidate.id === summary.advisor.current_candidate_id
      ) ?? null
    );
  }, [summary]);

  const recommendedCandidate = useMemo(() => {
    if (!summary || !summary.advisor.recommended_candidate_id) return null;
    return (
      summary.advisor.candidates.find(
        (candidate) => candidate.id === summary.advisor.recommended_candidate_id
      ) ?? null
    );
  }, [summary]);

  const selectedCandidate = useMemo(() => {
    if (!summary) return null;
    return (
      summary.advisor.candidates.find(
        (candidate) => candidate.id === selectedCandidateId
      ) ??
      recommendedCandidate ??
      currentCandidate
    );
  }, [summary, selectedCandidateId, recommendedCandidate, currentCandidate]);

  const sessionEvidence = useMemo(
    () =>
      summary
        ? collectSessionEvidence(
            summary.recommendations.recommendations,
            outcomes?.sessions_with_high_extra_reads ?? []
          )
        : [],
    [summary, outcomes]
  );

  if (err) return <div className="p-6 text-red-300">Error: {err}</div>;
  if (!summary || loading || !currentCandidate || !selectedCandidate) {
    return (
      <div className="p-6 italic text-neutral-400">
        Loading optimizations...
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Sessions analysed"
          value={summary.advisor.sessions_analysed.toLocaleString()}
          detail={`${summary.advisor.replayable_tasks.toLocaleString()} tasks`}
          tone="neutral"
        />
        <div title={summary.advisor.confidence_reason}>
          <MetricCard
            label="Confidence"
            value={fmtPolicyLabel(summary.advisor.confidence)}
            detail={
              summary.advisor.has_recommendation
                ? "hover for caveat"
                : "not enough history"
            }
            tone={confidenceTone(summary.advisor.confidence)}
          />
        </div>
        <MetricCard
          label="Current weekly cost"
          value={"-" + fmtUsd(currentCandidate.weekly_cost_usd)}
          detail={currentCandidate.policy.name}
          tone="red"
        />
        <div title={summary.advisor.message}>
          <MetricCard
            label="Potential weekly savings"
            value={fmtSignedUsd(summary.advisor.weekly_savings_usd)}
            detail={
              summary.advisor.has_recommendation
                ? fmtDeltaPercent(
                    -summary.advisor.weekly_savings_usd /
                      Math.max(summary.advisor.baseline_weekly_cost_usd, 0.0001)
                  )
                : "hover for note"
            }
            tone={summary.advisor.has_recommendation ? "emerald" : "amber"}
          />
        </div>
      </section>

      <section className="space-y-4">
        <SectionHeader
          eyebrow="Optimization Frontier"
          title="Select configuration"
          action={
            <Hint text="Current, recommended, and alternative policy points from the advisor frontier." />
          }
        />

        <div className="grid gap-4 xl:grid-cols-5">
          {orderedCandidates.map((candidate) => {
            const selected =
              candidate.id === selectedCandidateId && !showSandbox;
            const isCurrent =
              candidate.policy.preset === currentCandidate?.policy.preset;
            const isRecommended =
              candidate.policy.preset === recommendedCandidate?.policy.preset;
            return (
              <button
                key={candidate.id}
                type="button"
                onClick={() => {
                  setSelectedCandidateId(candidate.id);
                  setShowSandbox(false);
                }}
                className={cx(
                  "border p-4 text-left transition",
                  selected
                    ? "border-amber-500/60 bg-amber-950/15"
                    : "border-neutral-800 bg-neutral-950/60 hover:border-neutral-700"
                )}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <div className="text-sm font-semibold text-neutral-100">
                    {candidate.policy.name}
                  </div>
                  {isCurrent && <Chip tone="neutral">Current</Chip>}
                  {isRecommended && <Chip tone="amber">Recommended</Chip>}
                </div>
                <div className="mt-2 text-xs uppercase tracking-widest text-neutral-400">
                  {fmtPolicyLabel(candidate.policy.preset)}
                </div>
                {(() => {
                  const savings =
                    summary.advisor.baseline_weekly_cost_usd -
                    candidate.weekly_cost_usd;
                  const totalSaved = candidate.total_saved_usd ?? savings;
                  const isSaving = totalSaved > 0.01;
                  // Breakdown rows only reconcile to totalSaved on payloads
                  // that carry the per-component fields -- older cached
                  // advisor_history entries lack them, so skip the rows
                  // rather than show misleading $0.00 lines under a nonzero
                  // (lump-fallback) total.
                  const hasBreakdown = candidate.total_saved_usd !== undefined;
                  return (
                    <>
                      <div
                        className={cx(
                          "mt-4 text-2xl font-semibold",
                          isSaving ? "text-emerald-300" : "text-neutral-100"
                        )}
                      >
                        {isSaving ? "+" : ""}
                        {fmtUsd(Math.max(0, totalSaved))}
                      </div>
                      <div className="mt-1 text-xs text-neutral-400">
                        savings / week
                      </div>
                      {hasBreakdown && (
                        <div className="mt-3 space-y-1 border-t border-neutral-800 pt-3 text-xs">
                          <div className="flex justify-between text-neutral-400">
                            <span>Read</span>
                            <span className="font-mono text-neutral-300">
                              {fmtSignedUsd(candidate.read_saved_usd ?? 0)}
                            </span>
                          </div>
                          <div className="flex justify-between text-neutral-400">
                            <span>Carry</span>
                            <span className="font-mono text-neutral-300">
                              {fmtSignedUsd(candidate.carry_saved_usd ?? 0)}
                            </span>
                          </div>
                          <div className="flex justify-between text-neutral-400">
                            <span>Output</span>
                            <span className="font-mono text-neutral-300">
                              {fmtSignedUsd(candidate.output_saved_usd ?? 0)}
                            </span>
                          </div>
                          <div className="flex justify-between text-neutral-400">
                            <span>Routing</span>
                            <span className="font-mono text-neutral-300">
                              {fmtSignedUsd(candidate.routing_saved_usd ?? 0)}
                            </span>
                          </div>
                        </div>
                      )}
                    </>
                  );
                })()}
                <div className="mt-4 space-y-2 text-sm text-neutral-300">
                  <div>Quality {fmtPercent(candidate.estimated_quality)}</div>
                  <div>Latency {candidate.latency_mult.toFixed(2)}x</div>
                  <div>Escalation {fmtPercent(candidate.escalation_rate)}</div>
                </div>
              </button>
            );
          })}

          <button
            type="button"
            onClick={() => setShowSandbox(true)}
            className={cx(
              "border p-4 text-left transition",
              showSandbox
                ? "border-amber-500/60 bg-amber-950/15"
                : "border-neutral-800 bg-neutral-950/60 hover:border-neutral-700"
            )}
          >
            <div className="text-sm font-semibold text-neutral-100">
              Advanced
            </div>
            <div className="mt-2 text-xs uppercase tracking-widest text-neutral-400">
              Custom Tuning
            </div>
            <div className="mt-4 text-2xl font-semibold text-neutral-100">
              Sandbox
            </div>
            <div className="mt-1 text-xs text-neutral-400">
              Interactive tuning
            </div>
            <div className="mt-4 space-y-2 text-sm text-neutral-300 italic">
              Adjust quality floors, compaction triggers, and routing.
            </div>
          </button>
        </div>
      </section>

      {showSandbox && (
        <PolicySandbox initialPolicy={selectedCandidate.policy} />
      )}

      <CandidateDetail
        candidate={selectedCandidate}
        currentCandidate={currentCandidate}
        headerOverride={showSandbox ? "Customized Policy" : undefined}
      />

      <OptimizationComparison impact={summary.impact_validation} />

      <OptimizationHistory history={summary.advisor_history} />

      <NextActions summary={summary} selectedCandidate={selectedCandidate} />

      <SessionsBehindRecommendation evidence={sessionEvidence} />

      <SupportingEvidence summary={summary} outcomes={outcomes} />
    </div>
  );
}
