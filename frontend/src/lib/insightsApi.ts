const BASE = "/api";

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
  event_counts: Record<string, number>;
  commands_by_day: Array<{ day: string; count: number }>;
  top_commands: Array<{ name: string; count: number }>;
  agent_hosts: Array<{ name: string; count: number }>;
  top_reasonblocks: Array<{
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
    blocks_applied: number;
  };
}

export interface TelemetrySchema {
  events: Record<string, { props: string[]; example: Record<string, unknown> }>;
  buckets: Record<string, string[]>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

export function getTelemetryConfig(): Promise<TelemetryConfig> {
  return request<TelemetryConfig>("/telemetry/config");
}

export function updateTelemetryConfig(payload: {
  remote_enabled?: boolean;
  lexical_frustration_enabled?: boolean;
}): Promise<TelemetryConfig> {
  return request<TelemetryConfig>("/telemetry/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function acknowledgeTelemetry(): Promise<TelemetryConfig> {
  return request<TelemetryConfig>("/telemetry/ack", { method: "POST" });
}

export function getTelemetryEvents(
  limit = 100
): Promise<{ events: TelemetryEvent[] }> {
  return request<{ events: TelemetryEvent[] }>(
    `/telemetry/local?limit=${limit}`
  );
}

export function getTelemetrySummary(): Promise<TelemetrySummary> {
  return request<TelemetrySummary>("/telemetry/summary");
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
