import { Button as UiButton } from "../components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "../components/ui/dialog";
import { Input as UiInput } from "../components/ui/input";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Command, Search } from "lucide-react";

import "./reviewUi.css";

export interface ReviewPaletteAction {
  id: string;
  label: string;
  detail?: string;
  shortcut?: string;
  disabled?: boolean;
  /** This action moves focus into a newly opened view/dialog; skip Radix return-to-opener. */
  managesFocus?: boolean;
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
    scope: "global",
    rows: [
      ["j / k", "Next / previous review area"],
      ["J / K", "Next / previous file"],
      ["] / [", "Next / previous review item"],
      ["/", "Search review"],
    ],
  },
  {
    title: "Judge",
    scope: "global",
    rows: [
      ["r", "Mark reviewed and advance"],
      ["u", "Reopen current review area"],
      ["x", "Mark needs changes"],
      ["c", "Comment on current review area"],
    ],
  },
  {
    title: "View",
    scope: "global",
    rows: [
      ["e", "Toggle Context"],
      ["f", "Toggle focus mode"],
      ["s", "Split / unified diff (cycles Auto → Split → Unified)"],
      ["w", "Wrap / one-line code"],
      ["p", "Review actions"],
      ["?", "Keyboard shortcuts"],
      ["Esc", "Close transient UI"],
    ],
  },
  {
    title: "Web preview",
    scope: "preview",
    rows: [
      ["1 / 2 / 3", "Preview / Compare / Pixels"],
      ["d / t / m", "Desktop / tablet / mobile"],
      ["{ / }", "Previous / next rendered route"],
    ],
  },
] as const;

export default function ReviewCommandPalette({ mode, actions, onClose }: ReviewCommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const actionManagesFocusRef = useRef(false);
  const listboxId = `${useId()}-actions`;
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
    // the only modal on screen when it focuses itself. A view/dialog action explicitly
    // suppresses Radix's normal return-to-opener so its destination can own focus.
    actionManagesFocusRef.current = Boolean(action.managesFocus);
    onClose();
    action.run();
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent
        className="top-[12vh] max-w-[580px] translate-y-0"
        onCloseAutoFocus={(event) => {
          if (actionManagesFocusRef.current) {
            event.preventDefault();
            actionManagesFocusRef.current = false;
          }
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          if (mode === "actions") inputRef.current?.focus();
          else closeRef.current?.focus();
        }}
        onKeyDown={(event) => {
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
        <div className="flex items-center gap-3 border-b border-border/80 px-3.5 py-3">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-neutral-400">
            <Command size={13} aria-hidden="true" />
          </div>
          <div className="min-w-0 flex-1">
            <DialogTitle className="text-body font-semibold text-foreground">
              {mode === "actions" ? "Review actions" : "Keyboard shortcuts"}
            </DialogTitle>
            <DialogDescription className="mt-0.5 text-caption text-muted-foreground">
              {mode === "actions" ? "Type to filter · ↑↓ move · Enter run" : "Keyboard-first controls for the continuous reader"}
            </DialogDescription>
          </div>
          <DialogClose asChild>
            <UiButton ref={closeRef} type="button" aria-label="Close review commands" className="h-7 px-2 font-mono text-caption">
              Esc
            </UiButton>
          </DialogClose>
        </div>

        {mode === "actions" ? (
          <>
            <div className="border-b border-border/70 p-2.5">
              <div className="relative flex items-center">
                <Search size={13} className="pointer-events-none absolute left-2.5 text-muted-foreground" />
              <UiInput
                uiSize="xs"
                ref={inputRef}
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setSelected(0);
                }}
                role="combobox"
                aria-expanded="true"
                aria-controls={listboxId}
                aria-autocomplete="list"
                aria-activedescendant={filtered[selected] ? `${listboxId}-${filtered[selected].id}` : undefined}
                aria-label="Filter review actions"
                placeholder="Search actions…"
                className="pl-8 text-body"
              />
              </div>
            </div>
            <div id={listboxId} role="listbox" aria-label="Review actions" className="max-h-[52vh] overflow-y-auto p-1.5">
              {filtered.length === 0 ? (
                <div className="px-3 py-6 text-center text-caption text-muted-foreground">No matching review action.</div>
              ) : filtered.map((action, index) => (
                <UiButton unstyled
                  key={action.id}
                  id={`${listboxId}-${action.id}`}
                  type="button"
                  role="option"
                  tabIndex={-1}
                  aria-selected={index === selected}
                  aria-disabled={action.disabled || undefined}
                  disabled={action.disabled}
                  onMouseEnter={() => setSelected(index)}
                  onClick={() => run(action)}
                  className={`flex w-full items-center gap-3 rounded-md px-2.5 py-2 text-left transition-colors disabled:opacity-35 ${index === selected ? "bg-accent text-accent-foreground" : ""}`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-label text-neutral-200">{action.label}</div>
                    {action.detail && <div className="mt-0.5 truncate text-caption text-muted-foreground">{action.detail}</div>}
                  </div>
                  {action.shortcut && <kbd className="shrink-0 border border-border px-1.5 py-0.5 font-mono text-caption text-muted-foreground">{action.shortcut}</kbd>}
                </UiButton>
              ))}
            </div>
          </>
        ) : (
          <div className="grid max-h-[60vh] gap-5 overflow-y-auto p-4 sm:grid-cols-2">
            {SHORTCUT_GROUPS.map((group) => (
              <div key={group.title} data-shortcut-scope={group.scope}>
                <div className="mb-2 text-caption font-medium uppercase tracking-[0.14em] text-muted-foreground">{group.title}</div>
                <div className="space-y-2">
                  {group.rows.map(([keys, label]) => (
                    <div key={keys} className="flex items-start gap-2 text-caption">
                      <kbd className="min-w-12 shrink-0 border border-border px-1.5 py-0.5 text-center font-mono text-neutral-300">{keys}</kbd>
                      <span className="leading-4 text-muted-foreground">{label}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
