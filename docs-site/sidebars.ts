import type { SidebarsConfig } from "@docusaurus/plugin-content-docs";

const sidebars: SidebarsConfig = {
  docs: [
    {
      type: "doc",
      id: "README",
      label: "Home",
    },
    {
      label: "Setup & Troubleshooting",
      type: "category",
      items: [
        {
          type: "doc",
          id: "setup/installation",
          label: "Installation",
        },
        {
          type: "doc",
          id: "setup/privacy",
        },
        {
          type: "doc",
          id: "setup/troubleshooting",
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
          id: "hosts/lemoncode-install",
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
      ],
    },
    {
      label: "Reference",
      type: "category",
      items: [
        {
          type: "doc",
          id: "reference/cli",
        },
        {
          type: "doc",
          id: "reference/architecture",
        },
        {
          type: "doc",
          id: "reference/openai-gateway",
        },
      ],
    },
    {
      label: "Benchmarks",
      type: "category",
      items: [
        {
          type: "doc",
          id: "benchmarks/harbor-eval",
        },
        {
          type: "doc",
          id: "benchmarks/mini-eval",
        },
        {
          type: "doc",
          id: "benchmarks/results",
        },
      ],
    },
    {
      label: "Legal & Licensing",
      type: "category",
      items: [
        {
          type: "doc",
          id: "legal/licensing",
        },
        {
          type: "doc",
          id: "legal/licensing-report",
        },
        {
          type: "doc",
          id: "legal/dependency-licenses",
        },
      ],
    },
    {
      label: "Roadmap & Savings",
      type: "category",
      items: [
        {
          type: "doc",
          id: "planning/roadmap",
        },
        {
          type: "doc",
          id: "planning/savings-optimization-roadmap",
        },
      ],
    },
    {
      label: "Operations",
      type: "category",
      items: [
        {
          type: "doc",
          id: "operations/production-readiness",
        },
        {
          type: "doc",
          id: "operations/maintenance-mode-transition",
        },
      ],
    },
  ],
};

export default sidebars;