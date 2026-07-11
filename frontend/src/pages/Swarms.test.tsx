import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Swarms from "./Swarms";

const NEW_SPEC_CHOICE = "__new_program_md__";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installSwarmFetchMock(launchBodies: unknown[]) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);

      if (url.includes("/api/v1/swarm/launch/options")) {
        return Promise.resolve(
          jsonResponse({
            project_roots: [
              {
                path: "/workspace/project",
                label: "project",
                full_path: "/workspace/project",
                has_program_md: true,
              },
            ],
            selected_project_root: "/workspace/project",
            files: [
              { path: "PROGRAM.md", is_default: true, exists: true },
              { path: "docs/task.md", is_default: false, exists: true },
            ],
            selected_spec_path: "PROGRAM.md",
            spec_document: {
              path: "PROGRAM.md",
              content: "Captured prompt line 1\nCaptured prompt line 2\n",
              exists: true,
              is_default: true,
            },
            providers: [
              {
                id: "cli",
                label: "CLI runner",
                supported: true,
                reason: null,
                model_placeholder: null,
                credential_hint: null,
              },
              {
                id: "openai",
                label: "OpenAI API",
                supported: true,
                reason: null,
                model_placeholder: "gpt-4o-mini",
                credential_hint: "Uses server env only.",
              },
            ],
            runners: [
              {
                id: "claude",
                label: "Claude Code",
                supports_model: true,
                model_placeholder: "claude-sonnet-4.5",
                options_help:
                  "Extra CLI flags appended before the generated swarm prompt.",
              },
            ],
            defaults: {
              provider: "cli",
              runner: "claude",
              runs: 3,
              continuous: true,
              max_waves: 5,
              keep_worktrees: true,
              effort: "high",
            },
            notes: {
              default_spec: "PROGRAM.md",
              default_spec_missing: false,
              effort_behavior: "Effort is recorded in swarm metadata today.",
              provider_credentials: "Provider credentials stay in env.",
            },
          })
        );
      }

      if (url.endsWith("/api/v1/swarm/runs") && init?.method === "POST") {
        launchBodies.push(JSON.parse(String(init.body)));
        return Promise.resolve(
          jsonResponse({
            run_id: "swarm-123",
            status: "running",
            state_path: "/workspace/.lemoncrow/swarm/state.json",
            coordinator_pid: 4321,
            log_path: "/workspace/.lemoncrow/swarm/coordinator.log",
          })
        );
      }

      if (url.endsWith("/api/v1/swarm/runs")) {
        return Promise.resolve(
          jsonResponse([
            {
              run_id: "swarm-123",
              status: "running",
              mode: "continuous",
              repo_root: "/workspace/project",
              repo_label: "project",
              runner_name: "openai",
              runner_model: "gpt-4o-mini",
              launch_provider: "openai",
              launch_effort: "high",
              current_wave: 1,
              max_runs: 3,
              planned_runs: 2,
              planning_mode: "bounded",
              accepted_child_ids: [],
              primary_winner_child_id: null,
              failed_children: [],
              running_children: [],
              spec_title: "Prompt title",
              spec_excerpt: "Captured prompt line 1",
              spec_resolution: "default",
              used_program_md: true,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:05:00Z",
            },
          ])
        );
      }

      if (url.includes("/api/v1/swarm/runs/swarm-123")) {
        return Promise.resolve(
          jsonResponse({
            run: {
              run_id: "swarm-123",
              status: "running",
              mode: "continuous",
              repo_root: "/workspace/project",
              runner_name: "openai",
              runner_model: "gpt-4o-mini",
              launch_provider: "openai",
              launch_effort: "high",
              base_ref: "HEAD",
              base_snapshot_ref: "base-snapshot",
              integration_base_ref: "accepted-head",
              current_wave: 1,
              max_runs: 3,
              runs: 3,
              planning_mode: "bounded",
              accepted_child_ids: [],
              primary_winner_child_id: null,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:05:00Z",
              waves: [],
              children: [],
              accepted_commits: [],
            },
            spec: {
              source_path: "PROGRAM.md",
              copied_path:
                "/workspace/.lemoncrow/swarm/runs/swarm-123/PROGRAM.md",
              resolution: "default",
              used_program_md: true,
              title: "Prompt title",
              excerpt: "Captured prompt line 1",
              truncated: false,
              content: "Captured prompt line 1\nCaptured prompt line 2\n",
            },
            export: {
              run_id: "swarm-123",
              status: "running",
              mode: "continuous",
              runner_name: "openai",
              runner_model: "gpt-4o-mini",
              base_ref: "HEAD",
              base_snapshot_ref: "base-snapshot",
              integration_base_ref: "accepted-head",
              artifact_root: "/workspace/.lemoncrow/swarm/artifacts",
              base_snapshot_artifact: null,
              accepted_child_ids: [],
              accepted_commits: [],
              waves: [],
              artifacts: [],
              transplant_commands: [],
            },
            apply: {
              run_id: "swarm-123",
              wave_index: null,
              child_id: null,
              base_snapshot_ref: "base-snapshot",
              integration_base_ref: "accepted-head",
              selected_commits: [],
              commands: [],
              artifacts: [],
            },
          })
        );
      }

      if (url.includes("/logs")) {
        return Promise.resolve(
          jsonResponse({
            run_id: "swarm-123",
            child_id: null,
            stderr: false,
            tail: 80,
            content: "child heartbeat",
          })
        );
      }

      if (url.endsWith("/api/v1/workflow/current")) {
        return Promise.resolve(new Response("not found", { status: 404 }));
      }

      return Promise.resolve(new Response("not found", { status: 404 }));
    });
}

describe("Swarms page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("launches an existing PROGRAM.md without rewriting it by default", async () => {
    const launchBodies: unknown[] = [];
    installSwarmFetchMock(launchBodies);
    const { container } = render(
      <MemoryRouter>
        <Swarms />
      </MemoryRouter>
    );

    expect(
      screen.getByRole("button", { name: /^launch swarms$/i })
    ).toBeInTheDocument();
    expect(screen.queryByText(/project directory/i)).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: /^launch swarms$/i })
    );
    expect(
      await screen.findByRole("heading", { name: /launch swarms/i })
    ).toBeInTheDocument();
    expect(await screen.findByText(/project directory/i)).toBeInTheDocument();
    expect(
      await screen.findByRole("option", { name: /\/workspace\/project/i })
    ).toBeInTheDocument();
    expect(screen.getByText(/swarms working directory:/i)).toBeInTheDocument();
    expect(
      screen.getByText(/project-swarm-worktrees\/<generated-run-id>\//i)
    ).toBeInTheDocument();
    expect(container.querySelector("#swarm-project-root")).toBeNull();
    expect(screen.getByRole("button", { name: /^edit$/i })).toBeInTheDocument();
    expect(container.querySelector("#swarm-spec-content")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: /^launch swarms$/i })
    );

    await waitFor(() => expect(launchBodies).toHaveLength(1));
    expect(launchBodies[0]).toMatchObject({
      project_root: "/workspace/project",
      spec_path: "PROGRAM.md",
      spec_mode: "existing",
      spec_content: null,
      provider: "cli",
      runner: "claude",
      max_waves: 5,
    });
  });

  it("offers + New file (PROGRAM.md) and launches an OpenAI-backed swarm run", async () => {
    const launchBodies: unknown[] = [];
    installSwarmFetchMock(launchBodies);
    const { container } = render(
      <MemoryRouter>
        <Swarms />
      </MemoryRouter>
    );

    await userEvent.click(
      screen.getByRole("button", { name: /^launch swarms$/i })
    );
    expect(
      await screen.findByRole("heading", { name: /launch swarms/i })
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("option", {
        name: /\+ new file \(program\.md\)/i,
      })
    ).toBeInTheDocument();

    const specSelect = container.querySelector("#swarm-spec-select");
    expect(specSelect).not.toBeNull();
    if (!specSelect) {
      throw new Error("expected swarm spec select");
    }
    await userEvent.selectOptions(specSelect, NEW_SPEC_CHOICE);

    const textarea = await waitFor(() =>
      container.querySelector("#swarm-spec-content")
    );
    expect(textarea).not.toBeNull();
    if (!(textarea instanceof HTMLTextAreaElement)) {
      throw new Error("expected swarm spec textarea");
    }
    await userEvent.type(textarea, "New acceptance criteria\n");

    const providerSelect = container.querySelector("#swarm-provider");
    expect(providerSelect).not.toBeNull();
    if (!providerSelect) {
      throw new Error("expected provider select");
    }
    await userEvent.selectOptions(providerSelect, "openai");

    expect(container.querySelector("#swarm-runner")).toBeNull();

    const modelInput = container.querySelector("#swarm-runner-model");
    expect(modelInput).not.toBeNull();
    if (!(modelInput instanceof HTMLInputElement)) {
      throw new Error("expected model input");
    }
    await userEvent.type(modelInput, "gpt-4o-mini");
    const apiKeyInput = container.querySelector("#swarm-provider-api-key");
    expect(apiKeyInput).not.toBeNull();
    if (!(apiKeyInput instanceof HTMLInputElement)) {
      throw new Error("expected provider api key input");
    }
    await userEvent.type(apiKeyInput, "sk-test-key");
    const baseUrlInput = container.querySelector("#swarm-provider-base-url");
    expect(baseUrlInput).not.toBeNull();
    if (!(baseUrlInput instanceof HTMLInputElement)) {
      throw new Error("expected provider base url input");
    }
    await userEvent.type(baseUrlInput, "https://openrouter.example/v1");
    await userEvent.click(
      screen.getByRole("button", { name: /^launch swarms$/i })
    );

    await waitFor(() => expect(launchBodies).toHaveLength(1));
    expect(launchBodies[0]).toMatchObject({
      project_root: "/workspace/project",
      spec_path: "PROGRAM.md",
      spec_mode: "inline",
      spec_content: expect.stringContaining("New acceptance criteria"),
      provider: "openai",
      model: "gpt-4o-mini",
      runner: null,
      runner_model: null,
      runner_options: "",
      runs: 3,
      max_waves: 5,
      effort: "high",
      provider_api_key: "sk-test-key",
      provider_base_url: "https://openrouter.example/v1",
    });
    expect(providerSelect).toHaveAttribute("title", "Uses server env only.");
  });

  it("refreshes swarm activity without clearing the launch draft", async () => {
    installSwarmFetchMock([]);
    const { container } = render(
      <MemoryRouter>
        <Swarms />
      </MemoryRouter>
    );

    await userEvent.click(
      screen.getByRole("button", { name: /^launch swarms$/i })
    );
    const specSelect = await waitFor(() =>
      container.querySelector("#swarm-spec-select")
    );
    expect(specSelect).not.toBeNull();
    if (!(specSelect instanceof HTMLSelectElement)) {
      throw new Error("expected swarm spec select");
    }
    await userEvent.selectOptions(specSelect, NEW_SPEC_CHOICE);

    const textarea = await waitFor(() =>
      container.querySelector("#swarm-spec-content")
    );
    expect(textarea).not.toBeNull();
    if (!(textarea instanceof HTMLTextAreaElement)) {
      throw new Error("expected swarm spec textarea");
    }
    await userEvent.type(textarea, "Keep this draft");

    const providerSelect = container.querySelector("#swarm-provider");
    expect(providerSelect).not.toBeNull();
    if (!(providerSelect instanceof HTMLSelectElement)) {
      throw new Error("expected provider select");
    }
    await userEvent.selectOptions(providerSelect, "openai");

    const apiKeyInput = await waitFor(() =>
      container.querySelector("#swarm-provider-api-key")
    );
    expect(apiKeyInput).not.toBeNull();
    if (!(apiKeyInput instanceof HTMLInputElement)) {
      throw new Error("expected provider api key input");
    }
    await userEvent.type(apiKeyInput, "draft-key");

    await userEvent.click(screen.getByRole("button", { name: /^refresh$/i }));

    expect(textarea.value).toContain("Keep this draft");
    expect(apiKeyInput.value).toBe("draft-key");
    expect(
      screen.getByRole("heading", { name: /launch swarms/i })
    ).toBeInTheDocument();
  });

  it("folds the Workflow runtime snapshot into a collapsed section", async () => {
    installSwarmFetchMock([]);
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/swarm/launch/options")) {
          return Promise.resolve(
            jsonResponse({
              project_roots: [],
              selected_project_root: "",
              files: [],
              providers: [],
              runners: [],
              defaults: {
                provider: "cli",
                runner: "claude",
                runs: 3,
                continuous: true,
                max_waves: 5,
                keep_worktrees: true,
                effort: "high",
              },
              notes: {
                default_spec: "PROGRAM.md",
                default_spec_missing: true,
                effort_behavior: "",
              },
            })
          );
        }
        if (url.endsWith("/api/v1/swarm/runs")) {
          return Promise.resolve(jsonResponse([]));
        }
        if (url.endsWith("/api/v1/workflow/current")) {
          return Promise.resolve(
            jsonResponse({
              workspace_root: "/workspace/project",
              summary: {
                run_id: "wf-123",
                workflow_id: "owned-execute-review-loop",
                status: "awaiting_review",
                current_step: "execute",
                session_phase: "review",
                step_count: 2,
                completed_steps: 1,
                paused_step_id: "execute",
                failed_step_id: "",
                pause_reason: "",
                stop_reason: "",
                review_decision: "pending",
                created_at: "2026-01-01T00:00:00Z",
                updated_at: "2026-01-01T00:05:00Z",
              },
              workflow: { workflow_id: "owned-execute-review-loop", steps: [] },
              route: { mode: "native" },
              current_task: {},
              plan_review: {},
              task_outputs: {},
              step_order: [],
              available_actions: {
                can_pause: true,
                can_resume: true,
                can_stop: true,
                resume_requires_host_call: true,
                pause_is_snapshot_only: true,
                stop_is_snapshot_only: true,
              },
              control_payloads: { status: { op: "status", run_id: "wf-123" } },
              notes: {
                snapshot_kind: "workspace-current",
                live_control: false,
                summary:
                  "Workflow state is a workspace-local persisted snapshot.",
              },
            })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(
      <MemoryRouter>
        <Swarms />
      </MemoryRouter>
    );

    expect(screen.getByText("Workflow (advanced)")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Workflow" })
    ).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("Workflow (advanced)"));

    expect(
      await screen.findByRole("heading", { name: "Workflow" })
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(/owned-execute-review-loop/i)[0]
    ).toBeInTheDocument();
  });
});
