import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Playbook } from "../api";

const blocks = vi.hoisted(() => vi.fn());
vi.mock("../api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api")>();
  return { ...original, api: { ...original.api, blocks } };
});

import { BlocksSection } from "./Learnings";

function playbook(overrides: Partial<Playbook> = {}): Playbook {
  return {
    id: "pb-1",
    domain: "ci",
    title: "Regenerate the lockfile after a dependency bump",
    status: "active",
    situation: "",
    procedure: [],
    dead_ends: [],
    verification: [],
    failure_signals: ["install fails"],
    required_rubrics: [],
    when_not_to_apply: "",
    task_types: [],
    triggers: [],
    file_patterns: [],
    tool_patterns: [],
    usage_count: 0,
    success_count: 0,
    failure_count: 0,
    created_at: "2026-10-01T00:00:00Z",
    ...overrides,
  };
}

describe("Playbooks", () => {
  it("says there are none yet instead of blaming filters", async () => {
    blocks.mockResolvedValue([]);
    render(<BlocksSection />);

    expect(await screen.findByText("No playbooks yet")).toBeInTheDocument();
    expect(screen.queryByText(/match these filters/)).not.toBeInTheDocument();
  });

  it("summarises in one line and filters by the statuses the store writes", async () => {
    blocks.mockResolvedValue([playbook(), playbook({ id: "pb-2", title: "Read before editing", domain: "universal", failure_signals: [] })]);
    render(<BlocksSection />);

    expect(await screen.findByText(/2 playbooks · 1 applies broadly · 1 lists known pitfalls/)).toBeInTheDocument();
    expect(screen.queryByText("Visible now")).not.toBeInTheDocument();
    expect(screen.getByText("Known pitfalls")).toBeInTheDocument();
    expect(screen.getByText("Applies broadly")).toBeInTheDocument();
    expect(screen.queryByText(/risk pattern|signal/i)).not.toBeInTheDocument();
    for (const label of ["In use", "Paused", "Retired"]) {
      expect(screen.getByRole("radio", { name: label })).toBeInTheDocument();
    }
  });
});
