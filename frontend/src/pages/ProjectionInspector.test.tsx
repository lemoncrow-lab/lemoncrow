import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ProjectionInspector from "./ProjectionInspector";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ProjectionInspector page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders projection metadata and content preview", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (
          url.includes("/api/v1/files/projection") &&
          url.includes("view=exact")
        ) {
          return Promise.resolve(
            jsonResponse({
              mode: "full",
              path: "/tmp/main.go",
              language: "go",
              content: "package   main\nfunc   main()   {}\n",
              projection: {
                view: "exact",
                transformed: false,
                body_complete: true,
                untransformed_text: true,
              },
            })
          );
        }
        if (url.includes("/api/v1/files/projection")) {
          return Promise.resolve(
            jsonResponse({
              mode: "full",
              path: "/tmp/main.go",
              language: "go",
              content: "package main\nfunc main() {}\n",
              tokens_saved: 7,
              projection: {
                view: "compact",
                transformed: true,
                body_complete: true,
                untransformed_text: false,
                notice: "(compact; :full = source)",
              },
              projection_delta: {
                path: "/tmp/main.go",
                lang: "go",
                kind: "compact",
                original_tokens: 20,
                projected_tokens: 13,
                saved_tokens: 7,
              },
              projection_mapping: {
                version: "v1",
                projection_kind: "compact",
                path: "/tmp/main.go",
                lang: "go",
                source_length: 28,
                projected_length: 24,
                source_hash: "a",
                projected_hash: "b",
                source_line_offsets: [0, 13],
                segments: [
                  {
                    segment_id: "seg:0000",
                    kind: "exact",
                    source: {
                      start_offset: 0,
                      end_offset: 7,
                      start_line: 1,
                      end_line: 1,
                    },
                    projected_start: 0,
                    projected_end: 7,
                    exact: true,
                  },
                  {
                    segment_id: "seg:0001",
                    kind: "whitespace",
                    source: {
                      start_offset: 7,
                      end_offset: 10,
                      start_line: 1,
                      end_line: 1,
                    },
                    projected_start: 7,
                    projected_end: 8,
                    exact: false,
                  },
                ],
              },
            })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(
      <MemoryRouter
        initialEntries={["/projection?path=/tmp/main.go&view=compact"]}
      >
        <Routes>
          <Route path="/projection" element={<ProjectionInspector />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByDisplayValue("/tmp/main.go")).toBeInTheDocument();
    expect((await screen.findAllByText("compact")).length).toBeGreaterThan(0);
    expect(await screen.findByText("Segment preview")).toBeInTheDocument();
    expect((await screen.findAllByText(/package main/)).length).toBeGreaterThan(
      0
    );
    expect(
      await screen.findByText("(compact; :full = source)")
    ).toBeInTheDocument();
    await userEvent.click(
      await screen.findByRole("button", { name: /load exact compare/i })
    );
    expect(await screen.findByText("Exact comparison")).toBeInTheDocument();
    expect(await screen.findByText(/changed lines/i)).toBeInTheDocument();
  });
});
