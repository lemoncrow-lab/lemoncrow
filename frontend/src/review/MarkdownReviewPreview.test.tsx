import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  currentReviewId: vi.fn(() => "review-1"),
  fetchMarkdownImage: vi.fn(),
}));

vi.mock("./reviewApi", () => api);

import MarkdownReviewPreview, { changedMarkdownRanges } from "./MarkdownReviewPreview";

describe("MarkdownReviewPreview", () => {
  it("maps only changed patch lines, not surrounding hunk context", () => {
    const patch = [
      "diff --git a/README.md b/README.md",
      "--- a/README.md",
      "+++ b/README.md",
      "@@ -2,4 +2,4 @@",
      " context",
      "-old text",
      "+new text",
      " more context",
      " final context",
      "",
    ].join("\n");

    expect(changedMarkdownRanges(patch)).toEqual({
      deletions: [{ start: 3, end: 3 }],
      additions: [{ start: 3, end: 3 }],
    });
  });

  it("renders GFM and safe raw HTML while keeping changed blocks tied to source lines", async () => {
    const onSelect = vi.fn();
    const patch = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# Old\n+# New\n";
    render(
      <MarkdownReviewPreview
        preview={{
          kind: "markdown",
          old_content: "# Old\n",
          new_content: "# New\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n<details><summary>More</summary>Safe HTML</details>\n\n<script>unsafe()</script>\n",
        }}
        patch={patch}
        documentPath="README.md"
        status="modified"
        onSelect={onSelect}
      />,
    );

    const after = screen.getByTestId("markdown-additions");
    const heading = within(after).getByRole("heading", { name: "New" });
    expect(heading.getAttribute("data-changed")).toBe("true");
    expect(within(after).getByRole("table")).toBeTruthy();
    expect(within(after).getByText("Safe HTML")).toBeTruthy();
    expect(screen.queryByText("unsafe()")).toBeNull();

    await userEvent.click(heading);
    expect(onSelect).toHaveBeenCalledWith("additions", 1, 1);
  });

  it("reuses Markdown images within a revision and refreshes them when the revision changes", async () => {
    api.fetchMarkdownImage.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    let nextUrl = 0;
    const createObjectURL = vi.fn(() => `blob:review-image-${nextUrl++}`);
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });

    const view = (revisionId: string) => (
      <MarkdownReviewPreview
        preview={{
          kind: "markdown",
          old_content: "",
          new_content: "![Logo](docs/logo.png)\n",
        }}
        patch={"diff --git a/README.md b/README.md\n--- /dev/null\n+++ b/README.md\n@@ -0,0 +1 @@\n+![Logo](docs/logo.png)\n"}
        documentPath="README.md"
        revisionId={revisionId}
        status="added"
        mode="preview"
        onSelect={vi.fn()}
      />
    );

    const first = render(view("rrv-1"));
    await waitFor(() => expect(screen.getByRole("img", { name: "Logo" }).getAttribute("src")).toBe("blob:review-image-0"));
    expect(api.fetchMarkdownImage).toHaveBeenCalledWith("review-1", "README.md", "new", "docs/logo.png", "rrv-1");
    expect(api.fetchMarkdownImage).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);

    first.unmount();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    const second = render(view("rrv-1"));
    expect(screen.getByRole("img", { name: "Logo" }).getAttribute("src")).toBe("blob:review-image-0");
    expect(api.fetchMarkdownImage).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    second.unmount();

    const third = render(view("rrv-2"));
    await waitFor(() => expect(screen.getByRole("img", { name: "Logo" }).getAttribute("src")).toBe("blob:review-image-1"));
    expect(api.fetchMarkdownImage).toHaveBeenLastCalledWith("review-1", "README.md", "new", "docs/logo.png", "rrv-2");
    expect(api.fetchMarkdownImage).toHaveBeenCalledTimes(2);
    expect(createObjectURL).toHaveBeenCalledTimes(2);
    third.unmount();

    window.dispatchEvent(new Event("pagehide"));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:review-image-0");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:review-image-1");
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });


  it("uses dark inline-code colors for backtick text without changing fenced code backgrounds", () => {
    render(
      <MarkdownReviewPreview
        preview={{
          kind: "markdown",
          old_content: "",
          new_content: "Use `lc review` here.\n\n```sh\nlc review\n```\n",
        }}
        patch={"diff --git a/README.md b/README.md\n--- /dev/null\n+++ b/README.md\n@@ -0,0 +1,5 @@\n+Use `lc review` here.\n+\n+```sh\n+lc review\n+```\n"}
        documentPath="README.md"
        status="added"
        mode="preview"
        theme="dark"
        onSelect={vi.fn()}
      />,
    );

    const inline = screen.getByText("lc review", { selector: "p code" });
    const fenced = screen.getByText("lc review", { selector: "pre code" });
    expect(inline.closest("[data-preview-theme]")?.getAttribute("data-preview-theme")).toBe("dark");
    expect(fenced.closest("[data-preview-theme]")?.getAttribute("data-preview-theme")).toBe("dark");
    expect(inline.tagName).toBe("CODE");
    expect(fenced.parentElement?.tagName).toBe("PRE");
  });

  it("renders one complete after document in preview mode without diff highlighting", () => {
    render(
      <MarkdownReviewPreview
        preview={{
          kind: "markdown",
          old_content: "# Old\n\nOld body.\n",
          new_content: "# New\n\nNew body.\n",
        }}
        patch={"diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,3 +1,3 @@\n-# Old\n+# New\n \n-Old body.\n+New body.\n"}
        documentPath="README.md"
        status="modified"
        mode="preview"
        onSelect={vi.fn()}
      />,
    );

    const heading = screen.getByRole("heading", { name: "New" });
    expect(heading).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Old" })).toBeNull();
    expect(screen.queryByText("Before")).toBeNull();
    expect(screen.queryByText("After")).toBeNull();
    expect(heading.getAttribute("data-changed")).toBe("false");
  });
});
