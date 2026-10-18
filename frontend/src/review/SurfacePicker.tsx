import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import { cn } from "../lib/utils";
import { Search } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

export interface SurfacePickerOption {
  value: string;
  label: string;
  detail?: string;
  searchText?: string;
}

interface SurfacePickerProps {
  ariaLabel: string;
  value: string;
  options: SurfacePickerOption[];
  storageKey: string;
  placeholder?: string;
  onChange(value: string): void;
}

function loadQuery(storageKey: string): string {
  try {
    return window.localStorage.getItem(storageKey) ?? "";
  } catch {
    return "";
  }
}

function persistQuery(storageKey: string, query: string): void {
  try {
    window.localStorage.setItem(storageKey, query);
  } catch {
    // Filtering still works when browser storage is unavailable.
  }
}

export default function SurfacePicker({
  ariaLabel,
  value,
  options,
  storageKey,
  placeholder = "Search…",
  onChange,
}: SurfacePickerProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(0);
  const [query, setQuery] = useState(() => loadQuery(storageKey));
  const selected = options.find((option) => option.value === value) ?? options[0];
  const normalized = query.trim().toLocaleLowerCase();
  const filtered = useMemo(() => {
    if (!normalized) return options;
    return options.filter((option) => {
      const haystack = `${option.label} ${option.detail ?? ""} ${option.searchText ?? ""}`.toLocaleLowerCase();
      return normalized.split(/\s+/).every((token) => haystack.includes(token));
    });
  }, [normalized, options]);
  const optionId = (index: number) => `${listboxId}-option-${index}`;

  useEffect(() => {
    setHighlighted((current) => Math.min(current, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  useEffect(() => {
    if (open) document.getElementById(optionId(highlighted))?.scrollIntoView({ block: "nearest" });
  });

  if (!selected) return null;

  const choose = (next: string) => {
    onChange(next);
    setOpen(false);
  };

  // Combobox keys: focus stays in the filter; the highlighted option is announced via aria-activedescendant.
  const onFilterKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (filtered.length === 0) return;
    const last = filtered.length - 1;
    const next = event.key === "ArrowDown" ? Math.min(last, highlighted + 1)
      : event.key === "ArrowUp" ? Math.max(0, highlighted - 1)
        : event.key === "Home" ? 0
          : event.key === "End" ? last
            : -1;
    if (next >= 0) {
      event.preventDefault();
      setHighlighted(next);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const option = filtered[highlighted];
      if (option) choose(option.value);
    }
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setHighlighted(Math.max(0, filtered.findIndex((option) => option.value === value)));
      }}
    >
      <PopoverTrigger asChild>
        <UiButton unstyled
          type="button"
          aria-label={ariaLabel}
          tooltip={`${selected.label} · search and select`}
          className="flex max-w-72 items-center gap-1.5 rounded border border-neutral-700 bg-background px-2 py-1 font-mono text-caption text-neutral-300 hover:bg-muted/60"
        >
          <span className="truncate">{selected.label}</span>
          <span className="text-caption text-muted-foreground">⌄</span>
        </UiButton>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        aria-label={`${ariaLabel} picker`}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          inputRef.current?.focus();
        }}
        className="w-[min(34rem,80vw)] overflow-hidden p-0"
      >
        <div className="flex items-center gap-2 border-b border-border px-2.5 py-2">
          <Search size={12} className="shrink-0 text-muted-foreground" />
          <UiInput unstyled
            ref={inputRef}
            type="search"
            role="combobox"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-autocomplete="list"
            aria-activedescendant={filtered[highlighted] ? optionId(highlighted) : undefined}
            aria-label={`${ariaLabel} search`}
            value={query}
            placeholder={placeholder}
            onChange={(event) => {
              const next = event.currentTarget.value;
              setQuery(next);
              setHighlighted(0);
              persistQuery(storageKey, next);
            }}
            onKeyDown={onFilterKeyDown}
            className="min-w-0 flex-1 bg-transparent font-mono text-caption text-neutral-200 outline-none placeholder:text-muted-foreground"
          />
          <span className="shrink-0 font-mono text-caption text-muted-foreground">{filtered.length}/{options.length}</span>
        </div>
        <div id={listboxId} role="listbox" aria-label={`${ariaLabel} options`} className="max-h-72 overflow-y-auto p-1">
          {filtered.length > 0 ? filtered.map((option, index) => {
            const active = option.value === value;
            return (
              <UiButton unstyled
                key={option.value}
                id={optionId(index)}
                type="button"
                role="option"
                tabIndex={-1}
                aria-selected={active}
                onMouseEnter={() => setHighlighted(index)}
                onClick={() => choose(option.value)}
                className={cn(
                  "flex w-full items-start gap-3 rounded-md px-2 py-1.5 text-left",
                  index === highlighted ? "bg-accent" : active && "bg-violet-500/10"
                )}
              >
                <span className={`min-w-0 flex-1 truncate font-mono text-caption ${active ? "text-violet-200" : "text-neutral-300"}`}>{option.label}</span>
                {option.detail && <span className="max-w-48 shrink-0 truncate font-mono text-caption text-muted-foreground">{option.detail}</span>}
              </UiButton>
            );
          }) : (
            <div className="px-3 py-5 text-center text-caption text-muted-foreground">No matching surfaces</div>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
