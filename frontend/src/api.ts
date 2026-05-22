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
  session_id?: string;
  agent: string;
  model?: string;
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
  snippets?: string[];
  workspace_path?: string;

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
  model?: string;
  tokens?: Record<string, number>;
  raw?: any;
  cost?: number;
  count?: number;
  path?: string;
  diff?: string;
  tool_name?: string;
  arguments?: unknown;
  todos?: Array<{
    content: string;
    status?: string;
    priority?: string;
    id?: string;
  }>;
  attachments?: SessionAttachment[];
  subagent_id?: string;
  subagent_name?: string;
  subagent_status?: string;
  subagent_description?: string;
  artifact_id?: string;
  artifact_source?: string;
  artifact_kind?: string;
  artifact_label?: string;
  source_scope?: string;
}

export interface SessionAttachment {
  type: string;
  path?: string;
  display_name?: string;
  title?: string;
  content?: string;
  size_label?: string;
  line_count?: number;
  mime_type?: string;
  metadata?: Record<string, unknown>;
}

export interface SessionArtifact {
  id: string;
  source: string;
  kind: string;
  relative_path: string;
  label?: string;
  source_path?: string | null;
  scope: string;
}

export interface NestedTrace {
  id: string;
  session_id: string;
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
  session_id: string;
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
  session_id: string;
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
  session_id: string;
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
  session_id: string;
  agent?: string;
  task?: string;
  saved_tokens: number;
  saved_cost_usd: number;
}

export interface SavingsVerificationItem {
  session_id: string;
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
  session_id: string;
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

export interface OptimizationRule {
  id: string;
  title: string;
  severity: string;
  trigger: string;
  action: string;
}

export interface OptimizationHostCoverage {
  host: string;
  mode: string;
  automatic_at_start: boolean;
  automatic_mid_session: boolean;
  advisory_only: boolean;
  surfaces: string[];
  notes: string;
}

export interface OptimizationLever {
  id: string;
  title: string;
  category: string;
  automation: string;
  status: string;
  observed_tokens_saved: number;
  applies_to: string[];
  notes: string;
  examples: string[];
}

export interface OptimizationGap {
  id: string;
  priority: string;
  title: string;
  hosts: string[];
  notes: string;
}

export interface OptimizationAdvisorRoutingPolicy {
  policy: string;
  simple: string;
  medium: string;
  hard: string;
  escalate_on: string[];
}

export interface OptimizationAdvisorCompactionPolicy {
  prompt_cache_reorder: boolean;
  dedup: boolean;
  retrieval_filter: boolean;
  lossy_summary: boolean;
  trigger_at_context_fraction: number;
  preserve: string[];
}

export interface OptimizationAdvisorPolicy {
  name: string;
  preset: string;
  quality_floor: number;
  confidence_required: string;
  routing: OptimizationAdvisorRoutingPolicy;
  compaction: OptimizationAdvisorCompactionPolicy;
}

export interface OptimizationAdvisorCandidate {
  id: string;
  policy: OptimizationAdvisorPolicy;
  weekly_cost_usd: number;
  estimated_quality: number;
  latency_mult: number;
  escalation_rate: number;
  compaction_breakdown: Record<string, number>;
  routing_breakdown: Record<string, number>;
}

export interface OptimizationAdvisorGolden {
  total: number;
  passed: number;
  score: number;
  failures: string[];
}

export interface OptimizationAdvisor {
  current_policy: OptimizationAdvisorPolicy;
  recommended_policy: OptimizationAdvisorPolicy;
  candidates: OptimizationAdvisorCandidate[];
  current_candidate_id: string;
  recommended_candidate_id: string | null;
  confidence: string;
  confidence_reason: string;
  sessions_analysed: number;
  replayable_tasks: number;
  weekly_savings_usd: number;
  quality_delta: number;
  baseline_weekly_cost_usd: number;
  has_recommendation: boolean;
  message: string;
  bucket_counts: Record<string, number>;
  golden: OptimizationAdvisorGolden;
}

export interface OptimizationAdvisorHistoryEntry extends OptimizationAdvisor {
  recorded_at: string;
}

export interface OptimizationRecommendationSession {
  trace_id: string;
  host?: string;
  project?: string;
  cost_usd?: number;
  peer_average_usd?: number;
  multiple?: number;
  effective_input_tokens?: number;
  output_tokens?: number;
  input_output_ratio?: number;
  previous_input_multiple?: number | null;
  tools?: string[];
  reason?: string;
}

export interface OptimizationRecommendation {
  id: string;
  title: string;
  severity: string;
  action: string;
  session_count: number;
  estimated_tokens_saved: number;
  estimated_usd_saved: number;
  sessions: OptimizationRecommendationSession[];
}

export interface OptimizationContextAuditComponent {
  id: string;
  title: string;
  category: string;
  mode: string;
  estimated_tokens: number;
  file_count: number;
  optimizable: boolean;
  notes: string;
}

export interface OptimizationContextAudit {
  generated_at: string;
  audited_tokens_total: number;
  always_on_tokens: number;
  optimizable_tokens: number;
  component_count: number;
  components: OptimizationContextAuditComponent[];
  recommendations: string[];
}

export interface OptimizationQualitySignal {
  id: string;
  title: string;
  weight_pct: number;
  score: number;
  detail: string;
}

export interface OptimizationQualitySummary {
  generated_at: string;
  trace_count: number;
  score: number;
  grade: string;
  dominant_model: string | null;
  dominant_context_window_tokens: number;
  signals: OptimizationQualitySignal[];
  recommendations: string[];
  risk_flags: string[];
}

export interface OptimizationAutoOptimization {
  id: string;
  title: string;
  tokens_saved: number;
  cost_saved_usd: number;
  calls_saved: number;
  session_count: number;
  tools: string[];
}

export interface OptimizationImpactWindow {
  trace_count: number;
  avg_tokens: number;
  avg_cost_usd: number;
  avg_cache_leverage: number;
  avg_saved_tokens: number;
  tracked_turns: number;
  from: string | null;
  to: string | null;
}

export interface OptimizationImpactValidation {
  generated_at: string;
  window_days: number;
  strategy: string;
  verdict: string;
  before: OptimizationImpactWindow;
  after: OptimizationImpactWindow;
  deltas: {
    tokens_pct: number;
    cost_pct: number;
    cache_leverage_pct: number;
    saved_tokens_pct: number;
  };
  notes: string[];
}

export interface OptimizationRereadKind {
  id: string;
  title: string;
  event_count: number;
  tokens_saved: number;
  cost_saved_usd: number;
  path_count: number;
  last_seen_at: string | null;
}

export interface OptimizationRereadPath {
  path: string;
  event_count: number;
  tokens_saved: number;
  kinds: string[];
}

export interface OptimizationRereadTelemetry {
  generated_at: string;
  window_days: number;
  event_count: number;
  total_tokens_saved: number;
  total_cost_saved_usd: number;
  kinds: OptimizationRereadKind[];
  top_paths: OptimizationRereadPath[];
}

export interface OptimizationRoutingCandidate {
  trace_id: string;
  task: string;
  current_model: string;
  target_model: string;
  current_cost_usd: number;
  simulated_cost_usd: number;
  estimated_cost_saved_usd: number;
  total_tokens: number;
  reason: string;
}

export interface OptimizationModelRoutingSimulation {
  generated_at: string;
  window_days: number;
  candidate_count: number;
  estimated_cost_saved_usd: number;
  current_cost_usd: number;
  simulated_cost_usd: number;
  total_tokens_rerouted: number;
  heuristic: string;
  candidates: OptimizationRoutingCandidate[];
  live_recommendations: OptimizationLiveModelRecommendation[];
}

export interface OptimizationLiveModelRecommendation {
  at: string;
  session_id: string;
  agent: string;
  tool_name: string;
  tier: string;
  model: string;
  score: number;
  cache_affinity_model: string | null;
  reasons: string[];
}

export interface OptimizationsSummary {
  generated_at: string;
  window_days: number;
  automatic_hosts: number;
  advisory_only_hosts: number;
  observed_levers: number;
  runtime_coverage: OptimizationHostCoverage[];
  budget_guidance: string;
  budget_rules: OptimizationRule[];
  implemented_levers: OptimizationLever[];
  implementation_gaps: OptimizationGap[];
  advisor: OptimizationAdvisor;
  advisor_history: OptimizationAdvisorHistoryEntry[];
  recommendations: {
    window_days: number;
    host: string | null;
    hosts_supported: string[];
    trace_count: number;
    recommendations: OptimizationRecommendation[];
    estimated_tokens_saved: number;
    estimated_usd_saved: number;
    guidance: string;
  };
  context_audit: OptimizationContextAudit;
  quality_score: OptimizationQualitySummary;
  auto_optimizations: OptimizationAutoOptimization[];
  impact_validation: OptimizationImpactValidation;
  reread_telemetry: OptimizationRereadTelemetry;
  model_routing_simulation: OptimizationModelRoutingSimulation;
  external_optimizations: {
    id: string;
    tool: string;
    period: string;
    source: string;
    ok: boolean;
    summary: any;
    payload: {
      kind: string;
      overview: {
        sessions: number;
        calls: number;
        cost: number;
        health_grade: string;
        health_score: number;
        issue_count: number;
        estimated_tokens_saved: number;
        estimated_usd_saved: number;
      };
      recommendations: Array<{
        title: string;
        severity: "high" | "medium" | "low";
        description: string;
        estimated_tokens_saved: number;
        estimated_usd_saved: number;
        action: string;
      }>;
    };
    stdout: string;
    collected_at: string;
  } | null;
  savings: SavingsSummaryV2;
  data_sources: Array<{
    id: string;
    label: string;
    detail: string;
  }>;
}

export interface CallEntry {
  session_id: string;
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
  is_dev?: boolean;
  mode?: "active" | "passive";
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

export interface Agent {
  id: string;
  name: string;
  description: string;
  tools: string[];
  color: string;
  model?: string | null;
  file: string;
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
  session_id: string;
  pinned_blocks: string[];
  recalled_passages: Array<{ id: string; source_ref: string }>;
  summarized_events_count: number;
  tokens_pre: number | null;
  tokens_post: number | null;
  source_paths?: string[];
  source_files?: Array<{ path: string; artifact_id?: string }>;
  artifacts?: SessionArtifact[];
  conversations?: ConversationEntry[];
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

// -------------------------------------------------------------------------
// Week-2 types (Spec 06)
// -------------------------------------------------------------------------

export interface SessionSummary {
  session_id: string;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number;
  active_duration_seconds: number;
  vendor: string;
  started_model?: string | null;
  cost_status?: "recorded" | "estimated" | "unavailable";
  agent_settings?: Record<string, any>;
  skills?: string[];
  telemetry?: Record<string, any>;
  raw_artifact_ids?: string[];
  total_turns: number;
  total_cost_usd: number;
  total_atelier_savings_usd: number;
  label: string | null;
  models_used: Record<string, number>;
  input_tokens?: number;
  output_tokens?: number;
  cached_input_tokens?: number;
}

export interface TopTool {
  tool: string;
  calls: number;
  cost_usd: number;
}

export interface SessionReport extends SessionSummary {
  tool_call_count: number;
  input_token_cost_usd: number;
  cache_write_cost_usd: number;
  cache_read_cost_usd: number;
  output_token_cost_usd: number;
  input_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  output_tokens: number;
  routing_downtiered_turns: number;
  routing_savings_usd: number;
  compact_events: number;
  compact_savings_estimate_usd: number;
  top_tools_by_cost: TopTool[];
}

export interface MemoryFact {
  fact_id: string;
  vendor: "claude" | "codex" | "gemini";
  source_path: string;
  source_kind: string;
  content: string;
  line_number: number | null;
  captured_at: string;
  raw_meta: Record<string, unknown>;
}

export interface InsightsSessionSummary {
  session_id: string;
  cost_usd: number;
  label: string;
  duration_seconds: number;
}

export interface OutcomesSummary {
  route_decisions: number;
  route_avg_score: number;
  compact_events: number;
  compact_avg_score: number;
  sessions_with_high_extra_reads: string[];
}

export interface Opportunity {
  kind: string;
  message: string;
  estimated_savings_usd: number;
  sessions_affected: number;
}

export interface InsightsWindow {
  since: string;
  until: string;
  session_count: number;
  total_duration_seconds: number;
  total_cost_usd: number;
  total_atelier_savings_usd: number;
  cost_by_vendor: Record<string, number>;
  cost_by_tool: Record<string, number>;
  cost_by_model: Record<string, number>;
  top_sessions: InsightsSessionSummary[];
  outcomes_summary: OutcomesSummary;
  opportunities: Opportunity[];
}

export interface ReportMeta {
  week: string;
  week_start: string;
  generated_at: string;
  routing_sessions: number | null;
  total_routing_savings_usd: number | null;
  routing_quality_score: number | null;
  compact_retention_score: number | null;
}

export interface ReportContent {
  week: string;
  markdown: string;
  json: Record<string, unknown>;
}

export interface GranularToolUsage {
  agent: string;
  host?: string;
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

export interface DashboardDaily {
  date: string;
  sessions: number;
  cost: number;
  input_tokens: number;
  output_tokens: number;
}

export interface DashboardByDomain {
  domain: string;
  sessions: number;
  cost: number;
  avg_cost: number;
}

export interface DashboardByHost {
  host: string;
  sessions: number;
  cost: number;
  cache_pct: number;
  input_tokens: number;
  cached_tokens: number;
}

export interface DashboardByModel {
  model: string;
  sessions: number;
  cost: number;
  cache_pct: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
}

export interface DashboardHostModelOverview {
  host: string;
  model: string;
  sessions: number;
  user_typed_tokens: number;
  base_context_tokens: number;
  cached_prompt_tokens: number;
  cache_write_tokens: number;
  billable_output_tokens: number;
  tool_output_tokens: number;
  thinking_tokens: number;
  tool_calls: number;
  cost: number;
}

export interface DashboardTopSession {
  id: string;
  host: string;
  domain: string;
  model: string;
  date: string;
  cost: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
}

export interface DashboardTool {
  name: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
}

export interface ExternalAnalyticsMetric {
  key: string;
  label: string;
  value: number;
}

export interface ExternalAnalyticsSection {
  name: string;
  kind: string;
  count: number;
}

export interface ExternalAnalyticsSummary {
  tool: string;
  top_level_keys: string[];
  sections: ExternalAnalyticsSection[];
  highlights: ExternalAnalyticsMetric[];
}

export interface DashboardExternalLatest {
  id: string;
  tool: string;
  period: string;
  source: string;
  ok: boolean;
  returncode: number | null;
  summary: ExternalAnalyticsSummary;
  collected_at: string;
}

export interface DashboardExternalProvider {
  provider: string;
  providerDisplayName: string;
  models: number;
  calls: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  totalTokens: number;
  costUSD: number;
}

export interface DashboardExternalSummary {
  runs_total: number;
  successful_runs: number;
  failed_runs: number;
  latest: DashboardExternalLatest[];
  by_provider: DashboardExternalProvider[];
}

export interface ExternalAnalyticsRun {
  id: string;
  tool: string;
  period: string;
  source: string;
  command_display: string;
  ok: boolean;
  returncode: number | null;
  summary: ExternalAnalyticsSummary;
  payload: unknown;
  stdout: string;
  stderr: string;
  collected_at: string;
  created_at: string;
}

export interface ExternalAnalyticsResponse {
  totals: {
    runs_total: number;
    successful_runs: number;
    failed_runs: number;
  };
  latest_by_tool: Record<string, ExternalAnalyticsRun>;
  runs: ExternalAnalyticsRun[];
}

export interface AnalyticsDashboard {
  summary: {
    total_cost: number;
    projected_monthly_cost: number;
    total_sessions: number;
  };
  daily: DashboardDaily[];
  hourly: DashboardDaily[];
  by_domain: DashboardByDomain[];
  by_host: DashboardByHost[];
  by_model: DashboardByModel[];
  host_model_overview: DashboardHostModelOverview[];
  top_sessions: DashboardTopSession[];
  external: DashboardExternalSummary;
  tools: {
    core: DashboardTool[];
    shell: DashboardTool[];
    mcp: DashboardTool[];
  };
}

export interface AnalyticsSummary {
  total_cost: number;
  estimated_monthly_cost: number;
  top_cost_driver: string;
  user_input_tokens: number;
  model_thinking_tokens: number;
  llm_output_tokens: number;
  tool_output_tokens: number;
  cached_prompt_tokens: number;
  tool_calls: number;
  unique_tools: number;
  total_output_tokens: number;
  row_count: number;
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
    total_tool_calls: number;
    cache_hit_rate: number;
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
  overview: (days?: number) => {
    const params = new URLSearchParams();
    if (days) params.set("days", String(days));
    const suffix = params.size ? `?${params.toString()}` : "";
    return get<OverviewStats>(`/overview${suffix}`);
  },
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
  analyticsDashboard: (days = 30, host?: string) => {
    const params = new URLSearchParams();
    params.set("days", String(days));
    if (host) params.set("host", host);
    return get<AnalyticsDashboard>(`/analytics/dashboard?${params.toString()}`);
  },
  analyticsSummary: (
    agent?: string,
    model?: string,
    category?: string,
    search?: string,
    limit = 5000,
    days?: number
  ) => {
    const params = new URLSearchParams();
    if (agent) params.set("agent", agent);
    if (model) params.set("model", model);
    if (category) params.set("category", category);
    if (search) params.set("search", search);
    if (days) params.set("days", String(days));
    params.set("limit", String(limit));
    params.set("grouped", "true");
    return get<AnalyticsSummary>(`/analytics/summary?${params.toString()}`);
  },
  externalAnalytics: (days = 30, tool?: string, limit = 30) => {
    const params = new URLSearchParams();
    params.set("days", String(days));
    params.set("limit", String(limit));
    if (tool) params.set("tool", tool);
    return get<ExternalAnalyticsResponse>(
      `/analytics/external?${params.toString()}`
    );
  },
  pricing: () =>
    get<Record<string, { input: number; output: number; cache_read: number }>>(
      "/pricing"
    ),
  plans: (limit = 50) => get<PlanRecord[]>(`/plans?limit=${limit}`),
  traces: (
    limit = 50,
    offset = 0,
    domain?: string,
    host?: string,
    query?: string,
    days?: number
  ) => {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    if (domain && domain !== "all") params.set("domain", domain);
    if (host && host !== "all") params.set("host", host);
    if (query) params.set("query", query);
    if (days) params.set("days", String(days));
    return get<TraceListResponse>(`/traces?${params.toString()}`);
  },
  trace: (id: string) => get<Trace>(`/v1/traces/${id}`),
  ledger: (session_id: string) => get<any>(`/ledgers/${session_id}`),
  clusters: () => get<Cluster[]>("/clusters"),
  blocks: () => get<ReasonBlock[]>("/blocks"),
  block: (id: string) => get<ReasonBlock>(`/blocks/${id}`),
  savings: () => get<SavingsSummary>("/savings"),
  savingsSummary: (windowDays = 14) =>
    get<SavingsSummaryV2>(`/v1/savings/summary?window_days=${windowDays}`),
  optimizationsSummary: (windowDays = 14) =>
    get<OptimizationsSummary>(
      `/v1/optimizations/summary?window_days=${windowDays}`
    ),
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
  agents: () => get<Agent[]>("/agents"),
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
  telemetryAck: () => post<TelemetryConfigResponse>("/telemetry/ack", {}),
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
  fileContentUrl: (path: string) =>
    `${BASE}/v1/files/content?path=${encodeURIComponent(path)}`,
  // -----------------------------------------------------------------------
  // Week-2 endpoints (Spec 06)
  // -----------------------------------------------------------------------
  sessions: (since = "7d", limit = 200) =>
    get<SessionSummary[]>(`/v1/sessions?since=${since}&limit=${limit}`),
  sessionReport: (id: string) => get<SessionReport>(`/v1/sessions/${id}`),
  memoryFacts: (vendor?: string) => {
    const suffix = vendor ? `?vendor=${encodeURIComponent(vendor)}` : "";
    return get<MemoryFact[]>(`/v1/memory/facts${suffix}`);
  },
  memoryFact: (factId: string) => get<MemoryFact>(`/v1/memory/facts/${factId}`),
  insightsWindow: (since = "7d") =>
    get<InsightsWindow>(`/v1/insights?since=${since}`),
  outcomesSummary: (since = "7d") =>
    get<OutcomesSummary>(`/v1/outcomes/summary?since=${since}`),
  outcomesForSession: (sessionId: string) =>
    get<Record<string, unknown>[]>(`/v1/outcomes/${sessionId}`),
  reports: () => get<ReportMeta[]>("/v1/reports"),
  report: (week: string) => get<ReportContent>(`/v1/reports/${week}`),
};
