import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import type { TraceSummary } from "../api";
import { SESSION_WINDOW, SessionLessons, sessionLessons, shelfOf } from "./SessionLessons";

function trace(id: string, createdAt: string, learnings: TraceSummary["learnings"], extra: Partial<TraceSummary> = {}): TraceSummary {
  return {
    id,
    session_id: `sess-${id}`,
    agent: "claude-code",
    task: `Task ${id}`,
    status: "success",
    errors_seen: [],
    repeated_failures: [],
    validation_results: [],
    created_at: createdAt,
    learnings,
    ...extra,
  } as TraceSummary;
}

const TRACES = [
  trace("old", "2026-11-01T09:00:00Z", [
    { kind: "worked", text: "Run the focused test file before the full suite.", promote_to: "memory" },
    { kind: "next_rule", text: "Release: bump, tag, then publish.", promote_to: "playbook", evidence: "release.sh" },
  ]),
  trace("new", "2026-11-20T09:00:00Z", [
    { kind: "worked", text: "run the focused   test file before the full suite.", promote_to: null },
    { kind: "risk", text: "The fixture clock is UTC.", promote_to: "none" },
    { kind: "next_rule", text: "Every API change needs a contract test.", promote_to: "rubric" },
    { kind: "note", text: "   " },
  ]),
  trace("bare", "2026-11-10T09:00:00Z", undefined),
];

function serveTraces(response: () => Promise<Response>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(response);
}

function page(items: TraceSummary[]) {
  return () =>
    Promise.resolve(
      new Response(JSON.stringify({ items, metrics: { stats: { total: items.length, success: 0, failed: 0, partial: 0 }, hosts: [], domains: [], workspaces: [] } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
}

function renderShelf(shelf: "memory" | "playbook") {
  return render(
    <MemoryRouter>
      <SessionLessons shelf={shelf} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("sessionLessons", () => {
  it("sends playbook candidates to Playbooks, keeps rubric candidates out, and treats the rest as memory", () => {
    expect(shelfOf({ promote_to: "playbook" })).toBe("playbook");
    expect(shelfOf({ promote_to: "rubric" })).toBeNull();
    for (const promote_to of ["memory", "none", null, undefined] as const) {
      expect(shelfOf({ promote_to })).toBe("memory");
    }
  });

  it("merges a lesson recorded in several sessions, newest session first, and skips blank ones", () => {
    const memory = sessionLessons(TRACES, "memory");
    expect(memory.map((lesson) => lesson.text)).toEqual([
      "run the focused   test file before the full suite.",
      "The fixture clock is UTC.",
    ]);
    expect(memory[0].sessions.map((session) => session.id)).toEqual(["sess-new", "sess-old"]);

    const playbooks = sessionLessons(TRACES, "playbook");
    expect(playbooks).toHaveLength(1);
    expect(playbooks[0]).toMatchObject({ kind: "next_rule", evidence: "release.sh" });
  });
});

describe("SessionLessons", () => {
  it("shows each lesson with its kind and a link to the session that recorded it", async () => {
    serveTraces(page(TRACES));
    renderShelf("memory");

    expect(screen.getByRole("status")).toHaveTextContent("Reading your sessions");
    const list = await screen.findByRole("list", { name: "Agent Memory" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Worked")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("link", { name: "Task new" })).toHaveAttribute("href", "/activity/sess-new");
    expect(rows[0]).toHaveTextContent("and 1 more session");
    expect(within(rows[1]).getByText("Risk")).toBeInTheDocument();
    expect(screen.getByText("2 lessons from your 3 sessions.")).toBeInTheDocument();
    expect(screen.queryByText("Every API change needs a contract test.")).not.toBeInTheDocument();
  });

  it("shows the proposed playbooks with their evidence", async () => {
    serveTraces(page(TRACES));
    renderShelf("playbook");

    expect(await screen.findByText("Release: bump, tag, then publish.")).toBeInTheDocument();
    expect(screen.getByText("Evidence: release.sh")).toBeInTheDocument();
    expect(screen.getByText("1 playbook from your 3 sessions.")).toBeInTheDocument();
  });

  it("says nothing has been recorded yet when no session left a lesson", async () => {
    serveTraces(page([trace("bare", "2026-11-10T09:00:00Z", [])]));
    renderShelf("playbook");

    expect(await screen.findByText("No playbooks proposed yet")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("asks for the sessions in one bounded read and says when it reached the window", async () => {
    const many = Array.from({ length: SESSION_WINDOW }, (_, n) =>
      trace(`t${n}`, "2026-11-10T09:00:00Z", [{ kind: "note", text: `Lesson ${n}` }]),
    );
    const fetchSpy = serveTraces(page(many));
    renderShelf("memory");

    expect(await screen.findByText(`${SESSION_WINDOW} lessons from your ${SESSION_WINDOW} most recent sessions.`)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(String(fetchSpy.mock.calls[0][0])).toContain(`limit=${SESSION_WINDOW}`);
  });

  it("names a server error and offers a retry when the server can't be reached", async () => {
    serveTraces(() => Promise.resolve(new Response(JSON.stringify({ detail: "store locked" }), { status: 500 })));
    const { unmount } = renderShelf("memory");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't read your sessions");
    unmount();
    vi.restoreAllMocks();

    const fetchSpy = serveTraces(() => Promise.reject(new TypeError("Failed to fetch")));
    renderShelf("memory");
    const retry = await screen.findByRole("button", { name: /retry|try again/i });
    const before = fetchSpy.mock.calls.length;
    await userEvent.click(retry);
    expect(fetchSpy.mock.calls.length).toBeGreaterThan(before);
    expect(String(fetchSpy.mock.calls.at(-1)?.[0])).toContain("/api/traces");
  });
});
