import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ReviewBaseIdentity from "./ReviewBaseIdentity";
import type { ReviewOverview } from "./types";

function overview(rangeMode: string, recordedBase?: string, effectiveBase = "feedabcd1234567890"): ReviewOverview {
  return {
    session: { range_mode: rangeMode, review_base_sha: recordedBase },
    revision: { base_sha: effectiveBase },
  } as ReviewOverview;
}

describe("Review base identity", () => {
  it("shows one base once when the recorded and compared bases are the same", () => {
    render(<ReviewBaseIdentity overview={overview("working_tree", "fe804a9221ab0000", "fe804a9221ab0000")} />);
    const detail = screen.getByTestId("review-base-identity");
    expect(detail.textContent).toBe("Basefe804a9");
    expect(detail.getAttribute("title")).toBe("Changes are compared against fe804a9221ab0000.");
    expect(detail.textContent).not.toContain("Snapshot");
  });

  it("says plainly what each base is when the compared base moved past the recorded one", () => {
    render(<ReviewBaseIdentity overview={overview("working_tree", "1234567890abcdef", "fedcba9876543210")} />);
    const detail = screen.getByTestId("review-base-identity");
    expect(detail.textContent).toBe("Started from1234567·now compared tofedcba9");
    expect(detail.getAttribute("title")).toContain("The review started from 1234567890abcdef");
    expect(detail.getAttribute("title")).toContain("compared against fedcba9876543210");
    expect(detail.textContent).not.toContain("Review base");
  });

  it.each(["working_tree", "staged"])("names an unfixed base on %s Reviews", (mode) => {
    render(<ReviewBaseIdentity overview={overview(mode, "")} />);
    const detail = screen.getByTestId("review-base-identity");
    expect(detail.textContent).toBe("Basefeedabc· not fixed");
    expect(detail.getAttribute("title")).toContain("no fixed base");
    expect(detail.getAttribute("title")).toContain("feedabcd1234567890");
  });

  it("treats an omitted recorded base as not fixed", () => {
    render(<ReviewBaseIdentity overview={overview("working_tree")} />);
    expect(screen.getByText("· not fixed")).toBeTruthy();
  });

  it("does not apply mutable base semantics to commit-range Reviews", () => {
    render(<ReviewBaseIdentity overview={overview("commit_range")} />);
    expect(screen.queryByTestId("review-base-identity")).toBeNull();
  });
});
