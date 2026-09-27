import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pdf = vi.hoisted(() => {
  const cancel = vi.fn();
  const render = vi.fn(() => ({ promise: Promise.resolve(), cancel }));
  const getPage = vi.fn(async (_page: number) => ({
    getViewport: () => ({ width: 420, height: 594 }),
    render,
  }));
  const documentProxy = { numPages: 2, getPage };
  const destroy = vi.fn();
  const getDocument = vi.fn(() => ({ promise: Promise.resolve(documentProxy), destroy }));
  return {
    cancel,
    render,
    getPage,
    documentProxy,
    destroy,
    getDocument,
    GlobalWorkerOptions: { workerSrc: "" },
  };
});

vi.mock("pdfjs-dist", () => ({
  getDocument: pdf.getDocument,
  GlobalWorkerOptions: pdf.GlobalWorkerOptions,
}));

vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({
  default: "/assets/pdf.worker.min.mjs",
}));

import PdfReviewDocument from "./PdfReviewDocument";

beforeEach(() => {
  vi.clearAllMocks();
  pdf.GlobalWorkerOptions.workerSrc = "";
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PdfReviewDocument", () => {
  it("renders revision bytes in-app and navigates pages without an iframe", async () => {
    const blob = new Blob(["%PDF-demo"], { type: "application/pdf" });
    const { container } = render(<PdfReviewDocument blob={blob} label="After" />);

    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(1));
    expect(pdf.GlobalWorkerOptions.workerSrc).toBe("/assets/pdf.worker.min.mjs");
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText("1 / 2")).toBeTruthy();
    expect(screen.getByLabelText("After PDF page 1")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Next page of After" }));
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(2));
    expect(screen.getByText("2 / 2")).toBeTruthy();
    expect(screen.getByLabelText("After PDF page 2")).toBeTruthy();
  });
});
