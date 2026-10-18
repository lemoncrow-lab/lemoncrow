import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/**
 * One product surface for the AI route and background intelligence (UX0a).
 *
 * Local Settings, hosted "AI providers & compute" and hosted Usage render the
 * same projections: BI0's effective route (`IntelligenceRoute.to_wire`) and
 * BI1's envelope status (`build_status`). Nothing here is a control: each
 * value names the authority that owns it, because neither backend offers a
 * write path from this surface.
 */

export type PolicyOrigin = { layer: string; source: string; scope_id?: string };
type Setting<T = unknown> = { effective_value: T; origin: PolicyOrigin };

export type RouteStatus =
  | "ready"
  | "disabled"
  | "not_configured"
  | "refused"
  | "invalid";

export type IntelligenceRouteProjection = {
  status: RouteStatus;
  reason: string;
  status_note?: string;
  requested: {
    driver: string;
    model: string;
    driver_origin: PolicyOrigin;
    model_origin: PolicyOrigin;
  };
  resolved: {
    driver: string;
    model: string;
    model_source: string;
    driver_origin: PolicyOrigin;
    model_origin: PolicyOrigin;
  };
  run_on: Setting<string> & { offered_here: boolean; note: string };
  background_enabled: boolean;
  background_note: string;
  data_locus: null | {
    driver: string;
    trust: string;
    trust_source: string;
    provider: string;
    data_region: string;
    training: string;
    retention_days: number | string;
  };
  policy: { valid: boolean; errors: string[] };
  editable: boolean;
  edit_source: string;
  /** Local only: the settings keys that own these values. */
  settings?: string[];
};

export type BackgroundState = "running" | "limited" | "paused" | "disabled";

type CostFields = {
  cost_complete: boolean;
  managed_usd_used: number | null;
  measured_managed_usd: number | null;
  measured_other_usd?: number | null;
  reported_other_usd?: number | null;
  unmeasured_cost_calls: number;
};

export type BackgroundBudgetStatus =
  | { available: false; reason: string }
  | {
      available: true;
      enabled: boolean;
      state: BackgroundState;
      reason: string;
      period: { kind: string; start_unix_s: number; end_unix_s: number };
      policy: {
        enabled: boolean;
        monthly_model_calls: number | null;
        daily_model_calls: number | null;
        monthly_managed_usd: number | null;
        valid: boolean;
        configured: boolean;
      };
      background: CostFields & {
        model_calls_used: number;
        model_calls_held: number;
        model_calls_remaining: number | null;
        today_model_calls: number;
        today_model_calls_remaining: number | null;
        managed_usd_held: number | null;
        managed_usd_remaining: number | null;
      };
      managed_spend: { applies: boolean; enforced: boolean; note: string };
      priorities: Array<{
        priority: string;
        share_percent: number;
        state: string;
        reason: string;
      }>;
      automatic_review: { envelope_enforced: boolean; note: string };
      by_capability: Array<
        CostFields & {
          lane: string;
          priority: string;
          capability: string;
          admissions: number;
          model_calls_used: number;
          model_calls_held: number;
        }
      >;
    };

export type IntelligenceScope = "local" | "hosted";

const DRIVER_LABEL: Record<string, string> = {
  openai: "OpenAI-compatible API",
  litellm: "LiteLLM",
  ollama: "Ollama",
  claude: "Claude Code",
  codex: "Codex",
  opencode: "OpenCode",
};

const RUN_ON_LABEL: Record<IntelligenceScope, Record<string, string>> = {
  local: {
    local: "This machine",
    worker: "Your worker",
    managed: "LemonCrow-managed",
    auto: "Auto",
  },
  hosted: {
    local: "Your machine",
    worker: "Your worker",
    managed: "LemonCrow-managed",
    auto: "Auto",
  },
};

/** Why background work is paused or limited, in product words. Unknown codes fall back to the code itself. */
export const BACKGROUND_REASON_TEXT: Record<string, string> = {
  background_disabled: "background intelligence is off",
  background_exhausted: "monthly budget reached",
  daily_guard_exhausted: "daily limit reached",
  managed_spend_exhausted: "managed spend limit reached",
  cost_unmeasurable: "cost cannot be estimated under the managed spend limit",
  priority_reserved: "the rest of the budget is held for higher-priority work",
  policy_invalid: "budget settings are invalid",
  hosted_quota_exhausted: "organization spend quota reached",
  managed_unavailable: "managed execution is not available here",
  operation_limit: "the per-operation limit is too low",
  capability_limit: "the capability limit is too low",
  review_limit: "the Review's own model-call budget is spent",
};

const PRIORITY_LABEL: Record<string, string> = {
  review_auto: "Automatic AI Review",
  knowledge_refresh: "Project knowledge refresh",
  discussion_summary: "Discussion summaries",
  learning_suggestion: "Learning suggestions",
};

const CAPABILITY_LABEL: Record<string, string> = {
  "review.auto_ai": "Automatic AI Review",
  "knowledge.change_narration": "Change narration",
  "knowledge.repository_refresh": "Project knowledge refresh",
  "review.thread_summary": "Discussion summaries",
  "review.feedback_digest": "Feedback digest",
  "review.rule_suggestion": "Learning suggestions",
};

const count = (value: number) => new Intl.NumberFormat().format(value);
const usd = (value: number) =>
  new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);

export function reasonText(reason: string): string {
  return BACKGROUND_REASON_TEXT[reason] ?? reason.replaceAll("_", " ");
}

function driverLabel(driver: string): string {
  return DRIVER_LABEL[driver] ?? driver;
}

export function routeLabel(route: IntelligenceRouteProjection): string {
  if (route.status === "disabled") return "Off";
  if (route.status === "not_configured") return "Not configured";
  if (route.status === "refused") return "Not allowed here";
  if (route.status === "invalid") return "Settings invalid";
  const resolved = driverLabel(route.resolved.driver);
  return route.requested.driver === "auto" ? `Auto · ${resolved}` : resolved;
}

export function modelLabel(route: IntelligenceRouteProjection): string {
  if (route.requested.model !== "auto") return route.requested.model;
  return route.status === "ready" && route.resolved.model
    ? `Auto · ${route.resolved.model}`
    : "Auto";
}

export function runOnLabel(scope: IntelligenceScope, value: string): string {
  return RUN_ON_LABEL[scope][value] ?? value;
}

export function stateLabel(state: BackgroundState): string {
  if (state === "running") return "Running";
  if (state === "limited") return "Running · limited";
  if (state === "paused") return "Paused";
  return "Off";
}

function budgetLabel(
  status: Extract<BackgroundBudgetStatus, { available: true }>
): string {
  const calls = status.policy.monthly_model_calls;
  const parts = [calls === null ? "No call limit" : `${count(calls)} calls`];
  if (
    status.managed_spend.applies &&
    status.policy.monthly_managed_usd !== null
  ) {
    parts.push(`${usd(status.policy.monthly_managed_usd)} managed`);
  }
  return parts.join(" / ");
}

// The period is a UTC month and its end is exclusive (the first instant of the next month).
function periodLabel(period: {
  start_unix_s: number;
  end_unix_s: number;
}): string {
  const day = (unix_s: number) =>
    new Date(unix_s * 1000).toLocaleDateString(undefined, { timeZone: "UTC" });
  return `${day(period.start_unix_s)} – ${day(period.end_unix_s - 1)} (UTC)`;
}

function usageLabel(
  status: Extract<BackgroundBudgetStatus, { available: true }>
): string {
  const used = status.background;
  const parts = [`${count(used.model_calls_used)} calls`];
  if (used.model_calls_held > 0)
    parts.push(`${count(used.model_calls_held)} in progress`);
  if (status.managed_spend.applies) {
    if (used.managed_usd_used !== null) parts.push(usd(used.managed_usd_used));
    else if (used.measured_managed_usd !== null)
      parts.push(`at least ${usd(used.measured_managed_usd)}`);
  }
  return parts.join(" · ");
}

function Row({
  label,
  value,
  detail,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
}) {
  return (
    <div className="grid gap-1 py-2.5 sm:grid-cols-[200px_minmax(0,1fr)] sm:gap-3">
      <dt className="text-caption font-medium text-muted-foreground">
        {label}
      </dt>
      <dd className="min-w-0 text-caption text-foreground">
        <div className="break-words">{value}</div>
        {detail ? (
          <div className="mt-0.5 break-words text-muted-foreground">
            {detail}
          </div>
        ) : null}
      </dd>
    </div>
  );
}

/** The one durable paused state: no toast, no repeated banner. */
function PausedNotice({
  reason,
  editSource,
  usageHref,
}: {
  reason: string;
  editSource: string;
  usageHref?: string;
}) {
  return (
    <div
      role="status"
      data-testid="background-paused"
      className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-caption"
    >
      <p className="font-medium text-foreground">
        Background intelligence paused · {reasonText(reason)}
      </p>
      <p className="mt-1 text-muted-foreground">
        Current Reviews still run deterministic checks and remain reviewable.
      </p>
      <p className="mt-1 text-muted-foreground">
        {usageHref ? (
          <>
            <Link className="underline hover:text-foreground" to={usageHref}>
              Usage
            </Link>
            {" · "}
          </>
        ) : null}
        Change the budget in {editSource}.
      </p>
    </div>
  );
}

export function BackgroundBudgetPanel({
  status,
  scope,
  editSource,
  usageHref,
  routeStatus = "ready",
}: {
  /** ``null`` while the status is still loading. */
  status: BackgroundBudgetStatus | null;
  scope: IntelligenceScope;
  editSource: string;
  usageHref?: string;
  routeStatus?: RouteStatus;
}) {
  if (status === null) {
    return (
      <dl
        className="divide-y divide-border border-y border-border"
        data-testid="background-budget"
      >
        <Row label="Background budget" value="Loading…" />
      </dl>
    );
  }
  if (!status.available) {
    return (
      <dl
        className="divide-y divide-border border-y border-border"
        data-testid="background-budget"
      >
        <Row
          label="Background budget"
          value="Unavailable"
          detail={status.reason}
        />
      </dl>
    );
  }
  const used = status.background;
  const awaitingProvider = status.state === "running" && routeStatus !== "ready";
  const statusDetail = awaitingProvider
    ? "Background work is allowed by the budget, but AI cannot run until a model route is ready."
    : status.state === "running" ? undefined : reasonText(status.reason);
  return (
    <div className="space-y-3" data-testid="background-budget">
      {status.state === "paused" ? (
        <PausedNotice
          reason={status.reason}
          editSource={editSource}
          usageHref={usageHref}
        />
      ) : null}
      <dl className="divide-y divide-border border-y border-border">
        <Row label="Monthly budget" value={budgetLabel(status)} />
        <Row
          label="Usage this month"
          value={usageLabel(status)}
          detail={
            used.cost_complete
              ? undefined
              : `${count(used.unmeasured_cost_calls)} call${used.unmeasured_cost_calls === 1 ? "" : "s"} reported no cost${status.managed_spend.applies ? "; spend shown is a lower bound" : ""}.`
          }
        />
        <Row
          label="Status"
          value={awaitingProvider ? "Waiting for an AI provider" : stateLabel(status.state)}
          detail={statusDetail}
        />
      </dl>
      <details className="rounded-md border border-border/80 px-3 py-2 text-caption">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
          Advanced budget details
        </summary>
        <div className="mt-2 space-y-3 text-muted-foreground">
          <dl className="divide-y divide-border">
            <Row
              label="Daily guard"
              value={
                status.policy.daily_model_calls === null
                  ? `No daily limit · ${count(used.today_model_calls)} calls today`
                  : `${count(used.today_model_calls)} of ${count(status.policy.daily_model_calls)} calls today`
              }
            />
            <Row
              label="Automatic AI Review"
              value={status.automatic_review.note}
            />
            <Row label="Managed spend" value={status.managed_spend.note} />
            <Row label="Period" value={periodLabel(status.period)} />
          </dl>
          <div>
            <h4 className="text-label font-medium text-foreground">
              Work classes
            </h4>
            <ul className="mt-1 space-y-1">
              {status.priorities.map((item) => (
                <li key={item.priority}>
                  {PRIORITY_LABEL[item.priority] ?? item.priority}: {item.state}
                  {item.state === "running"
                    ? ""
                    : ` (${reasonText(item.reason)})`}{" "}
                  · up to {item.share_percent}% of the budget
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h4 className="text-label font-medium text-foreground">
              Activity this month
            </h4>
            {status.by_capability.length === 0 ? (
              <p className="mt-1">No background work has run this month.</p>
            ) : (
              <div className="mt-1 overflow-x-auto">
                <table className="w-full text-left">
                  <caption className="sr-only">
                    Background work this month by capability
                  </caption>
                  <thead>
                    <tr>
                      {["Work", "Runs", "Model calls", "In progress"].map(
                        (title) => (
                          <th
                            key={title}
                            scope="col"
                            className="py-1 pr-3 font-medium"
                          >
                            {title}
                          </th>
                        )
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {status.by_capability.map((row) => (
                      <tr key={`${row.lane}:${row.priority}:${row.capability}`}>
                        <th scope="row" className="py-1 pr-3 font-normal">
                          {CAPABILITY_LABEL[row.capability] ?? row.capability}
                        </th>
                        <td className="py-1 pr-3 tabular-nums">
                          {count(row.admissions)}
                        </td>
                        <td className="py-1 pr-3 tabular-nums">
                          {count(row.model_calls_used)}
                        </td>
                        <td className="py-1 pr-3 tabular-nums">
                          {count(row.model_calls_held)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          {scope === "local" ? null : (
            <p>
              Explicit requests (Ask, a manual or fresh Review) are counted
              separately and never stop because this budget is used up.
            </p>
          )}
        </div>
      </details>
    </div>
  );
}

export function IntelligenceOverview({
  scope,
  route,
  background,
  lifetime,
  budgetEditSource,
  usageHref,
}: {
  scope: IntelligenceScope;
  route: IntelligenceRouteProjection;
  background: BackgroundBudgetStatus | null;
  lifetime: string;
  budgetEditSource: string;
  usageHref?: string;
}) {
  const locus = route.data_locus;
  return (
    <section
      className="space-y-4"
      aria-labelledby="intelligence-overview-title"
      data-testid="intelligence-overview"
    >
      <div>
        <h2
          id="intelligence-overview-title"
          className="text-title font-semibold text-foreground"
        >
          AI route and background intelligence
        </h2>
        <p className="mt-1 max-w-2xl text-caption leading-5 text-muted-foreground">
          {lifetime}
        </p>
      </div>
      <dl className="divide-y divide-border border-y border-border">
        <Row
          label="AI route"
          value={routeLabel(route)}
          detail={
            route.status === "ready" ? undefined : route.reason || undefined
          }
        />
        <Row label="Model" value={modelLabel(route)} />
        <Row
          label="Runs on"
          value={runOnLabel(scope, route.run_on.effective_value)}
          detail={
            route.run_on.offered_here
              ? undefined
              : "Not offered here: automatic work is refused, never moved elsewhere."
          }
        />
        <Row
          label="Background intelligence"
          value={route.background_enabled ? "On" : "Off"}
        />
      </dl>
      <BackgroundBudgetPanel
        status={background}
        routeStatus={route.status}
        scope={scope}
        editSource={budgetEditSource}
        usageHref={usageHref}
      />
      <p
        className="max-w-2xl text-caption leading-5 text-muted-foreground"
        data-testid="intelligence-edit-source"
      >
        {scope === "local" && route.editable
          ? "Update this machine's AI preferences above. Provider credentials stay in your environment."
          : `Read-only here. These values come from ${route.edit_source}.`}
      </p>
      <details className="rounded-md border border-border/80 px-3 py-2 text-caption">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
          Advanced route details
        </summary>
        <dl className="mt-2 divide-y divide-border">
          <Row
            label="Exact route"
            value={
              route.resolved.driver
                ? `${driverLabel(route.resolved.driver)} · ${route.resolved.model || "driver default model"}`
                : "None"
            }
            detail={
              route.resolved.model_source === "unreported_default"
                ? "The driver's own default model; LemonCrow does not guess its name."
                : route.status_note
            }
          />
          <Row
            label="Requested"
            value={`${route.requested.driver} · ${route.requested.model}`}
            detail={`From ${route.requested.driver_origin.layer.replaceAll("_", " ")}`}
          />
          <Row
            label="Data locus"
            value={
              locus
                ? `${locus.trust.replaceAll("_", " ")} · ${locus.provider}`
                : "Not reported"
            }
            detail={
              locus
                ? `Region ${locus.data_region} · training ${locus.training} · retention ${locus.retention_days} days`
                : undefined
            }
          />
          <Row label="Background work" value={route.background_note} />
          {route.policy.errors.length > 0 ? (
            <Row label="Policy errors" value={route.policy.errors.join("; ")} />
          ) : null}
          {route.settings && route.settings.length > 0 ? (
            <Row
              label="Settings keys"
              value={
                <span className="font-mono text-micro">
                  {route.settings.join(", ")}
                </span>
              }
            />
          ) : null}
        </dl>
      </details>
    </section>
  );
}
