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
      id: "quickstart",
    },
    {
      type: "doc",
      id: "installation",
    },
    {
      label: "Use Atelier",
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
          id: "hosts/gemini-cli-install",
        },
        {
          type: "doc",
          id: "hosts/opencode-install",
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
      label: "Contributing",
      type: "category",
      items: [
        {
          type: "doc",
          id: "engineering/contributing",
        },
      ],
    },
    {
      label: "Archive & Internal",
      type: "category",
      collapsed: true,
      items: [
        {
          type: "doc",
          id: "archive/README",
          label: "Archive Home",
        },
        {
          type: "doc",
          id: "archive/authoring-and-packs",
        },
        {
          type: "doc",
          id: "archive/architecture-history",
        },
        {
          type: "doc",
          id: "archive/engineering-internals",
        },
        {
          type: "doc",
          id: "archive/legacy-integrations",
        },
        {
          type: "doc",
          id: "archive/benchmark-and-migration-history",
        },
        {
          type: "doc",
          id: "archive/internal-notes",
        },
      ],
    },
  ],
};

export default sidebars;
