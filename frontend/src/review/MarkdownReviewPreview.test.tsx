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
    expect(screen.queryByText("Revision rrv-1 · rendered Markdown")).toBeNull();
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

describe("MarkdownReviewPreview sanitizer", () => {
  // Repository Markdown is untrusted input rendered inside the authenticated Reader.
  const FORBIDDEN_TAGS = ["script", "style", "iframe", "frame", "object", "embed", "svg", "math", "form", "button", "textarea", "select", "meta", "base", "link", "template", "noscript", "source"];
  const URL_ATTRIBUTES = new Set(["href", "src", "srcset", "action", "formaction", "data", "poster", "xlink:href", "background", "cite", "longdesc"]);
  const EXECUTABLE_SCHEME = /^[\s\u0000-\u001f]*(javascript|vbscript|data):/i;

  function renderUntrusted(content: string) {
    return render(
      <MarkdownReviewPreview
        preview={{ kind: "markdown", old_content: "", new_content: content }}
        patch=""
        documentPath="README.md"
        revisionId="rrv-sanitize"
        status="added"
        mode="preview"
        onSelect={vi.fn()}
      />,
    );
  }

  function expectInert(root: HTMLElement) {
    for (const tag of FORBIDDEN_TAGS) expect(root.querySelector(tag), `<${tag}> survived sanitizing`).toBeNull();
    for (const element of Array.from(root.querySelectorAll("*"))) {
      for (const attribute of Array.from(element.attributes)) {
        const name = attribute.name.toLowerCase();
        expect(name.startsWith("on"), `<${element.tagName.toLowerCase()}> kept ${name}`).toBe(false);
        expect(name, `<${element.tagName.toLowerCase()}> kept an inline style`).not.toBe("style");
        if (URL_ATTRIBUTES.has(name)) {
          expect(attribute.value, `<${element.tagName.toLowerCase()} ${name}>`).not.toMatch(EXECUTABLE_SCHEME);
        }
      }
    }
    // The browser itself only ever loads Reader-proxied image bytes.
    for (const image of Array.from(root.querySelectorAll("img"))) {
      expect(image.getAttribute("src") ?? "").toMatch(/^blob:/);
    }
  }

  it.each([
    ["Markdown javascript: link", "[a](javascript:alert(1))"],
    ["Markdown mixed-case javascript: link", "[a](JaVaScRiPt:alert(1))"],
    ["Markdown vbscript: link", "[a](vbscript:msgbox(1))"],
    ["Markdown data: link", "[a](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)"],
    ["Markdown entity-encoded colon", "[a](javascript&#58;alert(1))"],
    ["Markdown reference-style javascript: link", "[a][x]\n\n[x]: javascript:alert(1)"],
    ["Markdown javascript: autolink", "<javascript:alert(1)>"],
    ["raw javascript: href", "<a href=\"javascript:alert(1)\">a</a>"],
    ["raw leading-space javascript: href", "<a href=\"  javascript:alert(1)\">a</a>"],
    ["raw hex-entity javascript: href", "<a href=\"jav&#x61;script:alert(1)\">a</a>"],
    ["raw fully entity-encoded href", "<a href=\"&#106;&#97;&#118;&#97;&#115;&#99;&#114;&#105;&#112;&#116;&#58;alert(1)\">a</a>"],
    ["raw tab-split scheme", "<a href=\"java&#x09;script:alert(1)\">a</a>"],
    ["raw newline-split scheme", "<a href=\"java&#10;script:alert(1)\">a</a>"],
    ["raw named-entity colon", "<a href=\"javascript&colon;alert(1)\">a</a>"],
    ["raw vbscript: href", "<a href=\"vbscript:msgbox(1)\">a</a>"],
    ["raw data: href", "<a href=\"data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;\">a</a>"],
    ["Markdown javascript: image", "![x](javascript:alert(1))"],
    ["raw javascript: image", "<img src=\"javascript:alert(1)\" alt=\"x\">"],
    ["raw data: SVG image", "<img src=\"data:image/svg+xml,&lt;svg onload=alert(1)&gt;\" alt=\"x\">"],
    ["image onerror", "<img src=\"missing.png\" onerror=\"alert(1)\" alt=\"x\">"],
    ["unquoted image onerror", "<img src=x onerror=alert(1)//>"],
    ["onclick", "<div onclick=\"alert(1)\">x</div>"],
    ["onmouseover", "<p onmouseover=\"alert(1)\">x</p>"],
    ["details ontoggle", "<details open ontoggle=\"alert(1)\"><summary>s</summary>x</details>"],
    ["anchor onfocus autofocus", "<a href=\"https://example.com\" onfocus=\"alert(1)\" autofocus>x</a>"],
    ["inline style", "<p style=\"background:url(javascript:alert(1))\">x</p>"],
    ["style element", "<style>body{display:none}</style>"],
    ["iframe src", "<iframe src=\"javascript:alert(1)\"></iframe>"],
    ["iframe srcdoc", "<iframe srcdoc=\"<script>alert(1)</script>\"></iframe>"],
    ["object data", "<object data=\"javascript:alert(1)\"></object>"],
    ["embed src", "<embed src=\"javascript:alert(1)\">"],
    ["SVG script", "<svg><script>alert(1)</script></svg>"],
    ["SVG onload and xlink:href", "<svg onload=\"alert(1)\"><a xlink:href=\"javascript:alert(1)\"><text>x</text></a></svg>"],
    ["MathML link", "<math><mtext><a href=\"javascript:alert(1)\">m</a></mtext></math>"],
    ["form action and formaction", "<form action=\"javascript:alert(1)\"><button formaction=\"javascript:alert(1)\">b</button></form>"],
    ["meta refresh", "<meta http-equiv=\"refresh\" content=\"0;url=javascript:alert(1)\">"],
    ["base href", "<base href=\"javascript:alert(1)//\">"],
    ["external stylesheet", "<link rel=\"stylesheet\" href=\"https://evil.example/x.css\">"],
    ["script element", "<script>alert(1)</script>"],
    ["noscript fallback", "<noscript><img src=x onerror=alert(1)></noscript>"],
    ["template content", "<template><img src=x onerror=alert(1)></template>"],
  ])("neutralizes %s", (_name, content) => {
    api.fetchMarkdownImage.mockRejectedValue(new Error("image unavailable"));
    const { container } = renderUntrusted(`${content}\n`);
    expectInert(container);
    expect(container.textContent).not.toContain("alert(1)</script>");
  });

  it("keeps protocol-relative links as outbound new-tab links without opener or referrer", () => {
    renderUntrusted("[docs](//evil.example/path)\n");
    const link = screen.getByRole("link", { name: "docs" });
    expect(link.getAttribute("href")).toBe("//evil.example/path");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it.each([
    ["protocol-relative", "![remote](//evil.example/pixel.png)", "//evil.example/pixel.png"],
    ["absolute https", "<img src=\"https://evil.example/pixel.png\" alt=\"remote\">", "https://evil.example/pixel.png"],
  ])("never lets the browser fetch a %s image directly", async (_name, content, source) => {
    api.fetchMarkdownImage.mockRejectedValue(new Error("404"));
    const { container } = renderUntrusted(`${content}\n`);
    expect(await screen.findByText("remote")).toBeTruthy();
    expect(api.fetchMarkdownImage).toHaveBeenCalledWith("review-1", "README.md", "new", source, "rrv-sanitize");
    expect(container.querySelector("img")).toBeNull();
    expectInert(container);
  });

  it("drops <picture> sources so the browser cannot bypass the revision image route", async () => {
    api.fetchMarkdownImage.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:picture-fallback") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    try {
      const { container } = renderUntrusted(
        "<picture><source media=\"(prefers-color-scheme: dark)\" srcset=\"https://evil.example/dark.png\"><img src=\"docs/light.png\" alt=\"Logo\"></picture>\n",
      );
      await waitFor(() => expect(screen.getByRole("img", { name: "Logo" }).getAttribute("src")).toBe("blob:picture-fallback"));
      expect(container.querySelector("[srcset]")).toBeNull();
      expect(api.fetchMarkdownImage).toHaveBeenCalledWith("review-1", "README.md", "new", "docs/light.png", "rrv-sanitize");
      expect(api.fetchMarkdownImage).not.toHaveBeenCalledWith(expect.anything(), expect.anything(), expect.anything(), "https://evil.example/dark.png", expect.anything());
      expectInert(container);
    } finally {
      Reflect.deleteProperty(URL, "createObjectURL");
      Reflect.deleteProperty(URL, "revokeObjectURL");
    }
  });

  it("prefixes author ids and names so Markdown cannot clobber Reader globals", () => {
    const { container } = renderUntrusted("<a id=\"getElementById\" name=\"cookie\" href=\"#x\">x</a>\n");
    const link = container.querySelector("a");
    expect(link?.getAttribute("id")).toBe("user-content-getElementById");
    expect(link?.getAttribute("name")).toBe("user-content-cookie");
  });

  it("turns author form controls into inert checkboxes", () => {
    const { container } = renderUntrusted("<input type=\"password\" name=\"secret\" value=\"x\">\n");
    const input = container.querySelector("input");
    expect(input?.getAttribute("type")).toBe("checkbox");
    expect(input?.hasAttribute("disabled")).toBe(true);
  });
});
