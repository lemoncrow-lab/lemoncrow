import { describe, expect, it } from "vitest";
import { grammarForPath } from "./monacoLanguageRouter";

describe("Monaco read-only language demand", () => {
  it("loads the expected small grammar for frequent code and config files", () => {
    expect(grammarForPath("repo/src/payments.tsx")).toBe("typescript");
    expect(grammarForPath("repo/src/checkout.py")).toBe("python");
    expect(grammarForPath("C:\\repo\\main.go")).toBe("go");
    expect(grammarForPath("config.json")).toBe("json");
    expect(grammarForPath("values.yaml")).toBe("yaml");
    expect(grammarForPath("deploy/Dockerfile")).toBe("dockerfile");
    expect(grammarForPath("README.md")).toBe("markdown");
    expect(grammarForPath("styles.css")).toBe("css");
  });
  it("retains on-demand support for uncommon Monaco languages", () => {
    expect(grammarForPath("frontend/Widget.vue")).toBe("extended");
    expect(grammarForPath("backend/Payments.kt")).toBe("extended");
    expect(grammarForPath("infra/main.tf")).toBe("extended");
    expect(grammarForPath("docs/doc.mdx")).toBe("extended");
  });
  it("does not download the full Monaco language set for unknown or binary files", () => {
    expect(grammarForPath("build/output.bin")).toBeNull();
    expect(grammarForPath("example.bespoke")).toBeNull();
    expect(grammarForPath(".env")).toBeNull();
  });
});
