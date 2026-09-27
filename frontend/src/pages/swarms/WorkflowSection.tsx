import { useEffect, useMemo, useState } from "react";
import { Pause, RefreshCw, Square } from "lucide-react";
import { api, type WorkflowCurrentDetail } from "../../api";
import {
  Button,
  Card,
  Chip,
  CopyButton,
  FieldLabel,
  Input,
  SnippetCard,
  cx,
} from "../../components/WorkbenchUI";
import { fmtDate } from "../../lib/format";
import { statusTone } from "../../lib/status";

function snippet(value: unknown): string {
  if (value == null) return "{}";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

// Workspace-local `workflow` runtime snapshot, folded into Runs as a
// collapsed section (see pages/Swarms.tsx). Not a historical run ledger.
export function WorkflowSection({ projectRoot }: { projectRoot?: string }) {
  const [detail, setDetail] = useState<WorkflowCurrentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState<"pause" | "stop" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionReason, setActionReason] = useState("");

  const loadCurrent = async () => {
    setLoading(true);
    try {
      const payload = await api.workflowCurrent(projectRoot);
      setDetail(payload);
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load workflow snapshot"
      );
      setDetail(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadCurrent();
  }, [projectRoot]);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      void loadCurrent();
    }, 15000);
    return () => window.clearInterval(intervalId);
  }, [projectRoot]);

  const summary = detail?.summary;
  const hasSnapshot = Boolean(summary?.run_id);
  const orderedSteps = useMemo(() => {
    if (!detail) return [];
    return detail.step_order
      .map((stepId) => ({
        stepId,
        result: detail.task_outputs[stepId] || null,
      }))
      .filter((item) => item.result);
  }, [detail]);

  const handlePause = async () => {
    setActing("pause");
    try {
      const payload = await api.pauseWorkflowCurrent(
        actionReason.trim() || undefined,
        projectRoot
      );
      setDetail(payload);
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to pause workflow snapshot"
      );
    } finally {
      setActing(null);
    }
  };

  const handleStop = async () => {
    setActing("stop");
    try {
      const payload = await api.stopWorkflowCurrent(
        actionReason.trim() || undefined,
        projectRoot
      );
      setDetail(payload);
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to stop workflow snapshot"
      );
    } finally {
      setActing(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button variant="ghost" onClick={() => void loadCurrent()}>
          <RefreshCw className="h-4 w-4" />
          Refresh
        </Button>
      </div>

      {error ? (
        <Card className="border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100">
          {error}
        </Card>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(300px,360px)_minmax(0,1fr)]">
        <Card className="space-y-3 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                Snapshot
              </h2>
              <p className="text-sm text-slate-500">
                There is at most one current workflow snapshot per workspace.
              </p>
            </div>
            <Chip tone="neutral">{hasSnapshot ? "1 item" : "0 items"}</Chip>
          </div>

          {loading ? (
            <p className="text-sm text-slate-500">Loading workflow snapshot…</p>
          ) : hasSnapshot && summary ? (
            <button
              type="button"
              className="w-full border border-brand-500/40 bg-brand-500/10 px-3 py-3 text-left"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-neutral-100">
                    {summary.workflow_id || "Current workflow"}
                  </div>
                  <div className="mt-1 text-xs text-slate-400">
                    run_id: {summary.run_id}
                  </div>
                </div>
                <Chip tone={statusTone(summary.status)}>{summary.status}</Chip>
              </div>
              <div className="mt-3 grid gap-2 text-xs text-slate-400">
                <div>current step: {summary.current_step || "—"}</div>
                <div>
                  progress: {summary.completed_steps}/{summary.step_count}
                </div>
                <div>updated: {fmtDate(summary.updated_at)}</div>
              </div>
            </button>
          ) : (
            <Card className="border-dashed border-neutral-800 bg-neutral-950/60 p-4 text-sm text-slate-500">
              No persisted workflow snapshot for this workspace yet.
            </Card>
          )}
        </Card>

        <div className="space-y-4">
          {!hasSnapshot || !detail || !summary ? (
            <Card className="border-dashed border-neutral-800 bg-neutral-950/60 p-6 text-sm text-slate-500">
              Run a workflow first, then this section will show its persisted
              state, route, review metadata, and step outputs.
            </Card>
          ) : (
            <>
              <div className="grid gap-3 md:grid-cols-4">
                <Card className="p-4">
                  <FieldLabel>Status</FieldLabel>
                  <div className="mt-2 flex items-center gap-2">
                    <Chip tone={statusTone(summary.status)}>
                      {summary.status}
                    </Chip>
                  </div>
                </Card>
                <Card className="p-4">
                  <FieldLabel>Current step</FieldLabel>
                  <div className="mt-2 text-sm text-neutral-100">
                    {summary.current_step || "—"}
                  </div>
                </Card>
                <Card className="p-4">
                  <FieldLabel>Progress</FieldLabel>
                  <div className="mt-2 text-sm text-neutral-100">
                    {summary.completed_steps}/{summary.step_count}
                  </div>
                </Card>
                <Card className="p-4">
                  <FieldLabel>Review decision</FieldLabel>
                  <div className="mt-2 text-sm text-neutral-100">
                    {summary.review_decision || "pending / none"}
                  </div>
                </Card>
              </div>

              <Card className="space-y-4 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                      Actions
                    </h2>
                    <p className="text-sm text-slate-500">
                      Pause/stop affect the persisted snapshot only. Resume
                      payloads are copyable because the actual resume path still
                      runs through the host workflow tool.
                    </p>
                  </div>
                  <Chip tone="neutral">{detail.notes.snapshot_kind}</Chip>
                </div>

                <div className="grid gap-3 lg:grid-cols-[minmax(14rem,18rem)_minmax(0,1fr)]">
                  <div className="space-y-2">
                    <FieldLabel>Action reason</FieldLabel>
                    <Input
                      value={actionReason}
                      onChange={(event) => setActionReason(event.target.value)}
                      placeholder="Optional reason"
                    />
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="ghost"
                        onClick={handlePause}
                        disabled={
                          !detail.available_actions.can_pause || acting !== null
                        }
                      >
                        <Pause className="h-4 w-4" />
                        {acting === "pause" ? "Pausing…" : "Pause snapshot"}
                      </Button>
                      <Button
                        variant="ghost"
                        onClick={handleStop}
                        disabled={
                          !detail.available_actions.can_stop || acting !== null
                        }
                      >
                        <Square className="h-4 w-4" />
                        {acting === "stop" ? "Stopping…" : "Stop snapshot"}
                      </Button>
                    </div>
                  </div>

                  <div className="space-y-3">
                    <div className="flex flex-wrap gap-2">
                      <CopyButton
                        text={snippet(detail.control_payloads.status)}
                        label="Copy status payload"
                      />
                      {"resume_approve" in detail.control_payloads ? (
                        <CopyButton
                          text={snippet(detail.control_payloads.resume_approve)}
                          label="Copy approve resume"
                        />
                      ) : null}
                      {"resume_revise" in detail.control_payloads ? (
                        <CopyButton
                          text={snippet(detail.control_payloads.resume_revise)}
                          label="Copy revise resume"
                        />
                      ) : null}
                      {"resume_rerun" in detail.control_payloads ? (
                        <CopyButton
                          text={snippet(detail.control_payloads.resume_rerun)}
                          label="Copy rerun resume"
                        />
                      ) : null}
                    </div>
                    <p className="text-xs text-slate-500">
                      {detail.notes.summary}
                    </p>
                  </div>
                </div>
              </Card>

              <div className="grid gap-4 xl:grid-cols-2">
                <SnippetCard
                  title="Workflow spec"
                  body={snippet(detail.workflow)}
                  caption="Stored workflow definition for this snapshot."
                />
                <SnippetCard
                  title="Route"
                  body={snippet(detail.route)}
                  caption="Route hints recorded with the workflow runtime."
                />
              </div>

              <div className="grid gap-4 xl:grid-cols-2">
                <SnippetCard
                  title="Current task"
                  body={snippet(detail.current_task)}
                  caption="Top-level workflow task pointer from workspace session state."
                />
                <SnippetCard
                  title="Plan review"
                  body={snippet(detail.plan_review)}
                  caption="Persisted review state for the current workflow snapshot."
                />
              </div>

              <Card className="space-y-3 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                      Step outputs
                    </h2>
                    <p className="text-sm text-slate-500">
                      Expand each step to inspect output, error state, and
                      execution receipt.
                    </p>
                  </div>
                  <Chip tone="neutral">{orderedSteps.length} steps</Chip>
                </div>

                {orderedSteps.length ? (
                  <div className="space-y-3">
                    {orderedSteps.map(({ stepId, result }) => (
                      <details
                        key={stepId}
                        className={cx(
                          "border bg-neutral-950/50 px-4 py-3",
                          summary.current_step === stepId
                            ? "border-brand-500/40"
                            : "border-neutral-800"
                        )}
                        open={summary.current_step === stepId}
                      >
                        <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2">
                          <div className="min-w-0">
                            <div className="font-semibold text-neutral-100">
                              {stepId}
                            </div>
                            <div className="mt-1 text-xs text-slate-500">
                              {result?.kind || "step"}
                            </div>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Chip tone={statusTone(result?.status || "idle")}>
                              {result?.status || "unknown"}
                            </Chip>
                            <span className="text-xs text-slate-500">
                              {result?.duration_seconds
                                ? `${result.duration_seconds.toFixed(2)}s`
                                : "—"}
                            </span>
                          </div>
                        </summary>
                        <div className="mt-3 grid gap-3 xl:grid-cols-2">
                          <SnippetCard
                            title="Output"
                            body={snippet(result?.output || "")}
                            caption={
                              result?.error
                                ? `Error: ${result.error}`
                                : "Primary step output"
                            }
                          />
                          <SnippetCard
                            title="Execution receipt"
                            body={snippet(result?.execution_receipt || {})}
                            caption="Recorded executor metadata for this step."
                          />
                        </div>
                      </details>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-slate-500">
                    No step outputs persisted yet.
                  </p>
                )}
              </Card>

              <Card className="grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-4">
                <div>
                  <FieldLabel>Workspace</FieldLabel>
                  <div className="mt-2 break-all text-sm text-neutral-100">
                    {detail.workspace_root || "—"}
                  </div>
                </div>
                <div>
                  <FieldLabel>Created</FieldLabel>
                  <div className="mt-2 text-sm text-neutral-100">
                    {fmtDate(summary.created_at)}
                  </div>
                </div>
                <div>
                  <FieldLabel>Updated</FieldLabel>
                  <div className="mt-2 text-sm text-neutral-100">
                    {fmtDate(summary.updated_at)}
                  </div>
                </div>
                <div>
                  <FieldLabel>Session phase</FieldLabel>
                  <div className="mt-2 text-sm text-neutral-100">
                    {summary.session_phase || "—"}
                  </div>
                </div>
              </Card>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
