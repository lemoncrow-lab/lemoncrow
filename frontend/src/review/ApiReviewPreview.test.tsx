import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ApiReviewPreview from "./ApiReviewPreview";
import type { ReviewSurfaceInfo, ReviewSurfaceRunResult } from "./types";

const surface: ReviewSurfaceInfo = {
  id: "api/status.bru",
  provider: "bruno",
  kind: "api.request",
  title: "Status",
  locator: "GET http://localhost:8000/status",
  runtime: "api-runtime",
  affected_paths: ["api/status.bru", "src/status.py"],
  capabilities: ["source", "execute", "results", "compare"],
  metadata: { method: "GET", url: "http://localhost:8000/status" },
};

function result(side: "old" | "new", statusCode: number, body: string): ReviewSurfaceRunResult {
  return {
    surface_id: surface.id,
    provider: surface.provider,
    revision_id: "rrv-4",
    side,
    status: "passed",
    summary: `HTTP ${statusCode}`,
    runner: "service",
    runtime: surface.runtime,
    exit_code: 0,
    duration_ms: side === "old" ? 8 : 9,
    output: "",
    data: { status_code: statusCode, body },
  };
}

describe("ApiReviewPreview", () => {
  it("shows before/after behavior as two sides of the exact revision", () => {
    render(
      <ApiReviewPreview
        surface={surface}
        mode="compare"
        run={{ old: result("old", 200, '{"state":"old"}'), new: result("new", 201, '{"state":"new"}') }}
        onModeChange={vi.fn()}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText("Before")).toBeTruthy();
    expect(screen.getByText("After")).toBeTruthy();
    expect(screen.getByText("Revision · old")).toBeTruthy();
    expect(screen.getByText("Revision · new")).toBeTruthy();
    expect(screen.getByText(/"state": "old"/)).toBeTruthy();
    expect(screen.getByText(/"state": "new"/)).toBeTruthy();
  });

  it("renders the response pane with HTTP status, assertions and a pretty JSON body", async () => {
    const onRun = vi.fn();
    render(
      <ApiReviewPreview
        surface={surface}
        mode="preview"
        run={{ new: { ...result("new", 200, ""), summary: "status ok", data: { status: 200, json: { ok: true }, assertions: [{ name: "status is 200", ok: true }] } } }}
        onModeChange={vi.fn()}
        onRun={onRun}
      />,
    );

    expect(screen.getByText("GET")).toBeTruthy();
    expect(screen.getByText("Response")).toBeTruthy();
    expect(screen.getByText("HTTP 200")).toBeTruthy();
    expect(screen.getByText("Assertions")).toBeTruthy();
    expect(screen.getByText('{ "name": "status is 200", "ok": true }', { normalizer: (text) => text.replace(/\s+/g, " ").trim() })).toBeTruthy();
    expect(screen.getByText((_content, element) => element?.tagName === "PRE" && element.textContent === '{\n  "ok": true\n}')).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Run request" }));
    expect(onRun).toHaveBeenCalledWith(false);
  });

  it("shows a non-JSON body verbatim and falls back to runner output without a body", () => {
    const { rerender } = render(
      <ApiReviewPreview
        surface={surface}
        mode="preview"
        run={{ new: result("new", 500, "<html>Internal Server Error</html>") }}
        onModeChange={vi.fn()}
        onRun={vi.fn()}
      />,
    );
    expect(screen.getByText("<html>Internal Server Error</html>")).toBeTruthy();

    rerender(
      <ApiReviewPreview
        surface={surface}
        mode="preview"
        run={{ new: { ...result("new", 0, ""), status: "error", data: {}, output: "connect ECONNREFUSED 127.0.0.1:8000" } }}
        onModeChange={vi.fn()}
        onRun={vi.fn()}
      />,
    );
    expect(screen.getByText("connect ECONNREFUSED 127.0.0.1:8000")).toBeTruthy();
    expect(screen.getByText("error").className).toContain("text-amber-300");
    expect(screen.queryByText(/^HTTP /, { selector: "span" })).toBeNull();
  });

  it("never rounds large JSON integers in a response body", () => {
    render(
      <ApiReviewPreview
        surface={surface}
        mode="preview"
        run={{ new: result("new", 200, '{"order_id":12345678901234567891,"total":1e400}') }}
        onModeChange={vi.fn()}
        onRun={vi.fn()}
      />,
    );

    const body = screen.getByText((_content, element) => element?.tagName === "PRE");
    expect(body.textContent).toContain("12345678901234567891");
    expect(body.textContent).toContain("1e400");
    expect(body.textContent).not.toContain("12345678901234567000");
  });

  it("shows run failures, keeps the definition reviewable without a runtime, and disables a running request", () => {
    const { rerender } = render(
      <ApiReviewPreview
        surface={{ ...surface, runtime: "" }}
        mode="compare"
        run={{ error: "api-runtime is not pinned to revision rrv-4" }}
        onModeChange={vi.fn()}
        onRun={vi.fn()}
      />,
    );
    expect(screen.getByText("api-runtime is not pinned to revision rrv-4")).toBeTruthy();
    expect(screen.getByText(/Response execution needs a revision-pinned runtime/)).toBeTruthy();
    expect(screen.getAllByText("Not run yet")).toHaveLength(2);
    expect(screen.queryByRole("button")).toBeNull();

    rerender(<ApiReviewPreview surface={surface} mode="compare" running onModeChange={vi.fn()} onRun={vi.fn()} />);
    expect((screen.getByRole("button", { name: "Running…" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("presents an unavailable hosted API runner as an environment limitation", () => {
    render(
      <ApiReviewPreview
        surface={surface}
        mode="preview"
        run={{ error: "review runtime 'demo-api' uses unavailable runner 'http-service'" }}
        onModeChange={vi.fn()}
        onRun={vi.fn()}
      />,
    );

    const message = screen.getByText("This check cannot run in this review environment. Its definition remains reviewable.");
    expect(message.className).toContain("text-amber-300");
    expect(screen.queryByText(/uses unavailable runner/)).toBeNull();
    expect(screen.queryByText("Not run yet")).toBeNull();
    expect((screen.getByRole("button", { name: "Run request" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("switches preview/compare with 1 and 2 only while active and not typing", async () => {
    const onModeChange = vi.fn();
    const { rerender } = render(
      <>
        <input aria-label="elsewhere" />
        <ApiReviewPreview surface={surface} mode="preview" onModeChange={onModeChange} onRun={vi.fn()} />
      </>,
    );
    await userEvent.keyboard("2");
    expect(onModeChange).not.toHaveBeenCalled();

    rerender(
      <>
        <input aria-label="elsewhere" />
        <ApiReviewPreview surface={surface} mode="preview" active onModeChange={onModeChange} onRun={vi.fn()} />
      </>,
    );
    await userEvent.keyboard("2");
    expect(onModeChange).toHaveBeenLastCalledWith("compare");
    await userEvent.keyboard("1");
    expect(onModeChange).toHaveBeenLastCalledWith("preview");

    await userEvent.click(screen.getByLabelText("elsewhere"));
    await userEvent.keyboard("2");
    expect(onModeChange).toHaveBeenCalledTimes(2);
  });
});
