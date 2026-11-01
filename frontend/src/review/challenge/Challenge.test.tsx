import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { blindSpotsModel } from "../blindSpots/blindSpotsModel";
import { investigationContext } from "../investigation/investigationContext";
import type { ReviewProjection } from "../types";
import Challenge from "./Challenge";
import { claimFromBlindSpot, type ChallengeClaim, type ChallengeRun } from "./challengeModel";
import { INJECTION, projection, recipes, RUN_PLAN, TARGETS } from "./fixtures";

const api = vi.hoisted(() => ({
  fetchCheckRecipes: vi.fn(),
  postCheckRecipesRun: vi.fn(),
  fetchRunPlan: vi.fn(),
  fetchStaticFindings: vi.fn(),
}));

vi.mock("../checkRecipesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../checkRecipesApi")>()),
  fetchCheckRecipes: api.fetchCheckRecipes,
  postCheckRecipesRun: api.postCheckRecipesRun,
}));
vi.mock("../runPlanApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../runPlanApi")>()),
  fetchRunPlan: api.fetchRunPlan,
}));
vi.mock("../staticFindingsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../staticFindingsApi")>()),
  fetchStaticFindings: api.fetchStaticFindings,
}));

function question(view: ReviewProjection, ref: string): ChallengeClaim {
  const identity = investigationContext({ projection: view, targets: TARGETS, checks: null }).snapshot_identity;
  const model = blindSpotsModel({
    context: investigationContext({ projection: view, targets: TARGETS, checks: null }),
    projection: view,
    targets: TARGETS,
    capabilities: { openSource: true, evidence: true, ask: false, decide: false },
  });
  const item = model.categories.flatMap((group) => group.items).find((row) => row.id.endsWith(`:question:${ref}`));
  const claim = item ? claimFromBlindSpot(item, view, TARGETS, identity) : null;
  if (!claim) throw new Error(`no claim for ${ref}`);
  return claim;
}

/** The Brief's ownership in miniature: a trigger, the drawer, and the held run. */
function Harness({ view, claim }: { view: ReviewProjection; claim: ChallengeClaim }) {
  const [open, setOpen] = useState(false);
  const [run, setRun] = useState<ChallengeRun | null>(null);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Challenge</button>
      {open && (
        <Challenge claim={claim} projection={view} targets={TARGETS} canRun run={run} onRun={setRun} onOpenTarget={() => {}} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

beforeEach(() => {
  api.fetchCheckRecipes.mockReset().mockResolvedValue(recipes());
  api.fetchRunPlan.mockReset().mockResolvedValue(RUN_PLAN);
  api.fetchStaticFindings.mockReset().mockResolvedValue({ findings: [] });
  api.postCheckRecipesRun.mockReset();
});

describe("Challenge drawer", () => {
  // The question does not name the check (it only covers the source), so a pass stays a check outcome, not a verdict.
  it("shows the chain and the consent panel, runs once only on confirmation, and keeps the claim beside the result", async () => {
    const user = userEvent.setup();
    const view = projection();
    api.postCheckRecipesRun.mockResolvedValue({ revision_id: "rev-fixture", verdicts: { tests: "PASS" }, notes: [] });
    render(<Harness view={view} claim={question(view, "q-dup")} />);
    await user.click(screen.getByRole("button", { name: "Challenge" }));

    const drawer = await screen.findByTestId("challenge");
    for (const step of ["challenge-claim", "challenge-sources", "challenge-checks", "challenge-candidate", "challenge-observed"]) {
      expect(within(drawer).getByTestId(step)).toBeInTheDocument();
    }
    const consent = await within(drawer).findByTestId("challenge-consent");
    expect(within(consent).getByTestId("challenge-command")).toHaveTextContent("pytest tests/test_checkout.py");
    expect(within(consent).getByTestId("challenge-cost")).toHaveTextContent("2");
    expect(api.postCheckRecipesRun).not.toHaveBeenCalled();

    await user.click(within(consent).getByRole("button", { name: "Confirm and run" }));
    await waitFor(() => expect(within(drawer).getByTestId("challenge-outcome")).toHaveTextContent(/inconclusive/i));
    expect(api.postCheckRecipesRun).toHaveBeenCalledTimes(1);
    expect(api.postCheckRecipesRun).toHaveBeenCalledWith("review-fixture", "rev-fixture", "tests", false);
    expect(within(drawer).getByTestId("challenge-check-outcome")).toHaveTextContent(/pass/i);
    expect(within(drawer).getByTestId("challenge-claim-text")).toHaveTextContent("Retrying checkout creates a duplicate order");
    expect(within(drawer).queryByRole("button", { name: "Confirm and run" })).toBeNull();
  });

  it("quotes injected text as data, closes on Escape and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    const view = projection();
    render(<Harness view={view} claim={question(view, "q-inject")} />);
    const trigger = screen.getByRole("button", { name: "Challenge" });
    await user.click(trigger);

    const quote = await screen.findByTestId("challenge-claim-text");
    expect(quote.textContent).toBe(INJECTION);
    expect(quote.querySelector("code, a, script")).toBeNull();
    await screen.findByTestId("challenge-consent");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("challenge")).toBeNull());
    expect(trigger).toHaveFocus();
    expect(api.postCheckRecipesRun).not.toHaveBeenCalled();
  });

  it("reads as stale on a new Snapshot and loads nothing to run", async () => {
    const view = projection();
    const claim = question(view, "q-dup");
    const next = { ...view, revision_id: "rev-next" };
    await act(async () => {
      render(<Challenge claim={claim} projection={next} targets={TARGETS} canRun run={null} onRun={() => {}} onOpenTarget={() => {}} onClose={() => {}} />);
    });
    const drawer = screen.getByTestId("challenge");
    expect(drawer).toHaveAttribute("data-stale", "true");
    expect(screen.getByTestId("challenge-stale")).toHaveTextContent("review-fixture@rev-next");
    expect(screen.getByTestId("challenge-inspect-only")).toBeInTheDocument();
    expect(screen.queryByTestId("challenge-consent")).toBeNull();
    expect(api.fetchCheckRecipes).not.toHaveBeenCalled();
  });
});
