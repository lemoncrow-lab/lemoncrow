import { useState, useMemo } from "react";
import { ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import { type FileEditRecord } from "../../api";
import { cx } from "../../components/WorkbenchUI";

// ---------------------------------------------------------------------------
// Shared row type for side-by-side rendering
// ---------------------------------------------------------------------------

type SBSRow = {
  left: { num: number; content: string; type: "equal" | "remove" } | null;
  right: { num: number; content: string; type: "equal" | "insert" } | null;
};

// ---------------------------------------------------------------------------
// Parse unified diff (--- a/, +++ b/, @@ hunks) → SBSRow[]
// Consecutive remove+insert blocks are zipped onto the same row (GitHub style)
// ---------------------------------------------------------------------------

function parseSideBySideFromUnifiedDiff(diff: string): SBSRow[] {
  const result: SBSRow[] = [];
  const lines = diff.split("\n");
  let oldNum = 0;
  let newNum = 0;
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Skip file headers
    if (line.startsWith("---") || line.startsWith("+++")) {
      i++;
      continue;
    }

    // Hunk header: @@ -old_start[,count] +new_start[,count] @@
    if (line.startsWith("@@")) {
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        oldNum = parseInt(m[1]) - 1;
        newNum = parseInt(m[2]) - 1;
      }
      i++;
      continue;
    }

    if (line.startsWith("-") || line.startsWith("+")) {
      // Collect contiguous change block: removes then inserts
      const removes: Array<{ num: number; content: string }> = [];
      const inserts: Array<{ num: number; content: string }> = [];
      while (i < lines.length && lines[i].startsWith("-")) {
        oldNum++;
        removes.push({ num: oldNum, content: lines[i].slice(1) });
        i++;
      }
      while (i < lines.length && lines[i].startsWith("+")) {
        newNum++;
        inserts.push({ num: newNum, content: lines[i].slice(1) });
        i++;
      }
      const maxLen = Math.max(removes.length, inserts.length);
      for (let r = 0; r < maxLen; r++) {
        result.push({
          left: removes[r]
            ? { num: removes[r].num, content: removes[r].content, type: "remove" }
            : null,
          right: inserts[r]
            ? { num: inserts[r].num, content: inserts[r].content, type: "insert" }
            : null,
        });
      }
    } else if (line.startsWith(" ")) {
      // Context line — shown on both sides
      oldNum++;
      newNum++;
      result.push({
        left: { num: oldNum, content: line.slice(1), type: "equal" },
        right: { num: newNum, content: line.slice(1), type: "equal" },
      });
      i++;
    } else {
      i++;
    }
  }

  return result;
}

// ---------------------------------------------------------------------------
// LCS diff engine — computes SBSRow[] from raw old/new strings
// ---------------------------------------------------------------------------

type LineDiffOp =
  | { op: "equal"; oldLine: string; newLine: string; oldNum: number; newNum: number }
  | { op: "remove"; oldLine: string; oldNum: number }
  | { op: "insert"; newLine: string; newNum: number };

function computeLineDiff(oldStr: string, newStr: string): LineDiffOp[] {
  const oldLines = oldStr ? oldStr.split("\n") : [];
  const newLines = newStr ? newStr.split("\n") : [];
  const m = oldLines.length;
  const n = newLines.length;
  if (m === 0 && n === 0) return [];

  // Guard: skip O(n*m) LCS for very large inputs
  if (m > 1500 || n > 1500) {
    return [
      ...oldLines.map((line, i) => ({ op: "remove" as const, oldLine: line, oldNum: i + 1 })),
      ...newLines.map((line, j) => ({ op: "insert" as const, newLine: line, newNum: j + 1 })),
    ];
  }

  const dp: Uint32Array[] = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] =
        oldLines[i - 1] === newLines[j - 1]
          ? dp[i - 1][j - 1] + 1
          : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }

  const ops: LineDiffOp[] = [];
  let i = m;
  let j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      ops.push({ op: "equal", oldLine: oldLines[i - 1], newLine: newLines[j - 1], oldNum: i, newNum: j });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.push({ op: "insert", newLine: newLines[j - 1], newNum: j });
      j--;
    } else {
      ops.push({ op: "remove", oldLine: oldLines[i - 1], oldNum: i });
      i--;
    }
  }
  return ops.reverse();
}

function lcsOpsToRows(ops: LineDiffOp[]): SBSRow[] {
  const result: SBSRow[] = [];
  let k = 0;
  while (k < ops.length) {
    const op = ops[k];
    if (op.op === "equal") {
      result.push({
        left: { num: op.oldNum, content: op.oldLine, type: "equal" },
        right: { num: op.newNum, content: op.newLine, type: "equal" },
      });
      k++;
    } else {
      const removes: Array<{ num: number; content: string }> = [];
      const inserts: Array<{ num: number; content: string }> = [];
      while (k < ops.length && ops[k].op === "remove") {
        const o = ops[k] as { op: "remove"; oldLine: string; oldNum: number };
        removes.push({ num: o.oldNum, content: o.oldLine });
        k++;
      }
      while (k < ops.length && ops[k].op === "insert") {
        const o = ops[k] as { op: "insert"; newLine: string; newNum: number };
        inserts.push({ num: o.newNum, content: o.newLine });
        k++;
      }
      const maxLen = Math.max(removes.length, inserts.length);
      for (let r = 0; r < maxLen; r++) {
        result.push({
          left: removes[r] ? { num: removes[r].num, content: removes[r].content, type: "remove" } : null,
          right: inserts[r] ? { num: inserts[r].num, content: inserts[r].content, type: "insert" } : null,
        });
      }
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// JSON edit payload parser — { filePath, oldString, newString }
// ---------------------------------------------------------------------------

function tryParseJsonEditPayload(content: string): {
  path: string;
  oldString: string;
  newString: string;
} | null {
  const trimmed = content.trim();
  if (!trimmed.startsWith("{")) return null;
  try {
    const obj = JSON.parse(trimmed);
    const path = obj.filePath || obj.file_path || obj.path || "";
    if (path && (obj.oldString !== undefined || obj.newString !== undefined)) {
      return {
        path,
        oldString: String(obj.oldString ?? ""),
        newString: String(obj.newString ?? ""),
      };
    }
  } catch {
    // not JSON
  }
  return null;
}

// ---------------------------------------------------------------------------
// File-edit detection
// ---------------------------------------------------------------------------

export type FileEditInfo = {
  path: string;
  diff: string | null;
  oldString?: string;
  newString?: string;
};

export function getFileEditInfo(turn: any): FileEditInfo | null {
  const kind = String(turn.kind || "").toLowerCase();

  // 0. JSON edit payload: { filePath, oldString, newString }
  if (turn.content) {
    const jsonEdit = tryParseJsonEditPayload(String(turn.content));
    if (jsonEdit) {
      return { path: jsonEdit.path, diff: null, oldString: jsonEdit.oldString, newString: jsonEdit.newString };
    }
  }

  // 1. Top-level keys from session parser
  if (kind === "file_edit") {
    const path: string = turn.path || "";
    const diff: string | null = turn.diff ?? null;
    const summaryPath =
      !path && turn.summary
        ? (String(turn.summary).match(/\(([^)]+)\)$/) || [])[1] || ""
        : path;
    return {
      path: summaryPath,
      diff: diff || (turn.content?.includes("@@") ? turn.content : turn.content || null),
    };
  }

  // 2. Payload-level keys (atelier ledger events)
  const payload = turn.raw?.payload || {};
  const payloadPath: string =
    payload.path || payload.file_path || payload.filename || payload.file || "";
  const payloadDiff: string | null = payload.diff ?? null;

  if (payloadDiff && payloadPath) return { path: payloadPath, diff: payloadDiff };

  // 3. Edit-like kind strings
  const FILE_EDIT_KINDS = ["edit_file", "file_write", "write_file", "file_patch", "str_replace", "file_create", "create_file"];
  if (FILE_EDIT_KINDS.some((k) => kind.includes(k))) {
    return { path: payloadPath, diff: payloadDiff || (turn.content?.includes("@@") ? turn.content : null) };
  }

  return null;
}

// ---------------------------------------------------------------------------
// SideBySideBody — shared renderer for both unified-diff and LCS-diff rows
// ---------------------------------------------------------------------------

function SideBySideBody({ rows }: { rows: SBSRow[] }) {
  return (
    <div className="flex font-mono text-[10px]">
      {/* Left: old */}
      <div className="w-1/2 border-r border-neutral-800/30 min-w-0">
        {rows.map((row, idx) =>
          row.left ? (
            <div
              key={idx}
              className={cx(
                "flex items-start",
                row.left.type === "remove" ? "bg-red-950/25 border-l-2 border-l-red-600/50" : ""
              )}
            >
              <span className="w-9 flex-shrink-0 text-right pr-2 py-0.5 text-neutral-700 select-none text-[9px] leading-5 border-r border-neutral-800/20">
                {row.left.num}
              </span>
              <span
                className={cx(
                  "flex-1 py-0.5 pl-2 whitespace-pre-wrap break-all leading-5 min-h-[1.4em]",
                  row.left.type === "remove" ? "text-red-300/80" : "text-neutral-400"
                )}
              >
                {row.left.type === "remove" && (
                  <span className="text-red-500/40 select-none mr-1">−</span>
                )}
                {row.left.content}
              </span>
            </div>
          ) : (
            <div key={idx} className="min-h-[1.4em] bg-neutral-900/25" />
          )
        )}
      </div>

      {/* Right: new */}
      <div className="w-1/2 min-w-0">
        {rows.map((row, idx) =>
          row.right ? (
            <div
              key={idx}
              className={cx(
                "flex items-start",
                row.right.type === "insert" ? "bg-emerald-950/25 border-l-2 border-l-emerald-600/50" : ""
              )}
            >
              <span className="w-9 flex-shrink-0 text-right pr-2 py-0.5 text-neutral-700 select-none text-[9px] leading-5 border-r border-neutral-800/20">
                {row.right.num}
              </span>
              <span
                className={cx(
                  "flex-1 py-0.5 pl-2 whitespace-pre-wrap break-all leading-5 min-h-[1.4em]",
                  row.right.type === "insert" ? "text-emerald-300/80" : "text-neutral-400"
                )}
              >
                {row.right.type === "insert" && (
                  <span className="text-emerald-500/40 select-none mr-1">+</span>
                )}
                {row.right.content}
              </span>
            </div>
          ) : (
            <div key={idx} className="min-h-[1.4em] bg-neutral-900/25" />
          )
        )}
      </div>
    </div>
  );
}

// Sticky column labels used by both diff surfaces
function SBSLabels() {
  return (
    <div className="flex sticky top-0 z-10 bg-[#080d08] border-b border-neutral-800/40 font-mono text-[8px] uppercase tracking-widest text-neutral-600">
      <div className="w-1/2 px-3 py-1.5 border-r border-neutral-800/40">Before</div>
      <div className="w-1/2 px-3 py-1.5">After</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SideBySideDiff — for { oldString, newString } payloads (LCS-based)
// ---------------------------------------------------------------------------

export function SideBySideDiff({
  path,
  oldString,
  newString,
  forceExpand,
}: {
  path: string;
  oldString: string;
  newString: string;
  forceExpand: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const isExpanded = forceExpand || expanded;

  const rows = useMemo(
    () => lcsOpsToRows(computeLineDiff(oldString, newString)),
    [oldString, newString]
  );

  const addCount = rows.filter((r) => r.right?.type === "insert").length;
  const delCount = rows.filter((r) => r.left?.type === "remove").length;

  return (
    <DiffShell
      path={path}
      addCount={addCount}
      delCount={delCount}
      forceExpand={forceExpand}
      expanded={expanded}
      onToggle={() => setExpanded(!expanded)}
    >
      {isExpanded && (
        <div className="border-t border-emerald-900/20 overflow-auto max-h-[520px]">
          <SBSLabels />
          <SideBySideBody rows={rows} />
        </div>
      )}
    </DiffShell>
  );
}

// ---------------------------------------------------------------------------
// InlineFileDiff — for unified git diffs (--- a/, +++ b/, @@ hunks)
// ---------------------------------------------------------------------------

export function InlineFileDiff({
  path,
  diff,
  forceExpand,
}: {
  path: string;
  diff: string | null;
  forceExpand: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const isExpanded = forceExpand || expanded;

  const rows = useMemo(
    () => (diff ? parseSideBySideFromUnifiedDiff(diff) : []),
    [diff]
  );

  const addCount = rows.filter((r) => r.right?.type === "insert").length;
  const delCount = rows.filter((r) => r.left?.type === "remove").length;

  return (
    <DiffShell
      path={path}
      addCount={addCount}
      delCount={delCount}
      forceExpand={forceExpand}
      expanded={expanded}
      onToggle={() => setExpanded(!expanded)}
    >
      {isExpanded && diff && (
        <div className="border-t border-emerald-900/20 overflow-auto max-h-[520px]">
          <SBSLabels />
          <SideBySideBody rows={rows} />
        </div>
      )}
    </DiffShell>
  );
}

// Shared wrapper shell used by both diff components above
function DiffShell({
  path,
  addCount,
  delCount,
  forceExpand,
  expanded,
  onToggle,
  children,
}: {
  path: string;
  addCount: number;
  delCount: number;
  forceExpand: boolean;
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="w-full border border-emerald-900/25 bg-[#050a05] rounded-sm overflow-hidden shadow-xl">
      <div className="flex items-center justify-between px-4 py-2.5 bg-[#080d08]">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <span className="text-[8px] font-black uppercase tracking-[0.2em] text-emerald-600/80 border border-emerald-800/40 bg-emerald-950/30 px-1.5 py-0.5 flex-shrink-0">
            FILE_EDIT
          </span>
          <span className="text-xs font-mono text-neutral-400 truncate">
            {path || "(unknown path)"}
          </span>
          {(addCount > 0 || delCount > 0) && (
            <div className="flex items-center gap-1.5 text-[9px] font-mono font-black flex-shrink-0">
              {addCount > 0 && <span className="text-emerald-600">+{addCount}</span>}
              {delCount > 0 && <span className="text-red-600">-{delCount}</span>}
            </div>
          )}
        </div>
        <div className="flex items-center gap-3 ml-4 flex-shrink-0">
          {path && (
            <a
              href={`/api/v1/files/content?path=${encodeURIComponent(path)}`}
              target="_blank"
              rel="noreferrer"
              className="text-[9px] text-neutral-500 hover:text-emerald-500 font-black uppercase tracking-widest transition-colors flex items-center gap-1"
            >
              View <ExternalLink size={10} />
            </a>
          )}
          {!forceExpand && (
            <button
              onClick={onToggle}
              className="text-[9px] text-neutral-500 hover:text-neutral-300 font-black uppercase tracking-widest transition-colors border-l border-neutral-800/60 pl-3 flex items-center gap-1.5"
            >
              {expanded ? (
                <>
                  Hide Diff <ChevronUp size={10} />
                </>
              ) : (
                <>
                  View Diff <ChevronDown size={10} />
                </>
              )}
            </button>
          )}
        </div>
      </div>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FileDetail — unified diff from trace.files_touched
// ---------------------------------------------------------------------------

export function FileDetail({
  file,
  forceExpand,
}: {
  file: string | FileEditRecord;
  forceExpand: boolean;
}) {
  const [internalExpanded, setInternalExpanded] = useState(false);
  const expanded = forceExpand || internalExpanded;
  const isObj = typeof file !== "string";
  const path = isObj ? file.path : file;
  const diff = isObj ? file.diff : null;

  const rows = useMemo(
    () => (diff ? parseSideBySideFromUnifiedDiff(diff) : []),
    [diff]
  );

  const addCount = rows.filter((r) => r.right?.type === "insert").length;
  const delCount = rows.filter((r) => r.left?.type === "remove").length;

  return (
    <div className="border border-neutral-800 bg-[#0d0d0d] rounded-none overflow-hidden group/file">
      <div className="flex items-center justify-between p-4 hover:bg-neutral-800/20 transition-all">
        <button
          onClick={() => setInternalExpanded(!internalExpanded)}
          className="flex-1 flex items-center gap-4 min-w-0 text-left"
        >
          <span className="text-xs font-mono text-neutral-400 group-hover/file:text-white transition-colors tracking-wide truncate">
            {path}
          </span>
          {diff && (addCount > 0 || delCount > 0) && (
            <div className="flex items-center gap-1.5 text-[9px] font-mono font-black flex-shrink-0">
              {addCount > 0 && <span className="text-emerald-600">+{addCount}</span>}
              {delCount > 0 && <span className="text-red-600">-{delCount}</span>}
            </div>
          )}
        </button>
        <div className="flex items-center gap-3 ml-4">
          <a
            href={`/api/v1/files/content?path=${encodeURIComponent(path)}`}
            target="_blank"
            rel="noreferrer"
            className="text-[9px] text-neutral-500 font-black tracking-widest hover:text-emerald-500 transition-colors uppercase flex items-center gap-1"
            title="View raw file content"
          >
            Raw <ExternalLink size={10} />
          </a>
          <button
            onClick={() => setInternalExpanded(!internalExpanded)}
            className="text-[9px] text-neutral-500 font-black tracking-widest group-hover/file:text-neutral-300 transition-colors uppercase pl-2 border-l border-neutral-800 flex items-center gap-1.5"
          >
            {expanded ? (
              <>
                Hide <ChevronUp size={10} />
              </>
            ) : (
              <>
                Diff <ChevronDown size={10} />
              </>
            )}
          </button>
        </div>
      </div>
      {expanded && diff && (
        <div className="border-t border-neutral-800 bg-[#080d08] overflow-auto max-h-[520px] shadow-[inset_0_2px_15px_rgba(0,0,0,0.8)]">
          <SBSLabels />
          <SideBySideBody rows={rows} />
        </div>
      )}
    </div>
  );
}
