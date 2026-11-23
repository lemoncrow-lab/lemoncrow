import { describe, expect, it } from "vitest";
import { syntaxPieces } from "./focusSyntax";

describe("Focus syntax colors", () => {
  it("colors only lexical tokens without modifying the frozen text", () => {
    const line = '    return "no permission" if self.level > 5 else None  # guard';
    const pieces = syntaxPieces(line, "app/auth.py");
    expect(pieces.map((piece) => piece.text).join("")).toBe(line);
    expect(pieces).toContainEqual({ text: "return", kind: "keyword" });
    expect(pieces).toContainEqual({ text: '"no permission"', kind: "string" });
    expect(pieces).toContainEqual({ text: "5", kind: "number" });
    expect(pieces).toContainEqual({ text: "# guard", kind: "comment" });
  });
  it("keeps a comment marker inside a string and shows an actual trailing comment", () => {
    const pieces = syntaxPieces('const url = "https://example.test/a#b"; // test', "src/a.ts");
    expect(pieces.map((item) => item.text).join("")).toBe('const url = "https://example.test/a#b"; // test');
    expect(pieces).toContainEqual({ text: '"https://example.test/a#b"', kind: "string" });
    expect(pieces).toContainEqual({ text: "// test", kind: "comment" });
  });
  it("does not imply language knowledge for data and unsupported files", () => {
    expect(syntaxPieces('key: "value" # unsafe?', "settings.yaml")).toEqual([{ text: 'key: "value" # unsafe?', kind: "plain" }]);
  });
});
