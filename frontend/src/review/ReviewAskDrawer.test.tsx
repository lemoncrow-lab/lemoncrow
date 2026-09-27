import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewAskDrawer from "./ReviewAskDrawer";
import * as api from "./reviewApi";
import type { ReviewAskMessage } from "./types";

vi.mock("./reviewApi", () => ({
  fetchReviewAsk: vi.fn(),
  streamReviewAsk: vi.fn(),
  sendReviewAskHandoff: vi.fn(),
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
    }, expect.any(Function)));
    expect(await screen.findByText(/coordinator keeps live runtime state/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Authenticator · lines 84–131" }));
    expect(onOpenReference).toHaveBeenCalledWith(expect.objectContaining({
      path: "src/api.py",
      start_line: 84,
      end_line: 131,
    }));
    expect(screen.queryByRole("button", { name: /Send change/i })).toBeNull();
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

    fireEvent.click(screen.getByLabelText("Ask AI model"));
    fireEvent.click(screen.getByRole("button", { name: "openai/gpt-5.6" }));
    fireEvent.change(screen.getByLabelText("Ask Review"), { target: { value: "Explain this." } });
    fireEvent.click(screen.getByRole("button", { name: "Send Ask question" }));

    await waitFor(() => expect(api.streamReviewAsk).toHaveBeenCalledWith("review-1", {
      revision_number: 5,
      body: "Explain this.",
      driver: "litellm",
      model: "openai/gpt-5.6",
    }, expect.any(Function)));
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
    fireEvent.click(screen.getByLabelText("Ask AI driver"));
    fireEvent.click(screen.getByRole("button", { name: "Claude" }));
    fireEvent.click(screen.getByLabelText("Ask AI model"));

    expect(screen.getByRole("button", { name: "sonnet" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "opus" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "haiku" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "zen/claude-sonnet-4-6" })).toBeNull();
    expect(screen.queryByRole("button", { name: "anthropic/claude-sonnet-4-6" })).toBeNull();
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
    }, expect.any(Function)));
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
});
