/** Bounded, display-only lexical highlighting for frozen Focus lines.
 * No parser claim: unrecognized languages render unchanged text, and semantic
 * evidence still comes only from the server's immutable patch and source.
 */
export type SyntaxKind = "plain" | "keyword" | "string" | "number" | "comment";
export interface SyntaxPiece { text: string; kind: SyntaxKind }

const KEYWORDS = /^(?:as|async|await|break|case|catch|class|const|continue|def|del|do|elif|else|except|export|false|False|finally|fn|for|from|func|function|if|import|in|interface|let|match|new|None|null|package|pass|private|public|raise|return|self|static|struct|super|switch|this|throw|true|True|try|type|undefined|var|while|with|yield)$/;
const TOKENS = /(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\/\/.*$|#.*$|\b[A-Za-z_$][\w$]*\b|\b\d+(?:\.\d+)?\b)/gm;

export function syntaxPieces(line: string, path: string): SyntaxPiece[] {
  const python = /\.pyi?$/.test(path);
  const javascript = /\.(?:m?[jt]sx?|cjs|go|rs|c|cc|cpp|h|hpp)$/.test(path);
  if (!python && !javascript) return [{ text: line, kind: "plain" }];
  const result: SyntaxPiece[] = [];
  let next = 0;
  for (const match of line.matchAll(TOKENS)) {
    const at = match.index ?? 0;
    const token = match[0];
    if (at > next) result.push({ text: line.slice(next, at), kind: "plain" });
    let kind: SyntaxKind = "plain";
    if ((python && token.startsWith("#")) || (javascript && token.startsWith("//"))) kind = "comment";
    else if (token.startsWith("\"") || token.startsWith("'") || token.startsWith("`")) kind = "string";
    else if (/^\d/.test(token)) kind = "number";
    else if (KEYWORDS.test(token)) kind = "keyword";
    result.push({ text: token, kind });
    next = at + token.length;
    if (kind === "comment") break;
  }
  if (next < line.length) result.push({ text: line.slice(next), kind: "plain" });
  return result.length ? result : [{ text: line, kind: "plain" }];
}
