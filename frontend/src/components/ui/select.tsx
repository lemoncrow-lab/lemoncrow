import {
  Children,
  Fragment,
  isValidElement,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "../../lib/utils";
import { fieldStyles } from "./input";
import { usePortalContainer } from "./portal-container";

type SelectSize = "xs" | "sm";

const SIZE_STYLES: Record<SelectSize, string> = {
  xs: "h-8 px-2.5 text-caption",
  sm: "h-9 px-3 text-xs",
};

export interface SelectItemProps {
  value: string | number;
  children: ReactNode;
  disabled?: boolean;
  title?: string;
  className?: string;
}

/** Declarative item marker consumed by Select; it never renders a native option. */
export function SelectItem(_props: SelectItemProps) {
  return null;
}

interface ParsedItem extends SelectItemProps {
  value: string;
}

interface SelectProps {
  value: string | number;
  onValueChange?(value: string): void;
  children: ReactNode;
  uiSize?: SelectSize;
  className?: string;
  contentClassName?: string;
  id?: string;
  disabled?: boolean;
  title?: string;
  placeholder?: ReactNode;
  searchable?: boolean;
  searchPlaceholder?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
}

function parseItems(children: ReactNode): ParsedItem[] {
  return Children.toArray(children).flatMap((child) => {
    if (!isValidElement(child)) return [];
    if (child.type === Fragment) return parseItems(child.props.children);
    if (child.type !== SelectItem) return [];
    const item = child as React.ReactElement<SelectItemProps>;
    return [{ ...item.props, value: String(item.props.value) }];
  });
}

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join(" ");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}

export function Select({
  value,
  onValueChange,
  children,
  uiSize = "sm",
  className,
  contentClassName,
  id,
  disabled = false,
  title,
  placeholder = "Select…",
  searchable = false,
  searchPlaceholder = "Search…",
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
}: SelectProps) {
  const generatedId = useId();
  const listboxId = `${id || generatedId}-listbox`;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const items = useMemo(() => parseItems(children), [children]);
  const selectedValue = String(value);
  const selected = items.find((item) => item.value === selectedValue);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlighted, setHighlighted] = useState(() => Math.max(0, items.findIndex((item) => item.value === selectedValue)));
  const [rect, setRect] = useState<DOMRect | null>(null);
  const portalContainer = usePortalContainer();

  const popupStyle: CSSProperties | undefined = rect
    ? (() => {
        const viewportPadding = 8;
        const gap = 5;
        const maxPopupHeight = 352;
        const popupWidth = Math.min(
          Math.max(rect.width, 180),
          Math.max(0, window.innerWidth - viewportPadding * 2)
        );
        const availableBelow = Math.max(
          0,
          window.innerHeight - rect.bottom - gap - viewportPadding
        );
        const availableAbove = Math.max(0, rect.top - gap - viewportPadding);
        const openAbove = availableBelow < 160 && availableAbove > availableBelow;
        const availableHeight = openAbove ? availableAbove : availableBelow;

        return {
          left: Math.max(
            viewportPadding,
            Math.min(rect.left, window.innerWidth - popupWidth - viewportPadding)
          ),
          top: openAbove ? undefined : rect.bottom + gap,
          bottom: openAbove ? window.innerHeight - rect.top + gap : undefined,
          minWidth: popupWidth,
          // Inline (not only a class) so it holds wherever the stylesheet has not loaded.
          pointerEvents: "auto",
          maxHeight: Math.max(48, Math.min(maxPopupHeight, availableHeight)),
        };
      })()
    : undefined;

  const allEnabledIndexes = useMemo(
    () => items.map((item, index) => (!item.disabled ? index : -1)).filter((index) => index >= 0),
    [items]
  );
  const visibleIndexes = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return items
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => {
        if (!searchable || !needle) return true;
        const haystack = `${item.value} ${item.title ?? ""} ${nodeText(item.children)}`.toLocaleLowerCase();
        return haystack.includes(needle);
      })
      .map(({ index }) => index);
  }, [items, query, searchable]);
  const enabledIndexes = useMemo(
    () => visibleIndexes.filter((index) => !items[index]?.disabled),
    [items, visibleIndexes]
  );

  const updateRect = () => {
    if (triggerRef.current) setRect(triggerRef.current.getBoundingClientRect());
  };

  const openMenu = (preferred?: "first" | "last") => {
    if (disabled || !items.length || !allEnabledIndexes.length) return;
    updateRect();
    setQuery("");
    const selectedIndex = items.findIndex((item) => item.value === selectedValue && !item.disabled);
    const fallback = preferred === "last" ? allEnabledIndexes.at(-1) : allEnabledIndexes[0];
    setHighlighted(selectedIndex >= 0 ? selectedIndex : fallback ?? 0);
    setOpen(true);
  };

  const choose = (index: number) => {
    const item = items[index];
    if (!item || item.disabled) return;
    onValueChange?.(item.value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const move = (delta: number) => {
    if (!enabledIndexes.length) return;
    const current = Math.max(0, enabledIndexes.indexOf(highlighted));
    const next = (current + delta + enabledIndexes.length) % enabledIndexes.length;
    setHighlighted(enabledIndexes[next]);
  };

  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      move(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (enabledIndexes.includes(highlighted)) choose(highlighted);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  };

  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) openMenu("first");
      else move(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) openMenu("last");
      else move(-1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!open) openMenu();
      else choose(highlighted);
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
    } else if (event.key === "Tab" && open) {
      setOpen(false);
    } else if (event.key === "Home" && open) {
      event.preventDefault();
      if (enabledIndexes.length) setHighlighted(enabledIndexes[0]);
    } else if (event.key === "End" && open) {
      event.preventDefault();
      if (enabledIndexes.length) setHighlighted(enabledIndexes.at(-1) ?? 0);
    }
  };

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: MouseEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target) || popupRef.current?.contains(target)) return;
      setOpen(false);
    };
    const reposition = () => updateRect();
    document.addEventListener("mousedown", dismiss);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("mousedown", dismiss);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open]);

  useEffect(() => {
    if (open && searchable) searchRef.current?.focus();
  }, [open, searchable]);

  useEffect(() => {
    if (!open || !searchable || enabledIndexes.includes(highlighted)) return;
    setHighlighted(enabledIndexes[0] ?? -1);
  }, [enabledIndexes, highlighted, open, searchable]);

  useEffect(() => {
    if (!open) return;
    const option = popupRef.current?.querySelector<HTMLElement>(`[data-select-index="${highlighted}"]`);
    if (option && typeof option.scrollIntoView === "function") {
      option.scrollIntoView({ block: "nearest" });
    }
  }, [highlighted, open]);

  return (
    <>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-activedescendant={
          open && items[highlighted]
            ? `${listboxId}-option-${highlighted}`
            : undefined
        }
        data-value={selectedValue}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        disabled={disabled}
        title={title}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onTriggerKeyDown}
        className={cn(
          "inline-flex min-w-0 items-center justify-between gap-2 text-left",
          fieldStyles,
          SIZE_STYLES[uiSize],
          className
        )}
      >
        <span className="min-w-0 flex-1 truncate">{selected?.children ?? placeholder}</span>
        <ChevronDown size={12} className={cn("shrink-0 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open && rect && createPortal(
        <div
          ref={popupRef}
          data-ui-select-popup
          className={cn(
            // pointer-events-auto: a modal Radix dialog disables pointer events outside its layer.
            "fixed z-50 flex max-h-[min(22rem,calc(100vh-24px))] flex-col overflow-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-lg shadow-black/20",
            contentClassName
          )}
          style={popupStyle}
        >
          {searchable && (
            <div className="border-b border-border p-1.5">
              <input
                ref={searchRef}
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={onSearchKeyDown}
                aria-label={searchPlaceholder}
                aria-controls={listboxId}
                aria-activedescendant={
                  enabledIndexes.includes(highlighted) && items[highlighted]
                    ? `${listboxId}-option-${highlighted}`
                    : undefined
                }
                placeholder={searchPlaceholder}
                className={cn(fieldStyles, "h-8 w-full px-2 text-caption")}
              />
            </div>
          )}
          <div
            id={listboxId}
            role="listbox"
            aria-label={ariaLabel}
            aria-labelledby={ariaLabelledBy}
            className="min-h-0 overflow-y-auto p-1"
          >
            {visibleIndexes.map((index) => {
              const item = items[index];
              const active = item.value === selectedValue;
              const focused = index === highlighted;
              return (
                <button
                  key={`${item.value}:${index}`}
                  id={`${listboxId}-option-${index}`}
                  type="button"
                  role="option"
                  tabIndex={-1}
                  aria-selected={active}
                  aria-disabled={item.disabled || undefined}
                  disabled={item.disabled}
                  title={item.title}
                  data-select-index={index}
                  onMouseEnter={() => !item.disabled && setHighlighted(index)}
                  onClick={() => choose(index)}
                  className={cn(
                    "flex w-full min-w-0 items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-label transition-colors",
                    focused ? "bg-accent text-accent-foreground" : "text-neutral-300",
                    item.disabled && "cursor-not-allowed opacity-40",
                    item.className
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">{item.children}</span>
                  {active && <Check size={12} className="shrink-0" />}
                </button>
              );
            })}
            {searchable && visibleIndexes.length === 0 && (
              <div className="px-2 py-3 text-center text-caption text-muted-foreground">No matches</div>
            )}
          </div>
        </div>,
        portalContainer ?? document.body
      )}
    </>
  );
}
