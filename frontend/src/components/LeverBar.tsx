interface LeverBarProps {
  label: string;
  value: number;
  maxValue: number;
}

function toTitle(label: string): string {
  return label
    .split("_")
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}

export default function LeverBar({ label, value, maxValue }: LeverBarProps) {
  const pct = maxValue > 0 ? Math.round((value / maxValue) * 100) : 0;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs">
        <span className="font-mono uppercase tracking-wide text-neutral-300">
          {toTitle(label)}
        </span>
        <span className="font-mono text-emerald-300">
          {value.toLocaleString()} tok
        </span>
      </div>
      <progress
        className="h-3 w-full [&::-webkit-progress-bar]:bg-neutral-900 [&::-webkit-progress-value]:bg-gradient-to-r [&::-webkit-progress-value]:from-cyan-500 [&::-webkit-progress-value]:to-emerald-400"
        value={pct}
        max={100}
      />
    </div>
  );
}
