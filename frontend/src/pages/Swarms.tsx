import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { Tip } from "../components/ui/tooltip";
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
  DisclosureCard,
  FieldLabel,
  Input,
  PageFrame,
  Select,
  SelectItem,
  SnippetCard,
  cx,
} from "../components/WorkbenchUI";
import { fmtDate } from "../lib/format";
import { setVisibleInterval } from "../lib/visibleInterval";
import { statusChipTone } from "../lib/status";
import { WorkflowSection } from "./swarms/WorkflowSection";

function planningTone(
  value?: string | null
): "info" | "warning" | "neutral" | "neutral" {
  if (value === "open-ended") return "neutral";
  if (value === "bounded") return "info";
  if (value === "adaptive") return "warning";
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
    <Tip content={text}>
      <span
        tabIndex={0}
        aria-label={text}
        className="inline-flex items-center text-muted-foreground transition hover:text-neutral-300"
      >
        <CircleHelp className="h-3.5 w-3.5" />
      </span>
    </Tip>
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
    <div className="flex h-full self-stretch flex-col overflow-hidden rounded-lg border border-border/70 bg-background/60">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/70 px-4 py-3">
        <div className="min-w-0">
          <FieldLabel>{label}</FieldLabel>
          <div className="mt-1 truncate font-mono text-sm text-foreground">
            {path}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">{modeLabel}</div>
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
          className="min-h-0 overflow-auto border-r border-border/70 bg-surface-code px-2 py-3 text-right font-mono text-xs leading-6 text-muted-foreground"
          aria-hidden="true"
        >
          {lines.map((line) => (
            <div key={line}>{line}</div>
          ))}
        </div>
        {isEditing ? (
          <UiTextarea unstyled
            id="swarm-spec-content"
            value={content}
            onChange={(event) => onChange?.(event.target.value)}
            onScroll={(event) => syncScroll(event.currentTarget.scrollTop)}
            className="h-full min-h-0 w-full resize-y overflow-auto bg-background/90 px-4 py-3 font-mono text-sm leading-6 text-foreground outline-none transition focus:bg-background"
            spellCheck={false}
            wrap="off"
          />
        ) : (
          <pre
            className="h-full min-h-0 overflow-auto bg-background/90 px-4 py-3 font-mono text-sm leading-6 text-foreground"
            onScroll={(event) => syncScroll(event.currentTarget.scrollTop)}
          >
            <code>{content || ""}</code>
          </pre>
        )}
      </div>
    </div>
  );
}

export default function Swarms() {
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
  const [isWorkflowOpen, setIsWorkflowOpen] = useState(false);
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

  const loadRunDetail = async (
    runId: string,
    isCancelled: () => boolean = () => false
  ) => {
    setLoadingDetail(true);
    try {
      const payload = await api.swarmRun(runId);
      if (isCancelled()) return;
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
      if (isCancelled()) return;
      setError(
        err instanceof Error ? err.message : "Failed to load swarm detail"
      );
      setDetail(null);
      setLogs("");
    } finally {
      if (!isCancelled()) setLoadingDetail(false);
    }
  };

  const loadLogs = async (
    runId: string,
    childId: string,
    isCancelled: () => boolean = () => false
  ) => {
    setRefreshingLogs(true);
    try {
      const payload = await api.swarmLogs(runId, childId || undefined);
      if (isCancelled()) return;
      setLogs(payload.content || "");
      setError(null);
    } catch (err) {
      if (isCancelled()) return;
      if (err instanceof ApiError && err.status === 404) {
        setLogs("No logs captured yet for this run.");
      } else {
        setError(err instanceof Error ? err.message : "Failed to load logs");
      }
    } finally {
      if (!isCancelled()) setRefreshingLogs(false);
    }
  };

  useEffect(() => {
    void loadLaunchOptions(undefined, undefined, false);
    void loadRuns();
  }, []);

  useEffect(() => {
    return setVisibleInterval(() => {
      void loadRuns(selectedRunId);
      if (selectedRunId) {
        void loadRunDetail(selectedRunId);
      }
    }, 15000);
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) return;
    let cancelled = false;
    void loadRunDetail(selectedRunId, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId || !detail) return;
    let cancelled = false;
    void loadLogs(selectedRunId, selectedChildId, () => cancelled);
    return () => {
      cancelled = true;
    };
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
    <PageFrame className="space-y-4">
      <div className="flex flex-wrap items-center justify-end gap-2">
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
              <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-neutral-400">
                Launch swarms
              </h2>
            </div>
            {loadingLaunchOptions ? (
              <span className="text-xs text-muted-foreground">Loading options…</span>
            ) : null}
          </div>

          <div className="grid items-stretch gap-4 xl:grid-cols-[minmax(16rem,0.68fr)_minmax(0,1.32fr)]">
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
                  onValueChange={(next) => {
                    if (!confirmDiscardSpecDraft()) return;
                    setSelectedProjectRoot(next);
                    void loadLaunchOptions(next, undefined);
                  }}
                >
                  {(launchOptions?.project_roots || []).map((item) => (
                    <SelectItem key={item.path} value={item.path}>
                      {item.full_path}
                      {item.has_program_md ? ` · ${defaultSpecPath}` : ""}
                    </SelectItem>
                  ))}
                </Select>
                {worktreeHint ? (
                  <p className="text-xs text-muted-foreground">
                    Swarms working directory:{" "}
                    <code className="text-neutral-200">{worktreeHint}</code>
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
                  onValueChange={(next) => {
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
                  <SelectItem value={NEW_SPEC_CHOICE}>
                    + New file ({defaultSpecPath})
                  </SelectItem>
                  {(launchOptions?.files || []).map((item) => (
                    <SelectItem key={item.path} value={item.path}>
                      {item.path}
                      {item.is_default ? " · default" : ""}
                    </SelectItem>
                  ))}
                </Select>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
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
                    onValueChange={setProvider}
                  >
                    {(launchOptions?.providers || []).map((item) => (
                      <SelectItem
                        key={item.id}
                        value={item.id}
                        disabled={!item.supported}
                      >
                        {item.label}
                      </SelectItem>
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
                      onValueChange={setRunner}
                    >
                      {(launchOptions?.runners || []).map((item) => (
                        <SelectItem key={item.id} value={item.id}>
                          {item.label}
                        </SelectItem>
                      ))}
                    </Select>
                  </div>
                ) : null}
              </div>

              <div
                className={cx(
                  "grid gap-3",
                  isCliProvider ? "md:grid-cols-2" : "md:grid-cols-1"
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
                <div className="grid gap-3 md:grid-cols-2">
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
                  <UiTextarea
                    id="swarm-provider-env"
                    value={providerEnvText}
                    onChange={(event) => setProviderEnvText(event.target.value)}
                    placeholder={
                      "AZURE_API_KEY=...\nAZURE_API_BASE=...\nAZURE_API_VERSION=..."
                    }
                    className="min-h-[7rem] font-mono"
                    spellCheck={false}
                  />
                </div>
              ) : null}

              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
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
                    onValueChange={setEffort}
                  >
                    <SelectItem value="high">high</SelectItem>
                    <SelectItem value="medium">medium</SelectItem>
                    <SelectItem value="low">low</SelectItem>
                  </Select>
                </div>
                <div className="flex items-end">
                  <Button
                    variant="default"
                    className="w-full bg-brand-500/20 text-brand-100 hover:bg-brand-500/30 hover:text-brand-50"
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
              <div className="space-y-2 text-sm text-neutral-400">
                <label className="inline-flex items-center gap-2">
                  <UiInput unstyled
                    type="checkbox"
                    checked={continuous}
                    onChange={(event) => setContinuous(event.target.checked)}
                  />
                  Continuous waves
                </label>
                <label className="inline-flex items-center gap-2">
                  <UiInput unstyled
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

      <div className="flex flex-col gap-4 md:flex-row">
        <Card className="w-full flex-shrink-0 space-y-3 p-4 md:w-80 lg:w-96">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-neutral-400">
                Swarms
              </h2>
              <p className="text-sm text-muted-foreground">
                Compact view of wave count, winner, prompt snapshot, and
                activity.
              </p>
            </div>
            <Chip tone="neutral">{runs.length} runs</Chip>
          </div>
          {loadingRuns ? (
            <p className="text-sm text-muted-foreground">Loading swarm runs…</p>
          ) : runs.length ? (
            <div className="space-y-2">
              {runs.map((run) => (
                <UiButton unstyled
                  key={run.run_id}
                  type="button"
                  onClick={() => setSelectedRunId(run.run_id)}
                  className={cx(
                    "w-full rounded-lg border px-3 py-3 text-left transition-colors",
                    selectedRunId === run.run_id
                      ? "border-border bg-neutral-800/70"
                      : "border-border/70 bg-background/60 hover:bg-muted/70"
                  )}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-medium text-foreground">
                          {run.spec_title || run.run_id}
                        </span>
                        <Chip tone={statusChipTone(run.status)}>{run.status}</Chip>
                        <Chip tone={planningTone(run.planning_mode)}>
                          {run.current_wave}/{run.max_runs}
                        </Chip>
                      </div>
                      <p className="mt-1 text-xs text-neutral-400">
                        {run.repo_label} · {run.runner_name}
                        {run.runner_model ? ` / ${run.runner_model}` : ""}
                        {run.launch_effort ? ` · ${run.launch_effort}` : ""}
                      </p>
                      {run.spec_excerpt ? (
                        <p className="mt-2 line-clamp-2 text-sm text-neutral-300">
                          {compactExcerpt(run.spec_excerpt)}
                        </p>
                      ) : null}
                    </div>
                    <div className="shrink-0 text-right text-xs text-muted-foreground">
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
                </UiButton>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              No swarm runs captured yet.
            </p>
          )}
        </Card>

        <div className="min-w-0 flex-1 space-y-4">
          {selectedRun && detail ? (
            <>
              <Card className="space-y-4 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-lg font-semibold text-foreground">
                        {detail.spec.title}
                      </h2>
                      <Chip tone={statusChipTone(detail.run.status)}>
                        {detail.run.status}
                      </Chip>
                      <Chip tone={planningTone(detail.run.planning_mode)}>
                        {detail.run.planning_mode || detail.run.mode}
                      </Chip>
                    </div>
                    <p className="mt-1 text-sm text-neutral-400">
                      {selectedRun.repo_label} · {detail.run.run_id} ·{" "}
                      {fmtDate(detail.run.updated_at)}
                    </p>
                    <p className="mt-2 text-sm text-muted-foreground">
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

                <div className="grid gap-3 lg:grid-cols-4">
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
                      Wave
                    </div>
                    <div className="mt-2 text-xl font-semibold text-foreground">
                      {detail.run.current_wave}/
                      {detail.run.max_runs || detail.run.runs}
                    </div>
                  </Card>
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
                      Accepted
                    </div>
                    <div className="mt-2 text-xl font-semibold text-foreground">
                      {detail.run.accepted_child_ids.length}
                    </div>
                  </Card>
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
                      Children
                    </div>
                    <div className="mt-2 text-xl font-semibold text-foreground">
                      {detail.run.children.length}
                    </div>
                  </Card>
                  <Card className="p-3">
                    <div className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
                      Winner
                    </div>
                    <div className="mt-2 truncate text-sm font-medium text-foreground">
                      {detail.run.primary_winner_child_id || "—"}
                    </div>
                  </Card>
                </div>
              </Card>

              <Card className="space-y-3 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-neutral-400">
                      Frozen spec snapshot
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      Rendered from the copied run snapshot, not the live
                      mutable file.
                    </p>
                  </div>
                  <div className="text-right text-xs text-muted-foreground">
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

              <div className="grid gap-4 xl:grid-cols-2">
                <Card className="space-y-3 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-neutral-400">
                      Accepted commits
                    </h3>
                    <Chip tone="success">
                      {detail.export.accepted_commits.length}
                    </Chip>
                  </div>
                  {detail.export.accepted_commits.length ? (
                    <div className="space-y-2">
                      {detail.export.accepted_commits.map((commit) => (
                        <div
                          key={`${commit.child_id}-${commit.order}`}
                          className="rounded-lg border border-border/70 bg-background/60 p-3"
                        >
                          <div className="flex items-center justify-between gap-3">
                            <span className="font-medium text-foreground">
                              {commit.child_id}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              #{commit.order} · {commitLabel(commit)}
                            </span>
                          </div>
                          <p className="mt-2 text-sm text-neutral-400">
                            {artifactSummary(commit.artifacts)}
                          </p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">
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
                      <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-neutral-400">
                        Child logs
                      </h3>
                      <p className="text-sm text-muted-foreground">
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
                      onValueChange={setSelectedChildId}
                    >
                      {detail.run.children.map((child) => (
                        <SelectItem key={child.child_id} value={child.child_id}>
                          {child.child_id} · {child.status}
                        </SelectItem>
                      ))}
                    </Select>
                  </div>

                  <pre className="max-h-[28rem] overflow-auto rounded-lg border border-border/70 bg-surface-code p-4 text-xs leading-6 text-foreground">
                    {logs || "No logs captured yet for this run."}
                  </pre>
                </Card>
              </div>
            </>
          ) : loadingDetail ? (
            <Card className="p-4 text-sm text-muted-foreground">
              Loading swarm detail…
            </Card>
          ) : (
            <Card className="p-4 text-sm text-muted-foreground">
              Select a run to inspect its frozen prompt snapshot, accepted
              commits, and logs.
            </Card>
          )}
        </div>
      </div>

      <DisclosureCard
        open={isWorkflowOpen}
        onToggle={() => setIsWorkflowOpen((current) => !current)}
        header={
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-neutral-400">
                Workflow (advanced)
              </h2>
              <p className="text-sm text-muted-foreground">
                Workspace-local `workflow` runtime snapshot — pause, stop, and
                inspect step outputs.
              </p>
            </div>
            <Chip tone="neutral">{isWorkflowOpen ? "Hide" : "Show"}</Chip>
          </div>
        }
      >
        <WorkflowSection projectRoot={selectedProjectRoot || undefined} />
      </DisclosureCard>
    </PageFrame>
  );
}
