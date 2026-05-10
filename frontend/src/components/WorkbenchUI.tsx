import { useState, type ReactNode } from "react";

type Tone =
  | "amber"
  | "cyan"
  | "emerald"
  | "violet"
  | "neutral"
  | "red"
  | "orange";

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
  orange: {
    panel: "border-orange-900/30 bg-orange-950/20",
    eyebrow: "text-orange-400",
    value: "text-orange-200",
    border: "border-orange-900/50",
  },
};

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
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
    <section className={cx("border p-5 md:p-6", palette.panel)}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl">
          <div
            className={cx(
              "text-[11px] font-mono uppercase tracking-[0.22em]",
              palette.eyebrow
            )}
          >
            {eyebrow}
          </div>
          <h1 className={cx("mt-2 text-3xl font-semibold", palette.value)}>
            {title}
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-neutral-400">
            {description}
          </p>
        </div>
        {children && <div className="min-w-[220px]">{children}</div>}
      </div>
    </section>
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
    <div className={cx("border p-4", palette.panel)}>
      <div
        className={cx(
          "text-[10px] font-mono uppercase tracking-widest",
          palette.eyebrow
        )}
      >
        {label}
      </div>
      <div className={cx("mt-2 text-lg font-semibold", palette.value)}>
        {value}
      </div>
      {detail && <div className="mt-2 text-xs text-neutral-500">{detail}</div>}
    </div>
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
        {eyebrow && (
          <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-neutral-500">
            {eyebrow}
          </div>
        )}
        <h2 className="mt-1 text-lg font-semibold text-neutral-100">{title}</h2>
        {description && (
          <p className="mt-1 text-sm text-neutral-400">{description}</p>
        )}
      </div>
      {action}
    </div>
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
    <div className="border border-neutral-800 bg-neutral-950/60 p-4">
      <div className="text-sm font-semibold text-neutral-100">{title}</div>
      <dl className="mt-4 space-y-3 text-sm">
        <div className="grid gap-1 md:grid-cols-[88px_1fr]">
          <dt className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Detects
          </dt>
          <dd className="text-neutral-300">{detects}</dd>
        </div>
        <div className="grid gap-1 md:grid-cols-[88px_1fr]">
          <dt className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Catches
          </dt>
          <dd className="text-neutral-300">{catches}</dd>
        </div>
        <div className="grid gap-1 md:grid-cols-[88px_1fr]">
          <dt className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Why
          </dt>
          <dd className="text-neutral-300">{why}</dd>
        </div>
      </dl>
    </div>
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
    <button
      type="button"
      onClick={handleCopy}
      className={cx(
        "border border-neutral-700 px-2.5 py-1 text-[10px] font-mono uppercase tracking-widest text-neutral-300 transition hover:border-amber-500/50 hover:text-amber-300",
        className
      )}
    >
      {copied ? copiedLabel : label}
    </button>
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
    <div className="border border-neutral-800 bg-neutral-950/70">
      <div className="flex items-center justify-between gap-3 border-b border-neutral-800 px-4 py-3">
        <div>
          <div className="text-sm font-semibold text-neutral-100">{title}</div>
          {caption && (
            <div className="mt-1 text-xs text-neutral-500">{caption}</div>
          )}
        </div>
        <CopyButton text={body} />
      </div>
      <pre className="overflow-x-auto border-0 bg-transparent p-4 text-xs leading-relaxed text-neutral-300">
        {body}
      </pre>
    </div>
  );
}

export function Chip({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: Tone;
}) {
  const palette = TONES[tone];
  return (
    <span
      className={cx(
        "inline-flex items-center border px-2 py-0.5 text-[10px] font-mono uppercase tracking-widest",
        palette.border,
        palette.eyebrow
      )}
    >
      {children}
    </span>
  );
}
