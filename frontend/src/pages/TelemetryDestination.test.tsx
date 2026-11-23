import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TelemetryDestinationPanel from "./TelemetryDestination";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function config(overrides: Record<string, unknown> = {}) {
  return {
    remote_enabled: true,
    lexical_frustration_enabled: false,
    destination: "lemoncrow",
    destination_setting: "auto",
    destination_source: "default",
    destination_url: "https://lemoncrow.com",
    destination_error: "",
    endpoint: "",
    token_set: false,
    server_url: "http://127.0.0.1:7420",
    server_hosted: false,
    managed: false,
    policy_path: "",
    ...overrides,
  };
}

describe("TelemetryDestinationPanel", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("saves a custom host with its token", async () => {
    const user = userEvent.setup();
    const posts: unknown[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          posts.push(JSON.parse(String(init.body)));
          return Promise.resolve(
            jsonResponse(
              config({
                destination: "custom",
                destination_setting: "custom",
                destination_source: "user",
                destination_url: "https://telemetry.corp.test",
                endpoint: "https://telemetry.corp.test",
                token_set: true,
              })
            )
          );
        }
        expect(String(input)).toBe("/api/telemetry/config");
        return Promise.resolve(jsonResponse(config()));
      }
    );

    render(<TelemetryDestinationPanel />);
    expect(
      await screen.findByText(
        "Daily anonymous rollups go to https://lemoncrow.com."
      )
    ).toBeTruthy();

    await user.click(
      screen.getByRole("combobox", { name: "Telemetry destination" })
    );
    await user.click(screen.getByRole("option", { name: "Custom host" }));
    await user.type(
      screen.getByLabelText("Custom telemetry URL"),
      "https://telemetry.corp.test"
    );
    await user.type(screen.getByLabelText("Custom telemetry token"), "s3cret");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(posts).toEqual([
        {
          destination: "custom",
          endpoint: "https://telemetry.corp.test",
          token: "s3cret",
        },
      ])
    );
    expect(
      await screen.findByText(
        "Daily anonymous rollups go to https://telemetry.corp.test."
      )
    ).toBeTruthy();
  });

  it("is read-only when an administrator policy manages the destination", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        config({
          destination: "off",
          destination_setting: "auto",
          destination_source: "policy",
          destination_url: "",
          managed: true,
          policy_path: "/etc/lemoncrow/telemetry.toml",
        })
      )
    );

    render(<TelemetryDestinationPanel />);

    expect(
      await screen.findByText("Managed by your organization")
    ).toBeTruthy();
    expect(screen.getByText("Nothing leaves this machine.")).toBeTruthy();
    expect(
      screen.getByText("Set by /etc/lemoncrow/telemetry.toml.")
    ).toBeTruthy();
    const off = screen.getByRole("radio", { name: "Off" }) as HTMLButtonElement;
    expect(off.getAttribute("aria-checked")).toBe("true");
    expect(off.disabled).toBe(true);
    expect((screen.getByRole("radio", { name: "On" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("combobox", { name: "Telemetry destination" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("turns sharing off and back on with one switch", async () => {
    const user = userEvent.setup();
    const posts: unknown[] = [];
    let current = config();
    vi.spyOn(globalThis, "fetch").mockImplementation((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { destination: string };
        posts.push(body);
        current = body.destination === "off"
          ? config({ destination: "off", destination_setting: "off", destination_source: "user", destination_url: "" })
          : config({ destination_source: "user" });
      }
      return Promise.resolve(jsonResponse(current));
    });

    render(<TelemetryDestinationPanel />);
    expect((await screen.findByRole("radio", { name: "On" })).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("combobox", { name: "Telemetry destination" })).toBeTruthy();

    await user.click(screen.getByRole("radio", { name: "Off" }));
    expect(await screen.findByText("Nothing leaves this machine.")).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Telemetry destination" })).toBeNull();

    await user.click(screen.getByRole("radio", { name: "On" }));
    expect(await screen.findByText("Daily anonymous rollups go to https://lemoncrow.com.")).toBeTruthy();
    expect(posts).toEqual([{ destination: "off" }, { destination: "auto" }]);
  });

  it("cannot turn sharing on while DO_NOT_TRACK or LEMONCROW_TELEMETRY=off is set", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(config({ destination: "off", destination_source: "env", destination_url: "" }))
    );
    render(<TelemetryDestinationPanel />);
    expect(await screen.findByText("Nothing leaves this machine (DO_NOT_TRACK or LEMONCROW_TELEMETRY=off is set).")).toBeTruthy();
    expect((screen.getByRole("radio", { name: "On" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the server's reason when a save is refused", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        Promise.resolve(
          init?.method === "POST"
            ? jsonResponse(
                { detail: "endpoint must use HTTPS outside loopback" },
                400
              )
            : jsonResponse(config())
        )
    );

    render(<TelemetryDestinationPanel />);
    await user.click(
      await screen.findByRole("combobox", { name: "Telemetry destination" })
    );
    await user.click(screen.getByRole("option", { name: "Custom host" }));
    await user.type(
      screen.getByLabelText("Custom telemetry URL"),
      "http://telemetry.corp.test"
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "endpoint must use HTTPS outside loopback"
    );
  });
});
