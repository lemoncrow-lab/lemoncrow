import { Suspense, useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "../components/ErrorBoundary";
import { retryableLazy } from "./retryableLazy";

function Loaded() {
  return <div>Review loaded</div>;
}

describe("retryableLazy", () => {
  it("re-imports after a lazy chunk failure when the enclosing boundary retries", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const load = vi.fn<() => Promise<{ default: typeof Loaded }>>()
      .mockRejectedValueOnce(new TypeError("Failed to fetch dynamically imported module"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch dynamically imported module"))
      .mockResolvedValue({ default: Loaded });
    const review = retryableLazy(load);

    function Harness() {
      const [, setGeneration] = useState(0);
      const Review = review.current();
      return (
        <ErrorBoundary
          label="Review"
          fallback={(_error, retry) => (
            <button
              type="button"
              onClick={() => {
                review.reset();
                setGeneration((generation) => generation + 1);
                retry();
              }}
            >
              Retry
            </button>
          )}
        >
          <Suspense fallback={<div>Loading</div>}>
            <Review />
          </Suspense>
        </ErrorBoundary>
      );
    }

    render(<Harness />);

    expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Review loaded")).toBeTruthy();
    // The failed attempt already tried twice; Retry's fresh import loads on its first try.
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("rides out one dropped chunk load without showing a failure", async () => {
    const load = vi.fn<() => Promise<{ default: typeof Loaded }>>()
      .mockRejectedValueOnce(new TypeError("Failed to fetch dynamically imported module"))
      .mockResolvedValue({ default: Loaded });
    const Review = retryableLazy(load).current();

    render(
      <Suspense fallback={<div>Loading</div>}>
        <Review />
      </Suspense>,
    );

    expect(await screen.findByText("Review loaded")).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(2);
  });
});
