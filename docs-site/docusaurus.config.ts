import { themes as prismThemes } from "prism-react-renderer";
import type { Config } from "@docusaurus/types";
import type * as Preset from "@docusaurus/preset-classic";

const REPO = "lemoncrow-lab/lemoncrow";

// Read the GitHub star count once at build time, so pages show it without every visitor
// calling api.github.com (unauthenticated calls are rate-limited per IP and often failed).
// An offline or rate-limited build gets null, and the navbar chip fetches it live instead.
async function githubStars(): Promise<number | null> {
  try {
    const response = await fetch(`https://api.github.com/repos/${REPO}`, { signal: AbortSignal.timeout(5000) });
    if (!response.ok) return null;
    const body = (await response.json()) as { stargazers_count?: unknown } | null;
    return typeof body?.stargazers_count === "number" ? body.stargazers_count : null;
  } catch {
    return null;
  }
}

const config: Config = {
  title: "LemonCrow",
  tagline: "Keep coding agents sharp on real codebases",
  favicon: "img/favicon.svg",
  url: "https://docs.lemoncrow.com",
  baseUrl: "/",
  organizationName: "lemoncrow",
  projectName: "lemoncrow",
  // A public page that links to an unpublished one fails the build.
  onBrokenLinks: "throw",
  markdown: { hooks: { onBrokenMarkdownLinks: "throw" } },
  // lc-tokens.css scopes its dark theme to `.dark`; Docusaurus marks the theme with
  // `data-theme` on <html>. Mirror one onto the other before first paint (this runs
  // in <head>, ahead of the theme script that sets `data-theme`).
  headTags: [
    {
      tagName: "script",
      attributes: {},
      innerHTML:
        "(function(){var h=document.documentElement;function s(){h.classList.toggle('dark',h.getAttribute('data-theme')==='dark')}s();new MutationObserver(s).observe(h,{attributes:true,attributeFilter:['data-theme']})})();",
    },
  ],
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
          editUrl: "https://github.com/lemoncrow-lab/lemoncrow/edit/main/docs/",
        },
        // Benchmark reports live under /benchmarks; there is no blog.
        blog: false,
        theme: {
          customCss: "./src/css/custom.css",
        },
      } satisfies Preset.Options,
    ],
  ],
  // Local search: the index is built with the site and searched in the browser; no outside service.
  themes: [
    [
      "@easyops-cn/docusaurus-search-local",
      {
        hashed: true,
        indexBlog: false,
        docsRouteBasePath: "/",
        searchBarShortcutKeymap: "mod+k",
      },
    ],
  ],
  themeConfig: {
    image: "img/og.png",
    colorMode: {
      defaultMode: "light",
      respectPrefersColorScheme: false,
      disableSwitch: false,
    },
    navbar: {
      // No title/logo config -- src/theme/Logo renders the lemon mark +
      // wordmark lockup directly, matching landing/src/components/Nav.tsx.
      items: [
        {
          type: "docSidebar",
          sidebarId: "docs",
          position: "left",
          label: "Docs",
        },
        {
          href: "https://github.com/lemoncrow-lab/lemoncrow/blob/main/BENCHMARKS.md",
          label: "Benchmarks",
          position: "left",
        },
        {
          href: "https://lemoncrow.com/pricing",
          label: "Pricing",
          position: "right",
        },
        {
          href: "https://lemoncrow.com",
          label: "lemoncrow.com",
          position: "right",
        },
        {
          type: "custom-githubStars",
          position: "right",
        },
        {
          href: "https://github.com/lemoncrow-lab/lemoncrow",
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
            { label: "Installation", to: "/setup/installation" },
            { label: "CLI Reference", to: "/reference/cli" },
            { label: "Troubleshooting", to: "/setup/troubleshooting" },
          ],
        },
        {
          title: "Hosts",
          items: [
            { label: "All Hosts", to: "/hosts/all-agent-clis" },
            { label: "Claude Code", to: "/hosts/claude-code-install" },
            { label: "Codex CLI", to: "/hosts/codex-install" },
            { label: "Cursor", to: "/hosts/cursor-install" },
          ],
        },
        {
          title: "More",
          items: [
            { label: "lemoncrow.com", href: "https://lemoncrow.com" },
            {
              label: "GitHub",
              href: "https://github.com/lemoncrow-lab/lemoncrow",
            },
            { label: "Privacy", to: "/setup/privacy" },
            {
              label: "License",
              href: "https://github.com/lemoncrow-lab/lemoncrow/blob/main/LICENSE",
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} LemonCrow. Open source under Apache-2.0.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default async function createConfig(): Promise<Config> {
  return { ...config, customFields: { ...config.customFields, githubStars: await githubStars() } };
}
