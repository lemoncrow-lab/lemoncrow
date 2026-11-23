import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { Rubric } from "../api";

const rubrics = vi.hoisted(() => vi.fn());
vi.mock("../api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api")>();
  return { ...original, api: { ...original.api, rubrics } };
});

import Rubrics, { verifyCall } from "./Rubrics";

const RUBRIC: Rubric = {
  id: "rubric_webhook_handlers",
  domain: "swe.backend",
  triggers: [],
  forbidden_phrases: [],
  required_checks: ["tests_pass"],
  block_if_missing: [],
  warning_checks: ["docs_updated"],
  escalation_conditions: [],
  related_blocks: ["verify_signature_first"],
  created_at: "",
};

function renderRubrics() {
  render(
    <MemoryRouter>
      <Rubrics />
    </MemoryRouter>,
  );
}

describe("Rubrics", () => {
  it("explains an empty list instead of showing a blank page", async () => {
    rubrics.mockResolvedValue([]);
    renderRubrics();
    expect(await screen.findByText("No rubrics yet")).toBeInTheDocument();
  });

  it("counts once, names domains, and shows a real verify call", async () => {
    rubrics.mockResolvedValue([RUBRIC]);
    renderRubrics();

    expect(await screen.findByText("Webhook handlers")).toBeInTheDocument();
    expect(screen.getByText("1 rubric")).toBeInTheDocument();
    expect(screen.queryByText(/total/)).not.toBeInTheDocument();
    expect(screen.getByText("Software engineering · Backend")).toBeInTheDocument();
    expect(verifyCall(RUBRIC)).toBe('verify(rubric_id="rubric_webhook_handlers", checks={"tests_pass": true, "docs_updated": true})');
  });
});
