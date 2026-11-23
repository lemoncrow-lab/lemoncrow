import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "./ui/button";

/**
 * An internal identifier kept out of the reading line: a small icon button that copies it.
 * The id itself is the button's hint, so it can still be read on hover or focus; the visible text leads with a name.
 */
export function CopyId({ value, label = "ID" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  if (!value) return null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-5 w-5 align-middle text-muted-foreground"
      aria-label={copied ? `Copied ${label}` : `Copy ${label} ${value}`}
      tooltip={copied ? "Copied" : `${label}: ${value}`}
      data-copy-id={value}
      onClick={() => void copy()}
    >
      {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
    </Button>
  );
}
