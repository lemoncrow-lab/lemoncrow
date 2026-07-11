import type { ReactNode } from "react";
import clsx from "clsx";
import Link from "@docusaurus/Link";
import useDocusaurusContext from "@docusaurus/useDocusaurusContext";
import Layout from "@theme/Layout";

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
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 32 32"
          fill="none"
          style={{ width: "64px", height: "64px", marginBottom: "16px" }}
        >
          <rect width="32" height="32" rx="6" fill="#9B75D9" />
          <text
            x="16"
            y="22"
            fontFamily="Arial,sans-serif"
            fontSize="18"
            fontWeight="bold"
            fill="white"
            textAnchor="middle"
          >
            A
          </text>
        </svg>
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
            maxWidth: "600px",
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
            to="/docs/installation"
          >
            Installation
          </Link>
          <Link
            className="button button--outline button--lg"
            href="https://github.com/lemoncrowhq/lemoncrow"
            style={{ borderColor: "#9B75D9", color: "#9B75D9" }}
          >
            GitHub
          </Link>
        </div>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="LemonCrow — Agent Reasoning Runtime"
      description="Structured procedure store for AI agents. Query procedures before and after complex tasks."
    >
      <HomepageHeader />
      <main>
        <section
          style={{ padding: "60px 20px", maxWidth: "900px", margin: "0 auto" }}
        >
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
              gap: "24px",
            }}
          >
            {[
              {
                icon: "🧠",
                title: "Reason Blocks",
                desc: "Reusable procedures agents query before and after tasks.",
              },
              {
                icon: "📇",
                title: "Traces",
                desc: "Execution artifacts with full prompt/response capture.",
              },
              {
                icon: "📏",
                title: "Rubrics",
                desc: "Domain verification gates for structured quality control.",
              },
              {
                icon: "🤖",
                title: "Agent Integrations",
                desc: "Works with Claude Code, Codex, Copilot, opencode, Gemini.",
              },
              {
                icon: "💰",
                title: "Cost Tracking",
                desc: "Token usage, model pricing, and savings analytics.",
              },
              {
                icon: "🚨",
                title: "Failure Analysis",
                desc: "Cluster and analyse agent errors automatically.",
              },
            ].map((f) => (
              <div
                key={f.title}
                style={{
                  background: "var(--card-bg)",
                  border: "1px solid var(--card-border)",
                  borderRadius: "12px",
                  padding: "24px",
                }}
              >
                <div style={{ fontSize: "28px", marginBottom: "12px" }}>
                  {f.icon}
                </div>
                <h3
                  style={{
                    color: "var(--card-title)",
                    fontSize: "18px",
                    margin: "0 0 8px",
                  }}
                >
                  {f.title}
                </h3>
                <p
                  style={{
                    color: "var(--card-text)",
                    fontSize: "14px",
                    margin: 0,
                    lineHeight: 1.6,
                  }}
                >
                  {f.desc}
                </p>
              </div>
            ))}
          </div>
        </section>
      </main>
    </Layout>
  );
}
