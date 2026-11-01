import { act, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewAskDrawer from "./ReviewAskDrawer";
import * as api from "./reviewApi";
import type { ReviewAskMessage } from "./types";

vi.mock("./reviewApi", () => ({
  createReviewAskConversation: vi.fn(),
  fetchReviewAsk: vi.fn(),
  streamReviewAsk: vi.fn(),
  sendReviewAskHandoff: vi.fn(),
  uploadReviewEvidence: vi.fn(),
}));

function message(overrides: Partial<ReviewAskMessage> = {}): ReviewAskMessage {
  return {
    id: "ram-1",
    review_id: "review-1",
    revision_id: "revision-5",
    revision_number: 5,
    role: "assistant",
    intent: "question",
    body: "The coordinator keeps live runtime state separate from durable Review state.",
    scope: {
      target_unit_key: "",
      path: "",
      symbol: "",
      side: "new",
      start_line: 0,
      end_line: 0,
    },
    references: [],
    action: {},
    created_at: "2026-09-24T00:00:00+00:00",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  vi.mocked(api.fetchReviewAsk).mockResolvedValue({
    review_id: "review-1",
    latest_revision_number: 5,
    messages: [],
  });
});

describe("ReviewAskDrawer", () => {
  it("asks against the viewed revision and attaches selected code without copying it into the prompt", async () => {
    vi.mocked(api.streamReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      human: message({
        id: "ram-human",
        role: "human",
        body: "Why is this abstraction needed?",
        scope: {
          target_unit_key: "symbol:src/api.py:Authenticator",
          path: "src/api.py",
          symbol: "Authenticator",
          side: "new",
          start_line: 84,
          end_line: 131,
        },
      }),
      assistant: message({
        id: "ram-answer",
        references: [{
          path: "src/api.py",
          side: "new",
          start_line: 84,
          end_line: 131,
          target_unit_key: "symbol:src/api.py:Authenticator",
          symbol: "Authenticator",
        }],
      }),
    });
    const onOpenReference = vi.fn();

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        baseRevisionNumber={2}
        latestRevisionNumber={5}
        scope={{
          target_unit_key: "symbol:src/api.py:Authenticator",
          path: "src/api.py",
          symbol: "Authenticator",
          side: "new",
          start_line: 84,
          end_line: 131,
        }}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={onOpenReference}
      />,
    );

    expect(await screen.findByText("src/api.py · Authenticator · lines 84–131")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Walk me through this change" }));

    await waitFor(() => expect(api.streamReviewAsk).toHaveBeenCalledWith("review-1", {
      revision_number: 5,
      base_revision_number: 2,
      body: "Walk me through this change",
      target_unit_key: "symbol:src/api.py:Authenticator",
      path: "src/api.py",
      symbol: "Authenticator",
      side: "new",
      start_line: 84,
      end_line: 131,
    }, expect.any(Function), expect.any(AbortSignal)));
    expect(await screen.findByText(/coordinator keeps live runtime state/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy prompt" })).toBeTruthy();
    expect(screen.queryByText("Enter to send · Shift+Enter for newline")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Authenticator · lines 84–131" }));
    expect(onOpenReference).toHaveBeenCalledWith(expect.objectContaining({
      path: "src/api.py",
      start_line: 84,
      end_line: 131,
    }));
    expect(screen.queryByRole("button", { name: /Send change/i })).toBeNull();
  });

  it("opens recent Ask conversations in place and starts a clean conversation", async () => {
    const firstHuman = message({
      id: "first-human", role: "human", body: "Why does this token refresh twice?", conversation_id: "conv-1",
      created_at: "2026-10-05T16:00:00+00:00",
    });
    const firstAnswer = message({ id: "first-answer", body: "Because both paths call refresh.", conversation_id: "conv-1" });
    const secondHuman = message({
      id: "second-human", role: "human", body: "Could this cache race?", conversation_id: "conv-2",
      created_at: "2026-10-05T17:00:00+00:00",
    });
    const secondAnswer = message({ id: "second-answer", body: "Yes, under concurrent replacement.", conversation_id: "conv-2" });
    const conversations = [
      {
        id: "conv-2", title: "Could this cache race?", origin: { kind: "selection", path: "src/cache.ts", start_line: 81, end_line: 113 },
        created_at: "2026-10-05T17:00:00+00:00", updated_at: "2026-10-05T17:01:00+00:00", message_count: 2,
      },
      {
        id: "conv-1", title: "Why does this token refresh twice?", origin: { kind: "selection", path: "src/auth.ts", start_line: 120, end_line: 164 },
        created_at: "2026-10-05T16:00:00+00:00", updated_at: "2026-10-05T16:01:00+00:00", message_count: 2,
      },
    ];
    vi.mocked(api.fetchReviewAsk)
      .mockResolvedValueOnce({ review_id: "review-1", latest_revision_number: 5, messages: [firstHuman, firstAnswer], conversations, active_conversation_id: "conv-1" })
      .mockResolvedValueOnce({ review_id: "review-1", latest_revision_number: 5, messages: [secondHuman, secondAnswer], conversations, active_conversation_id: "conv-2" });
    vi.mocked(api.createReviewAskConversation).mockResolvedValue({
      id: "conv-new", title: "New conversation", origin: { kind: "review", revision_number: 5 },
      created_at: "2026-10-05T18:00:00+00:00", updated_at: "2026-10-05T18:00:00+00:00", message_count: 0,
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    expect(await screen.findByText("Because both paths call refresh.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Ask history" }));
    expect(screen.getByLabelText("Recent Ask conversations")).toBeTruthy();
    expect(screen.getByText("src/cache.ts · lines 81–113")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Open conversation: Could this cache race?" }));
    await waitFor(() => expect(api.fetchReviewAsk).toHaveBeenCalledWith("review-1", "conv-2"));
    expect(await screen.findByText("Yes, under concurrent replacement.")).toBeTruthy();
    expect(screen.queryByText("Because both paths call refresh.")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "New Ask conversation" }));
    await waitFor(() => expect(api.createReviewAskConversation).toHaveBeenCalledWith(
      "review-1", expect.objectContaining({ kind: "review", revision_number: 5 }),
    ));
    expect(await screen.findByText("Ask, challenge, or request a change")).toBeTruthy();
    expect(screen.queryByText("Yes, under concurrent replacement.")).toBeNull();
  });

  it("selects the Ask driver and model from the composer and sends both with the question", async () => {
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      messages: [],
      ai: {
        driver: "litellm",
        model: "anthropic/claude-sonnet-4-5",
        models: ["anthropic/claude-sonnet-4-5", "openai/gpt-5.6"],
      },
    });
    vi.mocked(api.streamReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      human: message({ id: "ram-human-ai", role: "human", body: "Explain this." }),
      assistant: message({ id: "ram-ai-answer", body: "Here is the explanation." }),
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText("Ask AI driver").textContent).toContain("LiteLLM"));
    expect(screen.getByLabelText("Ask AI model").textContent).toContain("anthropic/claude-sonnet-4-5");

    await userEvent.click(screen.getByLabelText("Ask AI model"));
    await userEvent.click(screen.getByRole("menuitemradio", { name: "openai/gpt-5.6" }));
    fireEvent.change(screen.getByLabelText("Ask Review"), { target: { value: "Explain this." } });
    fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));

    await waitFor(() => expect(api.streamReviewAsk).toHaveBeenCalledWith("review-1", {
      revision_number: 5,
      body: "Explain this.",
      driver: "litellm",
      model: "openai/gpt-5.6",
    }, expect.any(Function), expect.any(AbortSignal)));
  });

  it("offers only the drivers the server runs and drops a saved agent CLI choice the server refuses", async () => {
    window.localStorage.setItem("lemoncrow.review.ask.driver", "claude");
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      messages: [],
      ai: {
        driver: "configured",
        model: "",
        models: ["ollama/qwen3"],
        drivers: ["configured", "ollama", "openai", "litellm"],
        models_by_driver: { ollama: ["ollama/qwen3"], openai: [], litellm: ["ollama/qwen3"] },
      },
    });
    vi.mocked(api.streamReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      human: message({ id: "ram-human-hosted", role: "human", body: "Explain this." }),
      assistant: message({ id: "ram-hosted-answer", body: "Here is the explanation." }),
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText("Ask AI driver").textContent).toContain("Default"));
    await userEvent.click(screen.getByLabelText("Ask AI driver"));
    const offered = screen.getAllByRole("menuitemradio").map((item) => item.textContent);
    expect(offered).toEqual(["Default", "Ollama", "OpenAI", "LiteLLM"]);
    await userEvent.keyboard("{Escape}");

    fireEvent.change(screen.getByLabelText("Ask Review"), { target: { value: "Explain this." } });
    fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));
    await waitFor(() => expect(api.streamReviewAsk).toHaveBeenCalled());
    expect(vi.mocked(api.streamReviewAsk).mock.calls[0][1]).not.toHaveProperty("driver");
  });

  it("renders an optimistic chat turn and live working feedback before the answer arrives", async () => {
    let emit: ((event: api.ReviewAskStreamEvent) => void) | null = null;
    let resolveResult: ((result: Awaited<ReturnType<typeof api.streamReviewAsk>>) => void) | null = null;
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      messages: [],
      ai: {
        driver: "claude",
        model: "sonnet",
        models: [],
        models_by_driver: { claude: ["sonnet", "opus", "haiku"] },
      },
    });
    vi.mocked(api.streamReviewAsk).mockImplementation((_id, _input, onEvent) => {
      emit = onEvent;
      return new Promise((resolve) => { resolveResult = resolve; });
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText("Ask AI driver").textContent).toContain("Claude"));
    const composer = screen.getByLabelText("Ask Review") as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: "Can you simplify this?" } });
    fireEvent.keyDown(composer, { key: "Enter", shiftKey: false });

    expect(await screen.findByText("Can you simplify this?")).toBeTruthy();
    expect(composer.value).toBe("");
    expect(screen.getByText(/Claude · sonnet/)).toBeTruthy();
    expect(screen.getByText("Starting…")).toBeTruthy();

    act(() => emit?.({ type: "working", elapsed_ms: 3200, driver: "claude", model: "sonnet" }));
    expect(screen.getByText("Working on this snapshot…")).toBeTruthy();
    expect(screen.getByText("3s")).toBeTruthy();

    act(() => emit?.({ type: "delta", text: "Partial streamed answer." }));
    expect(screen.getByText(/Partial streamed answer/)).toBeTruthy();
    expect(screen.queryByText("Working on this snapshot…")).toBeNull();

    await act(async () => {
      resolveResult?.({
        review_id: "review-1",
        latest_revision_number: 5,
        human: message({ id: "ram-live-human", role: "human", body: "Can you simplify this?" }),
        assistant: message({ id: "ram-live-answer", body: "Yes. Collapse the two wrappers into one adapter." }),
      });
    });
    expect(await screen.findByText(/Collapse the two wrappers/)).toBeTruthy();
    expect(screen.queryByText("Working on this snapshot…")).toBeNull();
  });

  it("opens @ file suggestions immediately and dismisses them on space, Escape, or outside click", async () => {
    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        filePaths={["src/session.py", "src/api/routes.py"]}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    const composer = await screen.findByLabelText("Ask Review") as HTMLTextAreaElement;

    fireEvent.change(composer, { target: { value: "@", selectionStart: 1 } });
    expect(screen.getByText("Files in this change")).toBeTruthy();
    expect(screen.getByRole("option", { name: /session\.py/i })).toBeTruthy();
    expect(screen.getByRole("option", { name: /routes\.py/i })).toBeTruthy();

    fireEvent.keyDown(composer, { key: " " });
    expect(screen.queryByRole("listbox", { name: "Changed files" })).toBeNull();

    fireEvent.change(composer, { target: { value: "@sess", selectionStart: 5 } });
    expect(screen.getByRole("listbox", { name: "Changed files" })).toBeTruthy();
    fireEvent.change(composer, { target: { value: "@sess ", selectionStart: 6 } });
    expect(screen.queryByRole("listbox", { name: "Changed files" })).toBeNull();

    fireEvent.change(composer, { target: { value: "@", selectionStart: 1 } });
    fireEvent.keyDown(composer, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: "Changed files" })).toBeNull();

    fireEvent.click(composer);
    expect(screen.getByRole("listbox", { name: "Changed files" })).toBeTruthy();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("listbox", { name: "Changed files" })).toBeNull();
  });

  it("attaches an external text file with + and sends its evidence id", async () => {
    const externalFile = new File(["# External notes\nCheck the timeout."], "notes.md", { type: "text/markdown" });
    vi.mocked(api.uploadReviewEvidence).mockResolvedValue({
      evidence: {
        id: "evd-notes",
        review_id: "review-1",
        revision_id: "revision-5",
        revision_number: 5,
        kind: "document",
        title: "notes.md",
        path: "",
        url: "",
        content_hash: "abc",
        mime_type: "text/markdown",
        bytes: externalFile.size,
        source: "human",
        source_ref: "notes.md",
        verification_status: "",
        detail: "",
        created_at: "2026-09-28T00:00:00+00:00",
        status: "current",
        content_url: "/api/evidence/evd-notes/content",
      },
    });
    vi.mocked(api.streamReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      human: message({
        id: "ram-external-human",
        role: "human",
        body: "Use the attached notes.",
        references: [{ kind: "attachment", evidence_id: "evd-notes", label: "notes.md", exactness: "snapshot" }],
      }),
      assistant: message({ id: "ram-external-answer", body: "The notes call out the timeout." }),
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        revisionId="revision-5"
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    fireEvent.change(await screen.findByLabelText("External file picker"), { target: { files: [externalFile] } });
    await waitFor(() => expect(api.uploadReviewEvidence).toHaveBeenCalledWith(
      "review-1",
      externalFile,
      "",
      "",
      "revision-5",
    ));
    expect(await screen.findByText("notes.md")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Ask Review"), { target: { value: "Use the attached notes." } });
    fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));

    await waitFor(() => expect(api.streamReviewAsk).toHaveBeenCalledWith("review-1", {
      revision_number: 5,
      body: "Use the attached notes.",
      evidence_ids: ["evd-notes"],
    }, expect.any(Function), expect.any(AbortSignal)));
  });

  it("auto-uploads pasted files, dropped files, and large pasted text", async () => {
    let evidenceCounter = 0;
    vi.mocked(api.uploadReviewEvidence).mockImplementation(async (_reviewId, file) => {
      evidenceCounter += 1;
      return {
        evidence: {
          id: `evd-auto-${evidenceCounter}`,
          review_id: "review-1",
          revision_id: "revision-5",
          revision_number: 5,
          kind: "document",
          title: file.name,
          path: "",
          url: "",
          content_hash: `hash-${evidenceCounter}`,
          mime_type: file.type || "text/plain",
          bytes: file.size,
          source: "human",
          source_ref: file.name,
          verification_status: "",
          detail: "",
          created_at: "2026-09-28T00:00:00+00:00",
          status: "current",
          content_url: `/api/evidence/evd-auto-${evidenceCounter}/content`,
        },
      };
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        revisionId="revision-5"
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    const composer = await screen.findByLabelText("Ask Review") as HTMLTextAreaElement;
    const pastedFile = new File(["paste contents"], "clipboard.md", { type: "text/markdown" });
    fireEvent.paste(composer, {
      clipboardData: {
        files: [pastedFile],
        getData: () => "",
      },
    });
    await waitFor(() => expect(api.uploadReviewEvidence).toHaveBeenCalledWith(
      "review-1",
      pastedFile,
      "",
      "",
      "revision-5",
    ));
    expect(await screen.findByText("clipboard.md")).toBeTruthy();

    const droppedFile = new File(["drop contents"], "dropped.txt", { type: "text/plain" });
    const dataTransfer = { types: ["Files"], files: [droppedFile], dropEffect: "none" };
    fireEvent.dragEnter(composer, { dataTransfer });
    expect(screen.getByText("Drop to attach to this chat")).toBeTruthy();
    fireEvent.drop(composer, { dataTransfer });
    await waitFor(() => expect(api.uploadReviewEvidence).toHaveBeenCalledWith(
      "review-1",
      droppedFile,
      "",
      "",
      "revision-5",
    ));
    expect(screen.queryByText("Drop to attach to this chat")).toBeNull();
    expect(await screen.findByText("dropped.txt")).toBeTruthy();

    const largePaste = "x".repeat(8_000);
    fireEvent.paste(composer, {
      clipboardData: {
        files: [],
        getData: (type: string) => type === "text/plain" ? largePaste : "",
      },
    });
    await waitFor(() => expect(api.uploadReviewEvidence).toHaveBeenCalledTimes(3));
    const pastedTextFile = vi.mocked(api.uploadReviewEvidence).mock.calls[2][1];
    expect(pastedTextFile.name).toBe("pasted-text.txt");
    expect(pastedTextFile.type).toBe("text/plain");
    expect(pastedTextFile.size).toBe(8_000);
    expect(composer.value).toBe("");
    expect(await screen.findByText("pasted-text.txt")).toBeTruthy();
  });

  it("attaches changed files from @ suggestions and sends them as snapshot mentions", async () => {
    vi.mocked(api.streamReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      human: message({
        id: "ram-mentioned-human",
        role: "human",
        body: "Explain this file",
        references: [{
          kind: "source",
          exactness: "snapshot",
          path: "src/session.py",
          side: "new",
          start_line: 0,
          end_line: 0,
          target_unit_key: "",
          symbol: "",
        }],
      }),
      assistant: message({ id: "ram-mentioned-answer", body: "It updates the session behavior." }),
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        filePaths={["src/session.py", "src/api/routes.py"]}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    const composer = await screen.findByLabelText("Ask Review") as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: "Explain @sess", selectionStart: 13 } });
    const option = await screen.findByRole("option", { name: /session\.py/i });
    fireEvent.click(option);

    expect(screen.getByTitle("src/session.py")).toBeTruthy();
    fireEvent.change(composer, { target: { value: "Explain this file" } });
    fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));

    await waitFor(() => expect(api.streamReviewAsk).toHaveBeenCalledWith("review-1", {
      revision_number: 5,
      body: "Explain this file",
      mentioned_paths: ["src/session.py"],
    }, expect.any(Function), expect.any(AbortSignal)));
    expect(await screen.findByText(/updates the session behavior/i)).toBeTruthy();
  });

  it("shows copied feedback after copying a prompt", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      messages: [message({ id: "ram-copy", role: "human", body: "Check this carefully." })],
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    const copy = await screen.findByRole("button", { name: "Copy prompt" });
    fireEvent.click(copy);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Check this carefully."));
    expect(screen.getByRole("button", { name: "Copied prompt" })).toBeTruthy();
    expect(screen.getByText("Copied")).toBeTruthy();
  });

  it("keeps native Claude models separate from provider-qualified gateway models", async () => {
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      messages: [],
      ai: {
        driver: "litellm",
        model: "",
        models: ["zen/claude-sonnet-4-6", "anthropic/claude-sonnet-4-6", "ollama/qwen3.6:27b"],
        models_by_driver: {
          claude: ["sonnet", "opus", "haiku"],
          codex: [],
          ollama: ["ollama/qwen3.6:27b"],
          litellm: ["zen/claude-sonnet-4-6", "anthropic/claude-sonnet-4-6", "ollama/qwen3.6:27b"],
        },
      },
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText("Ask AI driver").textContent).toContain("LiteLLM"));
    await userEvent.click(screen.getByLabelText("Ask AI driver"));
    await userEvent.click(screen.getByRole("menuitemradio", { name: "Claude" }));
    await userEvent.click(screen.getByLabelText("Ask AI model"));

    expect(screen.getByRole("menuitemradio", { name: "sonnet" })).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: "opus" })).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: "haiku" })).toBeTruthy();
    expect(screen.queryByRole("menuitemradio", { name: "zen/claude-sonnet-4-6" })).toBeNull();
    expect(screen.queryByRole("menuitemradio", { name: "anthropic/claude-sonnet-4-6" })).toBeNull();
  });

  it("repairs a persisted OpenCode model against the verified catalog on load", async () => {
    window.localStorage.setItem("lemoncrow.review.ask.driver", "opencode");
    window.localStorage.setItem("lemoncrow.review.ask.model", "zen/big-pickle");
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      messages: [],
      ai: {
        driver: "litellm",
        model: "",
        models: ["zen/big-pickle", "opencode/claude-haiku-4-5", "ollama/qwen3.8:27b"],
        models_by_driver: {
          opencode: ["opencode/claude-haiku-4-5", "ollama/qwen3.8:27b"],
          litellm: ["zen/big-pickle"],
        },
      },
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText("Ask AI driver").textContent).toContain("OpenCode"));
    await waitFor(() => expect(screen.getByLabelText("Ask AI model").textContent).toContain("opencode/claude-haiku-4-5"));
    expect(window.localStorage.getItem("lemoncrow.review.ask.model")).toBe("opencode/claude-haiku-4-5");
  });

  it("shows a proposed change inline and sends only after the reviewer clicks Send change", async () => {
    const change = message({
      id: "ram-change",
      intent: "change_request",
      body: "I can ask the author to collapse the first two classes.",
      action: {
        kind: "agent_handoff",
        handoff_instruction: "Combine LiveReviewHandle and LiveReviewState without changing behavior.",
        operation_id: "aop-1",
        instruction_hash: "hash-1",
        expected_revision_id: "revision-5",
        expected_tree_fingerprint: "tree-5",
        expected_source_fingerprint: "source-5",
        delivery: {
          supported: true,
          host: "codex",
          session_id: "session-1",
          target_ref: "codex:session-1",
          label: "Codex",
          reason: "",
        },
        handoff: {
          state: "prepared",
          target_ref: "codex:session-1",
          remote_ref: "",
          last_error: "",
          updated_at: "2026-09-24T00:00:00+00:00",
        },
      },
    });
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      messages: [change],
    });
    vi.mocked(api.sendReviewAskHandoff).mockResolvedValue({
      state: "sent",
      target_ref: "codex:session-1",
      remote_ref: "message-1",
      message: "sent",
      operation_id: "aop-1",
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );

    expect(await screen.findByText("Proposed change")).toBeTruthy();
    expect(screen.getByText(/Combine LiveReviewHandle and LiveReviewState/)).toBeTruthy();
    expect(api.sendReviewAskHandoff).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Send change/i }));
    await waitFor(() => expect(api.sendReviewAskHandoff).toHaveBeenCalledWith(
      "review-1",
      "ram-change",
      {
        operation_id: "aop-1",
        instruction_hash: "hash-1",
        expected_revision_id: "revision-5",
        expected_tree_fingerprint: "tree-5",
        expected_source_fingerprint: "source-5",
      },
    ));
    expect(await screen.findByRole("button", { name: /Change sent/i })).toBeDisabled();
  });

  it("posts Review Map context through the existing Ask request and keeps Map references clickable", async () => {
    const selection = {
      kind: "review_map_node" as const,
      review_id: "review-1",
      revision_id: "revision-5",
      level: "symbol" as const,
      focus_node_id: "symbol:authorize",
      node_id: "current:caller",
      exactness: "current_index" as const,
      path: "src/api/routes.py",
      line: 70,
      end_line: 105,
      symbol: "app.api.handle_request",
      target_ids: [],
      current_symbol_id: "caller",
    };
    const mapReference = {
      kind: "review_map_node",
      label: "handle_request",
      map_selection: selection,
    };
    vi.mocked(api.streamReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 5,
      human: message({
        id: "ram-map-human",
        role: "human",
        body: "What callers are affected?",
        context: { selection, label: "handle_request" },
      }),
      assistant: message({
        id: "ram-map-answer",
        body: "The current graph shows handle_request around this Snapshot change.",
        context: { selection, label: "handle_request" },
        references: [mapReference],
      }),
    });
    const onOpenReference = vi.fn();

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={5}
        scope={{ context: selection, label: "handle_request" }}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={onOpenReference}
      />,
    );

    expect(await screen.findByText("Review Map · handle_request · current index context")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Ask Review"), { target: { value: "What callers are affected?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));

    await waitFor(() => expect(api.streamReviewAsk).toHaveBeenCalledWith("review-1", {
      revision_number: 5,
      body: "What callers are affected?",
      context: selection,
    }, expect.any(Function), expect.any(AbortSignal)));
    expect(await screen.findByText(/current graph shows handle_request/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Map · handle_request" }));
    expect(onOpenReference).toHaveBeenCalledWith(mapReference);
  });

  it("keeps the conversation on its viewed revision and offers an explicit switch when a newer revision exists", async () => {
    const onSwitchLatest = vi.fn();
    vi.mocked(api.fetchReviewAsk).mockResolvedValue({
      review_id: "review-1",
      latest_revision_number: 6,
      messages: [message({ revision_number: 5 })],
    });

    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        latestRevisionNumber={6}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={onSwitchLatest}
        onOpenReference={vi.fn()}
      />,
    );

    expect(await screen.findByText("Snapshot 6 available")).toBeTruthy();
    expect(screen.getAllByText("Snapshot 5").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Switch to latest" }));
    expect(onSwitchLatest).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Ask about Snapshot 5")).toBeNull();
  });

  describe("cancellation", () => {
    function abortableStream(onSignal: (signal: AbortSignal) => void = () => {}) {
      return (_id: string, _input: unknown, _onEvent: unknown, signal?: AbortSignal) => {
        if (signal) onSignal(signal);
        return new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
        });
      };
    }

    function renderReviewAsk(onClose = vi.fn()) {
      return render(
        <ReviewAskDrawer
          reviewId="review-1"
          revisionNumber={5}
          latestRevisionNumber={5}
          onClearScope={vi.fn()}
          onClose={onClose}
          onSwitchLatest={vi.fn()}
          onOpenReference={vi.fn()}
        />,
      );
    }

    async function askQuestion(text: string) {
      fireEvent.change(await screen.findByLabelText("Ask Review"), { target: { value: text } });
      fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));
    }

    it("never paints another Review's private conversation when the Review ID changes", async () => {
      const oldMessage = message({ id: "private-message", body: "PRIVATE PREVIOUS REVIEW MESSAGE" });
      const deferred = new Promise<Awaited<ReturnType<typeof api.fetchReviewAsk>>>(() => undefined);
      vi.mocked(api.fetchReviewAsk).mockImplementation((id) => id === "first-review"
        ? Promise.resolve({ review_id: id, latest_revision_number: 2, messages: [oldMessage] })
        : deferred);
      const shared = {
        revisionId: "rev-2", revisionNumber: 2, latestRevisionNumber: 2,
        onClearScope: vi.fn(), onClose: vi.fn(), onSwitchLatest: vi.fn(), onOpenReference: vi.fn(),
      };
      const first = render(<ReviewAskDrawer {...shared} reviewId="first-review" />);
      expect(await screen.findByText("PRIVATE PREVIOUS REVIEW MESSAGE")).toBeInTheDocument();
      first.rerender(<ReviewAskDrawer {...shared} reviewId="second-review" />);
      expect(screen.queryByText("PRIVATE PREVIOUS REVIEW MESSAGE")).toBeNull();
      expect(screen.getByLabelText("Syncing conversation")).toBeInTheDocument();
    });

    it("rejects a late answer when the viewed Snapshot changes during the stream", async () => {
      let emit: ((event: api.ReviewAskStreamEvent) => void) | undefined;
      let finish: ((result: Awaited<ReturnType<typeof api.streamReviewAsk>>) => void) | undefined;
      let signal: AbortSignal | undefined;
      vi.mocked(api.streamReviewAsk).mockImplementation((_id, _input, onEvent, abortSignal) => {
        emit = onEvent;
        signal = abortSignal;
        return new Promise((resolve) => { finish = resolve; });
      });
      const shared = {
        reviewId: "review-1", latestRevisionNumber: 5, onClearScope: vi.fn(),
        onClose: vi.fn(), onSwitchLatest: vi.fn(), onOpenReference: vi.fn(),
      };
      const view = render(<ReviewAskDrawer {...shared} revisionId="rev-5" revisionNumber={5} />);
      await askQuestion("Why is this token trusted?");
      act(() => emit?.({ type: "delta", text: "An unverified assertion about the old Snapshot" }));
      expect(screen.getByText(/An unverified assertion/)).toBeInTheDocument();
      view.rerender(<ReviewAskDrawer {...shared} revisionId="rev-6" revisionNumber={6} latestRevisionNumber={6} />);
      expect(signal?.aborted).toBe(true);
      expect(await screen.findByText(/Snapshot changed while Ask was running/)).toBeInTheDocument();
      act(() => emit?.({ type: "delta", text: " INJECTED LATE" }));
      await act(async () => finish?.({
        review_id: "review-1", latest_revision_number: 6,
        human: message({ id: "old-human", revision_number: 5 }),
        assistant: message({ id: "old-answer", revision_number: 5, body: "INCORRECT LATE ANSWER" }),
      }));
      expect(screen.queryByText(/INJECTED LATE|INCORRECT LATE ANSWER/)).toBeNull();
      expect(screen.getByRole("button", { name: "Send Ask question" })).toBeInTheDocument();
    });

    it("stops an Ask that is still starting: the request is aborted and the question stays as a stopped turn, not a failure", async () => {
      let signal: AbortSignal | undefined;
      vi.mocked(api.streamReviewAsk).mockImplementation(abortableStream((value) => { signal = value; }));
      renderReviewAsk();

      await askQuestion("Is this retry safe?");
      expect(await screen.findByText("Starting…")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Send Ask question" })).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: "Stop Ask" }));

      expect(signal?.aborted).toBe(true);
      expect(await screen.findByText("Stopped before an answer arrived")).toBeTruthy();
      expect(screen.getByText("Is this retry safe?")).toBeTruthy();
      expect(screen.queryByText("Starting…")).toBeNull();
      // Cancelled is not failed: no error text and no Retry.
      expect(screen.queryByText(/aborted/i)).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Stop Ask" })).toBeNull();
      expect(screen.getByRole("button", { name: "Send Ask question" })).toBeTruthy();
    });

    it("stops mid-stream and keeps the streamed answer, ignoring anything that arrives after Stop", async () => {
      let emit: ((event: api.ReviewAskStreamEvent) => void) | null = null;
      let signal: AbortSignal | undefined;
      let resolveResult: ((result: Awaited<ReturnType<typeof api.streamReviewAsk>>) => void) | null = null;
      vi.mocked(api.streamReviewAsk).mockImplementation((_id, _input, onEvent, abortSignal) => {
        emit = onEvent;
        signal = abortSignal;
        // A transport that ignores the signal: Stop must not depend on it settling.
        return new Promise((resolve) => { resolveResult = resolve; });
      });
      renderReviewAsk();

      await askQuestion("Explain the retry loop.");
      act(() => emit?.({ type: "accepted", driver: "configured", model: "" }));
      act(() => emit?.({ type: "delta", text: "The loop retries three times" }));
      expect(screen.getByText(/The loop retries three times/)).toBeTruthy();

      fireEvent.click(screen.getByRole("button", { name: "Stop Ask" }));
      expect(signal?.aborted).toBe(true);
      expect(screen.getByText("The loop retries three times")).toBeTruthy();
      expect(screen.getByText("Stopped")).toBeTruthy();

      act(() => emit?.({ type: "delta", text: " and then gives up." }));
      await act(async () => {
        resolveResult?.({
          review_id: "review-1",
          latest_revision_number: 5,
          human: message({ id: "ram-late-human", role: "human", body: "Explain the retry loop." }),
          assistant: message({ id: "ram-late-answer", body: "A late full answer." }),
        });
      });
      expect(screen.queryByText(/gives up/)).toBeNull();
      expect(screen.queryByText("A late full answer.")).toBeNull();
      expect(screen.getAllByText("Explain the retry loop.")).toHaveLength(1);
      expect(screen.getByRole("button", { name: "Send Ask question" })).toBeTruthy();
    });

    it("cancels the active Review Ask stream when the drawer is closed", async () => {
      let signal: AbortSignal | undefined;
      vi.mocked(api.streamReviewAsk).mockImplementation(abortableStream((value) => { signal = value; }));
      const onClose = vi.fn();
      const view = renderReviewAsk(onClose);

      await askQuestion("What changed here?");
      expect(await screen.findByText("Starting…")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Close Ask" }));
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(signal?.aborted).toBe(false);

      // The Reader unmounts the drawer when Ask closes.
      view.unmount();
      expect(signal?.aborted).toBe(true);
    });

    it("starts a new Ask after a cancelled one and keeps the stopped turn above it", async () => {
      const signals: AbortSignal[] = [];
      vi.mocked(api.streamReviewAsk)
        .mockImplementationOnce((id, input, onEvent, signal) => {
          onEvent({ type: "delta", text: "Half an answer" });
          return abortableStream((value) => { signals.push(value); })(id, input, onEvent, signal);
        })
        .mockImplementationOnce(async (_id, _input, _onEvent, signal) => {
          if (signal) signals.push(signal);
          return {
            review_id: "review-1",
            latest_revision_number: 5,
            human: message({ id: "ram-second-human", role: "human", body: "Second question" }),
            assistant: message({ id: "ram-second-answer", body: "Second answer." }),
          };
        });
      renderReviewAsk();

      await askQuestion("First question");
      expect(await screen.findByText(/Half an answer/)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Stop Ask" }));
      expect(await screen.findByText("Stopped")).toBeTruthy();

      fireEvent.change(screen.getByLabelText("Ask Review"), { target: { value: "Second question" } });
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send Ask question" })); });
      expect(await screen.findByText("Second answer.")).toBeTruthy();

      expect(api.streamReviewAsk).toHaveBeenCalledTimes(2);
      expect(signals.map((signal) => signal.aborted)).toEqual([true, false]);
      const stopped = screen.getByText("Half an answer");
      const second = screen.getByText("Second answer.");
      expect(stopped.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(screen.getByText("First question")).toBeTruthy();
      expect(screen.getAllByText("Stopped")).toHaveLength(1);
    });

    it("lets Code chat stop its stream but leaves it running when the drawer closes, since only a finished stream saves the turn", async () => {
      const signals: AbortSignal[] = [];
      const transport = {
        allowedDrivers: ["configured" as const],
        load: vi.fn(async () => ({ review_id: "/repo", latest_revision_number: 0, messages: [] })),
        stream: vi.fn((_input: unknown, _onEvent: unknown, signal?: AbortSignal) => {
          if (signal) signals.push(signal);
          return new Promise<never>(() => {});
        }),
      };
      const view = render(
        <ReviewAskDrawer
          reviewId="/repo"
          revisionNumber={0}
          latestRevisionNumber={0}
          scope={{ path: "src/app.ts" }}
          onClearScope={vi.fn()}
          onClose={vi.fn()}
          onSwitchLatest={vi.fn()}
          onOpenReference={vi.fn()}
          transport={transport}
        />,
      );

      fireEvent.change(await screen.findByLabelText("Ask Code"), { target: { value: "Explain this code" } });
      fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));
      fireEvent.click(await screen.findByRole("button", { name: "Stop Ask" }));
      expect(signals[0]?.aborted).toBe(true);
      expect(await screen.findByText("Stopped before an answer arrived")).toBeTruthy();

      fireEvent.change(screen.getByLabelText("Ask Code"), { target: { value: "And this?" } });
      fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));
      await waitFor(() => expect(signals).toHaveLength(2));
      view.unmount();
      expect(signals[1].aborted).toBe(false);
    });
  });

  it("clears the drop overlay when a file drag leaves, even after an element under the pointer was replaced mid-drag", async () => {
    let resolveResult: ((result: Awaited<ReturnType<typeof api.streamReviewAsk>>) => void) | null = null;
    vi.mocked(api.streamReviewAsk).mockImplementation(() => new Promise((resolve) => { resolveResult = resolve; }));
    render(
      <ReviewAskDrawer
        reviewId="review-1"
        revisionNumber={5}
        revisionId="revision-5"
        latestRevisionNumber={5}
        onClearScope={vi.fn()}
        onClose={vi.fn()}
        onSwitchLatest={vi.fn()}
        onOpenReference={vi.fn()}
      />,
    );
    const composer = await screen.findByLabelText("Ask Review");
    const dropZone = composer.parentElement as HTMLElement;
    const dataTransfer = { types: ["Files"], files: [], dropEffect: "none" };
    const overlay = () => screen.queryByText("Drop to attach to this chat");
    // jsdom has no DragEvent, so relatedTarget is set by hand.
    const leave = (from: Element, to: Element | null) => {
      const event = createEvent.dragLeave(from, { dataTransfer });
      Object.defineProperty(event, "relatedTarget", { value: to });
      fireEvent(from, event);
    };

    fireEvent.change(composer, { target: { value: "Explain this." } });
    fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));
    const stopIcon = (await screen.findByRole("button", { name: "Stop Ask" })).querySelector("svg") as SVGElement;

    // A file is dragged onto the Stop icon; the answer lands and Send's icon replaces it under the pointer.
    fireEvent.dragEnter(stopIcon, { dataTransfer });
    expect(overlay()).toBeTruthy();
    await act(async () => {
      resolveResult?.({
        review_id: "review-1",
        latest_revision_number: 5,
        human: message({ id: "ram-drag-human", role: "human", body: "Explain this." }),
        assistant: message({ id: "ram-drag-answer", body: "Done." }),
      });
    });
    const sendIcon = screen.getByRole("button", { name: "Send Ask question" }).querySelector("svg") as SVGElement;
    expect(sendIcon).not.toBe(stopIcon);
    fireEvent.dragEnter(sendIcon, { dataTransfer });
    leave(stopIcon, sendIcon); // detached: this dragleave never reaches the composer

    // Moving between elements inside the composer keeps the overlay.
    fireEvent.dragEnter(composer, { dataTransfer });
    leave(sendIcon, composer);
    expect(overlay()).toBeTruthy();

    // Leaving the composer (and then dropping elsewhere) clears it.
    leave(composer, document.body);
    expect(overlay()).toBeNull();

    // Escape, or leaving the window, reports no related target; that clears it too.
    fireEvent.dragEnter(dropZone, { dataTransfer });
    expect(overlay()).toBeTruthy();
    leave(dropZone, null);
    expect(overlay()).toBeNull();
  });
});
