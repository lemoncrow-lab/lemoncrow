import { Button as UiButton } from "./ui/button";
interface ExpandableCardProps {
  id: string;
  icon: string;
  title: string;
  subtitle?: string;
  status?: "stable" | "beta" | "alpha";
  isExpanded?: boolean;
  onToggle?: (id: string) => void;
  children?: React.ReactNode;
}

const statusColors = {
  stable: "bg-emerald-900/30 text-emerald-300",
  beta: "bg-amber-900/30 text-amber-300",
  alpha: "bg-red-900/30 text-red-300",
};

export function ExpandableCard({
  id,
  icon,
  title,
  subtitle,
  status,
  isExpanded = false,
  onToggle,
  children,
}: ExpandableCardProps) {
  return (
    <div
      className="border border-border bg-muted/50 overflow-hidden transition-all duration-200"
      style={{
        borderRadius: 0,
      }}
    >
      {/* Header */}
      <UiButton unstyled
        onClick={() => onToggle?.(id)}
        className="w-full px-5 py-4 text-left hover:bg-accent/50 transition-colors flex items-start justify-between group"
      >
        <div className="flex-1 flex items-start gap-4 min-w-0">
          {/* Icon */}
          <div className="text-h3 flex-shrink-0 mt-0.5">{icon}</div>

          {/* Title & Subtitle */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-1">
              {/* Expandable indicator */}
              <span
                className={`text-amber-300 font-mono text-body transition-transform ${
                  isExpanded ? "rotate-90" : ""
                }`}
              >
                ❯
              </span>
              <h3 className="font-semibold text-foreground text-heading">
                {title}
              </h3>
              {status && (
                <span
                  className={`text-caption px-2 py-0.5 font-mono font-semibold uppercase tracking-wide ${
                    statusColors[status]
                  }`}
                >
                  {status}
                </span>
              )}
            </div>
            {subtitle && (
              <p className="font-mono text-label text-muted-foreground truncate">
                {subtitle}
              </p>
            )}
          </div>
        </div>
      </UiButton>

      {/* Expanded Content */}
      {isExpanded && (
        <div className="border-t border-border bg-background/50 px-5 py-4">
          <div className="space-y-4 font-mono text-heading text-subtle">
            {children}
          </div>
        </div>
      )}
    </div>
  );
}
