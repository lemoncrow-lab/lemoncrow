import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Suspense } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Button as UiButton } from "../components/ui/button";
import { lazyPanel } from "./lazyPanel";

function Sheet({ onClose }: { onClose(): void }) {
  return <UiButton unstyled type="button" onClick={onClose}>Comments sheet</UiButton>;
}

function chunkFailure() {
  return new TypeError("Failed to fetch dynamically imported module: /assets/ReviewCommentsSheet-abc123.js");
}

describe("lazyPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("fails only its own panel when the chunk will not load, and Retry loads it again", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const load = vi.fn<() => Promise<{ default: typeof Sheet }>>()
      .mockRejectedValueOnce(chunkFailure())
      .mockRejectedValueOnce(chunkFailure())
      .mockResolvedValue({ default: Sheet });
    const Panel = lazyPanel("Comments", load);

    render(
      <div>
        <p>Reader keeps its place</p>
        <Suspense fallback={null}>
          <Panel onClose={vi.fn()} />
        </Suspense>
      </div>,
    );

    expect(await screen.findByText("Comments couldn't open")).toBeTruthy();
    expect(screen.getByText("Reader keeps its place")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Comments sheet" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    // The failed open already tried twice; Retry's fresh import loads on its first try.
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("rides out one dropped chunk load without showing a failure", async () => {
    const load = vi.fn<() => Promise<{ default: typeof Sheet }>>()
      .mockRejectedValueOnce(chunkFailure())
      .mockResolvedValue({ default: Sheet });
    const Panel = lazyPanel("Comments", load);

    render(<Suspense fallback={null}><Panel onClose={vi.fn()} /></Suspense>);

    expect(await screen.findByRole("button", { name: "Comments sheet" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("closes the panel from the failure, and opening it again loads afresh", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const load = vi.fn<() => Promise<{ default: typeof Sheet }>>()
      .mockRejectedValueOnce(chunkFailure())
      .mockRejectedValueOnce(chunkFailure())
      .mockResolvedValue({ default: Sheet });
    const Panel = lazyPanel("Comments", load);
    const onClose = vi.fn();

    const { rerender } = render(<Suspense fallback={null}><Panel onClose={onClose} /></Suspense>);
    await userEvent.click(await screen.findByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    rerender(<Suspense fallback={null}>{null}</Suspense>);
    rerender(<Suspense fallback={null}><Panel onClose={onClose} /></Suspense>);
    expect(await screen.findByRole("button", { name: "Comments sheet" })).toBeTruthy();
  });
});
