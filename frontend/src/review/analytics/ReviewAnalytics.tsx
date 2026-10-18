import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { X } from "lucide-react";

import {
  Alert,
  Badge,
  Button,
  EmptyState,
  FieldLabel,
  Input,
  MetricCard,
  SectionHeader,
  Select,
  SelectItem,
} from "../../components/WorkbenchUI";
import { Sheet, SheetBody, SheetClose, SheetContent, SheetHeader, SheetTitle } from "../../components/ui/sheet";
import { useResolvedProductCapabilities } from "../../lib/RuntimeCapabilities";
import { useTimeRange } from "../../lib/TimeRangeContext";
import { reviewHref } from "../reviewApi";
import {
  fetchReviewAnalytics,
  fetchReviewAnalyticsDrill,
  type AnalyticsRow,
  type DrillRecord,
  type EscalationLane,
  type ReviewAnalyticsDrill,
  type ReviewAnalyticsReport,
} from "./reviewAnalyticsApi";

/* Review analytics on the shared Usage page (P13-ANALYTICS). Advisory only: nothing here feeds Merge when ready. */

const ALL_REPOSITORIES = "__all__";

const COST_CLASS_LABELS: Record<string, string> = {
  customer: "Your compute & keys",
  managed: "LemonCrow-managed",
  unattributed: "Not attributed",
};

const RECORD_LABELS: Record<string, string> = {
  review: "Review",
  outcome: "Review outcome",
  decision_event: "Decision",
  merge_commit: "Merge",
  ai_finding: "AI finding",
  deterministic_finding: "Check finding",
  intelligence_run: "Model run",
  hosted_jobs: "Job time",
  compute_usage: "Compute",
  compute_saved: "Reused result",
  review_action: "Fix request",
  escaped_defect: "Escaped defect",
  side_effect: "Side effect",
  auto_approval_decision: "Auto-approval check",
  break_glass: "Break-glass",
  run_plan: "Review plan",
};

const SIGNAL_LABELS: Record<string, { label: string; tone: "success" | "warning" | "neutral" }> = {
  signal: { label: "Signal", tone: "success" },
  noise: { label: "Mostly dismissed", tone: "warning" },
  insufficient_data: { label: "Too few judged", tone: "neutral" },
};

export function formatHours(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return "—";
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} d`;
}

function formatRate(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? "—" : `${Math.round(rate * 100)}%`;
}

function formatNumber(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(digits);
}

function formatWhen(at: string): string {
  const instant = new Date(at);
  return Number.isNaN(instant.getTime()) ? "" : instant.toLocaleString();
}

function recordLabel(kind: string): string {
  return RECORD_LABELS[kind] ?? kind.replace(/_/g, " ");
}

type OpenDrill = (key: string, label: string) => void;

/** A count that opens the Reviews it was computed from. */
function DrillCount({ row, onDrill, children }: { row: AnalyticsRow; onDrill: OpenDrill; children?: ReactNode }) {
  return (
    <Button
      type="button"
      variant="link"
      className="font-mono text-cyan-300 underline-offset-2 hover:text-cyan-200 hover:underline"
      onClick={() => onDrill(row.drill, row.label)}
      aria-label={`Show the Reviews behind ${row.label}`}
    >
      {children ?? row.count}
    </Button>
  );
}

/** Report sections fed by a single optional source: without it they have nothing to show. */
const WHOLE_SECTIONS = new Set([
  "depth",
  "escalation",
  "drift",
  "overrides",
  "auto_approval",
  "correction_loops",
  "escaped_defects",
  "cache_reuse",
  "checks",
  "waiting_by_owner",
  "reviewer_load",
]);

/** Report section -> labels of the sources this engine does not keep for it. */
type MissingSources = Map<string, string[]>;

function missingSources(report: ReviewAnalyticsReport): MissingSources {
  const missing: MissingSources = new Map();
  for (const source of report.unavailable ?? []) {
    for (const section of source.sections) missing.set(section, [...(missing.get(section) ?? []), source.label]);
  }
  return missing;
}

function Panel({
  title,
  basis,
  sections = [],
  missing,
  children,
}: {
  title: string;
  basis?: string;
  sections?: string[];
  missing?: MissingSources;
  children: ReactNode;
}) {
  const labels = [...new Set(sections.flatMap((section) => missing?.get(section) ?? []))];
  const empty = sections.length > 0 && sections.every((section) => missing?.has(section) && WHOLE_SECTIONS.has(section));
  return (
    <section className="min-w-0 space-y-3 rounded-lg border border-border/70 bg-background/60 p-4">
      <FieldLabel>{title}</FieldLabel>
      {labels.length > 0 && (
        <p className="text-label text-muted-foreground" data-testid="review-analytics-unavailable">
          Not available in this engine: {labels.join(", ")}.
        </p>
      )}
      {!empty && children}
      {!empty && basis && <p className="text-caption leading-5 text-muted-foreground">{basis}</p>}
    </section>
  );
}

function RowsTable({
  rows,
  onDrill,
  extra,
  empty = "Nothing in this period.",
}: {
  rows: AnalyticsRow[];
  onDrill: OpenDrill;
  extra?: { heading: string; cell: (row: AnalyticsRow) => ReactNode };
  empty?: string;
}) {
  if (!rows.length) return <p className="text-label text-muted-foreground">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-label">
        <thead className="text-left text-caption uppercase tracking-wider text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 font-medium">Item</th>
            <th className="py-1 pr-3 text-right font-medium">Count</th>
            <th className="py-1 pr-3 text-right font-medium">Reviews</th>
            {extra && <th className="py-1 text-right font-medium">{extra.heading}</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.drill} className="border-t border-border/50">
              <td className="max-w-[22rem] truncate py-1 pr-3 text-neutral-200" title={row.label}>
                {row.label}
              </td>
              <td className="py-1 pr-3 text-right">
                <DrillCount row={row} onDrill={onDrill} />
              </td>
              <td className="py-1 pr-3 text-right font-mono text-neutral-400">{row.reviews}</td>
              {extra && <td className="py-1 text-right font-mono text-neutral-300">{extra.cell(row)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const WAIT_COLUMN = {
  heading: "Median / longest wait",
  cell: (row: AnalyticsRow) => `${formatHours(row.median_hours)} / ${formatHours(row.max_hours)}`,
};

function withCostClass(rows: AnalyticsRow[], suffix: (row: AnalyticsRow) => string): AnalyticsRow[] {
  return rows.map((row) => ({
    ...row,
    label: `${COST_CLASS_LABELS[row.cost_class ?? ""] ?? row.cost_class ?? ""} · ${suffix(row)}`,
  }));
}

function LaneRows({ lanes, onDrill }: { lanes: Array<[string, EscalationLane]>; onDrill: OpenDrill }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-label">
        <thead className="text-left text-caption uppercase tracking-wider text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 font-medium">Depth</th>
            <th className="py-1 pr-3 text-right font-medium">Reviews</th>
            <th className="py-1 pr-3 text-right font-medium">Useful AI findings</th>
            <th className="py-1 pr-3 text-right font-medium">AI model calls</th>
            <th className="py-1 text-right font-medium">Calls per useful finding</th>
          </tr>
        </thead>
        <tbody>
          {lanes.map(([label, lane]) => (
            <tr key={label} className="border-t border-border/50">
              <td className="py-1 pr-3 text-neutral-200">{label}</td>
              <td className="py-1 pr-3 text-right">
                <DrillCount row={lane.reviews} onDrill={onDrill} />
              </td>
              <td className="py-1 pr-3 text-right">
                <DrillCount row={lane.useful_ai_findings} onDrill={onDrill} />
              </td>
              <td className="py-1 pr-3 text-right">
                <DrillCount row={lane.ai_model_calls} onDrill={onDrill}>
                  {formatNumber(lane.ai_model_calls.value ?? 0, 0)}
                </DrillCount>
              </td>
              <td className="py-1 text-right font-mono text-neutral-300">{formatNumber(lane.model_calls_per_useful)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DrillSheet({
  mode,
  report,
  target,
  onClose,
}: {
  mode: string;
  report: ReviewAnalyticsReport;
  target: { key: string; label: string } | null;
  onClose: () => void;
}) {
  const [drill, setDrill] = useState<ReviewAnalyticsDrill | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!target) return;
    let live = true;
    setDrill(null);
    setError("");
    fetchReviewAnalyticsDrill(mode, target.key, report.scope)
      .then((result) => live && setDrill(result))
      .catch((reason: unknown) => live && setError(reason instanceof Error ? reason.message : "Could not load records"));
    return () => {
      live = false;
    };
  }, [mode, report.scope, target]);

  return (
    <Sheet open={target !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-[min(720px,100%)]" aria-describedby={undefined}>
        <SheetHeader className="items-center">
          <div className="min-w-0 flex-1">
            <SheetTitle className="text-caption font-mono font-normal uppercase tracking-widest text-muted-foreground">
              Reviews behind this figure
            </SheetTitle>
            <div className="mt-0.5 truncate text-sm font-semibold text-foreground">{target?.label}</div>
          </div>
          <SheetClose asChild>
            <Button type="button" size="icon" variant="ghost" aria-label="Close records">
              <X size={16} />
            </Button>
          </SheetClose>
        </SheetHeader>
        <SheetBody>
          {error ? (
            <Alert tone="destructive" description={error} />
          ) : !drill ? (
            <EmptyState title="Loading records…" className="p-4" />
          ) : (
            <div className="space-y-3" data-testid="review-analytics-drill">
              <p className="text-label text-muted-foreground">
                {drill.count} {drill.count === 1 ? "record" : "records"} in {drill.reviews}{" "}
                {drill.reviews === 1 ? "Review" : "Reviews"}
                {drill.truncated ? ` — showing the newest ${drill.records.length}` : ""}.
              </p>
              {!drill.records.length ? (
                <p className="text-label text-muted-foreground">No records.</p>
              ) : (
                <ul className="divide-y divide-border/50">
                  {drill.records.map((record: DrillRecord) => (
                    <li key={`${record.repo_id}:${record.review_id}:${record.record_kind}:${record.record_id}`} className="py-2">
                      <div className="flex items-baseline justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-label text-neutral-200">
                            {record.review_title || record.review_id}
                          </div>
                          <div className="truncate text-caption text-muted-foreground">
                            {record.repo_name} · {recordLabel(record.record_kind)}
                            {record.label ? ` · ${record.label}` : ""}
                            {record.at ? ` · ${formatWhen(record.at)}` : ""}
                            {record.value ? ` · ${formatNumber(record.value, 2)}` : ""}
                          </div>
                        </div>
                        <Link className="shrink-0 text-label text-cyan-300 hover:underline" to={reviewHref(record.review_id)}>
                          Open Review
                        </Link>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}

function Filters({
  repositories,
  teamsSupported,
  repo,
  team,
  path,
  onApply,
}: {
  repositories: Array<[string, string]>;
  teamsSupported: boolean;
  repo: string;
  team: string;
  path: string;
  onApply: (next: { repo: string; team: string; path: string }) => void;
}) {
  const [draftTeam, setDraftTeam] = useState(team);
  const [draftPath, setDraftPath] = useState(path);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onApply({ repo, team: draftTeam.trim(), path: draftPath.trim() });
  };
  return (
    <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
      <Select
        aria-label="Repository"
        value={repo || ALL_REPOSITORIES}
        onValueChange={(value) =>
          onApply({ repo: value === ALL_REPOSITORIES ? "" : value, team: draftTeam.trim(), path: draftPath.trim() })
        }
        uiSize="xs"
        className="min-w-40"
      >
        <SelectItem value={ALL_REPOSITORIES}>All repositories</SelectItem>
        {repositories.map(([id, name]) => (
          <SelectItem key={id} value={id}>
            {name}
          </SelectItem>
        ))}
      </Select>
      {teamsSupported && (
        <Input
          aria-label="Team"
          placeholder="Team"
          uiSize="xs"
          className="w-32"
          value={draftTeam}
          onChange={(event) => setDraftTeam(event.target.value)}
        />
      )}
      <Input
        aria-label="Path"
        placeholder="Path, e.g. src/api"
        uiSize="xs"
        className="w-44"
        value={draftPath}
        onChange={(event) => setDraftPath(event.target.value)}
      />
      <Button type="submit" size="xs">
        Apply
      </Button>
    </form>
  );
}

function Report({ report, onDrill }: { report: ReviewAnalyticsReport; onDrill: OpenDrill }) {
  const findings = report.findings;
  const loops = report.correction_loops;
  const load = report.reviewer_load;
  const cost = report.cost;
  const perUseful = report.cost_per_useful_finding.filter((row) => row.per_useful_finding !== null);
  const missing = missingSources(report);
  const measureLabel: Record<string, string> = {
    provider_spend: "Provider spend",
    model_calls: "Model calls",
    compute_minutes: "Compute minutes",
    job_minutes: "Job minutes",
  };
  return (
    <div className="space-y-4">
      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Reviews"
          value={String(report.volume.reviews.count)}
          detail={`${report.time_to_first_review.awaiting.count} still waiting for a first review`}
        />
        <MetricCard
          label="Time to first review"
          value={formatHours(report.time_to_first_review.median_hours)}
          detail={`Median; 90th percentile ${formatHours(report.time_to_first_review.p90_hours)}`}
        />
        <MetricCard
          label="Time to merge"
          value={formatHours(report.merge_time.median_hours)}
          detail={`Median over ${report.merge_time.merged.count} merged`}
        />
        <MetricCard
          label="Useful findings"
          value={String(report.catch_contribution.useful.count)}
          detail={`${formatRate(report.catch_contribution.acted_on_rate)} of current findings were acted on`}
          tone="success"
        />
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Volume" sections={["merge_time"]} missing={missing}>
          <RowsTable rows={[report.volume.reviews, report.time_to_first_review.awaiting, report.merge_time.merged]} onDrill={onDrill} />
          <RowsTable rows={[...report.volume.by_status, ...report.volume.by_author_kind]} onDrill={onDrill} />
        </Panel>

        <Panel
          title="Who the next step is waiting on"
          basis={load.basis}
          sections={["waiting_by_owner", "reviewer_load"]}
          missing={missing}
        >
          <RowsTable rows={report.waiting_by_owner.filter((row) => row.count > 0)} onDrill={onDrill} extra={WAIT_COLUMN} empty="No open Reviews are waiting." />
          {load.mine && (
            <RowsTable rows={[load.mine.to_review, load.mine.waiting_on_me]} onDrill={onDrill} extra={WAIT_COLUMN} />
          )}
          {load.by_team && (
            <>
              <FieldLabel>{load.teams_shown === "all" ? "Teams" : "Your teams"}</FieldLabel>
              <RowsTable rows={load.by_team} onDrill={onDrill} extra={WAIT_COLUMN} empty="No team has been asked to review." />
            </>
          )}
        </Panel>

        <Panel title="Findings" basis={findings.basis} sections={["findings", "catch_contribution"]} missing={missing}>
          <RowsTable rows={[findings.current, ...findings.by_source, ...findings.by_outcome]} onDrill={onDrill} />
          <FieldLabel>Not counted above</FieldLabel>
          <RowsTable rows={findings.invalidated} onDrill={onDrill} />
          <FieldLabel>By severity and category</FieldLabel>
          <RowsTable rows={[...findings.by_severity, ...findings.by_category]} onDrill={onDrill} />
        </Panel>

        <Panel title="Rules and checks" basis={report.policy_effectiveness.basis} sections={["rules", "checks"]} missing={missing}>
          {!report.rules.length && !report.checks.length ? (
            <p className="text-label text-muted-foreground">No rule or check reported a finding in this period.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-label">
                <thead className="text-left text-caption uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-3 font-medium">Rule or check</th>
                    <th className="py-1 pr-3 text-right font-medium">Hits</th>
                    <th className="py-1 pr-3 text-right font-medium">Acted on</th>
                    <th className="py-1 pr-3 text-right font-medium">Dismissed</th>
                    <th className="py-1 text-right font-medium">Reads as</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ...report.rules.map((rule) => ({ name: rule.rule, detail: rule.check, ...rule })),
                    ...report.checks.map((check) => ({ name: check.check, detail: "", ...check })),
                  ].map((row) => {
                    const signal = SIGNAL_LABELS[row.signal] ?? SIGNAL_LABELS.insufficient_data;
                    const cell = (suffix: string, count: number) => (
                      <DrillCount row={{ key: suffix, label: `${row.name} — ${suffix.replace("_", " ")}`, count, reviews: 0, drill: row.drill[suffix] }} onDrill={onDrill} />
                    );
                    return (
                      <tr key={`${row.kind}:${row.name}`} className="border-t border-border/50">
                        <td className="max-w-[20rem] py-1 pr-3">
                          <div className="truncate font-mono text-neutral-200" title={row.name}>
                            {row.name}
                          </div>
                          {row.detail && <div className="truncate text-caption text-muted-foreground">{row.detail}</div>}
                        </td>
                        <td className="py-1 pr-3 text-right">{cell("hits", row.hits)}</td>
                        <td className="py-1 pr-3 text-right">{cell("acted_on", row.acted_on)}</td>
                        <td className="py-1 pr-3 text-right">{cell("dismissed", row.dismissed)}</td>
                        <td className="py-1 text-right">
                          <Badge tone={signal.tone}>{signal.label}</Badge>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title="Auto-approval and overrides" sections={["auto_approval", "overrides"]} missing={missing}>
          <RowsTable rows={[report.auto_approval.evaluated, report.auto_approval.eligible, report.auto_approval.not_eligible]} onDrill={onDrill} />
          <FieldLabel>Conditions that were not met</FieldLabel>
          <RowsTable
            rows={report.auto_approval.failed_gates.map((gate) => ({ ...gate, label: `${gate.label} — ${gate.reason.replace(/_/g, " ")}` }))}
            onDrill={onDrill}
            extra={{ heading: "Policy versions", cell: (row) => String((row as { policy_generation_count?: number }).policy_generation_count ?? 0) }}
            empty="Every evaluated condition was met."
          />
          <FieldLabel>Break-glass</FieldLabel>
          <RowsTable rows={[report.overrides.break_glass, ...report.overrides.bypassed]} onDrill={onDrill} />
        </Panel>

        <Panel title="Review depth and escalation" sections={["depth", "escalation"]} missing={missing}>
          <RowsTable rows={report.depth.distribution} onDrill={onDrill} />
          <LaneRows
            lanes={[
              ["Escalated", report.escalation.escalated],
              ["Not escalated", report.escalation.standard],
            ]}
            onDrill={onDrill}
          />
          {report.escalation.triggers.length > 0 && <RowsTable rows={report.escalation.triggers} onDrill={onDrill} />}
        </Panel>

        <Panel title="Cost" basis={cost.basis} sections={["cost", "cache_reuse"]} missing={missing}>
          <FieldLabel>Model calls</FieldLabel>
          <RowsTable
            rows={withCostClass(cost.model_calls, (row) => row.locus ?? "")}
            onDrill={onDrill}
            extra={{ heading: "Calls", cell: (row) => formatNumber(row.value ?? 0, 0) }}
          />
          {cost.provider_spend.length > 0 && (
            <RowsTable
              rows={withCostClass(cost.provider_spend, () => "provider spend")}
              onDrill={onDrill}
              extra={{ heading: "Amount", cell: (row) => `${formatNumber(row.value ?? 0, 2)} ${row.currency ?? ""}` }}
            />
          )}
          {cost.calls_without_cost.length > 0 && (
            <RowsTable rows={withCostClass(cost.calls_without_cost, () => "calls without a reported cost")} onDrill={onDrill} />
          )}
          <FieldLabel>Compute</FieldLabel>
          <RowsTable
            rows={[
              ...withCostClass(cost.compute_minutes, (row) => `measured on ${row.executor ?? ""}`),
              ...withCostClass(cost.job_minutes, (row) => `job time on ${row.executor ?? ""}`),
            ]}
            onDrill={onDrill}
            extra={{ heading: "Minutes", cell: (row) => formatNumber(row.value ?? 0, 1) }}
          />
          {perUseful.length > 0 && (
            <>
              <FieldLabel>Per useful finding</FieldLabel>
              <ul className="space-y-1 text-label">
                {perUseful.map((row) => (
                  <li key={`${row.measure}:${row.cost_class}:${row.currency ?? ""}`} className="flex justify-between gap-3">
                    <span className="text-neutral-300">
                      {COST_CLASS_LABELS[row.cost_class] ?? row.cost_class} · {measureLabel[row.measure]}
                    </span>
                    <span className="font-mono text-neutral-200">
                      {formatNumber(row.per_useful_finding, 2)}
                      {row.currency ? ` ${row.currency}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {report.cache_reuse.length > 0 && (
            <>
              <FieldLabel>Reused results</FieldLabel>
              <RowsTable rows={report.cache_reuse} onDrill={onDrill} extra={{ heading: "Minutes saved", cell: (row) => formatNumber(row.value ?? 0, 1) }} />
            </>
          )}
        </Panel>

        <Panel title="Fix requests" sections={["correction_loops"]} missing={missing}>
          <RowsTable
            rows={[loops.started, loops.snapshot_recorded, loops.re_reviewed, loops.in_flight, loops.failed, loops.cancelled]}
            onDrill={onDrill}
          />
          <p className="text-label text-neutral-300">
            Re-reviewed after the fix: <span className="font-mono">{formatRate(loops.completion_rate)}</span> of finished fix requests.
          </p>
        </Panel>

        <Panel title="Escaped defects" basis={report.escaped_defects.basis} sections={["escaped_defects"]} missing={missing}>
          <RowsTable rows={[report.escaped_defects.total, ...report.escaped_defects.by_kind]} onDrill={onDrill} />
          {report.escaped_defects.by_depth.length > 0 && (
            <>
              <FieldLabel>By review depth</FieldLabel>
              <RowsTable rows={report.escaped_defects.by_depth} onDrill={onDrill} />
            </>
          )}
          {report.escaped_defects.by_check.length > 0 && (
            <>
              <FieldLabel>Checks that passed on the escaped change</FieldLabel>
              <RowsTable rows={report.escaped_defects.by_check} onDrill={onDrill} />
            </>
          )}
        </Panel>

        <Panel title={`Side-effect drift by ${report.drift.granularity}`} sections={["drift"]} missing={missing}>
          <RowsTable rows={[report.drift.changes, ...report.drift.by_judgment]} onDrill={onDrill} />
          {report.drift.series.length > 0 && <RowsTable rows={report.drift.series} onDrill={onDrill} />}
          {report.drift.items.length > 0 && (
            <ul className="divide-y divide-border/50 text-label" aria-label="Side-effect changes">
              {report.drift.items.map((item) => (
                <li key={`${item.repo_id}:${item.review_id}:${item.revision_id}:${item.title}`} className="flex items-baseline justify-between gap-3 py-1.5">
                  <div className="min-w-0">
                    <div className="truncate text-neutral-200">
                      {item.critical && <Badge tone="warning" className="mr-1">Critical</Badge>}
                      {item.title}
                    </div>
                    <div className="truncate text-caption text-muted-foreground">
                      {item.repo_name} · {item.change} · {item.judgment.label}
                      {item.judgment.by ? ` by ${item.judgment.by}` : ""}
                    </div>
                  </div>
                  <Link className="shrink-0 text-cyan-300 hover:underline" to={reviewHref(item.review_id)}>
                    Open Review
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {report.drift.items_total > report.drift.items.length && (
            <p className="text-caption text-muted-foreground">
              Showing the newest {report.drift.items.length} of {report.drift.items_total} changes.
            </p>
          )}
        </Panel>
      </div>
    </div>
  );
}

function boundsNotices(report: ReviewAnalyticsReport): string[] {
  const out: string[] = [];
  const bounds = report.bounds;
  if (bounds.truncated) {
    out.push(`Showing the newest ${bounds.analyzed} of ${bounds.candidates} Reviews created in this period.`);
  }
  if (bounds.scan_truncated) out.push("Older Reviews were not reached; choose a shorter period or one repository to include them.");
  if (bounds.escapes_truncated) out.push("Escaped-defect matching reached its limit; that count may be low.");
  return [...out, ...report.notes];
}

export function ReviewAnalyticsSection({ mode }: { mode: string }) {
  const { days } = useTimeRange();
  const [filters, setFilters] = useState({ repo: "", team: "", path: "" });
  const [report, setReport] = useState<ReviewAnalyticsReport | null>(null);
  const [error, setError] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [loading, setLoading] = useState(true);
  const [repositories, setRepositories] = useState<Record<string, string>>({});
  const [target, setTarget] = useState<{ key: string; label: string } | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError("");
    fetchReviewAnalytics(mode, {
      days,
      repos: filters.repo ? [filters.repo] : [],
      teams: filters.team ? [filters.team] : [],
      paths: filters.path ? [filters.path] : [],
    })
      .then((result) => {
        if (!live) return;
        setReport(result);
        setRepositories((known) => {
          const next = { ...known };
          for (const row of result.volume.by_repository) next[row.key] = row.label;
          return next;
        });
      })
      .catch((reason: unknown) => {
        if (!live) return;
        // Without Review read access the section stays out of the way, as other Review surfaces do.
        if ((reason as { status?: number }).status === 403) setForbidden(true);
        setError(reason instanceof Error ? reason.message : "Could not load Review analytics");
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [mode, days, filters]);

  const repositoryOptions = useMemo(
    () => Object.entries(repositories).sort((a, b) => a[1].localeCompare(b[1])),
    [repositories],
  );
  const notices = report ? boundsNotices(report) : [];
  if (forbidden) return null;

  return (
    <section className="space-y-4" aria-label="Review analytics" data-testid="review-analytics">
      <SectionHeader
        eyebrow="Reviews"
        title="Review analytics"
        description={`Last ${days} ${days === 1 ? "day" : "days"}. Advisory only — never blocks Merge when ready. Select any count to see the Reviews behind it; no figure ranks people.`}
        action={
          <Filters
            repositories={repositoryOptions}
            teamsSupported={report?.teams_supported ?? mode !== "local"}
            repo={filters.repo}
            team={filters.team}
            path={filters.path}
            onApply={setFilters}
          />
        }
      />
      {error ? (
        <Alert tone="destructive" title="Review analytics are unavailable" description={error} />
      ) : !report ? (
        <EmptyState title={loading ? "Loading Review analytics…" : "No Review analytics"} className="p-4" />
      ) : (
        <>
          {notices.map((notice) => (
            <Alert key={notice} tone="warning" description={notice} />
          ))}
          {report.volume.reviews.count === 0 ? (
            <EmptyState title="No Reviews in this period" description="Try a longer period or clear the filters." className="p-4" />
          ) : (
            <Report report={report} onDrill={(key, label) => setTarget({ key, label })} />
          )}
          <DrillSheet mode={mode} report={report} target={target} onClose={() => setTarget(null)} />
        </>
      )}
    </section>
  );
}

/** Rendered on the shared Usage page only where Review is available in this deployment. */
export default function ReviewAnalytics() {
  const capabilities = useResolvedProductCapabilities();
  const review = capabilities?.products.review;
  if (!review?.available || review.mode === "none") return null;
  return <ReviewAnalyticsSection mode={review.mode} />;
}
