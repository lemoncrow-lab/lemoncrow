import type { ReactNode } from "react";
import Link from "@docusaurus/Link";
import useDocusaurusContext from "@docusaurus/useDocusaurusContext";
import Layout from "@theme/Layout";

const FEATURES = [
  {
    title: "Ranked code graph",
    desc: "Symbols, callers, callees, usages, and repository history before broad file exploration.",
  },
  {
    title: "Bounded context",
    desc: "Exact ranges, outlines, duplicate suppression, and recoverable output spills.",
  },
  {
    title: "Durable memory",
    desc: "Compaction support, handover packets, and facts that survive the current conversation.",
  },
  {
    title: "Verified execution",
    desc: "Grounded edits, hooks, run ledgers, and outcome checks around the model you already use.",
  },
  {
    title: "Host-neutral",
    desc: "One runtime for Claude Code, Codex, Copilot, opencode, and MCP-compatible hosts.",
  },
  {
    title: "Local-first",
    desc: "Parsing, indexing, and the repository working set stay on your machine.",
  },
];

function HomepageHeader() {
  const { siteConfig } = useDocusaurusContext();
  return (
    <header
      className="hero"
      style={{
        background: "var(--hero-bg)",
        padding: "80px 20px 60px",
      }}
    >
      <div className="container" style={{ textAlign: "center" }}>
        <img
          src="/img/favicon.svg"
          width="64"
          height="64"
          alt=""
          style={{ marginBottom: "16px" }}
        />
        <h1
          style={{
            fontSize: "48px",
            fontWeight: 700,
            color: "var(--hero-text)",
            margin: "0 0 12px",
            letterSpacing: "-0.02em",
          }}
        >
          {siteConfig.title}
        </h1>
        <p
          style={{
            fontSize: "20px",
            color: "var(--hero-subtext)",
            maxWidth: "680px",
            margin: "0 auto 32px",
            lineHeight: 1.6,
          }}
        >
          {siteConfig.tagline}
        </p>
        <div
          style={{
            display: "flex",
            gap: "12px",
            justifyContent: "center",
            flexWrap: "wrap",
          }}
        >
          <Link
            className="button button--secondary button--lg"
            to="/installation"
          >
            Install LemonCrow
          </Link>
          <Link
            className="button button--outline button--lg"
            href="https://github.com/lemoncrow-lab/lemoncrow/blob/main/BENCHMARKS.md"
            style={{ borderColor: "#9B75D9", color: "#9B75D9" }}
          >
            Matched benchmarks
          </Link>
        </div>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="LemonCrow — Context and Execution Runtime for Coding Agents"
      description="Keep coding agents sharp on real codebases with a local code graph, exact-range tools, bounded output, durable memory, and verified execution."
    >
      <HomepageHeader />
      <main>
        <section
          style={{ padding: "60px 20px", maxWidth: "960px", margin: "0 auto" }}
        >
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
              gap: "24px",
            }}
          >
            {FEATURES.map((feature) => (
              <div
                key={feature.title}
                style={{
                  background: "var(--card-bg)",
                  border: "1px solid var(--card-border)",
                  borderRadius: "12px",
                  padding: "24px",
                }}
              >
                <h3
                  style={{
                    color: "var(--card-title)",
                    fontSize: "18px",
                    margin: "0 0 8px",
                  }}
                >
                  {feature.title}
                </h3>
                <p
                  style={{
                    color: "var(--card-text)",
                    fontSize: "14px",
                    margin: 0,
                    lineHeight: 1.6,
                  }}
                >
                  {feature.desc}
                </p>
              </div>
            ))}
          </div>
        </section>
      </main>
    </Layout>
  );
}
