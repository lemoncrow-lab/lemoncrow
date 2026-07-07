const BASE = "/api";
const TELEMETRY_ACK_STORAGE_KEY = "atelier.telemetry.acknowledged";

export interface TelemetryConfig {
  remote_enabled: boolean;
  lexical_frustration_enabled: boolean;
  posthog_key: string;
  posthog_host: string;
  anon_id: string;
  acknowledged: boolean;
  service_version: string;
  dev_mode: boolean;
}

export interface TelemetryEvent {
  id: number;
  ts: number;
  event: string;
  session_id?: string | null;
  props: Record<string, unknown>;
  exported: boolean;
}

export interface TelemetrySummary {
  events_total: number;
  unique_event_types: number;
  active_sessions: number;
  first_event_ts: number | null;
  last_event_ts: number | null;
  event_counts: Record<string, number>;
  commands_by_day: Array<{ day: string; count: number }>;
  top_commands: Array<{ name: string; count: number }>;
  agent_hosts: Array<{ name: string; count: number }>;
  top_playbooks: Array<{
    block_id_hash: string;
    count: number;
    domain: string;
  }>;
  retrieval_score_distribution: Array<{ name: string; count: number }>;
  plan_checks: Record<string, number>;
  frustration_behavioral: Array<{ name: string; count: number }>;
  frustration_lexical: Array<{ name: string; count: number }>;
  value_estimate: {
    tokens_saved_estimate: number;
    cache_hits: number;
    total_tool_calls: number;
    cache_hit_rate: number;
    blocks_applied: number;
  };
}

export interface TelemetrySchema {
  events: Record<string, { props: string[]; example: Record<string, unknown> }>;
  buckets: Record<string, string[]>;
}

export interface TelemetryQuery {
  limit?: number;
  since?: number;
  event?: string;
  host?: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

export function getTelemetryConfig(): Promise<TelemetryConfig> {
  return request<TelemetryConfig>("/telemetry/config").then((config) => ({
    ...config,
    acknowledged: config.acknowledged || hasLocalTelemetryAcknowledgement(),
  }));
}

export function updateTelemetryConfig(payload: {
  lexical_frustration_enabled?: boolean;
}): Promise<TelemetryConfig> {
  return request<TelemetryConfig>("/telemetry/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function acknowledgeTelemetry(): Promise<TelemetryConfig> {
  markLocalTelemetryAcknowledged();
  return request<TelemetryConfig>("/telemetry/ack", { method: "POST" }).then(
    (config) => ({
      ...config,
      acknowledged: true,
    })
  );
}

export function hasLocalTelemetryAcknowledgement(): boolean {
  try {
    return globalThis.localStorage?.getItem(TELEMETRY_ACK_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function markLocalTelemetryAcknowledged(): void {
  try {
    globalThis.localStorage?.setItem(TELEMETRY_ACK_STORAGE_KEY, "1");
  } catch {
    // Storage can be disabled in private contexts; the server ack remains.
  }
}

function buildTelemetryQuery(params: TelemetryQuery = {}): string {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.since !== undefined) query.set("since", String(params.since));
  if (params.event) query.set("event", params.event);
  if (params.host) query.set("host", params.host);
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export function getTelemetryEvents(
  query: TelemetryQuery = {}
): Promise<{ events: TelemetryEvent[] }> {
  return request<{ events: TelemetryEvent[] }>(
    `/telemetry/local${buildTelemetryQuery(query)}`
  );
}

export function getTelemetrySummary(
  query: Omit<TelemetryQuery, "limit"> = {}
): Promise<TelemetrySummary> {
  return request<TelemetrySummary>(
    `/telemetry/summary${buildTelemetryQuery(query)}`
  );
}

export function getTelemetrySchema(): Promise<TelemetrySchema> {
  return request<TelemetrySchema>("/telemetry/schema");
}

export function postLocalTelemetryEvent(
  event: string,
  props: Record<string, unknown>
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/telemetry/local", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event, props }),
  });
}
