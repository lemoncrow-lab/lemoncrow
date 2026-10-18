import { useEffect, useState } from "react";
import { Card, PageFrame } from "../components/WorkbenchUI";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Select, SelectItem } from "../components/ui/select";
import { Checkbox } from "../components/ui/checkbox";
import { requestBrowserJson } from "../review/reviewApi";

type Kind = "laptop" | "vps" | "sandbox";
interface Device { worker_id: string; owner_id: string; device_kind: Kind; execution_classes: string; expires_at: number; revoked_at: number | null; last_seen_at?: number | null }
interface Listed { devices: Device[]; truncated?: boolean }
interface AuthorizedRepos { repositories: Array<{ repo_id: string }> }
interface Pairing { pairing_code: string; expires_at: number; repo_id: string }
interface AgentTask {
  job_id: string; review_id: string; revision_id: string;
  task_kind: "investigate" | "propose_fix";
  state: string; publication_state: string; attempt: number; deadline_at: number;
  observation: null | {
    text: string; provenance: "author_agent_unverified";
    included_source_paths: string[]; source_files_available: number;
    input_tokens: number; output_tokens: number; estimated_cost_micros: number;
  };
}
const REPO = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const names: Record<Kind, string> = { laptop: "Laptop", vps: "Headless VPS", sandbox: "Agent sandbox" };
function date(time: number): string { return new Date(time * 1000).toLocaleString(); }
function failure(reason: unknown): string { return reason instanceof Error ? reason.message : "Request failed"; }

export default function ReviewDevices() {
  const [repoInput, setRepoInput] = useState(() => new URLSearchParams(window.location.search).get("repo_id") ?? "");
  const [repo, setRepo] = useState("");
  const [availableRepos, setAvailableRepos] = useState<string[]>([]);
  const [reposLoaded, setReposLoaded] = useState(false);
  const [reposLoading, setReposLoading] = useState(false);
  const [kind, setKind] = useState<Kind>("laptop");
  const [days, setDays] = useState(7);
  const [ai, setAi] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [code, setCode] = useState<Pairing | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [confirmId, setConfirmId] = useState("");
  const [taskReviewId, setTaskReviewId] = useState(() => new URLSearchParams(window.location.search).get("review_id") ?? "");
  const [taskRevisionId, setTaskRevisionId] = useState(() => new URLSearchParams(window.location.search).get("revision_id") ?? "");
  const [taskKind, setTaskKind] = useState<"investigate" | "propose_fix">("investigate");
  const [sourceApproved, setSourceApproved] = useState(false);
  const [task, setTask] = useState<AgentTask | null>(null);
  const [recentTasks, setRecentTasks] = useState<AgentTask[]>([]);
  useEffect(() => {
    // The Reader may link directly to this *exact* Snapshot. Loading device
    // status is a GET only: no pairing, billing or task is created on visit.
    const params = new URLSearchParams(window.location.search);
    const linkedRepo = params.get("repo_id") ?? "";
    if (!REPO.test(linkedRepo) || !REPO.test(params.get("review_id") ?? "")
      || !REPO.test(params.get("revision_id") ?? "")) return;
    let active = true;
    setBusy("loading");
    void requestBrowserJson<Listed>("/v1/review-devices?repo_id=" + encodeURIComponent(linkedRepo))
      .then((answer) => {
        if (!active) return;
        setDevices(answer.devices);
        setTruncated(answer.truncated === true);
        setRepo(linkedRepo);
      })
      .catch((reason: unknown) => { if (active) setError(failure(reason)); })
      .finally(() => { if (active) setBusy(""); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!code) return;
    const remaining = Math.max(0, code.expires_at * 1000 - Date.now());
    if (!remaining) { setCode(null); return; }
    const timer = window.setTimeout(() => setCode(null), remaining);
    return () => window.clearTimeout(timer);
  }, [code]);

  const discoverRepos = async (): Promise<void> => {
    if (reposLoading) return;
    setReposLoading(true);
    setError("");
    try {
      const result = await requestBrowserJson<AuthorizedRepos>("/v1/review-devices/repositories");
      setAvailableRepos(result.repositories.map(({ repo_id }) => repo_id));
      setReposLoaded(true);
    } catch (cause) { setError(failure(cause)); }
    finally { setReposLoading(false); }
  };

  const refresh = async (id: string): Promise<void> => {
    const answer = await requestBrowserJson<Listed>("/v1/review-devices?repo_id=" + encodeURIComponent(id));
    setDevices(answer.devices);
    setTruncated(answer.truncated === true);
  };
  const selectRepo = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    setRepo(""); setCode(null); setDevices([]); setTask(null); setRecentTasks([]); setConfirmId(""); setError("");
    const id = repoInput.trim();
    if (!REPO.test(id)) { setError("Enter a valid Hosted Review repository ID."); return; }
    setBusy("loading");
    try { await refresh(id); setRepo(id); }
    catch (reason) { setError(failure(reason)); }
    finally { setBusy(""); }
  };
  const pair = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    if (!repo || busy) return;
    setBusy("pair"); setCode(null); setError("");
    try {
      const classes = ai ? ["runtime_validation", "review_intelligence"] : ["runtime_validation"];
      const received = await requestBrowserJson<Pairing>("/v1/review-devices/pair", {
        method: "POST",
        body: JSON.stringify({
          repo_id: repo, kind, execution_classes: classes, credential_lifetime_s: days * 86400,
        }),
      });
      if (received.repo_id !== repo || received.expires_at <= Date.now() / 1000) throw Error("Pairing response is stale or for another repository.");
      setCode(received);
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(""); }
  };
  const loadTasks = async (): Promise<void> => {
    if (!repo || busy) return;
    setBusy("task-list"); setError("");
    try {
      const result = await requestBrowserJson<{ tasks: AgentTask[] }>(
        "/v1/review-agent-tasks?repo_id=" + encodeURIComponent(repo),
      );
      setRecentTasks(result.tasks);
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(""); }
  };
  const createTask = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    if (!repo || busy || !sourceApproved || !REPO.test(taskReviewId) || !REPO.test(taskRevisionId)) {
      setError("Select a valid Review and current revision, then explicitly approve external source disclosure.");
      return;
    }
    setBusy("task"); setError("");
    try {
      const created = await requestBrowserJson<AgentTask>("/v1/review-agent-tasks", {
        method: "POST",
        body: JSON.stringify({
          repo_id: repo, review_id: taskReviewId, revision_id: taskRevisionId,
          task_kind: taskKind, request_id: crypto.randomUUID(),
          external_source_approved: true,
        }),
      });
      if (!created.job_id || created.review_id !== taskReviewId || created.revision_id !== taskRevisionId) {
        throw Error("Hosted Review returned a task outside the approved Snapshot.");
      }
      setTask(created);
      setSourceApproved(false);
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(""); }
  };
  const refreshTask = async (): Promise<void> => {
    if (!repo || !task || busy) return;
    setBusy("task-refresh"); setError("");
    try {
      const answer = await requestBrowserJson<AgentTask>(
        "/v1/review-agent-tasks/" + encodeURIComponent(task.job_id) + "?repo_id=" + encodeURIComponent(repo),
      );
      if (answer.job_id !== task.job_id) throw Error("Review task identity changed.");
      setTask(answer);
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(""); }
  };
  const cancelTask = async (): Promise<void> => {
    if (!repo || !task || busy) return;
    setBusy("task-cancel"); setError("");
    try {
      const answer = await requestBrowserJson<AgentTask>(
        "/v1/review-agent-tasks/" + encodeURIComponent(task.job_id) + "/cancel",
        { method: "POST", body: JSON.stringify({ repo_id: repo }) },
      );
      if (answer.job_id !== task.job_id) throw Error("Review task identity changed.");
      setTask(answer);
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(""); }
  };
  const revoke = async (id: string): Promise<void> => {
    if (!repo || busy || confirmId !== id) return;
    setBusy("revoke"); setError("");
    try {
      const answer = await requestBrowserJson<{ revoked: boolean }>("/v1/review-devices/revoke", {
        method: "POST", body: JSON.stringify({ repo_id: repo, worker_id: id }),
      });
      if (!answer.revoked) throw Error("Machine was already revoked or is outside your permissions.");
      await refresh(repo);
      setConfirmId("");
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(""); }
  };
  return (
    <PageFrame className="space-y-5">
      <header className="max-w-3xl space-y-2">
        <h2 className="text-heading font-semibold text-foreground">Review connections</h2>
        <p className="text-body text-muted-foreground">
          Pair a laptop, VPS or agent sandbox once. The machine connects outbound to Hosted Review,
          independently of LemonCrow Runtime and MCP.
        </p>
        <p className="text-caption text-muted-foreground">
          Connection does not imply that an executor is online or that checks are independently verified.
          Those capabilities require separately qualified workers.
        </p>
        <p className="text-caption text-muted-foreground">
          Not available yet: unattended agent execution on a connected machine, external sandbox
          providers as a supported execution target, and model tasks billed by LemonCrow.
        </p>
      </header>
      <div className="max-w-3xl space-y-2">
        <Button type="button" size="xs" variant="outline" disabled={reposLoading}
          onClick={() => { void discoverRepos(); }}>
          {reposLoading ? "Loading authorized repositories…" : "Browse my repositories"}
        </Button>
        {reposLoaded && (
          availableRepos.length ? (
            <label className="block max-w-sm space-y-1 text-caption text-foreground">
              <span className="block">Authorized repositories</span>
              <Select value={availableRepos.includes(repoInput) ? repoInput : "__choose__"}
                onValueChange={(value) => {
                  setRepoInput(value === "__choose__" ? "" : value);
                  setRepo(""); setCode(null); setDevices([]); setTask(null); setRecentTasks([]);
                }}>
                <SelectItem value="__choose__">Choose a repository</SelectItem>
                {availableRepos.map((id) => <SelectItem key={id} value={id}>{id}</SelectItem>)}
              </Select>
            </label>
          ) : <p className="text-caption text-muted-foreground">No repositories grant Review management access.</p>
        )}
      </div>
      <form aria-label="Select Review repository" className="max-w-3xl flex flex-wrap items-end gap-2"
        onSubmit={(event) => { void selectRepo(event); }}>
        <label className="min-w-52 flex-1 space-y-1 text-label text-foreground">
          <span className="block">Repository ID</span>
          <Input value={repoInput} onChange={(event) => setRepoInput(event.target.value)}
            maxLength={128} placeholder="Repository ID from Hosted Review" />
        </label>
        <Button type="submit" disabled={!!busy}>Load connections</Button>
      </form>
      {error && <p role="alert" className="text-caption text-destructive">{error}</p>}
      {repo && <section className="max-w-3xl space-y-4" aria-label="Connections for selected repository">
        <Card className="space-y-3 p-4">
          <h3 className="font-semibold text-foreground">Connect a machine</h3>
          <form className="space-y-3" onSubmit={(event) => { void pair(event); }}>
            <div className="flex flex-wrap gap-4">
              <label className="space-y-1 text-label text-foreground">
                <span className="block">Machine type</span>
                <Select id="review-device-kind" value={kind} onValueChange={(value) => {
                  const next = value as Kind;
                  setKind(next); setDays(next === "sandbox" ? 1 : 7); setCode(null);
                }}>
                  {Object.entries(names).map(([value, label]) =>
                    <SelectItem key={value} value={value}>{label}</SelectItem>)}
                </Select>
              </label>
              <label className="space-y-1 text-label text-foreground">
                <span className="block">Credential lifetime</span>
                <Select id="review-device-lifetime" value={String(days)} onValueChange={(value) => {
                  setDays(Number(value)); setCode(null);
                }}>
                  {(kind === "sandbox" ? [1] : [1, 7, 30]).map((n) =>
                    <SelectItem key={n} value={String(n)}>{n === 1 ? "1 day" : String(n) + " days"}</SelectItem>)}
                </Select>
              </label>
            </div>
            <label className="flex items-center gap-2 text-caption text-foreground">
              <Checkbox checked={ai} onChange={(event) => { setAi(event.target.checked); setCode(null); }} />
              Allow budgeted Review AI tasks (requires a separately configured model route)
            </label>
            <Button type="submit" disabled={!!busy}>{busy === "pair" ? "Creating…" : "Create one-time code"}</Button>
          </form>
          {code && <div className="space-y-2 rounded-md border border-border p-3" role="status">
            <p className="text-label font-medium text-foreground">
              One-time pairing code · expires {date(code.expires_at)}
            </p>
            <Input aria-label="One-time pairing code" readOnly value={code.pairing_code}
              className="w-full rounded border border-border bg-background p-2 font-mono text-caption text-foreground" />
            <p className="text-caption text-muted-foreground">
              On the machine, run <code>lcr connect</code> and paste the code. Then run <code>lcr watch</code> to capture changes and report paired machine presence. For presence without capture, use <code>lcr device online</code>. It works only once,
              and is cleared when this page closes or the code expires.
            </p>
            <Button type="button" size="xs" onClick={() => setCode(null)}>Hide code</Button>
          </div>}
        </Card>
        <Card className="space-y-3 p-4">
          <h3 className="font-semibold text-foreground">Run one AI investigation</h3>
          <p className="text-caption text-muted-foreground">
            Queue an exact Review Snapshot for a paired machine. A local operator must also approve the model route,
            source disclosure and spending. This never runs automatically or marks a check as verified.
          </p>
          <form aria-label="Create Review investigation" className="space-y-3" onSubmit={(event) => { void createTask(event); }}>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-1 text-label text-foreground">
                <span className="block">Review ID</span>
                <Input value={taskReviewId} maxLength={128} onChange={(event) => setTaskReviewId(event.target.value)} />
              </label>
              <label className="space-y-1 text-label text-foreground">
                <span className="block">Current revision ID</span>
                <Input value={taskRevisionId} maxLength={128} onChange={(event) => setTaskRevisionId(event.target.value)} />
              </label>
            </div>
            <label className="block max-w-xs space-y-1 text-label text-foreground">
              <span className="block">Task</span>
              <Select value={taskKind} onValueChange={(value) => setTaskKind(value as "investigate" | "propose_fix")}>
                <SelectItem value="investigate">Investigate possible regressions</SelectItem>
                <SelectItem value="propose_fix">Suggest a fix (never apply)</SelectItem>
              </Select>
            </label>
            <label className="flex items-center gap-2 text-caption text-foreground">
              <Checkbox checked={sourceApproved} onChange={(event) => setSourceApproved(event.target.checked)} />
              I approve sending bounded, policy-permitted Snapshot excerpts to an external AI provider.
            </label>
            <Button type="submit" disabled={!!busy || !sourceApproved}>Create approved task</Button>
          </form>
          <div className="space-y-2">
            <Button type="button" size="xs" variant="outline" disabled={!!busy} onClick={() => { void loadTasks(); }}>
              Load previous AI tasks
            </Button>
            {recentTasks.length > 0 && <ul className="max-h-52 space-y-1 overflow-auto" aria-label="Previous AI tasks">
              {recentTasks.map((item) => <li key={item.job_id}>
                <Button type="button" size="xs" variant="ghost" disabled={!!busy}
                  onClick={() => { setTask(item); setTaskReviewId(item.review_id); setTaskRevisionId(item.revision_id); }}>
                  {item.task_kind} · {item.job_id} · {item.state}
                </Button>
              </li>)}
            </ul>}
          </div>
          {task && <div className="space-y-2 rounded-md border border-border p-3" aria-label="AI investigation status">
            <p className="font-medium text-label text-foreground">Task {task.job_id} · {task.state}</p>
            <p className="text-caption text-muted-foreground">Exact revision {task.revision_id}. Publication: {task.publication_state}.</p>
            <p className="text-caption text-muted-foreground">
              On your paired machine, use <code>lcr task run {task.job_id} --help</code> to see the
              required provider, attested route, price caps and explicit approval options. The API key is entered locally.
              To see pending AI tasks without running them, use <code>lcr pending --watch --class review_intelligence</code>.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button type="button" size="xs" variant="outline" disabled={!!busy} onClick={() => { void refreshTask(); }}>Refresh task</Button>
              {(task.state === "queued" || task.state === "running") &&
                <Button type="button" size="xs" variant="outline" disabled={!!busy} onClick={() => { void cancelTask(); }}>Cancel task</Button>}
            </div>
            {task.observation && <div className="space-y-2">
              <p className="text-label font-medium text-foreground">Author-agent observation · not independently verified</p>
              <p className="text-caption text-muted-foreground">
                {task.observation.included_source_paths.length} of {task.observation.source_files_available} available source files ·
                {" "}{task.observation.input_tokens} input tokens · {task.observation.output_tokens} output tokens ·
                {" "}estimated ${(task.observation.estimated_cost_micros / 1_000_000).toFixed(4)} (not billed cost)
              </p>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-caption text-foreground">{task.observation.text}</pre>
            </div>}
          </div>}
        </Card>
        <Card className="space-y-3 p-4">
          <div className="flex items-center justify-between gap-3">
            <h3 className="font-semibold text-foreground">Paired machines</h3>
            <Button type="button" size="xs" disabled={!!busy} onClick={() => {
              setError("");
              void refresh(repo).catch((reason: unknown) => setError(failure(reason)));
            }}>Refresh</Button>
          </div>
          {!devices.length ? <p className="text-caption text-muted-foreground">No machines found.</p> : (
            <ul className="divide-y divide-border">
              {devices.map((device) => {
                const revoked = device.revoked_at != null;
                const expired = device.expires_at <= Date.now() / 1000;
                return <li key={device.worker_id} className="space-y-1 py-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <div className="text-label font-medium text-foreground">
                        {names[device.device_kind] || "Machine"} · {device.worker_id}
                      </div>
                      <p className="text-caption text-muted-foreground">
                        {revoked ? "Revoked" : expired ? "Expired" : "Credential active · online status unknown"} ·
                        {" "}Expires {date(device.expires_at)}
                      </p>
                      <p className="text-caption text-muted-foreground">
                        {device.last_seen_at
                          ? "Last machine check-in " + date(device.last_seen_at) + " (not proof of active execution)"
                          : "No machine check-ins recorded"}
                      </p>
                    </div>
                    {!revoked && <Button type="button" size="xs"
                      variant={confirmId === device.worker_id ? "destructive" : "outline"}
                      disabled={!!busy} onClick={() => {
                        if (confirmId === device.worker_id) void revoke(device.worker_id);
                        else setConfirmId(device.worker_id);
                      }}>
                      {confirmId === device.worker_id ? "Confirm revoke" : "Revoke access"}
                    </Button>}
                  </div>
                  {confirmId === device.worker_id && !revoked && <div className="text-caption text-muted-foreground">
                    Revocation immediately blocks new authenticated requests.
                    <Button type="button" variant="ghost" size="xs" onClick={() => setConfirmId("")}>Cancel</Button>
                  </div>}
                </li>;
              })}
            </ul>
          )}
          {truncated && !!devices.length && <p className="text-caption text-muted-foreground">
            Showing the first {devices.length} machines, active ones first. Older revoked or expired machines are not listed.
          </p>}
        </Card>
      </section>}
    </PageFrame>
  );
}
