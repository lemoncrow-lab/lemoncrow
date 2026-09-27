import { render, screen } from "@testing-library/react";
import { ConversationTurn, CommandDetail } from "./TurnRenderers";

describe("ConversationTurn rich cards", () => {
  it("renders TodoWrite turns as a task list card", () => {
    render(
      <ConversationTurn
        forceExpand
        turn={{
          kind: "todo_write",
          at: "2026-05-16T00:00:00Z",
          summary: "TodoWrite · 2 items",
          content: "",
          todos: [
            {
              content: "Render todo cards instead of JSON",
              status: "in_progress",
              priority: "high",
            },
            {
              content: "Show subagent work inside parent session",
              status: "pending",
            },
          ],
        }}
      />
    );

    expect(screen.getByText("Task List")).toBeInTheDocument();
    expect(
      screen.getByText("Render todo cards instead of JSON")
    ).toBeInTheDocument();
    expect(screen.getByText("in_progress")).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  it("renders inline attachment previews for selection context", () => {
    render(
      <ConversationTurn
        forceExpand
        turn={{
          kind: "attachment",
          at: "2026-05-16T00:00:00Z",
          summary: "Attached context",
          content: "",
          attachments: [
            {
              type: "selection",
              display_name: "Selection in spec.md",
              path: "/tmp/spec.md",
              content: "# Spec\n\nImportant context",
              line_count: 2,
            },
          ],
        }}
      />
    );

    expect(screen.getByText("Attached Context")).toBeInTheDocument();
    expect(screen.getByText("Selection in spec.md")).toBeInTheDocument();
    expect(screen.getByText("/tmp/spec.md")).toBeInTheDocument();
    expect(screen.getByText(/# Spec/)).toBeInTheDocument();
    expect(screen.getByText(/Important context/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Projection" })).toHaveAttribute(
      "href",
      "/projection?path=%2Ftmp%2Fspec.md&view=compact"
    );
  });

  it("hides generic main transcript filenames from the turn header", () => {
    render(
      <ConversationTurn
        forceExpand
        turn={{
          kind: "attachment",
          at: "2026-05-16T00:00:00Z",
          summary: "Attached context",
          content: "",
          artifact_label: "events.jsonl",
          source_scope: "main",
          attachments: [
            {
              type: "directory",
              display_name: "@frontend/src/pages/sessions/",
              path: "/tmp/sessions",
            },
          ],
        }}
      />
    );

    expect(
      screen.getByText("@frontend/src/pages/sessions/")
    ).toBeInTheDocument();
    expect(screen.queryByText("events.jsonl")).not.toBeInTheDocument();
  });

  it("shows model badges in the turn header and subagent card", () => {
    render(
      <ConversationTurn
        forceExpand
        turn={{
          kind: "subagent_event",
          at: "2026-05-16T00:00:00Z",
          summary: "Explore Agent",
          content: "Investigate model labels",
          model: "gpt-5.4",
          subagent_status: "started",
          subagent_name: "Explore Agent",
        }}
      />
    );

    expect(screen.getAllByText("gpt-5.4").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("STARTED")).toBeInTheDocument();
  });
});

describe("CommandDetail exit-code styling", () => {
  it("treats a missing exit_code as unknown, not failed", () => {
    render(
      <CommandDetail command={{ command: "echo hi" }} forceExpand={false} />
    );
    const badge = screen.getByText("EXIT_?");
    expect(badge).toHaveClass("text-neutral-400");
    expect(badge).not.toHaveClass("text-red-600");
  });

  it("still renders a failed exit code in red", () => {
    render(
      <CommandDetail
        command={{ command: "false", exit_code: 1 }}
        forceExpand={false}
      />
    );
    const badge = screen.getByText("EXIT_1");
    expect(badge).toHaveClass("text-red-600");
  });

  it("still renders a successful exit code in emerald", () => {
    render(
      <CommandDetail
        command={{ command: "true", exit_code: 0 }}
        forceExpand={false}
      />
    );
    const badge = screen.getByText("EXIT_0");
    expect(badge).toHaveClass("text-emerald-600");
  });
});
