// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { dismissFlowToast, FlowToastHost, showFlowToast, TOAST_MS, UNDO_TOAST_MS } from "./flowToast";

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { act(() => dismissFlowToast()); cleanup(); vi.useRealTimers(); });

const show = (toast: Parameters<typeof showFlowToast>[0]) => act(() => showFlowToast(toast));
const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });

describe("shared toast (SPEC 4.20, R12)", () => {
  it("shows one toast even when two screens each mount a host", () => {
    render(<MemoryRouter><FlowToastHost /><FlowToastHost /></MemoryRouter>);
    show({ text: "Link copied" });
    expect(screen.getAllByTestId("flow-toast")).toHaveLength(1);
  });

  it("the newest toast replaces the last", () => {
    render(<MemoryRouter><FlowToastHost /></MemoryRouter>);
    show({ text: "first" });
    show({ text: "second" });
    expect(screen.getByTestId("flow-toast")).toHaveTextContent("second");
    expect(screen.queryByText("first")).toBeNull();
  });

  it("goes after 5 s, and after 10 s with Undo", () => {
    render(<MemoryRouter><FlowToastHost /></MemoryRouter>);
    show({ text: "plain" });
    advance(TOAST_MS - 1);
    expect(screen.queryByTestId("flow-toast")).not.toBeNull();
    advance(1);
    expect(screen.queryByTestId("flow-toast")).toBeNull();
    show({ text: "with undo", undo: vi.fn() });
    advance(TOAST_MS + 1);
    expect(screen.queryByTestId("flow-toast")).not.toBeNull();
    advance(UNDO_TOAST_MS - TOAST_MS);
    expect(screen.queryByTestId("flow-toast")).toBeNull();
  });

  it("identical text restarts the timer", () => {
    render(<MemoryRouter><FlowToastHost /></MemoryRouter>);
    show({ text: "Link copied" });
    advance(4_000);
    show({ text: "Link copied" });
    advance(4_000);
    expect(screen.queryByTestId("flow-toast")).not.toBeNull();
    advance(1_000);
    expect(screen.queryByTestId("flow-toast")).toBeNull();
  });

  it("the timer pauses on hover and on focus", () => {
    render(<MemoryRouter><FlowToastHost /></MemoryRouter>);
    show({ text: "held" });
    fireEvent.mouseEnter(screen.getByTestId("flow-toast"));
    advance(TOAST_MS * 3);
    expect(screen.queryByTestId("flow-toast")).not.toBeNull();
    fireEvent.mouseLeave(screen.getByTestId("flow-toast"));
    advance(TOAST_MS);
    expect(screen.queryByTestId("flow-toast")).toBeNull();
    show({ text: "focused" });
    fireEvent.focus(screen.getByTestId("flow-toast"));
    advance(TOAST_MS * 3);
    expect(screen.queryByTestId("flow-toast")).not.toBeNull();
  });

  it("Dismiss closes it", () => {
    render(<MemoryRouter><FlowToastHost /></MemoryRouter>);
    show({ text: "bye" });
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("flow-toast")).toBeNull();
  });

  it("Undo runs the action and closes", () => {
    const undo = vi.fn();
    render(<MemoryRouter><FlowToastHost /></MemoryRouter>);
    show({ text: "Snoozed", undo });
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(undo).toHaveBeenCalledOnce();
    expect(screen.queryByTestId("flow-toast")).toBeNull();
  });

  it("clears on a route change", () => {
    let go: (to: string) => void = () => undefined;
    function Nav() { const navigate = useNavigate(); go = navigate; return null; }
    render(<MemoryRouter initialEntries={["/a"]}><Nav /><FlowToastHost /></MemoryRouter>);
    show({ text: "stays on /a" });
    expect(screen.queryByTestId("flow-toast")).not.toBeNull();
    act(() => go("/b"));
    expect(screen.queryByTestId("flow-toast")).toBeNull();
  });

  it("a toast closed while hovered does not freeze the next one", () => {
    render(<MemoryRouter><FlowToastHost /></MemoryRouter>);
    show({ text: "first" });
    fireEvent.mouseEnter(screen.getByTestId("flow-toast"));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("flow-toast")).toBeNull();
    show({ text: "second" });
    advance(TOAST_MS);
    expect(screen.queryByTestId("flow-toast")).toBeNull();
  });
});
