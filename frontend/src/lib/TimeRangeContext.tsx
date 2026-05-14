import { createContext, useContext, useState, ReactNode, useEffect } from "react";

export const TIME_RANGE_OPTIONS = [
  { value: "1d", label: "Last 24 Hours", days: 1, seconds: 86400 },
  { value: "7d", label: "Last 7 Days", days: 7, seconds: 604800 },
  { value: "30d", label: "Last 30 Days", days: 30, seconds: 2592000 },
  { value: "90d", label: "Last 90 Days", days: 90, seconds: 7776000 },
] as const;

export type TimeRangeValue = (typeof TIME_RANGE_OPTIONS)[number]["value"];

interface TimeRangeContextType {
  range: TimeRangeValue;
  setRange: (range: TimeRangeValue) => void;
  days: number;
  seconds: number;
}

const TimeRangeContext = createContext<TimeRangeContextType | undefined>(undefined);

export function TimeRangeProvider({ children }: { children: ReactNode }) {
  const [range, setRange] = useState<TimeRangeValue>(() => {
    const saved = localStorage.getItem("atelier_time_range");
    if (saved && TIME_RANGE_OPTIONS.some(o => o.value === saved)) {
      return saved as TimeRangeValue;
    }
    return "7d";
  });

  const selectedOption = TIME_RANGE_OPTIONS.find(o => o.value === range)!;

  useEffect(() => {
    localStorage.setItem("atelier_time_range", range);
  }, [range]);

  return (
    <TimeRangeContext.Provider
      value={{
        range,
        setRange,
        days: selectedOption.days,
        seconds: selectedOption.seconds,
      }}
    >
      {children}
    </TimeRangeContext.Provider>
  );
}

export function useTimeRange() {
  const context = useContext(TimeRangeContext);
  if (context === undefined) {
    throw new Error("useTimeRange must be used within a TimeRangeProvider");
  }
  return context;
}
