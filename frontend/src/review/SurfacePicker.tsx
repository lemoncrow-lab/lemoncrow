import { Search } from "lucide-react";
import { useMemo, useRef, useState } from "react";

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
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
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

  if (!selected) return null;

  return (
    <details
      data-review-dropdown
      ref={detailsRef}
      className="group relative shrink-0"
      onToggle={(event) => {
        if (event.currentTarget.open) window.requestAnimationFrame(() => inputRef.current?.focus());
      }}
    >
      <summary
        role="button"
        aria-label={ariaLabel}
        title={`${selected.label} · search and select`}
        className="flex max-w-72 cursor-pointer list-none items-center gap-1.5 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 font-mono text-[10px] text-neutral-300 outline-none hover:border-neutral-600 focus:border-violet-700 [&::-webkit-details-marker]:hidden"
      >
        <span className="truncate">{selected.label}</span>
        <span className="text-[10px] text-neutral-600">⌄</span>
      </summary>
      <div className="absolute left-0 top-full z-50 mt-1 w-[min(34rem,80vw)] overflow-hidden rounded-md border border-neutral-700 bg-neutral-950 shadow-2xl">
        <div className="flex items-center gap-2 border-b border-neutral-800 px-2.5 py-2">
          <Search size={12} className="shrink-0 text-neutral-600" />
          <input
            ref={inputRef}
            type="search"
            aria-label={`${ariaLabel} search`}
            value={query}
            placeholder={placeholder}
            onChange={(event) => {
              const next = event.currentTarget.value;
              setQuery(next);
              persistQuery(storageKey, next);
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                detailsRef.current?.removeAttribute("open");
                event.currentTarget.blur();
              }
            }}
            className="min-w-0 flex-1 bg-transparent font-mono text-[10px] text-neutral-200 outline-none placeholder:text-neutral-700"
          />
          <span className="shrink-0 font-mono text-[10px] text-neutral-600">{filtered.length}/{options.length}</span>
        </div>
        <div role="listbox" aria-label={`${ariaLabel} options`} className="max-h-72 overflow-y-auto p-1">
          {filtered.length > 0 ? filtered.map((option) => {
            const active = option.value === value;
            return (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  onChange(option.value);
                  detailsRef.current?.removeAttribute("open");
                }}
                className={`flex w-full items-start gap-3 rounded px-2 py-1.5 text-left ${active ? "bg-violet-500/10" : "hover:bg-neutral-900"}`}
              >
                <span className={`min-w-0 flex-1 truncate font-mono text-[10px] ${active ? "text-violet-200" : "text-neutral-300"}`}>{option.label}</span>
                {option.detail && <span className="max-w-48 shrink-0 truncate font-mono text-[10px] text-neutral-600">{option.detail}</span>}
              </button>
            );
          }) : (
            <div className="px-3 py-5 text-center text-[10px] text-neutral-600">No matching surfaces</div>
          )}
        </div>
      </div>
    </details>
  );
}
