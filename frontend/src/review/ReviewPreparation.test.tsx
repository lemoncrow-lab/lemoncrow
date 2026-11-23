import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";

import ReviewPreparation, { PREPARATION_STALL_MS, PreparationProgress } from "./ReviewPreparation";
import { useLayoutEffect } from "react";
import { ReviewPreparationProvider, useReviewPreparation } from "./reviewPreparationContext";
import { authenticatedFetch } from "./reviewApi";

vi.mock("./reviewApi", () => ({ authenticatedFetch: vi.fn() }));

const id = "a".repeat(32);
const fetchProgress = vi.mocked(authenticatedFetch);

function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search + location.hash}</output>;
}

function mount(path = `/reviews/preparing/${id}`) {
  return render(<MemoryRouter initialEntries={[path]}>
    <ReviewPreparation /><Location />
  </MemoryRouter>);
}

const steps = [
  { key: "packet_diff", label: "Diff captured", state: "done", elapsed: 0.2 },
  { key: "publish", label: "Publishing reviewable source revision", state: "running", elapsed: null },
  { key: "state_units", label: "Loading parts and marks", state: "pending", elapsed: null },
];

afterEach(() => {
  vi.clearAllMocks();
});

function ReaderProgress({ pending, onOpenCode }: { pending: boolean; onOpenCode?: () => void }) {
  const context = useReviewPreparation();
  const setReader = context?.setReader;
  useLayoutEffect(() => {
    setReader?.({ pending, onOpenCode, steps: [{
      key: "reader_diff", label: "Preparing first visual diff",
      state: pending ? "running" : "done", elapsed: null,
    }] });
  }, [setReader, pending, onOpenCode]);
  return null;
}

describe("Review preparation", () => {
  it("keeps the elapsed timer moving while reader preparation has no status updates", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-10-13T10:00:00Z"));
      render(<MemoryRouter><PreparationProgress fullPage steps={[{
        key: "reader_brief", label: "Preparing Brief", state: "running", elapsed: null,
        started_at: Date.now() / 1000,
      }]} /></MemoryRouter>);
      expect(screen.getByText("0.0s")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(2000));
      expect(screen.getByText("2.0s")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
  it("keeps one display through Reader loading and hides after both preparations finish", async () => {
    fetchProgress.mockResolvedValue(new Response(JSON.stringify({
      id, state: "ready", detail: "Review ready", review_url: "/r/1234abcd", steps: [],
    })));
    const view = (pending: boolean) => <MemoryRouter initialEntries={[`/r/1234abcd?startup=${id}`]}>
      <ReviewPreparationProvider>
        <ReviewPreparation /><ReaderProgress pending={pending} /><Location />
      </ReviewPreparationProvider>
    </MemoryRouter>;
    const mounted = render(view(true));
    await waitFor(() => expect(fetchProgress).toHaveBeenCalledTimes(1));
    expect(screen.getAllByRole("region", { name: "Review preparation" })).toHaveLength(1);
    expect(within(screen.getByRole("region", { name: "Review preparation" })).getByRole("status"))
      .toHaveTextContent("Preparing first visual diff");
    expect(screen.getByTestId("review-loading-screen")).toBeInTheDocument();
    expect(screen.getByTestId("location").textContent).toContain("startup");
    mounted.rerender(view(false));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Review preparation" })).toBeNull());
    expect(screen.getByTestId("location").textContent).toBe("/r/1234abcd");
  });

  it("passes the Open code action from the Reader through the shared preparation screen", () => {
    const onOpenCode = vi.fn();
    render(<MemoryRouter initialEntries={["/r/1234abcd"]}>
      <ReviewPreparationProvider>
        <ReviewPreparation /><ReaderProgress pending onOpenCode={onOpenCode} />
      </ReviewPreparationProvider>
    </MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Open code" }));
    expect(onOpenCode).toHaveBeenCalledTimes(1);
  });

  it("paints a preparation surface while the first status request is pending", () => {
    fetchProgress.mockImplementationOnce(() => new Promise(() => {}));
    mount();
    expect(screen.getByRole("heading", { name: "Preparing your review" })).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText("Starting preparation…")).toBeInTheDocument();
  });

  it("opens ready source before durable state finishes and keeps its progress panel", async () => {
    fetchProgress.mockResolvedValue(new Response(JSON.stringify({
      id, state: "running", detail: "", review_url: "/r/1234abcd#p=prep-1&sr=1", steps,
    })));
    mount();
    await waitFor(() => expect(screen.getByTestId("location").textContent)
      .toBe(`/r/1234abcd?startup=${id}#p=prep-1&sr=1`));
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "33");
    fireEvent.click(screen.getByRole("button", { name: "Show preparation steps" }));
    expect(screen.getByText("Loading parts and marks")).toBeInTheDocument();
    expect(screen.getByText("0.2s")).toBeInTheDocument();
  });
  it("updates the revision file count while keeping the overall step count", async () => {
    const snapshotSteps = (count: number) => [steps[0], {
      ...steps[1], label: `Creating new revision · Finding changed parts · ${count}/2249 files`,
    }, steps[2]];
    fetchProgress.mockResolvedValueOnce(new Response(JSON.stringify({
      id, state: "running", review_url: "", steps: snapshotSteps(0),
    }))).mockResolvedValue(new Response(JSON.stringify({
      id, state: "running", review_url: "", steps: snapshotSteps(1200),
    })));
    mount();
    const preparation = within(screen.getByRole("region", { name: "Review preparation" }));
    await waitFor(() => expect(preparation.getByRole("status")).toHaveTextContent("0/2249 files"));
    await waitFor(() => expect(preparation.getByRole("status")).toHaveTextContent("1200/2249 files"), { timeout: 3000 });
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "1 of 3 steps complete");
    expect(screen.getAllByText(/1200\/2249 files/)).toHaveLength(2);
  });

  it("automatically removes completed progress and preserves the Reader's URL state", async () => {
    fetchProgress.mockResolvedValueOnce(new Response(JSON.stringify({
      id, state: "running", detail: "", review_url: "/r/1234abcd", steps,
    }))).mockResolvedValue(new Response(JSON.stringify({
      id, state: "ready", detail: "Review ready", review_url: "/r/1234abcd", steps,
    })));
    mount(`/r/1234abcd?view=implementation&startup=${id}#target-1`);
    await waitFor(() => expect(screen.getByRole("region", { name: "Review preparation" })).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("location").textContent)
      .toBe("/r/1234abcd?view=implementation#target-1"), { timeout: 3000 });
    expect(screen.queryByRole("region", { name: "Review preparation" })).not.toBeInTheDocument();
    expect(fetchProgress).toHaveBeenCalledTimes(2);
  });

  it("goes directly to the Reader without carrying completed preparation state", async () => {
    fetchProgress.mockResolvedValue(new Response(JSON.stringify({
      id, state: "ready", detail: "Review ready", review_url: "/r/1234abcd#p=prep-1&sr=1", steps,
    })));
    mount();
    await waitFor(() => expect(screen.getByTestId("location").textContent)
      .toBe("/r/1234abcd#p=prep-1&sr=1"));
    expect(screen.queryByRole("region", { name: "Review preparation" })).not.toBeInTheDocument();
  });

  it("keeps source usable when terminal state fails", async () => {
    fetchProgress.mockResolvedValue(new Response(JSON.stringify({
      id, state: "failed", detail: "State load timed out", review_url: "/r/1234abcd",
      steps: [steps[0], { ...steps[2], state: "failed" }],
    })));
    mount();
    await waitFor(() => expect(screen.getByTestId("location").textContent)
      .toBe(`/r/1234abcd?startup=${id}`));
    expect(screen.getByText("State load timed out")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Review preparation stopped" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss preparation progress" }));
    expect(screen.queryByRole("region", { name: "Review preparation" })).not.toBeInTheDocument();
  });

  it("an address that cannot be a preparation says so, with a way home, and never polls", () => {
    mount("/reviews/preparing/not-an-id");
    expect(screen.getByTestId("preparation-not-found")).toHaveTextContent("This preparation link isn't valid");
    expect(screen.getByRole("link", { name: "All reviews" })).toHaveAttribute("href", "/reviews");
    expect(fetchProgress).not.toHaveBeenCalled();
  });

  it("shows an expired job with a recovery action and stops polling", async () => {
    fetchProgress.mockResolvedValue(new Response("Expired", { status: 404 }));
    mount();
    await waitFor(() => expect(screen.getByText("This preparation has expired."))
      .toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Open reviews" })).toHaveAttribute("href", "/reviews");
    // Nothing will open and reconnecting cannot help: neither is offered.
    expect(screen.queryByText("The review opens as soon as the source revision is ready.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Reconnect" })).toBeNull();
    expect(screen.getByText("lc review").tagName).toBe("CODE");
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 950)); });
    expect(fetchProgress).toHaveBeenCalledTimes(1);
  });

  it("offers a way out when one step stops moving, and not before", () => {
    vi.useFakeTimers();
    try {
      render(<MemoryRouter><PreparationProgress fullPage steps={[
        { key: "reader_structure", label: "Reading review structure", state: "running", elapsed: null },
      ]} /></MemoryRouter>);
      act(() => vi.advanceTimersByTime(PREPARATION_STALL_MS - 2000));
      expect(screen.queryByTestId("preparation-stalled")).not.toBeInTheDocument();
      act(() => vi.advanceTimersByTime(3000));
      const stalled = screen.getByTestId("preparation-stalled");
      expect(within(stalled).getByRole("button", { name: "Reload" })).toBeInTheDocument();
      expect(within(stalled).getByRole("link", { name: "Open reviews" })).toHaveAttribute("href", "/reviews");
    } finally {
      vi.useRealTimers();
    }
  });

  it("abandons a progress request that never answers and keeps polling", async () => {
    vi.useFakeTimers();
    try {
      fetchProgress.mockImplementation((_path, init) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }));
      mount();
      expect(fetchProgress).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(15_100); });
      expect(screen.getByText("Cannot load preparation progress. Reconnecting…")).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(2_100); });
      expect(fetchProgress).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
