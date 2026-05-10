import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Watchdogs from "./Watchdogs";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Watchdogs page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads backend watchdog config and saves profile changes", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (
          url.includes("/api/watchdogs/config") &&
          (!init || !init.method || init.method === "GET")
        ) {
          return Promise.resolve(
            jsonResponse({
              active_profile: "coding",
              runtime_wired: true,
              config_path: "/tmp/.atelier/watchdog_profiles.json",
              library: [
                {
                  key: "repeated_command_failure",
                  title: "Repeated command failure",
                  description:
                    "Same command or test fails repeatedly with the same error signature.",
                  default_weight: 0.3,
                  severity: "high",
                },
              ],
              profiles: [
                {
                  id: "coding",
                  label: "Coding",
                  description:
                    "Prioritize command retries, verification gaps, and risky edits.",
                  weights: { repeated_command_failure: 0.3 },
                },
                {
                  id: "qa",
                  label: "QA",
                  description:
                    "Push verification and command stability to the top.",
                  weights: { repeated_command_failure: 0.28 },
                },
              ],
            })
          );
        }
        if (url.includes("/api/watchdogs/config") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body));
          return Promise.resolve(
            jsonResponse({
              active_profile: payload.active_profile,
              runtime_wired: true,
              config_path: "/tmp/.atelier/watchdog_profiles.json",
              library: [
                {
                  key: "repeated_command_failure",
                  title: "Repeated command failure",
                  description:
                    "Same command or test fails repeatedly with the same error signature.",
                  default_weight: 0.3,
                  severity: "high",
                },
              ],
              profiles: [
                {
                  id: "coding",
                  label: "Coding",
                  description:
                    "Prioritize command retries, verification gaps, and risky edits.",
                  weights: { repeated_command_failure: 0.3 },
                },
                {
                  id: "qa",
                  label: "QA",
                  description:
                    "Push verification and command stability to the top.",
                  weights: { repeated_command_failure: 0.28 },
                },
              ],
            })
          );
        }
        if (url.includes("/api/traces")) {
          return Promise.resolve(
            jsonResponse([
              {
                id: "t1",
                agent: "copilot",
                task: "task",
                status: "completed",
                files_touched: [],
                tools_called: [],
                commands_run: [],
                errors_seen: [],
                repeated_failures: [{ signature: "sig", count: 2 }],
                validation_results: [{ name: "lint", passed: false }],
                created_at: "2026-05-08T00:00:00Z",
              },
            ])
          );
        }
        if (url.includes("/api/plans")) {
          return Promise.resolve(
            jsonResponse([
              {
                trace_id: "t1",
                domain: "coding",
                task: "task",
                status: "blocked",
                plan_checks: [],
              },
            ])
          );
        }
        if (url.includes("/api/clusters")) {
          return Promise.resolve(
            jsonResponse([
              {
                id: "c1",
                domain: "coding",
                fingerprint: "f",
                trace_ids: [],
                sample_errors: [],
                suggested_block_title: "",
                suggested_rubric_check: "",
                suggested_eval_case: "",
                suggested_prompt: "",
                severity: "medium",
              },
            ])
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(<Watchdogs />);

    expect(await screen.findByText("Watchdog profile")).toBeInTheDocument();
    expect(await screen.findByText("saved to runtime")).toBeInTheDocument();

    const select = await screen.findByRole("combobox", {
      name: "Select watchdog profile",
    });
    await user.selectOptions(select, "qa");

    expect(select).toHaveValue("qa");
    const saveButton = screen.getByRole("button", { name: "Save" });
    expect(saveButton).toBeEnabled();

    await user.click(saveButton);

    expect(await screen.findByText("saved")).toBeInTheDocument();
  });
});
