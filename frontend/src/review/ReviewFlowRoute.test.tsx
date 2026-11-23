import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useNavigationType, type NavigateFunction } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewFlowRoute, readLocalSendState, type ReviewFlowFactsSource, type ReviewSendState } from "./ReviewFlowRoute";
import { noteRouterLocation, resetBootLocationForTest } from "./reviewBootLocation";
import { buildReviewFlowPath } from "./reviewRoutes";
import { useReviewFlowNavigation } from "./useReviewFlowNavigation";

const NOTHING: ReviewSendState = { sentRecord: false, notesSentToAgent: false, latestRevision: 0 };
const VERDICT: ReviewSendState = { sentRecord: true, notesSentToAgent: false, latestRevision: 1 };
const DELIVERED_AND_REVISION_2: ReviewSendState = { sentRecord: false, notesSentToAgent: true, latestRevision: 2 };

function facts(overrides: Partial<ReviewFlowFactsSource> = {}): ReviewFlowFactsSource & { read: ReturnType<typeof vi.fn> } {
  const read = vi.fn(async () => NOTHING);
  return { read, readSendState: read, readSecondReviewer: async () => false, ...overrides } as ReviewFlowFactsSource & {
    read: ReturnType<typeof vi.fn>;
  };
}

let navigateRef: NavigateFunction = () => undefined;

function Reader() {
  const location = useLocation();
  const navType = useNavigationType();
  navigateRef = useNavigate();
  const { goTo } = useReviewFlowNavigation("shop");
  return (
    <div data-testid="reader">
      <span data-testid="where">{`${location.pathname}${location.search}${location.hash}`}</span>
      <span data-testid="how">{navType}</span>
      <button onClick={() => goTo({ kind: "one", part: "can_view_order" })}>to-one</button>
      <button onClick={() => goTo({ kind: "wrap" })}>to-wrap</button>
      <button onClick={() => goTo({ kind: "done" })}>to-done</button>
      <button onClick={() => goTo({ kind: "overview" }, { replace: true })}>replace-overview</button>
    </div>
  );
}

function Boot() {
  // The app notes the router's location on every render (App.tsx).
  noteRouterLocation(useLocation());
  return null;
}

function mount(entry: string, source: ReviewFlowFactsSource) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Boot />
      <Routes>
        <Route path="/r/:reviewRef/*" element={<ReviewFlowRoute facts={source}><Reader /></ReviewFlowRoute>} />
        <Route path="/home" element={<div data-testid="home" />} />
      </Routes>
    </MemoryRouter>,
  );
}

const go = async (to: string | number) => act(async () => { (navigateRef as (to: string | number) => void)(to); });
const how = () => screen.getByTestId("how").textContent;

const where = () => screen.getByTestId("where").textContent;
const frame = () => document.querySelector("[data-review-flow]");

describe("ReviewFlowRoute", () => {
  beforeEach(resetBootLocationForTest);
  afterEach(resetBootLocationForTest);

  it("marks the screen the address names and keeps the address", async () => {
    mount("/r/shop/one/order_total", facts());
    expect(where()).toBe("/r/shop/one/order_total");
    expect(frame()?.getAttribute("data-review-flow")).toBe("one");
    expect(frame()?.getAttribute("data-review-flow-part")).toBe("order_total");
  });

  it("opens the Overview for the bare review address", () => {
    mount("/r/shop", facts());
    expect(frame()?.getAttribute("data-review-flow")).toBe("overview");
  });

  it("replaces an unknown path under a review with the Overview, leaving no entry behind", async () => {
    mount("/r/shop/nope", facts());
    await waitFor(() => expect(where()).toBe("/r/shop"));
    expect(how()).toBe("REPLACE");
  });

  it.each([
    ["/r/shop/tour", "/r/shop/walkthrough"],
    ["/r/shop/tour/api", "/r/shop/walkthrough/api"],
    ["/r/shop?view=tour&guided=api", "/r/shop/walkthrough/api"],
    ["/r/shop?map=architecture&map-story=file%3Aa", "/r/shop/walkthrough?view=map"],
  ])("replaces the walkthrough's old address %s with %s", async (entry, walkthrough) => {
    mount(entry, facts());
    await waitFor(() => expect(where()).toBe(walkthrough));
    expect(how()).toBe("REPLACE");
    expect(frame()?.getAttribute("data-review-flow")).toBe("walkthrough");
  });

  describe("R5: a cold /done or /rev/<n>", () => {
    it.each(["/r/shop/done", "/r/shop/rev/2"])("with nothing sent, %s is replaced by the Overview", async (entry) => {
      const source = facts();
      mount(entry, source);
      expect(screen.queryByTestId("reader")).toBeNull();
      await waitFor(() => expect(where()).toBe("/r/shop"));
      expect(source.read).toHaveBeenCalledWith("shop", entry.endsWith("/rev/2") ? 2 : 0);
      expect(how()).toBe("REPLACE");
    });

    it("with a persisted pending or undone send, /done stays on reload without a premature verdict", async () => {
      for (const state of ["pending", "undone", "failed"] as const) {
        mount("/r/shop/done", facts({ readSendState: async () => ({ ...NOTHING, flowSendExists: true }) }));
        await waitFor(() => expect(screen.getByTestId("reader")).toBeInTheDocument());
        expect(where(), state).toBe("/r/shop/done");
        cleanup(); resetBootLocationForTest();
      }
    });

    it("with a verdict recorded, /done stays", async () => {
      mount("/r/shop/done", facts({ readSendState: async () => VERDICT }));
      await waitFor(() => expect(screen.getByTestId("reader")).toBeInTheDocument());
      expect(where()).toBe("/r/shop/done");
    });

    it("/rev/<n> needs feedback delivered to the agent and a revision n after the first that exists, not a verdict", async () => {
      const cases: Array<[string, ReviewSendState, boolean]> = [
        ["/r/shop/rev/2", DELIVERED_AND_REVISION_2, true],
        ["/r/shop/rev/2", { ...DELIVERED_AND_REVISION_2, sentRecord: true }, true],
        ["/r/shop/rev/2", VERDICT, false],
        ["/r/shop/rev/2", { ...DELIVERED_AND_REVISION_2, latestRevision: 1 }, false],
        ["/r/shop/rev/3", DELIVERED_AND_REVISION_2, false],
        ["/r/shop/rev/1", DELIVERED_AND_REVISION_2, false],
        ["/r/shop/rev/2", { ...DELIVERED_AND_REVISION_2, notesSentToAgent: false }, false],
        ["/r/shop/rev/3", { ...DELIVERED_AND_REVISION_2, latestRevision: 3 }, true],
      ];
      for (const [entry, state, stays] of cases) {
        resetBootLocationForTest();
        mount(entry, facts({ readSendState: async () => state }));
        if (stays) await waitFor(() => expect(screen.getByTestId("reader")).toBeInTheDocument());
        else await waitFor(() => expect(where()).toBe("/r/shop"));
        expect(where(), `${entry} ${JSON.stringify(state)}`).toBe(stays ? entry : "/r/shop");
        cleanup();
      }
    });

    it("asks about Revision n for /rev/<n> and about nothing in particular otherwise", async () => {
      const source = facts();
      source.read.mockResolvedValue(DELIVERED_AND_REVISION_2);
      mount("/r/shop/rev/2", source);
      await waitFor(() => expect(source.read).toHaveBeenCalledWith("shop", 2));
      cleanup();
      resetBootLocationForTest();
      mount("/r/shop/done", source);
      await waitFor(() => expect(source.read).toHaveBeenCalledWith("shop", 0));
    });

    it("shows nothing while it checks, then decides", async () => {
      let resolve: (state: ReviewSendState) => void = () => undefined;
      const source = facts({ readSendState: () => new Promise<ReviewSendState>((r) => { resolve = r; }) });
      mount("/r/shop/done", source);
      expect(screen.queryByTestId("reader")).toBeNull();
      await act(async () => resolve(VERDICT));
      expect(screen.getByTestId("reader")).toBeInTheDocument();
    });

    it("treats a failed check as nothing sent", async () => {
      mount("/r/shop/rev/2", facts({ readSendState: async () => { throw new Error("offline"); } }));
      await waitFor(() => expect(where()).toBe("/r/shop"));
    });

    it("does not ask for a screen that needs no record", () => {
      const source = facts();
      mount("/r/shop/wrap", source);
      expect(source.read).not.toHaveBeenCalled();
      expect(where()).toBe("/r/shop/wrap");
    });

    it("a Back to an entry the page did not load with is never cold (P1-3 D)", async () => {
      const source = facts();
      mount("/r/shop", source);
      await go("/r/shop/done");
      await go("/r/shop/wrap");
      await go(-1);
      expect(where()).toBe("/r/shop/done");
      await go(-2);
      expect(where()).toBe("/r/shop");
      await go(2);
      expect(where()).toBe("/r/shop/wrap");
      expect(source.read).not.toHaveBeenCalled();
    });

    it("opens Done from a link followed inside the session without asking", async () => {
      const source = facts();
      mount("/r/shop", source);
      await go("/r/shop/done");
      expect(where()).toBe("/r/shop/done");
      expect(source.read).not.toHaveBeenCalled();
    });
  });

  describe("R8: /second", () => {
    it("is replaced by the Overview for anyone but the second reviewer", async () => {
      mount("/r/shop/second", facts());
      await waitFor(() => expect(where()).toBe("/r/shop"));
      expect(how()).toBe("REPLACE");
    });
    it("stays for the second reviewer, on a cold load and on a link followed inside the session", async () => {
      const source = facts({ readSecondReviewer: async () => true });
      mount("/r/shop/second", source);
      await waitFor(() => expect(frame()?.getAttribute("data-review-flow")).toBe("second"));
      expect(where()).toBe("/r/shop/second");
      await go("/r/shop/wrap");
      await go("/r/shop/second");
      expect(where()).toBe("/r/shop/second");
    });
    it("is replaced even by a link followed inside the session", async () => {
      mount("/r/shop", facts());
      await go("/r/shop/second");
      await waitFor(() => expect(where()).toBe("/r/shop"));
    });
  });

  describe("NAV-1: Back and Forward step between screens and parts", () => {
    it("pushes an entry per screen and part, and replace leaves none", async () => {
      mount("/r/shop", facts());
      const click = async (name: string) => act(async () => { screen.getByRole("button", { name }).click(); });
      await click("to-one");
      expect(where()).toBe("/r/shop/one/can_view_order");
      expect(how()).toBe("PUSH");
      await click("to-wrap");
      expect(where()).toBe("/r/shop/wrap");
      await go(-1);
      expect(where()).toBe("/r/shop/one/can_view_order");
      expect(frame()?.getAttribute("data-review-flow-part")).toBe("can_view_order");
      await go(-1);
      expect(where()).toBe("/r/shop");
      await go(1);
      await go(1);
      expect(where()).toBe("/r/shop/wrap");
      await click("replace-overview");
      expect(where()).toBe("/r/shop");
      expect(how()).toBe("REPLACE");
      await go(-1);
      expect(where()).toBe("/r/shop/one/can_view_order");
    });

    it("builds the address from the review and the screen", async () => {
      mount("/r/shop", facts());
      expect(buildReviewFlowPath("shop", { kind: "one", part: "can_view_order" })).toBe("/r/shop/one/can_view_order");
    });
  });

  it("renders its children untouched when the address is not a flow screen", () => {
    mount("/r/shop/compare/1..2", facts());
    expect(screen.getByTestId("reader")).toBeInTheDocument();
    expect(frame()).toBeNull();
  });
});

describe("readLocalSendState fails closed", () => {
  afterEach(() => vi.restoreAllMocks());

  /** Answers by endpoint; `undefined` is a failed read. */
  const answer = (bodies: { outcome?: unknown; revisions?: unknown; annotations?: unknown }) =>
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      const body = url.includes("/outcome") ? bodies.outcome : url.includes("/annotations") ? bodies.annotations : bodies.revisions;
      return Promise.resolve(
        body === undefined
          ? new Response("no", { status: 500 })
          : new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
      );
    });
  const feedback = (published: number, addressed = 0) => ({ feedback: { published, addressed, unpublished: 0, in_flight: 0, open_total: 1 } });

  it("reads a verdict and the latest revision", async () => {
    answer({ outcome: { current: { outcome: "lgtm" }, history: [] }, revisions: { latest_revision_number: 1 } });
    expect(await readLocalSendState("shop", 2)).toEqual({ sentRecord: true, notesSentToAgent: false, latestRevision: 2 });
  });

  it("reads Revision n-1's own annotation list for /rev/<n>: wire n-2", async () => {
    const spy = answer({ outcome: { current: null }, revisions: { latest_revision_number: 1 }, annotations: feedback(1) });
    expect(await readLocalSendState("shop", 2)).toMatchObject({ notesSentToAgent: true, latestRevision: 2 });
    expect(spy.mock.calls.map(([url]) => String(url))).toContain("/api/reviews/shop/revisions/0/annotations");
    await readLocalSendState("shop", 3);
    expect(spy.mock.calls.map(([url]) => String(url))).toContain("/api/reviews/shop/revisions/1/annotations");
  });

  it("counts delivered or addressed feedback as notes sent, and nothing else", async () => {
    const base = { outcome: { current: null }, revisions: { latest_revision_number: 1 } };
    answer({ ...base, annotations: feedback(1) });
    expect((await readLocalSendState("shop", 2)).notesSentToAgent).toBe(true);
    answer({ ...base, annotations: feedback(0, 2) });
    expect((await readLocalSendState("shop", 2)).notesSentToAgent).toBe(true);
    answer({ ...base, annotations: feedback(0, 0) });
    expect((await readLocalSendState("shop", 2)).notesSentToAgent).toBe(false);
  });

  it("does not take a verdict, or the other screens' reads, for notes sent", async () => {
    answer({ outcome: { current: { outcome: "changes_requested" }, history: [] }, revisions: { latest_revision_number: 1 }, annotations: feedback(5) });
    expect((await readLocalSendState("shop", 0)).notesSentToAgent).toBe(false);
    expect((await readLocalSendState("shop", 1)).notesSentToAgent).toBe(false);
  });

  it("counts revisions from 1 where the wire counts from 0", async () => {
    answer({ outcome: { current: null }, revisions: { latest_revision_number: 0 } });
    expect((await readLocalSendState("shop")).latestRevision).toBe(1);
    answer({ outcome: { current: null }, revisions: { latest_revision_number: 2 } });
    expect((await readLocalSendState("shop")).latestRevision).toBe(3);
  });

  it("counts a 200 answer without a current verdict as nothing sent", async () => {
    answer({ outcome: {}, revisions: { latest_revision_number: 1 } });
    expect(await readLocalSendState("shop")).toEqual({ sentRecord: false, notesSentToAgent: false, latestRevision: 2 });
    answer({ outcome: { current: null, history: [] }, revisions: {} });
    expect(await readLocalSendState("shop")).toEqual(NOTHING);
  });

  it("answers nothing sent when the reads fail or are malformed", async () => {
    answer({});
    expect(await readLocalSendState("shop", 2)).toEqual(NOTHING);
    answer({ outcome: { current: { outcome: "lgtm" } }, revisions: { latest_revision_number: "one" }, annotations: { feedback: { published: "1", addressed: null } } });
    expect(await readLocalSendState("shop", 2)).toEqual({ sentRecord: true, notesSentToAgent: false, latestRevision: 0 });
  });
});
