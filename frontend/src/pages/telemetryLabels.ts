/**
 * Plain names for the telemetry event types (server `telemetry/schema.py` EVENTS). The page shows these; the raw
 * name stays in the privacy table and in an event's JSON, which show exactly what is recorded (runtime-23.2).
 */
const EVENT_LABELS: Record<string, string> = {
  session_start: "Session started",
  session_end: "Session ended",
  session_interrupted: "Session interrupted",
  cli_command_invoked: "CLI command started",
  cli_command_completed: "CLI command finished",
  mcp_tool_called: "Tool called",
  api_request: "API request",
  playbook_retrieved: "Playbook found",
  playbook_applied: "Playbook used",
  playbook_rejected: "Playbook not used",
  plan_check_passed: "Plan check passed",
  plan_check_blocked: "Plan check stopped a plan",
  plan_check_overridden: "Plan check overridden",
  plan_modified_by_user: "Plan edited by you",
  rescue_offered: "Help offered",
  rescue_accepted: "Help accepted",
  frustration_signal_behavioral: "Retry or undo pattern",
  frustration_signal_lexical: "Stuck wording in a prompt",
  value_estimate: "Savings estimate",
  code_index_completed: "Code index built",
  code_context_retrieved: "Code context read",
  code_symbol_retrieved: "Code symbol read",
  optimization_proposal_evaluated: "Optimization suggestion checked",
  optimization_proposal_result: "Optimization suggestion answered",
  mcp_auto_update_failed: "Tool update failed",
};

const ACRONYMS: Record<string, string> = { api: "API", cli: "CLI", mcp: "MCP", id: "ID", pr: "PR" };

/** The plain name of an event type; one the page does not know yet reads as words, not snake_case. */
export function eventLabel(name: string): string {
  const known = EVENT_LABELS[name];
  if (known) return known;
  const words = name.split("_").filter(Boolean).map((word) => ACRONYMS[word] ?? word);
  if (words.length === 0) return name;
  return [words[0].charAt(0).toUpperCase() + words[0].slice(1), ...words.slice(1)].join(" ");
}

/** A session id, short enough to tell sessions apart without a full UUID on every row. */
export function shortSessionId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}
