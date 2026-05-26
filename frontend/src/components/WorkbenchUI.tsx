import { type ReactNode, useState } from "react";
import { Check } from "lucide-react";
import { cn } from "../lib/utils";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card } from "./ui/card";

export { Alert } from "./ui/alert";
export { Badge, Badge as UiBadge } from "./ui/badge";
export { Button } from "./ui/button";
export { Card, CardContent, CardFooter, CardHeader } from "./ui/card";
export { DisclosureCard } from "./ui/disclosure-card";
export { EmptyState } from "./ui/empty-state";
export { Input } from "./ui/input";
export { Select } from "./ui/select";
export { ToggleGroup } from "./ui/toggle-group";

type Tone =
  | "amber"
  | "cyan"
  | "emerald"
  | "violet"
  | "neutral"
  | "red"
  | "purple";

const TONES: Record<
  Tone,
  { panel: string; eyebrow: string; value: string; border: string }
> = {
  amber: {
    panel: "border-neutral-800 bg-neutral-950/60",
    eyebrow: "text-neutral-400",
    value: "text-neutral-100",
    border: "border-neutral-800",
  },
  cyan: {
    panel: "border-neutral-800 bg-neutral-950/60",
    eyebrow: "text-neutral-400",
    value: "text-neutral-100",
    border: "border-neutral-800",
  },
  emerald: {
    panel: "border-neutral-800 bg-neutral-950/60",
    eyebrow: "text-neutral-400",
    value: "text-neutral-100",
    border: "border-neutral-800",
  },
  violet: {
    panel: "border-neutral-800 bg-neutral-950/60",
    eyebrow: "text-neutral-400",
    value: "text-neutral-100",
    border: "border-neutral-800",
  },
  neutral: {
    panel: "border-neutral-800 bg-neutral-950/60",
    eyebrow: "text-neutral-400",
    value: "text-neutral-100",
    border: "border-neutral-800",
  },
  red: {
    panel: "border-red-900/30 bg-red-950/20",
    eyebrow: "text-red-400",
    value: "text-red-200",
    border: "border-red-900/50",
  },
  purple: {
    panel: "border-purple-900/30 bg-purple-950/20",
    eyebrow: "text-purple-400",
    value: "text-purple-200",
    border: "border-purple-900/50",
  },
};

export const cx = cn;

export function FieldLabel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "text-[10px] font-mono uppercase tracking-widest text-neutral-500",
        className
      )}
    >
      {children}
    </div>
  );
}

export function PageHero({
  eyebrow,
  title,
  description,
  tone = "neutral",
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  tone?: Tone;
  children?: ReactNode;
}) {
  const palette = TONES[tone];
  return (
    <Card className={cn("p-5 md:p-6", palette.panel)}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl">
          <div
            className={cn(
              "text-[11px] font-mono uppercase tracking-[0.22em]",
              palette.eyebrow
            )}
          >
            {eyebrow}
          </div>
          <h1 className={cn("mt-2 text-3xl font-semibold", palette.value)}>
            {title}
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-neutral-400">
            {description}
          </p>
        </div>
        {children && <div className="min-w-[220px]">{children}</div>}
      </div>
    </Card>
  );
}

export function MetricCard({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: Tone;
}) {
  const palette = TONES[tone];
  return (
    <Card className={cn("p-4", palette.panel)}>
      <FieldLabel className={palette.eyebrow}>{label}</FieldLabel>
      <div className={cn("mt-2 text-lg font-semibold", palette.value)}>
        {value}
      </div>
      {detail && <div className="mt-2 text-xs text-neutral-500">{detail}</div>}
    </Card>
  );
}

export function SectionHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
      <div>
        {eyebrow && <FieldLabel className="tracking-[0.2em]">{eyebrow}</FieldLabel>}
        <h2 className="mt-1 text-lg font-semibold text-neutral-100">{title}</h2>
        {description && (
          <p className="mt-1 text-sm leading-relaxed text-neutral-400">
            {description}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}

export function FeaturePanel({
  icon,
  title,
  subtitle,
  description,
  status = "stable",
  bullets = [],
}: {
  icon: ReactNode;
  title: string;
  subtitle?: string;
  description: ReactNode;
  status?: string;
  bullets?: string[];
}) {
  return (
    <Card tone="muted" className="p-5">
      <div className="flex items-start gap-4">
        <div className="shrink-0 text-3xl">{icon}</div>
        <div className="flex-1">
          <div className="mb-2 flex items-center gap-3">
            <h2 className="font-mono text-lg font-bold text-neutral-200">
              {title}
            </h2>
            <Badge tone="emerald">{status}</Badge>
          </div>
          {subtitle && (
            <p className="mb-3 font-mono text-[11px] text-neutral-500">
              {subtitle}
            </p>
          )}
          <div className="text-xs leading-relaxed text-neutral-300">
            {description}
          </div>
          {bullets.length > 0 && (
            <div className="mt-3 space-y-1 text-xs text-emerald-300/90">
              {bullets.map((bullet) => (
                <p key={bullet} className="flex items-center gap-1.5">
                  <Check size={12} className="text-emerald-500" /> {bullet}
                </p>
              ))}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

export function ExplainerCard({
  title,
  detects,
  catches,
  why,
}: {
  title: string;
  detects: string;
  catches: string;
  why: string;
}) {
  return (
    <Card className="p-4">
      <div className="text-sm font-semibold text-neutral-100">{title}</div>
      <dl className="mt-4 space-y-3 text-sm">
        <div className="grid gap-1 md:grid-cols-[88px_1fr]">
          <dt>
            <FieldLabel>Detects</FieldLabel>
          </dt>
          <dd className="text-neutral-300">{detects}</dd>
        </div>
        <div className="grid gap-1 md:grid-cols-[88px_1fr]">
          <dt>
            <FieldLabel>Catches</FieldLabel>
          </dt>
          <dd className="text-neutral-300">{catches}</dd>
        </div>
        <div className="grid gap-1 md:grid-cols-[88px_1fr]">
          <dt>
            <FieldLabel>Why</FieldLabel>
          </dt>
          <dd className="text-neutral-300">{why}</dd>
        </div>
      </dl>
    </Card>
  );
}

export function CopyButton({
  text,
  label = "Copy",
  copiedLabel = "Copied",
  className = "",
}: {
  text: string;
  label?: string;
  copiedLabel?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <Button
      size="xs"
      className={className}
      onClick={handleCopy}
    >
      {copied ? copiedLabel : label}
    </Button>
  );
}

export function SnippetCard({
  title,
  body,
  caption,
}: {
  title: string;
  body: string;
  caption?: string;
}) {
  return (
    <Card className="bg-neutral-950/70">
      <div className="flex items-center justify-between gap-3 border-b border-neutral-800 px-4 py-3">
        <div>
          <div className="text-sm font-semibold text-neutral-100">{title}</div>
          {caption && <div className="mt-1 text-xs text-neutral-500">{caption}</div>}
        </div>
        <CopyButton text={body} />
      </div>
      <pre className="overflow-x-auto border-0 bg-transparent p-4 text-xs leading-relaxed text-neutral-300">
        {body}
      </pre>
    </Card>
  );
}

export function Chip({
  children,
  tone = "neutral",
  className = "",
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  const badgeTone =
    tone === "violet" ? "purple" : tone === "purple" ? "purple" : tone;
  return (
    <Badge tone={badgeTone} className={className}>
      {children}
    </Badge>
  );
}

export function Slider({
  label,
  value,
  min,
  max,
  step = 0.01,
  onChange,
  formatValue = (input) => input.toString(),
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
  formatValue?: (value: number) => string;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <FieldLabel>{label}</FieldLabel>
        <span className="font-mono text-sm text-neutral-100">
          {formatValue(value)}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(parseFloat(event.target.value))}
        className="h-1 w-full cursor-pointer appearance-none bg-neutral-800 accent-amber-500"
      />
    </div>
  );
}

export function Switch({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex items-center gap-3 transition hover:opacity-80"
    >
      <div
        className={cn(
          "flex h-4 w-8 items-center rounded-full px-0.5 transition",
          checked ? "bg-amber-600" : "bg-neutral-800"
        )}
      >
        <div
          className={cn(
            "h-3 w-3 rounded-full bg-white transition",
            checked ? "translate-x-4" : "translate-x-0"
          )}
        />
      </div>
      <span className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
        {label}
      </span>
    </button>
  );
}
