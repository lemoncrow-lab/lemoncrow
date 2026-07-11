import { themes as prismThemes } from "prism-react-renderer";
import type { Config } from "@docusaurus/types";
import type * as Preset from "@docusaurus/preset-classic";

const config: Config = {
  title: "LemonCrow",
  tagline: "Open-Core Runtime Engineering for Agents",
  favicon: "img/favicon.svg",
  url: "https://docs.lemoncrow.com",
  baseUrl: "/",
  organizationName: "lemoncrow",
  projectName: "lemoncrow",
  onBrokenLinks: "warn",
  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },
  presets: [
    [
      "classic",
      {
        docs: {
          routeBasePath: "/",
          sidebarPath: "./sidebars.ts",
          editUrl: "https://github.com/lemoncrowhq/lemoncrow/edit/main/docs/",
        },
        blog: {
          showReadingTime: true,
          blogTitle: "Benchmarks",
          blogDescription:
            "A/B benchmark reports comparing LemonCrow-on vs LemonCrow-off",
          postsPerPage: "ALL",
        },
        theme: {
          customCss: "./src/css/custom.css",
        },
      } satisfies Preset.Options,
    ],
  ],
  themeConfig: {
    image: "img/lemoncrow-logo.svg",
    colorMode: {
      defaultMode: "light",
      respectPrefersColorScheme: false,
      disableSwitch: false,
    },
    navbar: {
      // No title/logo config -- src/theme/Logo renders the chip+wordmark
      // lockup directly, matching landing/src/components/Nav.tsx.
      items: [
        {
          type: "docSidebar",
          sidebarId: "docs",
          position: "left",
          label: "Docs",
        },
        {
          href: "https://github.com/lemoncrowhq/lemoncrow/blob/main/BENCHMARKS.md",
          label: "Benchmarks",
          position: "left",
        },
        {
          href: "https://lemoncrow.com",
          label: "Website",
          position: "right",
          className: "navbar-cta",
        },
        {
          type: "custom-githubStars",
          position: "right",
        },
        {
          href: "https://github.com/lemoncrowhq/lemoncrow",
          label: "GitHub",
          position: "right",
          className: "header-github-link",
        },
      ],
    },
    footer: {
      links: [
        {
          title: "Docs",
          items: [
            { label: "Home", to: "/" },
            { label: "Installation", to: "/installation" },
            { label: "CLI Reference", to: "/cli" },
            { label: "Troubleshooting", to: "/troubleshooting" },
          ],
        },
        {
          title: "Hosts",
          items: [
            { label: "All Hosts", to: "/hosts/all-agent-clis" },
            { label: "Claude Code", to: "/hosts/claude-code-install" },
            { label: "Codex CLI", to: "/hosts/codex-install" },
            { label: "Copilot", to: "/hosts/copilot-install" },
          ],
        },
        {
          title: "More",
          items: [
            { label: "LemonCrow Website", href: "https://lemoncrow.com" },
            { label: "GitHub", href: "https://github.com/lemoncrowhq/lemoncrow" },
            { label: "Contact", href: "mailto:contact@lemoncrow.com" },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} LemonCrow. Open source under MIT License.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
