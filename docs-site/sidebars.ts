import type { SidebarsConfig } from "@docusaurus/plugin-content-docs";

const sidebars: SidebarsConfig = {
  docs: [
    {
      type: "doc",
      id: "README",
      label: "Home",
    },
    {
      type: "doc",
      id: "installation",
    },
    {
      label: "Use LemonCrow",
      type: "category",
      items: [
        {
          type: "doc",
          id: "cli",
        },
        {
          type: "doc",
          id: "troubleshooting",
        },
        {
          type: "doc",
          id: "production-readiness",
        },
      ],
    },
    {
      label: "Hosts",
      type: "category",
      items: [
        {
          type: "doc",
          id: "hosts/all-agent-clis",
        },
        {
          type: "doc",
          id: "hosts/host-capability-matrix",
        },
        {
          type: "doc",
          id: "hosts/claude-code-install",
        },
        {
          type: "doc",
          id: "hosts/codex-install",
        },
        {
          type: "doc",
          id: "hosts/copilot-install",
        },
        {
          type: "doc",
          id: "hosts/antigravity-install",
        },
        {
          type: "doc",
          id: "hosts/opencode-install",
        },
        {
          type: "doc",
          id: "hosts/cursor-install",
        },
        {
          type: "doc",
          id: "hosts/hermes-install",
        },
      ],
    },
    {
      label: "SDK & API",
      type: "category",
      items: [
        {
          type: "doc",
          id: "sdk/python",
        },
        {
          type: "doc",
          id: "sdk/mcp",
        },
        {
          type: "doc",
          id: "integrations/host-matrix",
        },
      ],
    },
    {
      label: "Strategy & Roadmap",
      type: "category",
      items: [
        {
          type: "doc",
          id: "strategy",
        },
        {
          type: "doc",
          id: "roadmap",
        },
      ],
    },
  ],
};

export default sidebars;
