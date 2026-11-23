import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import InteractivePreviewPanel, {
  PREVIEW_FRAME_SANDBOX,
} from "./InteractivePreview";
import type {
  InteractivePreviewOffer,
  InteractivePreviewSession,
} from "./interactivePreviewApi";

const api = vi.hoisted(() => ({
  fetchInteractivePreview: vi.fn(),
  startInteractivePreview: vi.fn(),
  enterInteractivePreview: vi.fn(),
  stopInteractivePreview: vi.fn(),
}));

vi.mock("./interactivePreviewApi", () => api);

function offer(
  options: Partial<InteractivePreviewOffer> = {}
): InteractivePreviewOffer {
  return {
    state: "available",
    reason: "",
    revision_id: "rev-2",
    run_on: "local",
    executor_class: "author_machine",
    recipe: {
      mode: "command",
      source: ".lemoncrow/review.yaml interactive_preview",
      framework: "",
    },
    previews: [],
    preview: null,
    budget: { remaining_minutes: 100, minutes_per_minute: 2 },
    ttl: { default_seconds: 600, max_seconds: 900, min_seconds: 60 },
    opens_from: ["behavior_changes", "checks_rail"],
    observation: "human",
    sessions: [],
    ...options,
  };
}

function session(
  options: Partial<InteractivePreviewSession> = {}
): InteractivePreviewSession {
  return {
    id: "ips_" + "a".repeat(24),
    review_id: "review-1",
    revision_id: "rev-2",
    created_at: 1,
    expires_at: 601,
    ttl_seconds: 600,
    state: "running",
    run_on: "local",
    executor_class: "author_machine",
    mode: "command",
    comparable: true,
    comparability: "",
    stop_reason: "",
    limited_by_budget: false,
    compute_seconds: 0,
    events: 0,
    recording_evidence_id: "",
    origins: { old: "http://127.0.0.1:41001", new: "http://127.0.0.1:41002" },
    remaining_seconds: 600,
    ...options,
  };
}

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.stopInteractivePreview.mockResolvedValue({
    session: session({ state: "stopped" }),
  });
});

describe("InteractivePreviewPanel", () => {
  it("renders nothing without a previewable app", async () => {
    api.fetchInteractivePreview.mockResolvedValue(
      offer({
        state: "not_offered",
        reason: "No previewable app was detected in the trusted base.",
      })
    );
    const { container } = render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-2"
        openedFrom="checks_rail"
      />
    );
    await waitFor(() =>
      expect(api.fetchInteractivePreview).toHaveBeenCalledWith("review-1")
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders nothing when the server has no preview route", async () => {
    api.fetchInteractivePreview.mockRejectedValue(
      Object.assign(new Error("404"), { status: 404 })
    );
    const { container } = render(
      <InteractivePreviewPanel
        reviewId="review-1"
        openedFrom="behavior_changes"
      />
    );
    await waitFor(() => expect(api.fetchInteractivePreview).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("says why it is not available and offers no start", async () => {
    api.fetchInteractivePreview.mockResolvedValue(
      offer({
        state: "not_available",
        reason: "managed sandbox execution is not enabled here; Gate E",
        run_on: null,
        executor_class: null,
      })
    );
    render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-2"
        openedFrom="checks_rail"
      />
    );
    expect(
      await screen.findByTestId("interactive-preview-not-available")
    ).toHaveTextContent(
      "Not available: managed sandbox execution is not enabled here; Gate E"
    );
    expect(
      screen.queryByRole("button", { name: "Open side by side" })
    ).toBeNull();
  });

  it("shows Limited by budget and offers no start", async () => {
    api.fetchInteractivePreview.mockResolvedValue(
      offer({
        state: "limited_by_budget",
        reason:
          "Limited by budget: 1.0 compute minutes remain in this Review's budget",
      })
    );
    render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-2"
        openedFrom="behavior_changes"
      />
    );
    expect(
      await screen.findByTestId("interactive-preview-limited")
    ).toHaveTextContent(
      "Limited by budget: 1.0 compute minutes remain in this Review's budget"
    );
    expect(
      screen.queryByRole("button", { name: "Open side by side" })
    ).toBeNull();
  });

  it("opens the environment.toml preview the reviewer picks, the first declared by default", async () => {
    api.fetchInteractivePreview.mockResolvedValue(
      offer({ previews: ["web", "docs"], preview: "web" })
    );
    api.startInteractivePreview.mockRejectedValue(new Error("refused"));
    render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-2"
        openedFrom="checks_rail"
      />
    );
    const picker = await screen.findByRole("combobox", { name: "Preview" });
    expect(picker).toHaveTextContent("web");
    await userEvent.click(picker);
    await userEvent.click(screen.getByRole("option", { name: "docs" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Open side by side" })
    );
    await waitFor(() =>
      expect(api.startInteractivePreview).toHaveBeenCalledWith(
        "review-1",
        "rev-2",
        undefined,
        "docs"
      )
    );
  });

  it("offers no picker with one preview or none", async () => {
    api.fetchInteractivePreview.mockResolvedValue(
      offer({ previews: ["web"], preview: "web" })
    );
    render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-2"
        openedFrom="checks_rail"
      />
    );
    expect(
      await screen.findByRole("button", { name: "Open side by side" })
    ).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Preview" })).toBeNull();
  });

  it("opens base and head side by side in sandboxed frames and stops on request", async () => {
    api.fetchInteractivePreview.mockResolvedValue(offer());
    api.startInteractivePreview.mockResolvedValue({
      session: session({
        comparable: false,
        comparability: "lockfile differs",
      }),
      token: "one-time",
    });
    api.enterInteractivePreview.mockImplementation(
      async (
        _review: string,
        _session: string,
        _token: string,
        side: string
      ) => ({
        url:
          side === "old"
            ? "http://127.0.0.1:41001/__lemoncrow/enter#ticket=t1"
            : "http://127.0.0.1:41002/__lemoncrow/enter#ticket=t2",
      })
    );
    render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-2"
        openedFrom="checks_rail"
      />
    );
    expect(
      await screen.findByText(
        /up to 15 min · 2 compute min per minute · 100 min left in budget/
      )
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Open side by side" })
    );

    const frame = await screen.findByTestId("interactive-preview-frame");
    expect(api.startInteractivePreview).toHaveBeenCalledWith(
      "review-1",
      "rev-2"
    );
    expect(
      api.enterInteractivePreview.mock.calls.map((call) => [call[2], call[3]])
    ).toEqual([
      ["one-time", "old"],
      ["one-time", "new"],
    ]);
    const base = within(frame).getByTitle("Base preview");
    const head = within(frame).getByTitle("Head preview");
    expect(base).toHaveAttribute(
      "src",
      "http://127.0.0.1:41001/__lemoncrow/enter#ticket=t1"
    );
    expect(head).toHaveAttribute(
      "src",
      "http://127.0.0.1:41002/__lemoncrow/enter#ticket=t2"
    );
    for (const element of [base, head]) {
      expect(element).toHaveAttribute("sandbox", PREVIEW_FRAME_SANDBOX);
      expect(element).toHaveAttribute("referrerpolicy", "no-referrer");
    }
    expect(PREVIEW_FRAME_SANDBOX.split(" ")).not.toContain(
      "allow-top-navigation"
    );
    expect(PREVIEW_FRAME_SANDBOX.split(" ")).not.toContain("allow-popups");
    expect(
      within(frame).getByTestId("interactive-preview-not-comparable")
    ).toHaveTextContent("Not comparable: lockfile differs");
    expect(
      within(frame).getByTestId("interactive-preview-remaining")
    ).toHaveTextContent(/^(10:00|9:5\d) left$/);
    expect(
      within(frame).getByText(
        /a human observation, recorded as evidence, never a check result/
      )
    ).toBeInTheDocument();

    await userEvent.click(
      within(frame).getByRole("button", { name: "Stop preview" })
    );
    await waitFor(() =>
      expect(api.stopInteractivePreview).toHaveBeenCalledWith(
        "review-1",
        session().id,
        "rev-2"
      )
    );
    await waitFor(() =>
      expect(screen.queryByTestId("interactive-preview-frame")).toBeNull()
    );
  });

  it("shows a refused start and never opens a frame", async () => {
    api.fetchInteractivePreview.mockResolvedValue(offer());
    api.startInteractivePreview.mockRejectedValue(
      Object.assign(new Error("interactive preview is not scheduled"), {
        status: 409,
      })
    );
    render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-2"
        openedFrom="checks_rail"
      />
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Open side by side" })
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "interactive preview is not scheduled"
    );
    expect(api.enterInteractivePreview).not.toHaveBeenCalled();
    expect(screen.queryByTestId("interactive-preview-frame")).toBeNull();
  });

  it("is read-only on a historical or read-only view", async () => {
    api.fetchInteractivePreview.mockResolvedValue(offer());
    render(
      <InteractivePreviewPanel
        reviewId="review-1"
        revisionId="rev-1"
        openedFrom="checks_rail"
      />
    );
    await screen.findByText("Interactive preview");
    expect(
      screen.queryByRole("button", { name: "Open side by side" })
    ).toBeNull();
  });
});
