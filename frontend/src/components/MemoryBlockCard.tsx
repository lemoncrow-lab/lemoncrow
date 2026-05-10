import type { MemoryBlock } from "../api";

interface MemoryBlockCardProps {
  block: MemoryBlock;
  onEdit: (block: MemoryBlock) => void;
  badges?: string[];
}

export default function MemoryBlockCard({
  block,
  onEdit,
  badges = [],
}: MemoryBlockCardProps) {
  return (
    <article className="py-3 border-b border-neutral-800">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-mono text-sm text-neutral-200 font-bold">
              {block.label}
            </h3>
            {block.pinned && (
              <span className="text-[10px] uppercase tracking-tight px-1.5 py-0.5 border border-amber-700/50 text-amber-300">
                pinned
              </span>
            )}
            <span className="text-[10px] uppercase tracking-tight px-1.5 py-0.5 border border-neutral-700 text-neutral-500">
              v{block.version}
            </span>
            {badges.map((badge) => (
              <span
                key={badge}
                className="text-[10px] uppercase tracking-tight px-1.5 py-0.5 border border-cyan-900/60 text-cyan-300"
              >
                {badge}
              </span>
            ))}
          </div>
          {block.description && (
            <p className="text-xs text-neutral-500 mt-1">{block.description}</p>
          )}
          <p className="text-xs text-neutral-300 mt-2 whitespace-pre-wrap break-words leading-relaxed">
            {block.value}
          </p>
        </div>

        <button
          type="button"
          aria-label={`Edit memory block ${block.label}`}
          onClick={() => onEdit(block)}
          className="text-[11px] px-2.5 py-1 border border-neutral-700 text-neutral-300 hover:text-amber-300 hover:border-amber-500/40 transition"
        >
          Edit
        </button>
      </div>
    </article>
  );
}
