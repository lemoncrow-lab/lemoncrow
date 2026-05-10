const BASE = "/api";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok)
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new ApiError(
      res.status,
      detail ? `${res.status} ${detail}` : `${res.status} ${res.statusText}`
    );
  }
  return res.json();
  }

  async function getText(path: string): Promise<string> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok)
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  return res.text();
  }

  export interface OverviewStats {
  total_traces: number;
  total_blocks: number;
  total_rubrics: number;
  total_clusters: number;
  total_raw_tokens_estimate: number;
  total_saved_tokens_estimate: number;
  total_compressed_tokens_estimate: number;
  average_compression_ratio: number;
  estimated_total_cost_usd: number;
  estimated_saved_cost_usd: number;
  usd_per_1k_tokens: number;
  is_estimate: boolean;
}

export interface PlanRecord {
  trace_id: string;
  domain: string;
  task: string;
  status: string;
  plan_checks: { name: string; passed: boolean; detail?: string }[];
}

export interface ToolCall {
  name: string;
  args_hash: string;
  count: number;
  args?: Record<string, unknown>;
  result_summary?: string;
}

export interface CommandRecord {
  command: string;
  exit_code?: number | null;
  stdout?: string;
  stderr?: string;
}

export interface FileEditRecord {
  path: string;
  diff?: string;
  event?: string;
}

export interface RepeatedFailure {
  signature: string;
  count: number;
}

export interface ValidationResult {
  name: string;
  passed: boolean;
  detail?: string;
}

export interface Trace {
  id: string;
  run_id?: string;
  agent: string;
  host?: string;
  domain?: string;
  task: string;
  status: string;
  files_touched: (string | FileEditRecord)[];
  tools_called: ToolCall[];
  commands_run: (string | CommandRecord)[];
  errors_seen: string[];
  repeated_failures: RepeatedFailure[];
  diff_summary?: string;
  output_summary?: string;
  validation_results: ValidationResult[];
  reasoning?: string[];
  created_at: string;
  note?: string;
  conversations?: ConversationEntry[];
  trace?: NestedTrace;
  raw_artifact_ids?: string[];
  _live?: boolean; // true for RunLedger sessions not yet committed to SQLite

  // Metrics
  input_tokens?: number;
  output_tokens?: number;
  thinking_tokens?: number;
  cached_input_tokens?: number;
  cache_creation_input_tokens?: number;
}

export interface ConversationEntry {
  kind: string;
  at: string;
  summary: string;
  content?: string;
  tokens?: Record<string, number>;
  raw?: any;
  cost?: number;
}

export interface NestedTrace {
  id: string;
  run_id: string;
  agent: string;
  domain: string;
  task: string;
  status: string;
  files_touched: string[];
  tools_called: { name: string; args_hash: string; count: number }[];
  commands_run: string[];
  errors_seen: string[];
  repeated_failures: string[];
  diff_summary: string;
  output_summary: string;
  validation_results: any[];
  raw_artifact_ids: string[];
  created_at: string;
}

export interface ReasonBlock {
  id: string;
  domain: string;
  title: string;
  status: string;
  // content fields
  situation: string;
  procedure: string[];
  dead_ends: string[];
  verification: string[];
  failure_signals: string[];
  required_rubrics: string[];
  when_not_to_apply: string;
  // match hints
  task_types: string[];
  triggers: string[];
  file_patterns: string[];
  tool_patterns: string[];
  // stats
  usage_count: number;
  success_count: number;
  failure_count: number;
  created_at: string;
  updated_at?: string;
}

export interface Cluster {
  id: string;
  domain: string;
  fingerprint: string;
  trace_ids: string[];
  sample_errors: string[];
  suggested_block_title: string;
  suggested_rubric_check: string;
  suggested_eval_case: string;
  suggested_prompt: string;
  severity: string;
}

export interface SavingsPerOp {
  op_key: string;
  domain?: string;
  task_sample?: string;
  baseline_cost_usd: number;
  last_cost_usd: number;
  current_cost_usd: number;
  delta_vs_last_usd: number;
  delta_vs_base_usd: number;
  pct_vs_base: number;
  calls_count: number;
}

export interface SavingsSummary {
  operations_tracked: number;
  total_calls: number;
  would_have_cost_usd: number;
  actually_cost_usd: number;
  saved_usd: number;
  saved_pct: number;
  per_operation: SavingsPerOp[];
}

export interface SavingsByDay {
  day: string;
  naive: number;
  actual: number;
}

export interface SavingsSource {
  lever: string;
  tool_name: string;
  calls_saved: number;
  tokens_saved: number;
  cost_saved_usd: number;
  time_saved_ms: number;
}

export interface SavingsToolAggregate {
  tool_name: string;
  lever: string;
  turns: number;
  session_count: number;
  actual_tokens: number;
  naive_tokens: number;
  saved_tokens: number;
  actual_cost_usd: number;
  baseline_cost_usd: number;
  saved_cost_usd: number;
  live_calls_saved: number;
  live_time_saved_ms: number;
  live_saved_usd: number;
}

export interface SavingsProofItem {
  run_id: string;
  turn_index: number;
  tool_name: string;
  lever: string;
  model: string;
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  actual_tokens: number;
  naive_tokens: number;
  saved_tokens: number;
  actual_cost_usd: number;
  baseline_cost_usd: number;
  saved_cost_usd: number;
  lever_savings: Record<string, number>;
  created_at: string;
  source: string;
}

export interface SavingsProofSession {
  run_id: string;
  trace_id?: string | null;
  agent: string;
  task: string;
  status: string;
  trace_confidence?: string | null;
  capture_sources?: string[];
  missing_surfaces?: string[];
  created_at: string;
  tracked_tool_calls: number;
  actual_tokens: number;
  naive_tokens: number;
  saved_tokens: number;
  actual_cost_usd: number;
  baseline_cost_usd: number;
  saved_cost_usd: number;
  live_calls_saved: number;
  live_time_saved_ms: number;
  live_saved_usd: number;
  items: SavingsProofItem[];
  has_ledger: boolean;
  note?: string;
}

export interface SavingsCoverageGap {
  run_id: string;
  trace_id?: string | null;
  agent: string;
  task: string;
  status: string;
  trace_confidence?: string | null;
  created_at: string;
  reason: string;
  missing_surfaces?: string[];
}

export interface SavingsVerificationRun {
  run_id: string;
  agent?: string;
  task?: string;
  saved_tokens: number;
  saved_cost_usd: number;
}

export interface SavingsVerificationItem {
  run_id: string;
  turn_index: number;
  tool_name: string;
  lever: string;
  actual_tokens: number;
  naive_tokens: number;
  saved_tokens: number;
  created_at: string;
}

export interface SavingsVerificationSummary {
  data_root: string;
  headline_kind: string;
  headline_explanation: string;
  tracked_row_count: number;
  tracked_run_count: number;
  trace_linked_run_count: number;
  ledger_backed_run_count: number;
  live_event_count: number;
  coverage_gap_count: number;
  compact_output_row_count?: number;
  compact_output_saved_tokens?: number;
  dominant_run?: SavingsVerificationRun | null;
  dominant_item?: SavingsVerificationItem | null;
  dominant_run_share_pct: number;
  dominant_item_share_pct: number;
  warning?: string | null;
}

export interface SavingsBenchmark {
  run_id: string;
  model: string;
  n_prompts: number;
  total_tokens_baseline: number;
  total_tokens_atelier: number;
  tokens_saved: number;
  reduction_pct: number;
  total_cost_baseline_usd: number;
  total_cost_atelier_usd: number;
  cost_saved_usd: number;
  total_time_baseline_ms: number;
  total_time_atelier_ms: number;
  time_saved_ms: number;
  baseline_success_rate: number;
  atelier_success_rate: number;
}

export interface SavingsSummaryV2 {
  window_days: number;
  total_naive_tokens: number;
  total_actual_tokens: number;
  reduction_pct: number;
  per_lever: Record<string, number>;
  by_day: SavingsByDay[];
  saved_usd?: number;
  saved_pct?: number;
  would_have_cost_usd?: number;
  actually_cost_usd?: number;
  tracked_actual_cost_usd?: number;
  tracked_baseline_cost_usd?: number;
  tracked_saved_cost_usd?: number;
  cost_basis?: string;
  unpriced_cache_write_tokens?: number;
  total_calls?: number;
  tracked_tool_calls?: number;
  live_calls_saved?: number;
  live_time_saved_ms?: number;
  live_saved_usd?: number;
  top_sources?: SavingsSource[];
  tool_aggregates?: SavingsToolAggregate[];
  session_proof?: SavingsProofSession[];
  coverage_gaps?: SavingsCoverageGap[];
  verification?: SavingsVerificationSummary;
  latest_benchmark?: SavingsBenchmark | null;
}

export interface CallEntry {
  run_id: string;
  domain?: string;
  task?: string;
  operation: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
  lessons_used: string[];
  op_key: string;
  at: string;
}

export interface Rubric {
  id: string;
  domain: string;
  triggers: string[];
  forbidden_phrases: string[];
  required_checks: string[];
  block_if_missing: string[];
  warning_checks: string[];
  escalation_conditions: string[];
  related_blocks: string[];
  created_at: string;
  updated_at?: string;
}

export interface MCPStatus {
  tool_name: string;
  available: boolean;
  description?: string;
}

export interface HostAdapter {
  host_id: string;
  label: string;
  status: string;
  active_domains: string[];
  mcp_tools: string[];
  last_seen?: string | null;
  atelier_version?: string | null;
  description?: string | null;
  install_command?: string | null;
}

export interface Skill {
  name: string;
  description: string;
  content: string;
}

export interface MemoryBlock {
  id: string;
  agent_id: string;
  label: string;
  value: string;
  limit_chars: number;
  description: string;
  read_only: boolean;
  metadata: Record<string, unknown>;
  pinned: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface MemoryUpsertBlockResult {
  id: string;
  version: number;
}

export interface MemoryPassage {
  id: string;
  agent_id: string;
  text: string;
  source: string;
  source_ref: string;
  tags: string[];
  dedup_hit?: boolean;
  created_at: string;
}

export interface MemoryRecallPassage {
  id: string;
  agent_id?: string;
  text: string;
  source_ref: string;
  tags: string[];
  source?: string;
  created_at?: string;
}

export interface MemoryRecallResult {
  passages: MemoryRecallPassage[];
  recall_id: string;
}

export interface RunInspectorData {
  run_id: string;
  pinned_blocks: string[];
  recalled_passages: Array<{ id: string; source_ref: string }>;
  summarized_events_count: number;
  tokens_pre: number | null;
  tokens_post: number | null;
  conversations?: Array<{
    kind: string;
    at?: string;
    summary: string;
    content: string;
    tokens?: Record<string, number>;
    raw?: any;
    cost?: number;
  }>;
}

export interface WatchdogLibraryEntry {
  key: string;
  title: string;
  description: string;
  default_weight: number;
  severity: "high" | "medium" | "low";
}

export interface WatchdogProfile {
  id: string;
  label: string;
  description: string;
  weights: Record<string, number>;
}

export interface WatchdogConfig {
  active_profile: string;
  profiles: WatchdogProfile[];
  library: WatchdogLibraryEntry[];
  runtime_wired: boolean;
  config_path: string;
}

export interface GranularToolUsage {
  agent: string;
  event_type: string;
  tool_name: string;
  sub_command: string | null;
  category: string;
  input_tokens: number;
  user_prompt_tokens?: number;
  output_tokens: number;
  created_at?: string;
  first_seen?: string;
  last_seen?: string;
  call_count?: number;
  session_count?: number;
  model?: string;
  cost?: number;
}

export interface TelemetryConfigResponse {
  remote_enabled: boolean;
  lexical_frustration_enabled: boolean;
  dev_mode: boolean;
}

export interface TelemetryLocalEvent {
  id: number;
  ts: number;
  event: string;
  session_id: string | null;
  props: Record<string, unknown>;
  exported: boolean;
}

export interface TelemetryLocalResponse {
  events: TelemetryLocalEvent[];
}

export interface TelemetrySummary {
  events_total: number;
  event_counts: Record<string, number>;
  commands_by_day: { day: string; count: number }[];
  top_commands: { name: string; count: number }[];
  agent_hosts: { name: string; count: number }[];
  top_reasonblocks: { block_id_hash: string; count: number; domain: string }[];
  retrieval_score_distribution: { name: string; count: number }[];
  plan_checks: Record<string, number>;
  frustration_behavioral: { name: string; count: number }[];
  frustration_lexical: { name: string; count: number }[];
  value_estimate: {
    tokens_saved_estimate: number;
    cache_hits: number;
    blocks_applied: number;
  };
}

export interface HealthResponse {
  status: string;
  timestamp: string;
}

export interface RawArtifact {
  id: string;
  source: string;
  created_at: string;
  [key: string]: unknown;
}

export interface TraceListResponse {
  items: Trace[];
  metrics: {
    stats: {
      total: number;
      success: number;
      failed: number;
      partial: number;
    };
    hosts: string[];
    domains: string[];
  };
}

export const api = {
  overview: () => get<OverviewStats>("/overview"),
  granularAnalytics: (
    agent?: string,
    category?: string,
    limit = 5000,
    days?: number
  ) => {
    const params = new URLSearchParams();
    if (agent) params.set("agent", agent);
    if (category) params.set("category", category);
    if (days) params.set("days", String(days));
    params.set("limit", String(limit));
    params.set("grouped", "true");
    return get<GranularToolUsage[]>(`/analytics?${params.toString()}`);
  },
  pricing: () =>
    get<Record<string, { input: number; output: number; cache_read: number }>>(
      "/pricing"
    ),
  plans: (limit = 50) => get<PlanRecord[]>(`/plans?limit=${limit}`),
  traces: (limit = 50, offset = 0, domain?: string, agent?: string) => {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    if (domain && domain !== "all") params.set("domain", domain);
    if (agent && agent !== "all") params.set("agent", agent);
    return get<TraceListResponse>(`/traces?${params.toString()}`);
  },
  trace: (id: string) => get<Trace>(`/v1/traces/${id}`),
  ledger: (run_id: string) => get<any>(`/ledgers/${run_id}`),
  clusters: () => get<Cluster[]>("/clusters"),
  blocks: () => get<ReasonBlock[]>("/blocks"),
  block: (id: string) => get<ReasonBlock>(`/blocks/${id}`),
  savings: () => get<SavingsSummary>("/savings"),
  savingsSummary: (windowDays = 14) =>
    get<SavingsSummaryV2>(`/v1/savings/summary?window_days=${windowDays}`),
  calls: (limit = 200) => get<CallEntry[]>(`/calls?limit=${limit}`),
  rubrics: () => get<Rubric[]>("/v1/rubrics"),
  rubric: (id: string) => get<Rubric>(`/v1/rubrics/${id}`),
  mcp_status: () => get<MCPStatus[]>("/mcp/status"),
  hosts: () => get<HostAdapter[]>("/hosts"),
  watchdogConfig: () => get<WatchdogConfig>("/watchdogs/config"),
  updateWatchdogConfig: (payload: {
    active_profile?: string;
    profiles?: Record<string, Record<string, number>>;
  }) => post<WatchdogConfig>("/watchdogs/config", payload),
  skills: () => get<Skill[]>("/skills"),
  skill: (name: string) => get<Skill>(`/skills/${name}`),
  memoryBlocks: (agentId?: string, label?: string) => {
    const params = new URLSearchParams();
    if (agentId) params.set("agent_id", agentId);
    if (label) params.set("label", label);
    const suffix = params.size ? `?${params.toString()}` : "";
    return get<MemoryBlock[] | MemoryBlock>(`/v1/memory/blocks${suffix}`);
  },
  memoryPassages: (agentId: string, limit = 25) => {
    const params = new URLSearchParams();
    params.set("agent_id", agentId);
    params.set("limit", String(limit));
    return get<MemoryPassage[]>(`/v1/memory/passages?${params.toString()}`);
  },
  memoryUpsertBlock: (payload: {
    agent_id: string;
    label: string;
    value: string;
    expected_version: number;
    pinned: boolean;
    description?: string;
    read_only?: boolean;
    limit_chars?: number;
    actor?: string;
  }) => post<MemoryUpsertBlockResult>("/v1/memory/blocks", payload),
  memoryRecall: (payload: {
    agent_id: string;
    query: string;
    top_k?: number;
    tags?: string[];
    since?: string;
  }) => post<MemoryRecallResult>("/v1/memory/recall", payload),
  health: () => get<HealthResponse>("/health"),
  config: () => get<Record<string, unknown>>("/config"),
  recordTrace: (payload: Partial<Trace>) =>
    post<{ id: string }>("/v1/traces", payload),
  telemetryConfig: () => get<TelemetryConfigResponse>("/telemetry/config"),
  updateTelemetryConfig: (payload: {
    remote_enabled?: boolean;
    lexical_frustration_enabled?: boolean;
  }) => post<TelemetryConfigResponse>("/telemetry/config", payload),
  telemetryAck: () =>
    post<TelemetryConfigResponse>("/telemetry/ack", {}),
  telemetryLocal: (limit = 100) =>
    get<TelemetryLocalResponse>(`/telemetry/local?limit=${limit}`),
  postTelemetryLocal: (event: string, props: Record<string, unknown>) =>
    post<{ ok: boolean }>("/telemetry/local", { event, props }),
  telemetrySummary: () => get<TelemetrySummary>("/telemetry/summary"),
  telemetrySchema: () => get<Record<string, unknown>>("/telemetry/schema"),
  rawArtifact: (artifactId: string) =>
    get<RawArtifact>(`/raw-artifacts/${artifactId}`),
  rawArtifactContent: (artifactId: string) =>
    getText(`/raw-artifacts/${artifactId}/content`),
  };
