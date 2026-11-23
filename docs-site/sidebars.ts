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
          id: "setup/ai-review",
          label: "Your first AI review",
        },
        {
          type: "doc",
          id: "setup/workspaces-lite-quickstart",
          label: "Workspaces Lite quickstart",
        },
        {
          type: "doc",
          id: "setup/pro-hosted-runtime",
          label: "Workspaces Pro: hosted retrieval",
        },
        {
          type: "doc",
          id: "setup/github-action",
          label: "GitHub Action: AI pull request review",
        },
        {
          type: "doc",
          id: "setup/web-qa",
        },
        {
          type: "doc",
          id: "setup/proof",
        },
        {
          type: "doc",
          id: "setup/production-watch",
        },
        {
          type: "doc",
          id: "setup/importing-review-rules",
          label: "Importing review rules",
        },
        {
          type: "doc",
          id: "setup/hosted-review",
          label: "Hosted Review: administrator guide",
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
          id: "hosts/codex-capabilities",
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
        {
          type: "doc",
          id: "hosts/pi-install",
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
          id: "reference/review",
        },
        {
          type: "doc",
          id: "reference/review-walkthrough",
        },
        {
          type: "doc",
          id: "reference/environment",
        },
        {
          type: "doc",
          id: "reference/support-matrix",
          label: "Support matrix",
        },
        {
          type: "doc",
          id: "reference/review-preview-plugins",
        },
      ],
    },
    {
      label: "Architecture",
      type: "category",
      items: [
        {
          type: "doc",
          id: "reference/architecture",
        },
        {
          type: "doc",
          id: "architecture/runtime-lite",
        },
        {
          type: "doc",
          id: "architecture/oss-private-code-intelligence",
        },
        {
          type: "doc",
          id: "architecture/review-authority-map",
        },
        {
          type: "doc",
          id: "architecture/workspaces-and-review",
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
    // planning/roadmap, planning/savings-optimization-roadmap and operations/production-readiness
    // stay published (sync-docs.mjs PUBLIC_DOCS) and reachable by link, but are not in the sidebar.
  ],
};

export default sidebars;