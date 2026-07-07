import {
  createContext,
  useContext,
  useState,
  ReactNode,
  useEffect,
} from "react";

export const TIME_RANGE_OPTIONS = [
  { value: "1d", label: "Today" },
  { value: "7d", label: "This Week" },
  { value: "30d", label: "This Month" },
  { value: "90d", label: "This Quarter" },
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

function startOfLocalDay(now: Date) {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function startOfLocalWeek(now: Date) {
  const start = startOfLocalDay(now);
  const weekday = start.getDay();
  const daysFromMonday = (weekday + 6) % 7;
  start.setDate(start.getDate() - daysFromMonday);
  return start;
}

function startOfLocalMonth(now: Date) {
  return new Date(now.getFullYear(), now.getMonth(), 1);
}

function startOfLocalQuarter(now: Date) {
  const quarterMonth = Math.floor(now.getMonth() / 3) * 3;
  return new Date(now.getFullYear(), quarterMonth, 1);
}

function rangeStart(range: TimeRangeValue, now: Date) {
  switch (range) {
    case "1d":
      return startOfLocalDay(now);
    case "7d":
      return startOfLocalWeek(now);
    case "30d":
      return startOfLocalMonth(now);
    case "90d":
      return startOfLocalQuarter(now);
  }
}

function calendarDaysInRange(start: Date, end: Date) {
  const startLocal = new Date(
    start.getFullYear(),
    start.getMonth(),
    start.getDate()
  );
  const endLocal = new Date(end.getFullYear(), end.getMonth(), end.getDate());
  return Math.max(
    1,
    Math.floor((endLocal.getTime() - startLocal.getTime()) / 86_400_000) + 1
  );
}

export function TimeRangeProvider({ children }: { children: ReactNode }) {
  const [range, setRange] = useState<TimeRangeValue>(() => {
    const saved = localStorage.getItem("atelier_time_range");
    if (saved && TIME_RANGE_OPTIONS.some((o) => o.value === saved)) {
      return saved as TimeRangeValue;
    }
    return "7d";
  });
  const [clock, setClock] = useState(() => Date.now());

  const now = new Date(clock);
  const start = rangeStart(range, now);
  const seconds = Math.max(
    0,
    Math.floor((now.getTime() - start.getTime()) / 1000)
  );
  const days = calendarDaysInRange(start, now);

  useEffect(() => {
    localStorage.setItem("atelier_time_range", range);
  }, [range]);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      setClock(Date.now());
    }, 60_000);

    return () => window.clearInterval(intervalId);
  }, []);

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
