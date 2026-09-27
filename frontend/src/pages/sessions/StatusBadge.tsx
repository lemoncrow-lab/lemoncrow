import { cx } from "../../components/WorkbenchUI";
import { statusBadgeClass, statusDotClass } from "../../lib/status";

export function StatusDot({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-block h-2 w-2 rounded-full",
        statusDotClass(status),
        className
      )}
      title={status}
      aria-label={`Status: ${status}`}
    />
  );
}

export function StatusBadge({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "text-[10px] px-2 py-0.5 border uppercase font-black tracking-[0.2em] font-mono inline-block",
        statusBadgeClass(status),
        className
      )}
    >
      {status}
    </span>
  );
}
