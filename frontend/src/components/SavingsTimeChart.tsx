import type { SavingsByDay } from "../api";

interface SavingsTimeChartProps {
  data: SavingsByDay[];
  windowDays: number;
}

const WIDTH = 880;
const HEIGHT = 220;
const PAD_X = 32;
const PAD_Y = 22;

function buildAreaPath(
  points: Array<{ x: number; y: number }>,
  baseline: number
): string {
  if (points.length === 0) return "";
  const head = `M ${points[0].x} ${baseline}`;
  const line = points.map((p) => `L ${p.x} ${p.y}`).join(" ");
  const tail = `L ${points[points.length - 1].x} ${baseline} Z`;
  return `${head} ${line} ${tail}`;
}

export default function SavingsTimeChart({
  data,
  windowDays,
}: SavingsTimeChartProps) {
  if (data.length === 0) {
    return <div className="text-sm text-neutral-400">No trend data yet.</div>;
  }

  const maxVal = Math.max(1, ...data.map((d) => d.naive));
  const baseline = HEIGHT - PAD_Y;
  const chartWidth = WIDTH - PAD_X * 2;
  const chartHeight = HEIGHT - PAD_Y * 2;

  const actualPoints = data.map((d, i) => ({
    x: PAD_X + (i * chartWidth) / Math.max(1, data.length - 1),
    y: PAD_Y + chartHeight - (d.actual / maxVal) * chartHeight,
  }));

  const naivePoints = data.map((d, i) => ({
    x: PAD_X + (i * chartWidth) / Math.max(1, data.length - 1),
    y: PAD_Y + chartHeight - (d.naive / maxVal) * chartHeight,
  }));

  const actualPath = buildAreaPath(actualPoints, baseline);
  const naivePath = buildAreaPath(naivePoints, baseline);

  return (
    <div className="border border-neutral-800 bg-neutral-950/70 p-3">
      <div className="mb-2 flex items-center justify-between text-[11px] text-neutral-400 font-mono uppercase tracking-widest">
        <span>{windowDays}-day token trend</span>
        <span>Naive vs Actual</span>
      </div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full"
        role="img"
        aria-label={`${windowDays}-day token savings trend`}
      >
        <defs>
          <linearGradient id="naiveFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#c084fc" stopOpacity="0.45" />
            <stop offset="100%" stopColor="#c084fc" stopOpacity="0.08" />
          </linearGradient>
          <linearGradient id="actualFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#10b981" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#10b981" stopOpacity="0.12" />
          </linearGradient>
        </defs>

        <line
          x1={PAD_X}
          x2={WIDTH - PAD_X}
          y1={baseline}
          y2={baseline}
          stroke="#404040"
          strokeWidth="1"
        />
        <line
          x1={PAD_X}
          x2={PAD_X}
          y1={PAD_Y}
          y2={baseline}
          stroke="#404040"
          strokeWidth="1"
        />

        <path d={naivePath} fill="url(#naiveFill)" />
        <path d={actualPath} fill="url(#actualFill)" />

        {data.map((d, i) => {
          if (i % 3 !== 0 && i !== data.length - 1) return null;
          const x = PAD_X + (i * chartWidth) / Math.max(1, data.length - 1);
          return (
            <text
              key={d.day}
              x={x}
              y={HEIGHT - 4}
              fontSize="10"
              textAnchor="middle"
              fill="#737373"
            >
              {d.day.slice(5)}
            </text>
          );
        })}
      </svg>
      <div className="mt-2 flex items-center gap-4 text-[11px] text-neutral-400">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 bg-orange-400" />
          Naive
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 bg-emerald-400" />
          Actual
        </span>
      </div>
    </div>
  );
}
