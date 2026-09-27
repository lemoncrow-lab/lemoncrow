import {
  createContext,
  useContext,
  useState,
  ReactNode,
  useEffect,
} from "react";

export const TIME_RANGE_OPTIONS = [
  { value: "1d", label: "Today" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
] as const;

export type TimeRangeValue = (typeof TIME_RANGE_OPTIONS)[number]["value"];

interface TimeRangeContextType {
  range: TimeRangeValue;
  setRange: (range: TimeRangeValue) => void;
  days: number;
  seconds: number;
}

const TimeRangeContext = createContext<TimeRangeContextType | undefined>(
  undefined
);

const RANGE_DAYS: Record<TimeRangeValue, number> = {
  "1d": 1,
  "7d": 7,
  "30d": 30,
  "90d": 90,
};

export function TimeRangeProvider({ children }: { children: ReactNode }) {
  const [range, setRange] = useState<TimeRangeValue>(() => {
    const saved = localStorage.getItem("lemoncrow_time_range");
    if (saved && TIME_RANGE_OPTIONS.some((o) => o.value === saved)) {
      return saved as TimeRangeValue;
    }
    return "7d";
  });
  const days = RANGE_DAYS[range];
  const seconds = days * 86_400;

  useEffect(() => {
    localStorage.setItem("lemoncrow_time_range", range);
  }, [range]);

  return (
    <TimeRangeContext.Provider
      value={{
        range,
        setRange,
        days,
        seconds,
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
