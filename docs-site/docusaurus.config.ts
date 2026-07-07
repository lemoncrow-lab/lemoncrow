import { themes as prismThemes } from "prism-react-renderer";
import type { Config } from "@docusaurus/types";
import type * as Preset from "@docusaurus/preset-classic";

const config: Config = {
  title: "Atelier",
  tagline: "Open-Source Runtime Engineering for Agents",
  favicon: "img/favicon.svg",
  url: "https://docs.atelier.ws",
  baseUrl: "/",
  organizationName: "atelier-ws",
  projectName: "atelier",
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
          editUrl: "https://github.com/atelier-ws/atelier/edit/main/docs/",
        },
        blog: {
          showReadingTime: true,
          blogTitle: "Benchmarks",
          blogDescription:
            "A/B benchmark reports comparing Atelier-on vs Atelier-off",
          postsPerPage: "ALL",
        },
        theme: {
          customCss: "./src/css/custom.css",
        },
      } satisfies Preset.Options,
    ],
  ],
  themeConfig: {
    image: "img/atelier-logo.svg",
    colorMode: {
      defaultMode: "light",
      respectPrefersColorScheme: true,
      disableSwitch: false,
    },
    navbar: {
      title: "Atelier",
      logo: {
        alt: "Atelier Logo",
        src: "img/atelier-logo.svg",
      },
      items: [
        {
          type: "docSidebar",
          sidebarId: "docs",
          position: "left",
          label: "Docs",
        },
        {
          to: "/blog",
          label: "Benchmarks",
          position: "left",
        },
        {
          href: "https://atelier.ws",
          label: "Website",
          position: "right",
        },
        {
          href: "https://github.com/atelier-ws/atelier",
          label: "GitHub",
          position: "right",
        },
      ],
    },
    footer: {
      style: "dark",
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
            { label: "Atelier Website", href: "https://atelier.ws" },
            { label: "GitHub", href: "https://github.com/atelier-ws/atelier" },
            { label: "Contact", href: "mailto:contact@atelier.ws" },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Atelier. Open source under MIT License.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
