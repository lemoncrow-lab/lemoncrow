import { useEffect, useMemo, useRef, useState } from "react";
import { CircleHelp, RefreshCw } from "lucide-react";
import {
  api,
  ApiError,
  type SwarmAcceptedCommit,
  type SwarmArtifactRef,
  type SwarmLaunchOptionsResponse,
  type SwarmRunDetailResponse,
  type SwarmRunListItem,
} from "../api";
import {
  Button,
  Card,
  Chip,
  FieldLabel,
  Input,
  Select,
  SnippetCard,
  cx,
} from "../components/WorkbenchUI";

function fmtDate(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

function statusTone(
  value: string
): "emerald" | "amber" | "red" | "purple" | "neutral" {
  if (value === "success") return "emerald";
  if (value === "running" || value === "pending") return "amber";
  if (value === "failed" || value === "stopped") return "red";
  if (value === "applying") return "purple";
  return "neutral";
}

function planningTone(
  value?: string | null
): "cyan" | "amber" | "neutral" | "purple" {
  if (value === "open-ended") return "purple";
  if (value === "bounded") return "cyan";
  if (value === "adaptive") return "amber";
  return "neutral";
}

function artifactSummary(artifacts: SwarmArtifactRef[]): string {
  if (!artifacts.length) return "No exported artifacts";
  return artifacts.map((artifact) => artifact.label).join(", ");
}

function commitLabel(commit: SwarmAcceptedCommit): string {
  if (commit.commit_ref) return commit.commit_ref;
  if (commit.patch_path) return commit.patch_path;
  return commit.child_id;
}

function compactExcerpt(value?: string | null): string {
  return (value || "").replace(/\s+/g, " ").trim();
}

const NEW_SPEC_CHOICE = "__new_program_md__";

function lineCount(value: string): number {
  return Math.max(1, value.split("\n").length);
}

function worktreePoolHint(repoRoot: string): string {
  const normalized = repoRoot.trim().replace(/[\\/]+$/, "");
  if (!normalized) return "";
  const parts = normalized.split(/[\\/]/).filter(Boolean);
  const repoName = parts[parts.length - 1] || "repo";
  const lastSlash = Math.max(
    normalized.lastIndexOf("/"),
    normalized.lastIndexOf("\\")
  );
  const parent = lastSlash > 0 ? normalized.slice(0, lastSlash) : normalized;
  return `${parent}/${repoName}-swarm-worktrees/<generated-run-id>/`;
}

function HoverHint({ text }: { text: string }) {
  return (
    <span
      title={text}
      aria-label={text}
      className="inline-flex items-center text-slate-500 transition hover:text-slate-300"
    >
      <CircleHelp className="h-3.5 w-3.5" />
    </span>
  );
}

function parseProviderEnvText(input: string): {
  env: Record<string, string>;
  error?: string;
} {
  const env: Record<string, string> = {};
  for (const [index, rawLine] of input.split("\n").entries()) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator <= 0) {
      return {
        env: {},
        error: `Invalid provider env line ${index + 1}. Use KEY=VALUE.`,
      };
    }
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1);
    env[key] = value;
  }
  return { env };
}

function MarkdownSourceEditor({
  label,
  path,
  content,
  modeLabel,
  isEditing,
  onToggleEdit,
  onChange,
}: {
  label: string;
  path: string;
  content: string;
  modeLabel: string;
  isEditing: boolean;
  onToggleEdit?: () => void;
  onChange?: (value: string) => void;
}) {
  const gutterRef = useRef<HTMLDivElement | null>(null);
  const lines = useMemo(
    () => Array.from({ length: lineCount(content) }, (_, index) => index + 1),
    [content]
  );

  const syncScroll = (top: number) => {
    if (gutterRef.current) {
      gutterRef.current.scrollTop = top;
    }
  };

  return (
    <div className="flex h-full self-stretch flex-col overflow-hidden border border-white/10 bg-slate-950/50">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-white/10 px-4 py-3">
        <div className="min-w-0">
          <FieldLabel>{label}</FieldLabel>
          <div className="mt-1 truncate font-mono text-sm text-slate-100">
            {path}
          </div>
          <div className="mt-1 text-xs text-slate-500">{modeLabel}</div>
        </div>
        <div className="flex items-center gap-2">
          <Chip tone="neutral">{lines.length} lines</Chip>
          {onToggleEdit ? (
            <Button variant="ghost" onClick={onToggleEdit}>
              {isEditing ? "Done" : "Edit"}
            </Button>
          ) : null}
        </div>
      </div>
      <div className="grid flex-1 grid-cols-[3.5rem_minmax(0,1fr)]">
        <div
          ref={gutterRef}
          className="min-h-0 overflow-auto border-r border-white/10 bg-slate-900/90 px-2 py-3 text-right font-mono text-xs leading-6 text-slate-500"
          aria-hidden="true"
        >
          {lines.map((line) => (
            <div key={line}>{line}</div>
          ))}
        </div>
        {isEditing ? (
          <textarea
            id="swarm-spec-content"
            value={content}
            onChange={(event) => onChange?.(event.target.value)}
            onScroll={(event) => syncScroll(event.currentTarget.scrollTop)}
            className="h-full min-h-0 w-full resize-y overflow-auto bg-slate-950/90 px-4 py-3 font-mono text-sm leading-6 text-slate-100 outline-none transition focus:bg-slate-950"
            spellCheck={false}
            wrap="off"
          />
        ) : (
          <pre
            className="h-full min-h-0 overflow-auto bg-slate-950/90 px-4 py-3 font-mono text-sm leading-6 text-slate-100"
            onScroll={(event) => syncScroll(event.currentTarget.scrollTop)}
          >
            <code>{content || ""}</code>
          </pre>
        )}
      </div>
    </div>
  );
}

export default function Swarm() {
  const [runs, setRuns] = useState<SwarmRunListItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SwarmRunDetailResponse | null>(null);
  const [launchOptions, setLaunchOptions] =
    useState<SwarmLaunchOptionsResponse | null>(null);
  const [selectedProjectRoot, setSelectedProjectRoot] = useState("");
  const [selectedSpecChoice, setSelectedSpecChoice] = useState("");
  const [selectedSpecPath, setSelectedSpecPath] = useState("");
  const [specContent, setSpecContent] = useState("");
  const [isEditingSpec, setIsEditingSpec] = useState(false);
  const [isSpecDirty, setIsSpecDirty] = useState(false);
  const [provider, setProvider] = useState("cli");
  const [runner, setRunner] = useState("claude");
  const [modelValue, setModelValue] = useState("");
  const [providerApiKey, setProviderApiKey] = useState("");
  const [providerBaseUrl, setProviderBaseUrl] = useState("");
  const [providerEnvText, setProviderEnvText] = useState("");
  const [runnerOptions, setRunnerOptions] = useState("");
  const [launchRuns, setLaunchRuns] = useState(3);
  const [continuous, setContinuous] = useState(true);
  const [maxWaves, setMaxWaves] = useState(5);
  const [keepWorktrees, setKeepWorktrees] = useState(true);
  const [effort, setEffort] = useState("high");
  const [logs, setLogs] = useState("");
  const [selectedChildId, setSelectedChildId] = useState("");
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [loadingLaunchOptions, setLoadingLaunchOptions] = useState(true);
  const [refreshingLogs, setRefreshingLogs] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [isLaunchPanelOpen, setIsLaunchPanelOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const launchOptionsRequestRef = useRef(0);

  const confirmDiscardSpecDraft = () => {
    if (!isSpecDirty) return true;
    return window.confirm("Discard your current spec edits?");
  };

  const loadLaunchOptions = async (
    projectRoot?: string,
    specPath?: string,
    preserveSelections = true
  ) => {
    const requestId = ++launchOptionsRequestRef.current;
    setLoadingLaunchOptions(true);
    try {
      const payload = await api.swarmLaunchOptions(projectRoot, specPath);
      if (requestId !== launchOptionsRequestRef.current) {
        return;
      }
      const resolvedSpecPath =
        payload.selected_spec_path || payload.notes.default_spec;
      setLaunchOptions(payload);
      setSelectedProjectRoot(payload.selected_project_root);
      setSelectedSpecChoice(resolvedSpecPath);
      setSelectedSpecPath(resolvedSpecPath || "");
      setSpecContent(payload.spec_document?.content || "");
      setIsEditingSpec(false);
      setIsSpecDirty(false);
      if (!preserveSelections) {
        setProvider(payload.defaults.provider);
        setRunner(payload.defaults.runner);
        setLaunchRuns(payload.defaults.runs);
        setContinuous(payload.defaults.continuous);
        setMaxWaves(payload.defaults.max_waves ?? 5);
        setKeepWorktrees(payload.defaults.keep_worktrees);
        setEffort(payload.defaults.effort);
        setModelValue("");
        setProviderApiKey("");
        setProviderBaseUrl("");
        setProviderEnvText("");
        setRunnerOptions("");
      }
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load swarm launch options"
      );
      setLaunchOptions(null);
    } finally {
      setLoadingLaunchOptions(false);
    }
  };

  const loadRuns = async (preferredRunId?: string | null) => {
    setLoadingRuns(true);
    try {
      const payload = await api.swarmRuns();
      setRuns(payload);
      setSelectedRunId((current) => {
        const candidate = preferredRunId ?? current;
        if (candidate && payload.some((item) => item.run_id === candidate)) {
          return candidate;
        }
        return payload[0]?.run_id ?? null;
      });
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load swarm runs"
      );
      setRuns([]);
      setSelectedRunId(null);
      setDetail(null);
      setLogs("");
    } finally {
      setLoadingRuns(false);
    }
  };

  const loadRunDetail = async (runId: string) => {
    setLoadingDetail(true);
    try {
      const payload = await api.swarmRun(runId);
      setDetail(payload);
      const defaultChild =
        payload.run.children.find((child) => child.status === "running")
          ?.child_id ??
        payload.run.children[0]?.child_id ??
        "";
      setSelectedChildId((current) => {
        if (
          current &&
          payload.run.children.some((child) => child.child_id === current)
        ) {
          return current;
        }
        return defaultChild;
      });
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load swarm detail"
      );
      setDetail(null);
      setLogs("");
    } finally {
      setLoadingDetail(false);
    }
  };

  const loadLogs = async (runId: string, childId: string) => {
    setRefreshingLogs(true);
    try {
      const payload = await api.swarmLogs(runId, childId || undefined);
      setLogs(payload.content || "");
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setLogs("No logs captured yet for this run.");
      } else {
        setError(err instanceof Error ? err.message : "Failed to load logs");
      }
    } finally {
      setRefreshingLogs(false);
    }
  };

  useEffect(() => {
    void loadLaunchOptions(undefined, undefined, false);
    void loadRuns();
  }, []);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      void loadRuns(selectedRunId);
      if (selectedRunId) {
        void loadRunDetail(selectedRunId);
      }
    }, 15000);
    return () => window.clearInterval(intervalId);
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) return;
    void loadRunDetail(selectedRunId);
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId || !detail) return;
    void loadLogs(selectedRunId, selectedChildId);
  }, [selectedRunId, selectedChildId, detail]);

  const selectedRun = useMemo(
    () => runs.find((item) => item.run_id === selectedRunId) ?? null,
    [runs, selectedRunId]
  );

  const selectedProvider = useMemo(
    () => launchOptions?.providers.find((item) => item.id === provider) ?? null,
    [launchOptions, provider]
  );

  const selectedRunner = useMemo(
    () => launchOptions?.runners.find((item) => item.id === runner) ?? null,
    [launchOptions, runner]
  );
  const isCliProvider = provider === "cli";
  const defaultSpecPath = launchOptions?.notes.default_spec || "PROGRAM.md";
  const worktreeHint = useMemo(
    () => worktreePoolHint(selectedProjectRoot),
    [selectedProjectRoot]
  );
  const selectedSpecLabel =
    selectedSpecChoice === NEW_SPEC_CHOICE
      ? `+ New file (${defaultSpecPath})`
      : selectedSpecPath;
  const specModeLabel =
    selectedSpecChoice === NEW_SPEC_CHOICE
      ? `New markdown file. Launch will create ${selectedSpecPath || defaultSpecPath}.`
      : isEditingSpec
        ? "Editing the selected file content for this launch."
        : "Read-only snapshot of the selected file. Click Edit to modify it.";

  const handleLaunch = async () => {
    if (!selectedProjectRoot) return;
    setLaunching(true);
    try {
      const creatingNewSpec = selectedSpecChoice === NEW_SPEC_CHOICE;
      const launchSpecMode =
        creatingNewSpec || isSpecDirty ? "inline" : "existing";
      const providerEnv =
        provider === "litellm"
          ? parseProviderEnvText(providerEnvText)
          : { env: {} };
      if (providerEnv.error) {
        setError(providerEnv.error);
        return;
      }
      const launched = await api.launchSwarmRun({
        project_root: selectedProjectRoot.trim(),
        spec_path: selectedSpecPath || null,
        spec_mode: launchSpecMode,
        spec_content: creatingNewSpec || isSpecDirty ? specContent : null,
        provider,
        runner: isCliProvider ? runner : null,
        runner_model: isCliProvider ? modelValue || null : null,
        model: isCliProvider ? null : modelValue || null,
        runner_options: isCliProvider ? runnerOptions : "",
        runs: launchRuns,
        continuous,
        max_waves: continuous ? maxWaves : 1,
        keep_worktrees: keepWorktrees,
        effort,
        provider_api_key: provider === "openai" ? providerApiKey || null : null,
        provider_base_url:
          provider === "openai" ? providerBaseUrl.trim() || null : null,
        provider_env: provider === "litellm" ? providerEnv.env : {},
      });
      await loadRuns(launched.run_id);
      setSelectedRunId(launched.run_id);
      setIsSpecDirty(false);
      setIsEditingSpec(false);
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to launch swarm run"
      );
    } finally {
      setLaunching(false);
    }
  };

  const handleStop = async () => {
    if (!selectedRunId) return;
    setStopping(true);
    try {
      await api.stopSwarmRun(selectedRunId);
      await loadRuns(selectedRunId);
      await loadRunDetail(selectedRunId);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to stop swarm run");
    } finally {
      setStopping(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="pl-2 text-xl font-semibold text-white">Swarm</h1>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            onClick={() => {
              void loadRuns(selectedRunId);
              if (selectedRunId) void loadRunDetail(selectedRunId);
            }}
          >
            <RefreshCw className="h-4 w-4" />
            Refresh
          </Button>
          <Button
            variant="ghost"
            onClick={() => setIsLaunchPanelOpen((current) => !current)}
          >
            {isLaunchPanelOpen ? "Close" : "Launch swarms"}
          </Button>
        </div>
      </div>

      {error ? (
        <Card className="border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100">
          {error}
        </Card>
      ) : null}

      {isLaunchPanelOpen ? (
        <Card className="space-y-4 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                Launch swarms
              </h2>
            </div>
            {loadingLaunchOptions ? (
              <span className="text-xs text-slate-500">Loading options…</span>
            ) : null}
          </div>

          <div className="grid items-stretch gap-4 lg:grid-cols-[minmax(16rem,0.68fr)_minmax(0,1.32fr)]">
            <div className="max-w-[25rem] space-y-4">
              <div className="space-y-2">
                <FieldLabel className="flex items-center gap-1">
                  Project directory
                  <HoverHint text="Choose the repo root that the swarm should branch from." />
                </FieldLabel>
                <Select
                  id="swarm-working-directory"
                  className="w-full"
                  value={selectedProjectRoot}
                  onChange={(event) => {
                    const next = event.target.value;
                    if (!confirmDiscardSpecDraft()) return;
                    setSelectedProjectRoot(next);
                    void loadLaunchOptions(next, undefined);
                  }}
                >
                  {(launchOptions?.project_roots || []).map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.full_path}
                      {item.has_program_md ? ` · ${defaultSpecPath}` : ""}
                    </option>
                  ))}
                </Select>
                {worktreeHint ? (
                  <p className="text-xs text-slate-500">
                    Swarms working directory:{" "}
                    <code className="text-slate-200">{worktreeHint}</code>
                  </p>
                ) : null}
              </div>

              <div className="space-y-2">
                <FieldLabel className="flex items-center gap-1">
                  Spec file
                  <HoverHint
                    text={`Pick an existing task file or start a new ${defaultSpecPath} buffer.`}
                  />
                </FieldLabel>
                <Select
                  id="swarm-spec-select"
                  className="w-full"
                  title={`Pick an existing task file or start a new ${defaultSpecPath} buffer.`}
                  value={selectedSpecChoice || selectedSpecPath}
                  onChange={(event) => {
                    const next = event.target.value;
                    if (!confirmDiscardSpecDraft()) return;
                    if (next === NEW_SPEC_CHOICE) {
                      setSelectedSpecChoice(NEW_SPEC_CHOICE);
                      setSelectedSpecPath(defaultSpecPath);
                      setSpecContent("");
                      setIsEditingSpec(true);
                      setIsSpecDirty(false);
                      return;
                    }
                    setSelectedSpecChoice(next);
                    setSelectedSpecPath(next);
                    void loadLaunchOptions(
                      selectedProjectRoot || undefined,
                      next || undefined
                    );
                  }}
                >
                  <option value={NEW_SPEC_CHOICE}>
                    + New file ({defaultSpecPath})
                  </option>
                  {(launchOptions?.files || []).map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.path}
                      {item.is_default ? " · default" : ""}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-2">
                  <FieldLabel className="flex items-center gap-1">
                    Provider
                    <HoverHint
                      text={
                        selectedProvider?.credential_hint ||
                        "Provider-backed swarm workers inherit credentials from the server environment."
                      }
                    />
                  </FieldLabel>
                  <Select
                    id="swarm-provider"
                    className="w-full"
                    title={selectedProvider?.credential_hint || undefined}
                    value={provider}
                    onChange={(event) => setProvider(event.target.value)}
                  >
                    {(launchOptions?.providers || []).map((item) => (
                      <option
                        key={item.id}
                        value={item.id}
                        disabled={!item.supported}
                      >
                        {item.label}
                      </option>
                    ))}
                  </Select>
                  {!selectedProvider?.supported && selectedProvider?.reason ? (
                    <p className="text-xs text-amber-300">
                      {selectedProvider.reason}
                    </p>
                  ) : null}
                </div>
                {isCliProvider ? (
                  <div className="space-y-2">
                    <FieldLabel>CLI runner</FieldLabel>
                    <Select
                      id="swarm-runner"
                      className="w-full"
                      value={runner}
                      onChange={(event) => setRunner(event.target.value)}
                    >
                      {(launchOptions?.runners || []).map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.label}
                        </option>
                      ))}
                    </Select>
                  </div>
                ) : null}
              </div>

              <div
                className={cx(
                  "grid gap-3",
                  isCliProvider ? "sm:grid-cols-2" : "sm:grid-cols-1"
                )}
              >
                <div className="space-y-2">
                  <FieldLabel>
                    {isCliProvider ? "Runner model" : "Provider model"}
                  </FieldLabel>
                  <Input
                    id="swarm-runner-model"
                    value={modelValue}
                    onChange={(event) => setModelValue(event.target.value)}
                    placeholder={
                      isCliProvider
                        ? selectedRunner?.model_placeholder || "optional"
                        : selectedProvider?.model_placeholder || "optional"
                    }
                  />
                </div>
                {isCliProvider ? (
                  <div className="space-y-2">
                    <FieldLabel className="flex items-center gap-1">
                      CLI options
                      <HoverHint
                        text={
                          selectedRunner?.options_help ||
                          "Extra CLI flags are appended before the generated swarm prompt."
                        }
                      />
                    </FieldLabel>
                    <Input
                      id="swarm-runner-options"
                      title={selectedRunner?.options_help || undefined}
                      value={runnerOptions}
                      onChange={(event) => setRunnerOptions(event.target.value)}
                      placeholder="--option value"
                    />
                  </div>
                ) : null}
              </div>

              {provider === "openai" ? (
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-2">
                    <FieldLabel className="flex items-center gap-1">
                      API key
                      <HoverHint text="Optional per-run OpenAI key. Passed only to the coordinator env and never persisted to swarm state." />
                    </FieldLabel>
                    <Input
                      id="swarm-provider-api-key"
                      type="password"
                      value={providerApiKey}
                      onChange={(event) =>
                        setProviderApiKey(event.target.value)
                      }
                      placeholder="sk-..."
                    />
                  </div>
                  <div className="space-y-2">
                    <FieldLabel className="flex items-center gap-1">
                      Base URL
                      <HoverHint text="Optional OpenAI-compatible endpoint for this swarm run." />
                    </FieldLabel>
                    <Input
                      id="swarm-provider-base-url"
                      value={providerBaseUrl}
                      onChange={(event) =>
                        setProviderBaseUrl(event.target.value)
                      }
                      placeholder="https://api.openai.com/v1"
                    />
                  </div>
                </div>
              ) : null}

              {provider === "litellm" ? (
                <div className="space-y-2">
                  <FieldLabel className="flex items-center gap-1">
                    Env overrides
                    <HoverHint text="Optional per-run LiteLLM credentials or provider env. Use one KEY=VALUE pair per line. These values are passed only to the coordinator env and are not stored in swarm state." />
                  </FieldLabel>
                  <textarea
                    id="swarm-provider-env"
                    value={providerEnvText}
                    onChange={(event) => setProviderEnvText(event.target.value)}
                    placeholder={
                      "AZURE_API_KEY=...\nAZURE_API_BASE=...\nAZURE_API_VERSION=..."
                    }
                    className="min-h-[7rem] w-full border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm text-neutral-200 outline-none transition hover:border-neutral-600 focus:border-purple-500/60"
                    spellCheck={false}
                  />
                </div>
              ) : null}

              <div className="grid gap-3 sm:grid-cols-4">
                <div className="space-y-2">
                  <FieldLabel>Runs / wave</FieldLabel>
                  <Input
                    id="swarm-runs"
                    type="number"
                    min={1}
                    value={String(launchRuns)}
                    onChange={(event) =>
                      setLaunchRuns(Number(event.target.value || 1))
                    }
                  />
                </div>
                <div className="space-y-2">
                  <FieldLabel className="flex items-center gap-1">
                    Max waves
                    <HoverHint text="Hard stop for continuous runs. Set 0 only when you want an unbounded run." />
                  </FieldLabel>
                  <Input
                    id="swarm-max-waves"
                    type="number"
                    min={0}
                    value={String(maxWaves)}
                    disabled={!continuous}
                    onChange={(event) =>
                      setMaxWaves(Number(event.target.value || 0))
                    }
                  />
                </div>
                <div className="space-y-2">
                  <FieldLabel className="flex items-center gap-1">
                    Effort
                    <HoverHint text="Effort is recorded in swarm metadata today." />
                  </FieldLabel>
                  <Select
                    id="swarm-effort"
                    className="w-full"
                    value={effort}
                    onChange={(event) => setEffort(event.target.value)}
                  >
                    <option value="high">high</option>
                    <option value="medium">medium</option>
                    <option value="low">low</option>
                  </Select>
                </div>
                <div className="flex items-end">
                  <Button
                    variant="accent"
                    className="w-full bg-purple-500/20 text-purple-100 hover:bg-purple-500/30 hover:text-purple-50"
                    onClick={handleLaunch}
                    disabled={
                      launching ||
                      !selectedProjectRoot ||
                      !selectedProvider?.supported
                    }
                  >
                    {launching ? "Launching…" : "Launch swarms"}
                  </Button>
                </div>
              </div>
              <div className="space-y-2 text-sm text-slate-400">
                <label className="inline-flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={continuous}
                    onChange={(event) => setContinuous(event.target.checked)}
                  />
                  Continuous waves
                </label>
                <label className="inline-flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={keepWorktrees}
                    onChange={(event) => setKeepWorktrees(event.target.checked)}
                  />
                  Keep worktrees after completion
                </label>
              </div>
            </div>

            <MarkdownSourceEditor
              label="Markdown task file"
              path={selectedSpecLabel || defaultSpecPath}
              content={specContent}
              modeLabel={specModeLabel}
              isEditing={
                isEditingSpec || selectedSpecChoice === NEW_SPEC_CHOICE
              }
              onToggleEdit={
                selectedSpecChoice === NEW_SPEC_CHOICE
                  ? undefined
                  : () => setIsEditingSpec((current) => !current)
              }
              onChange={(value) => {
                setSpecContent(value);
                setIsEditingSpec(true);
                setIsSpecDirty(true);
              }}
            />
          </div>
        </Card>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(360px,430px)_minmax(0,1fr)]">
        <Card className="space-y-3 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                Runs
              </h2>
              <p className="text-sm text-slate-500">
                Compact view of wave count, winner, prompt snapshot, and
                activity.
              </p>
            </div>
            <Chip tone="neutral">{runs.length} runs</Chip>
          </div>
          {loadingRuns ? (
            <p className="text-sm text-slate-500">Loading swarm runs…</p>
          ) : runs.length ? (
            <div className="space-y-2">
              {runs.map((run) => (
                <button
                  key={run.run_id}
                  type="button"
                  onClick={() => setSelectedRunId(run.run_id)}
                  className={cx(
                    "w-full rounded-none border px-3 py-3 text-left transition",
                    selectedRunId === run.run_id
                      ? "border-cyan-400/60 bg-cyan-500/10"
                      : "border-white/10 bg-white/[0.03] hover:border-white/20"
                  )}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-medium text-white">
                          {run.spec_title || run.run_id}
                        </span>
                        <Chip tone={statusTone(run.status)}>{run.status}</Chip>
                        <Chip tone={planningTone(run.planning_mode)}>
                          {run.current_wave}/{run.max_runs}
                        </Chip>
                      </div>
                      <p className="mt-1 text-xs text-slate-400">
                        {run.repo_label} · {run.runner_name}
                        {run.runner_model ? ` / ${run.runner_model}` : ""}
                        {run.launch_effort ? ` · ${run.launch_effort}` : ""}
                      </p>
                      {run.spec_excerpt ? (
                        <p className="mt-2 line-clamp-2 text-sm text-slate-300">
                          {compactExcerpt(run.spec_excerpt)}
                        </p>
                      ) : null}
                    </div>
                    <div className="shrink-0 text-right text-xs text-slate-500">
                      <div>{fmtDate(run.updated_at)}</div>
                      <div className="mt-1">
                        accepted {run.accepted_child_ids.length} · fail{" "}
                        {run.failed_children.length}
                      </div>
                      {run.running_children.length ? (
                        <div className="mt-1 text-amber-300">
                          live {run.running_children.length}
                        </div>
                      ) : null}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          ) : (
            <p className="text-sm text-slate-500">
              No swarm runs captured yet.
            </p>
          )}
        </Card>

        <div className="space-y-4">
          {selectedRun && detail ? (
            <>
              <Card className="space-y-4 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-lg font-semibold text-white">
                        {detail.spec.title}
                      </h2>
                      <Chip tone={statusTone(detail.run.status)}>
                        {detail.run.status}
                      </Chip>
                      <Chip tone={planningTone(detail.run.planning_mode)}>
                        {detail.run.planning_mode || detail.run.mode}
                      </Chip>
                    </div>
                    <p className="mt-1 text-sm text-slate-400">
                      {selectedRun.repo_label} · {detail.run.run_id} ·{" "}
                      {fmtDate(detail.run.updated_at)}
                    </p>
                    <p className="mt-2 text-sm text-slate-500">
                      {detail.run.runner_name}
                      {detail.run.runner_model
                        ? ` / ${detail.run.runner_model}`
                        : ""}
                      {detail.run.launch_provider
                        ? ` · ${detail.run.launch_provider}`
                        : ""}
                      {detail.run.launch_effort
                        ? ` · effort ${detail.run.launch_effort}`
                        : ""}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    disabled={stopping || detail.run.status !== "running"}
                    onClick={handleStop}
                  >
                    {stopping ? "Stopping…" : "Stop run"}
                  </Button>
                </div>

                <div className="grid gap-3 md:grid-cols-4">
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-slate-500">
                      Wave
                    </div>
                    <div className="mt-2 text-xl font-semibold text-white">
                      {detail.run.current_wave}/
                      {detail.run.max_runs || detail.run.runs}
                    </div>
                  </Card>
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-slate-500">
                      Accepted
                    </div>
                    <div className="mt-2 text-xl font-semibold text-white">
                      {detail.run.accepted_child_ids.length}
                    </div>
                  </Card>
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-slate-500">
                      Children
                    </div>
                    <div className="mt-2 text-xl font-semibold text-white">
                      {detail.run.children.length}
                    </div>
                  </Card>
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-slate-500">
                      Winner
                    </div>
                    <div className="mt-2 truncate text-sm font-medium text-white">
                      {detail.run.primary_winner_child_id || "—"}
                    </div>
                  </Card>
                </div>
              </Card>

              <Card className="space-y-3 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                      Frozen spec snapshot
                    </h3>
                    <p className="text-sm text-slate-500">
                      Rendered from the copied run snapshot, not the live
                      mutable file.
                    </p>
                  </div>
                  <div className="text-right text-xs text-slate-500">
                    <div>{detail.spec.source_path}</div>
                    <div>{detail.spec.copied_path}</div>
                  </div>
                </div>
                <MarkdownSourceEditor
                  label="Frozen spec snapshot"
                  path={detail.spec.source_path}
                  content={detail.spec.content}
                  modeLabel="Copied from the run snapshot, not the live file."
                  isEditing={false}
                />
              </Card>

              <div className="grid gap-4 lg:grid-cols-2">
                <Card className="space-y-3 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                      Accepted commits
                    </h3>
                    <Chip tone="emerald">
                      {detail.export.accepted_commits.length}
                    </Chip>
                  </div>
                  {detail.export.accepted_commits.length ? (
                    <div className="space-y-2">
                      {detail.export.accepted_commits.map((commit) => (
                        <div
                          key={`${commit.child_id}-${commit.order}`}
                          className="rounded-2xl border border-white/10 bg-white/[0.03] p-3"
                        >
                          <div className="flex items-center justify-between gap-3">
                            <span className="font-medium text-white">
                              {commit.child_id}
                            </span>
                            <span className="text-xs text-slate-500">
                              #{commit.order} · {commitLabel(commit)}
                            </span>
                          </div>
                          <p className="mt-2 text-sm text-slate-400">
                            {artifactSummary(commit.artifacts)}
                          </p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-slate-500">
                      No accepted commits recorded yet.
                    </p>
                  )}
                  <SnippetCard
                    title="Apply commands"
                    body={
                      detail.apply.commands.join("\n") ||
                      "# No transplant commands yet"
                    }
                    caption="Ready-to-run transplant commands from accepted child commits."
                  />
                </Card>

                <Card className="space-y-3 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                        Child logs
                      </h3>
                      <p className="text-sm text-slate-500">
                        Tail output from the selected child worktree.
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      onClick={() => {
                        if (selectedRunId)
                          void loadLogs(selectedRunId, selectedChildId);
                      }}
                    >
                      {refreshingLogs ? "Refreshing…" : "Refresh logs"}
                    </Button>
                  </div>

                  <div className="space-y-2">
                    <FieldLabel>Child</FieldLabel>
                    <Select
                      id="swarm-child-logs"
                      value={selectedChildId}
                      onChange={(event) =>
                        setSelectedChildId(event.target.value)
                      }
                    >
                      {detail.run.children.map((child) => (
                        <option key={child.child_id} value={child.child_id}>
                          {child.child_id} · {child.status}
                        </option>
                      ))}
                    </Select>
                  </div>

                  <pre className="max-h-[28rem] overflow-auto rounded-2xl border border-white/10 bg-slate-950/80 p-4 text-xs leading-6 text-slate-100">
                    {logs || "No logs captured yet for this run."}
                  </pre>
                </Card>
              </div>
            </>
          ) : loadingDetail ? (
            <Card className="p-4 text-sm text-slate-500">
              Loading swarm detail…
            </Card>
          ) : (
            <Card className="p-4 text-sm text-slate-500">
              Select a run to inspect its frozen prompt snapshot, accepted
              commits, and logs.
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
