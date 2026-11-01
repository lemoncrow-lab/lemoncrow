import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import BriefWorkspaceImpact from "./BriefWorkspaceImpact";
import type { ReviewWorkspaceEvidence, ReviewWorkspaceImpact, ReviewWorkspaceReached } from "./types";

// A stored packet from the terminal carries each end's source line as `detail`; the Reader must never show it.
const at = (path: string, line: number, detail: string) => ({ path, line, detail }) as ReviewWorkspaceEvidence;

function hit(repo: string, links = 1, extra: Partial<ReviewWorkspaceReached> = {}): ReviewWorkspaceReached {
  return {
    repo,
    hops: 1,
    confidence: "high",
    direct: true,
    path: ["billing", repo],
    edges: [
      {
        kind: "package",
        confidence: "high",
        direct: true,
        more: 0,
        links: Array.from({ length: links }, (_, index) => ({
          key: `k${index}`,
          confidence: "high",
          provider: at("billing/billing/__init__.py", 0, "PROVIDER SOURCE LINE"),
          consumer: at(`${repo}/main.py`, index + 1, "CONSUMER SOURCE LINE"),
        })),
      },
    ],
    ...extra,
  };
}

function impact(reached: ReviewWorkspaceReached[], extra: Partial<ReviewWorkspaceImpact> = {}): ReviewWorkspaceImpact {
  return { repo: "billing", root: "/w", changed: [{ repo: "billing", files: ["billing/billing/invoices.py"] }], reached, ...extra };
}

describe("Brief workspace impact", () => {
  it("names each reached repository with its hops, confidence, level and evidence, then says it is not a finding", () => {
    const twoHops = hit("shop", 1, { hops: 2, direct: false, confidence: "medium", path: ["billing", "bazaar", "shop"] });
    render(<BriefWorkspaceImpact impact={impact([hit("bazaar", 3), twoHops])} />);
    const section = screen.getByRole("region", { name: "Workspace impact" });
    expect(within(section).getByText("2 other repositories can be affected (/w, this repository is billing)")).toBeInTheDocument();
    const [first, second] = within(section).getAllByTestId("workspace-impact-repo");
    expect(first.textContent).toContain("bazaar1 hophighdirect");
    expect(within(first).getByText("package k0: bazaar/main.py:1 uses billing/billing/__init__.py")).toBeInTheDocument();
    expect(within(first).getByText("package k1: bazaar/main.py:2 uses billing/billing/__init__.py")).toBeInTheDocument();
    expect(within(first).queryByText(/package k2:/)).toBeNull();
    expect(within(first).getByText("1 more package evidence")).toBeInTheDocument();
    expect(second.textContent).toContain("2 hops");
    expect(second.textContent).toContain("repository-level");
    expect(within(second).getByText("via billing → bazaar → shop")).toBeInTheDocument();
    expect(within(section).getByText("Not findings: repositories that depend on what changed, for a person to check.")).toBeInTheDocument();
    expect(section.textContent).not.toContain("SOURCE LINE");
  });

  it("shows five repositories until Show all, and never a source line from another repository", async () => {
    const user = userEvent.setup();
    render(<BriefWorkspaceImpact impact={impact(Array.from({ length: 7 }, (_, index) => hit(`r${index}`, 3)))} />);
    expect(screen.getAllByTestId("workspace-impact-repo")).toHaveLength(5);
    expect(screen.queryByText("r6")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Show all 7" }));
    expect(screen.getAllByTestId("workspace-impact-repo")).toHaveLength(7);
    expect(screen.getByText("r6")).toBeInTheDocument();
    expect(screen.getByText("package k2: r6/main.py:3 uses billing/billing/__init__.py")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
    expect(document.body.textContent).not.toContain("SOURCE LINE");
  });

  it("renders nothing when absent or empty, and only the partial notes when nothing was reached", () => {
    const { container, rerender } = render(<BriefWorkspaceImpact impact={undefined} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<BriefWorkspaceImpact impact={{}} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<BriefWorkspaceImpact impact={impact([])} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<BriefWorkspaceImpact impact={impact([], { incomplete: ["bazaar"], errors: [{ repo: "shop", error: "git failed" }] })} />);
    const section = screen.getByRole("region", { name: "Workspace impact" });
    expect(within(section).getByText("Partial: shop: changes not listed")).toBeInTheDocument();
    expect(within(section).getByText("Partial: bazaar was not fully scanned; reach through it may be missing")).toBeInTheDocument();
    expect(section.textContent).not.toMatch(/can be affected|Not findings/);
  });
});
