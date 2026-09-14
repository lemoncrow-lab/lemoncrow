import { useEffect, useMemo, useRef, useState } from "react";

import { trapModalTab } from "./focusTrap";

export interface ReviewPaletteAction {
  id: string;
  label: string;
  detail?: string;
  shortcut?: string;
  disabled?: boolean;
  run(): void;
}

interface ReviewCommandPaletteProps {
  mode: "actions" | "shortcuts";
  actions: ReviewPaletteAction[];
  onClose(): void;
}

const SHORTCUT_GROUPS = [
  {
    title: "Navigate",
    rows: [
      ["j / k", "Next / previous review target"],
      ["J / K", "Next / previous file"],
      ["] / [", "Next / previous high-attention target"],
      ["/", "Search review"],
    ],
  },
  {
    title: "Judge",
    rows: [
      ["r", "Mark reviewed and advance"],
      ["u", "Reopen current target"],
      ["x", "Mark needs changes"],
      ["c", "Comment on current target"],
    ],
  },
  {
    title: "View",
    rows: [
      ["e", "Toggle Context"],
      ["f", "Toggle focus mode"],
      ["s", "Split / unified diff"],
      ["p", "Review actions"],
      ["?", "Keyboard shortcuts"],
      ["Esc", "Close transient UI"],
    ],
  },
] as const;

export default function ReviewCommandPalette({ mode, actions, onClose }: ReviewCommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return actions;
    return actions.filter((action) =>
      [action.label, action.detail ?? "", action.shortcut ?? ""].join(" ").toLowerCase().includes(needle),
    );
  }, [actions, query]);

  useEffect(() => {
    if (mode === "actions") inputRef.current?.focus();
    else closeRef.current?.focus();
  }, [mode]);

  useEffect(() => {
    setSelected((current) => Math.min(current, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  function run(action: ReviewPaletteAction | undefined) {
    if (!action || action.disabled) return;
    // Close first, then run: the action may mount another dialog, and that dialog must be
    // the only modal on screen when it focuses itself. Whoever owns the palette is
    // responsible for not stealing focus back from it (see ReviewReader.closeCommands).
    onClose();
    action.run();
  }

  return (
    <div
      className="absolute inset-0 z-[90] flex items-start justify-center bg-black/55 px-4 pt-[12vh]"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
          return;
        }
        if (mode !== "actions" || filtered.length === 0) return;
        if (event.key === "ArrowDown") {
          event.preventDefault();
          setSelected((current) => (current + 1) % filtered.length);
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          setSelected((current) => (current - 1 + filtered.length) % filtered.length);
        } else if (event.key === "Enter") {
          event.preventDefault();
          run(filtered[selected]);
        }
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="review-command-title"
        className="w-full max-w-[560px] border border-neutral-700 bg-neutral-950 shadow-2xl"
      >
        <div className="flex items-center gap-3 border-b border-neutral-800 px-3 py-2.5">
          <div className="min-w-0 flex-1">
            <h2 id="review-command-title" className="text-[12px] font-medium text-neutral-100">
              {mode === "actions" ? "Review actions" : "Keyboard shortcuts"}
            </h2>
            <div className="mt-0.5 text-[9px] text-neutral-600">
              {mode === "actions" ? "Type to filter · ↑↓ move · Enter run" : "Keyboard-first controls for the continuous reader"}
            </div>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close review commands" className="text-[10px] text-neutral-500 hover:text-neutral-200">
            Esc
          </button>
        </div>

        {mode === "actions" ? (
          <>
            <div className="border-b border-neutral-900 p-2">
              <input
                ref={inputRef}
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setSelected(0);
                }}
                aria-label="Filter review actions"
                placeholder="Search actions…"
                className="w-full border border-neutral-800 bg-neutral-950 px-2.5 py-2 text-[11px] text-neutral-200 outline-none placeholder:text-neutral-700 focus:border-sky-800"
              />
            </div>
            <div className="max-h-[52vh] overflow-y-auto p-1.5">
              {filtered.length === 0 ? (
                <div className="px-3 py-6 text-center text-[10px] text-neutral-600">No matching review action.</div>
              ) : filtered.map((action, index) => (
                <button
                  key={action.id}
                  type="button"
                  disabled={action.disabled}
                  onMouseEnter={() => setSelected(index)}
                  onClick={() => run(action)}
                  className={`flex w-full items-center gap-3 px-2.5 py-2 text-left disabled:opacity-35 ${index === selected ? "bg-neutral-900" : "hover:bg-neutral-900/70"}`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-[11px] text-neutral-200">{action.label}</div>
                    {action.detail && <div className="mt-0.5 truncate text-[9px] text-neutral-600">{action.detail}</div>}
                  </div>
                  {action.shortcut && <kbd className="shrink-0 border border-neutral-800 px-1.5 py-0.5 font-mono text-[9px] text-neutral-500">{action.shortcut}</kbd>}
                </button>
              ))}
            </div>
          </>
        ) : (
          <div className="grid max-h-[60vh] gap-5 overflow-y-auto p-4 sm:grid-cols-3">
            {SHORTCUT_GROUPS.map((group) => (
              <div key={group.title}>
                <div className="mb-2 text-[9px] font-medium uppercase tracking-[0.14em] text-neutral-500">{group.title}</div>
                <div className="space-y-2">
                  {group.rows.map(([keys, label]) => (
                    <div key={keys} className="flex items-start gap-2 text-[10px]">
                      <kbd className="min-w-12 shrink-0 border border-neutral-800 px-1.5 py-0.5 text-center font-mono text-neutral-300">{keys}</kbd>
                      <span className="leading-4 text-neutral-500">{label}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
