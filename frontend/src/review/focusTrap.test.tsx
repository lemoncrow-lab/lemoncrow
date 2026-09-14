import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { trapModalTab } from "./focusTrap";

function Trap({ children, onOtherKey, testId = "overlay" }: { children?: React.ReactNode; onOtherKey?: () => void; testId?: string }) {
  return (
    <div
      data-testid={testId}
      tabIndex={-1}
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        onOtherKey?.();
      }}
    >
      {children}
    </div>
  );
}

function LateContent() {
  const [ready, setReady] = useState(false);
  return (
    <Trap>
      <button type="button" onClick={() => setReady(true)}>load</button>
      {ready && <button type="button">late</button>}
    </Trap>
  );
}

describe("trapModalTab", () => {
  it("wraps Shift+Tab from the first focusable back to the last", async () => {
    render(
      <>
        <button type="button">outside</button>
        <Trap>
          <button type="button">first</button>
          <button type="button">last</button>
        </Trap>
      </>,
    );

    screen.getByRole("button", { name: "first" }).focus();
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "last" }));
  });

  it("wraps Tab from the last focusable back to the first", async () => {
    render(
      <>
        <Trap>
          <button type="button">first</button>
          <button type="button">last</button>
        </Trap>
        <button type="button">outside</button>
      </>,
    );

    screen.getByRole("button", { name: "last" }).focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "first" }));
  });

  it("holds focus inside a dialog that has no focusable children", async () => {
    render(
      <>
        <button type="button">outside</button>
        <Trap>
          <p>Nothing here can take focus.</p>
        </Trap>
      </>,
    );

    const overlay = screen.getByTestId("overlay");
    overlay.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(overlay);
  });

  it("picks up focusable children that render after mount", async () => {
    render(<LateContent />);

    const load = screen.getByRole("button", { name: "load" });
    load.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(load);

    await userEvent.click(load);
    const late = screen.getByRole("button", { name: "late" });
    load.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(late);
    await userEvent.tab();
    expect(document.activeElement).toBe(load);
  });

  it("traps each stacked dialog against its own surface", async () => {
    render(
      <>
        <Trap testId="under">
          <button type="button">under first</button>
          <button type="button">under last</button>
        </Trap>
        <Trap testId="over">
          <button type="button">over first</button>
          <button type="button">over last</button>
        </Trap>
      </>,
    );

    screen.getByRole("button", { name: "over last" }).focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "over first" }));
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "over last" }));
  });

  it("leaves keys other than Tab to the caller", async () => {
    const onOtherKey = vi.fn();
    render(
      <Trap onOtherKey={onOtherKey}>
        <button type="button">only</button>
      </Trap>,
    );

    screen.getByRole("button", { name: "only" }).focus();
    await userEvent.keyboard("{Escape}");
    expect(onOtherKey).toHaveBeenCalledTimes(1);

    await userEvent.tab();
    expect(onOtherKey).toHaveBeenCalledTimes(1);
  });

  it("stops trapping once the dialog unmounts", async () => {
    const view = render(
      <>
        <button type="button">page one</button>
        <button type="button">page two</button>
        <Trap>
          <button type="button">dialog</button>
        </Trap>
      </>,
    );

    view.rerender(
      <>
        <button type="button">page one</button>
        <button type="button">page two</button>
      </>,
    );

    screen.getByRole("button", { name: "page one" }).focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "page two" }));
  });
});
