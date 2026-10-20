import { lazy, Suspense } from "react";
import { File as GenericFileIcon } from "lucide-react";

const SymbolsFileIcon = lazy(async () => {
  const module = await import("@react-symbols/icons/utils");
  return { default: module.FileIcon };
});

interface FileTypeIconProps {
  path: string;
  className?: string;
}

/** File-type glyphs encode content and keep their colours, but sit on the 14px step of the icon scale. */
const SIZE = 14;

function basename(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  return normalized.split("/").pop() || normalized;
}

/**
 * Resolve a development-file icon from the full path.
 *
 * The Symbols VS Code icon set covers extensions plus well-known filenames
 * such as package.json, Dockerfile, vite.config.*, and README.*. It is loaded
 * as a separate chunk so the icon catalog does not inflate initial bundles.
 */
export default function FileTypeIcon({ path, className = "" }: FileTypeIconProps) {
  const fileName = basename(path);
  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center ${className}`.trim()}
      aria-hidden="true"
      data-file-icon={fileName}
    >
      <Suspense fallback={<GenericFileIcon width={SIZE} height={SIZE} className="text-muted-foreground" />}>
        <SymbolsFileIcon
          fileName={fileName}
          autoAssign
          width={SIZE}
          height={SIZE}
        />
      </Suspense>
    </span>
  );
}
