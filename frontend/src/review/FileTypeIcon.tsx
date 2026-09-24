import { lazy, Suspense } from "react";
import { File as GenericFileIcon } from "lucide-react";

const SymbolsFileIcon = lazy(async () => {
  const module = await import("@react-symbols/icons/utils");
  return { default: module.FileIcon };
});

interface FileTypeIconProps {
  path: string;
  size?: number;
  className?: string;
}

function basename(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  return normalized.split("/").pop() || normalized;
}

/**
 * Resolve a development-file icon from the full path.
 *
 * The Symbols VS Code icon set covers extensions plus well-known filenames
 * such as package.json, Dockerfile, vite.config.*, and README.*. It is loaded
 * as a separate chunk so the icon catalog does not inflate Review's startup
 * bundle. Keeping resolution behind one component also gives project-specific
 * extensions a single integration point later.
 */
export default function FileTypeIcon({ path, size = 14, className = "" }: FileTypeIconProps) {
  const fileName = basename(path);
  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center ${className}`.trim()}
      aria-hidden="true"
      data-file-icon={fileName}
    >
      <Suspense fallback={<GenericFileIcon width={size} height={size} strokeWidth={1.6} className="text-neutral-600" />}>
        <SymbolsFileIcon
          fileName={fileName}
          autoAssign
          width={size}
          height={size}
        />
      </Suspense>
    </span>
  );
}
