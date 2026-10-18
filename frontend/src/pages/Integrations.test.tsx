import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchLocalIntegrations } from "../control/controlApi";
import Integrations from "./Integrations";

vi.mock("../control/controlApi", () => ({
  fetchLocalIntegrations: vi.fn(),
}));

const mockedFetchLocalIntegrations = vi.mocked(fetchLocalIntegrations);

describe("Integrations", () => {
  beforeEach(() => {
    mockedFetchLocalIntegrations.mockResolvedValue({
      dispatcher_available: true,
      cached_status_available: true,
      internal_dispatchable_count: 12,
      tools: [],
      hosts: [
        {
          id: "claude",
          label: "Claude Code",
          description: "Claude plugin and MCP integration.",
          detected: true,
          configured: true,
          status: "configured",
          cached_status: "installed",
          support: "supported",
          components: [
            {
              id: "mcp",
              label: "MCP registration",
              description: "Short-name lc MCP server registered with Claude.",
              present: true,
              path: "~/.claude.json",
            },
            {
              id: "plugin",
              label: "Plugin bundle",
              description: "LemonCrow Claude plugin package.",
              present: true,
              path: "~/.lemoncrow/claude-plugin/.claude-plugin/plugin.json",
            },
            {
              id: "hooks",
              label: "Lifecycle hooks",
              description: "Claude session and tool lifecycle hooks.",
              present: false,
              path: "~/.lemoncrow/claude-plugin/hooks/hooks.json",
            },
          ],
        },
        {
          id: "opencode",
          label: "OpenCode",
          description: "OpenCode MCP integration and imported-session support.",
          detected: false,
          configured: true,
          status: "configured",
          support: "supported",
          components: [
            {
              id: "mcp",
              label: "MCP registration",
              description: "lc MCP server in the OpenCode config.",
              present: true,
              path: "~/.config/opencode/opencode.json",
            },
            ...["code", "execute", "explore", "general", "plan", "research", "review", "solve"].map(
              (role) => ({
                id: `agent:lemoncrow.${role}`,
                label: `Agent · ${role}`,
                description: "Installed LemonCrow OpenCode agent role.",
                present: true,
                path: `~/.config/opencode/agents/lemoncrow.${role}.md`,
              }),
            ),
          ],
        },
        {
          id: "pi",
          label: "Pi",
          description: "Default LemonCrow-managed coding frontend.",
          detected: true,
          configured: true,
          status: "configured",
          support: "supported",
          components: [
            {
              id: "binary",
              label: "Managed host",
              description: "Checksum-pinned Pi host binary installed by LemonCrow.",
              present: true,
              path: "~/.lemoncrow/bin/pi-host",
            },
          ],
        },
        {
          id: "lemoncode",
          label: "LemonCode",
          description: "Managed rollback frontend; lc code auto prefers Pi.",
          detected: true,
          configured: true,
          status: "configured",
          support: "supported",
          components: [
            {
              id: "binary",
              label: "Managed host",
              description: "LemonCode rollback host binary managed by LemonCrow.",
              present: true,
              path: "~/.lemoncrow/bin/lemoncode-host",
            },
          ],
        },
        {
          id: "antigravity",
          label: "Antigravity",
          description: "Antigravity integration is not part of the current setup wizard yet.",
          detected: false,
          configured: false,
          status: "not_detected",
          support: "coming_soon",
          components: [],
        },
        {
          id: "hermes",
          label: "Hermes Agent",
          description: "Hermes integration is not part of the current setup wizard yet.",
          detected: false,
          configured: false,
          status: "not_detected",
          support: "coming_soon",
          components: [],
        },
      ],
    });
  });

  it("expands a configured host into concrete integration surfaces", async () => {
    const user = userEvent.setup();
    render(<Integrations />);

    const claude = await screen.findByRole("button", { name: /Claude Code/ });
    expect(within(claude).getByText("2/3 surfaces present")).toBeInTheDocument();
    expect(within(claude).getByText("MCP registration · Plugin bundle")).toBeInTheDocument();

    await user.click(claude);

    const surfaces = screen.getByLabelText("Claude Code integration surfaces");
    expect(within(surfaces).getByText("MCP registration")).toBeInTheDocument();
    expect(within(surfaces).getByText("Lifecycle hooks")).toBeInTheDocument();
    expect(within(surfaces).getByText("Missing")).toBeInTheDocument();
    expect(screen.getByText("~/.lemoncrow/claude-plugin/hooks/hooks.json")).toBeInTheDocument();
  });

  it("shows OpenCode role files and the Pi/LemonCode managed frontend relationship", async () => {
    const user = userEvent.setup();
    render(<Integrations />);

    const opencode = await screen.findByRole("button", { name: /OpenCode/ });
    expect(within(opencode).getByText(/\+6 more/)).toBeInTheDocument();
    await user.click(opencode);
    const surfaces = screen.getByLabelText("OpenCode integration surfaces");
    expect(within(surfaces).getByText("Agent · code")).toBeInTheDocument();
    expect(within(surfaces).getByText("Agent · solve")).toBeInTheDocument();
    expect(
      screen.getByText("~/.config/opencode/agents/lemoncrow.review.md"),
    ).toBeInTheDocument();

    expect(screen.getByText("Default LemonCrow-managed coding frontend.")).toBeInTheDocument();
    expect(screen.getByText("Managed rollback frontend; lc code auto prefers Pi.")).toBeInTheDocument();
  });

  it("shows Pi and marks Antigravity and Hermes as coming soon", async () => {
    const user = userEvent.setup();
    render(<Integrations />);

    expect(await screen.findByRole("button", { name: /^Pi\b/ })).toBeInTheDocument();

    const antigravity = screen.getByRole("button", { name: /Antigravity/ });
    const hermes = screen.getByRole("button", { name: /Hermes Agent/ });
    expect(within(antigravity).getByText("Coming soon")).toBeInTheDocument();
    expect(within(hermes).getByText("Coming soon")).toBeInTheDocument();

    await user.click(antigravity);
    expect(
      screen.getByText(/not offered by the current LemonCrow setup wizard/i),
    ).toBeInTheDocument();
  });
});
