import { Select, SelectItem } from "./ui/select";
import { cn } from "../lib/utils";
import { TIME_RANGE_OPTIONS, useTimeRange, type TimeRangeValue } from "../lib/TimeRangeContext";

/**
 * The time window of the page it sits on (Usage, its inspector sheets, Telemetry). Home and Sessions keep their
 * fixed windows, so the window is chosen where it applies rather than in the top bar.
 */
export function TimeWindowSelect({ className, label = "Time window" }: { className?: string; label?: string }) {
  const { range, setRange } = useTimeRange();
  return (
    <div className={cn("inline-flex shrink-0 items-center gap-2 text-meta text-muted-foreground", className)}>
      <span aria-hidden="true">Window</span>
      <Select
        value={range}
        onValueChange={(value) => setRange(value as TimeRangeValue)}
        uiSize="xs"
        className="min-w-32"
        aria-label={label}
      >
        {TIME_RANGE_OPTIONS.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </Select>
    </div>
  );
}
