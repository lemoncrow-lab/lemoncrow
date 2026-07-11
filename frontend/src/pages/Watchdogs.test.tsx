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
              config_path: "/tmp/.lemoncrow/watchdog_profiles.json",
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
              config_path: "/tmp/.lemoncrow/watchdog_profiles.json",
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
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(<Watchdogs />);

    expect(await screen.findByText("Watchdog profile")).toBeInTheDocument();
    expect(await screen.findByText("saved to runtime")).toBeInTheDocument();
    // Guardrail pressure / observed sessions (derived from api.traces /
    // api.plans / api.clusters) were removed — Watchdogs only loads config.
    expect(screen.queryByText("Guardrail pressure")).not.toBeInTheDocument();
    expect(screen.queryByText("Observed sessions")).not.toBeInTheDocument();

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
