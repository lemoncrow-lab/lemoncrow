// Guard: the Review UI has one vocabulary. A revision is a "Revision N" (counted from 1), the reviewable pieces are
// "parts", and no retired or "never use" word reaches a person: "Snapshot", "target(s)", "changed targets",
// "review areas", "review-first items" (the 1-based revision guard) and the SPEC 2 never-use list (NEVER_USE).
// Identifiers, comments and API field names (snapshot_number, "snapshot" enum values) are not user text.
//
// The scan reads the TypeScript AST and judges every string that could reach a screen: JSX text, string literals
// and template literals (judged on their joined text). It fails closed: a string with a space or a capital is copy,
// a one-token lowercase string is copy only in a copy position, and exclusions cover only code positions.
//
// Two layers. New flow surfaces are held to zero. Existing review copy is ratcheted by userVocabulary.baseline.ts:
// a file may not gain hits of a phrase, and a baseline entry must shrink as its copy is rewritten.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { describe, expect, it } from "vitest";

import { VOCABULARY_BASELINE, type VocabularyBaselineEntry } from "./userVocabulary.baseline";

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOTS = [path.resolve(here, ".."), path.resolve(here, "../../../enterprise/frontend/src")];
const REPO_ROOT = path.resolve(here, "../../..");

interface NeverUse {
  term: string;
  /** Matched as the entire string instead of a word or phrase, ignoring case and trailing punctuation. */
  whole?: boolean;
  /** The replacement, shown in the failure message. */
  use: string;
}

/** Every "Never use" word or phrase from SPEC section 2. */
const NEVER_USE: readonly NeverUse[] = [
  { term: "measured", use: '"Checked in code" (section header: "Checked in the code")' },
  { term: "pointer", use: '"AI hints"' },
  { term: "pointers", use: '"AI hints"' },
  { term: "grounded in", use: '"based on <file lines>"' },
  { term: "inferred", use: '"Not verified · matched by name only"' },
  { term: "best guess", use: '"Not verified · matched by name only"' },
  { term: "route table", use: '"checked in code · routes"' },
  { term: "boundaries crossed", use: '"0 protected areas entered"' },
  { term: "weight", use: '"Importance"' },
  { term: "weights", use: '"Importance"' },
  { term: "Signed by your click", use: '"Set by you · Must fix"' },
  { term: "STRUCTURE SHOWN", use: '"What you were shown"' },
  { term: "Not sure", use: '"Decide later"' },
  { term: "deferred", use: '"Decide later" or "You left this for later"' },
  { term: "Confirm fixed", use: '"Looks fixed"' },
  { term: "Not yet", use: '"Still needs a change" (only "Plan approved by you? Not yet" is allowed)' },
  { term: "Reach", whole: true, use: '"Who can reach this" (as a label; prose "reach" is fine)' },
  { term: "Lower risk", use: '"Quick check"' },
  { term: "Skim", use: '"Quick check"' },
  { term: "skimmed", use: '"quick-checked"' },
  { term: "glanced", use: '"quick-checked"' },
  { term: "Something else is wrong", use: '"Needs a change"' },
  { term: "Flag something else", use: '"Needs a change"' },
  // Not scanned: the section 2 rule depends on position. "Show" is banned only on the untrusted strip ("Show hidden text"),
  // and "Ask" only on a card (the top bar's "Ask" is allowed). A string scan cannot tell these apart.
  { term: "Guided", use: '"One at a time"' },
  { term: "Full diff", use: '"All files"' },
  { term: "Classic", use: "none in the new flow" },
  { term: "in-toto", use: '"a signed file (in-toto)" (Details only)' },
  { term: "git note", use: '"a git note" (Details only)' },
  { term: "git notes", use: '"a git note" (Details only)' },
  { term: "AI would pick", use: '"AI suggests: Must fix · Use"' },
  { term: "unresolved", use: '"Not verified · couldn\'t trace"' },
  { term: "[author]", use: '"Dana Kim (GitHub @dkim)" or a real name' },
];

function escapeRe(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface Rule {
  term: string;
  use: string;
  re: RegExp;
  /** Only in multi-word prose (the lowercase "snapshot" rule). */
  needsSpace?: boolean;
}

/**
 * A word or phrase, matched wherever it stands alone: "non-measured" and "checked/measured" count, and so do the
 * prefixed forms "unmeasured", "nonmeasured" and "mismeasured" of a single never-use word, while "measured_by" does not.
 */
const wordRe = (term: string) => new RegExp(`(?<![\\w$])${/^\w+$/.test(term) ? "(?:un|non|mis)?" : ""}${escapeRe(term)}(?!\\w)`, "i");

/** The 1-based revision guard, kept from the base branch ("Snapshot", "target(s)", "changed targets", "review areas"). */
const RETIRED: readonly Rule[] = [
  { term: "Snapshot", use: '"Revision N" via revisionName(), or "revision"', re: /(?<![_\w$/.])Snapshots?(?![_\w(])/ },
  { term: "target(s)", use: '"part"/"parts" with a plural helper', re: /target\(s\)/i },
  { term: "changed targets", use: '"parts"', re: /\bchanged targets?\b/i },
  { term: "review areas", use: '"parts"; "area" is only for folder groupings', re: /\breview areas?\b/i },
  { term: "review-first items", use: '"parts to review first"', re: /\breview-first items\b/i },
  // Lowercase "snapshot" is prose only when the string has several words and the word stands alone.
  { term: "snapshot", use: '"revision"', re: /(?<![-_\w$/.])snapshots?(?![-_\w(])/, needsSpace: true },
];

/** Retired product names: the products are Workspaces Lite and Workspaces Pro on every surface. `runtime` stays a code key. */
const RETIRED_PRODUCT_NAMES: readonly Rule[] = [
  { term: "LemonCrow Runtime", use: '"Workspaces Lite" or "Workspaces Pro"', re: /\bLemonCrow Runtime\b/i },
  { term: "Runtime Lite", use: '"Workspaces Lite"', re: /\bRuntime Lite\b/i },
  { term: "Runtime Pro", use: '"Workspaces Pro"', re: /\bRuntime Pro\b/i },
  { term: "Hosted Runtime", use: '"Workspaces Pro"', re: /\bHosted Runtime\b/i },
  { term: "LemonCrow Dashboard", use: '"Workspaces Lite"', re: /\bLemonCrow Dashboard\b/i },
  { term: "Workspaces Cloud", use: "nothing: Workspaces Cloud is not offered", re: /\bWorkspaces Cloud\b/i },
];

const PATTERNS: readonly Rule[] = [
  ...NEVER_USE.filter((b) => !b.whole).map((b) => ({ term: b.term, use: b.use, re: wordRe(b.term) })),
  ...RETIRED,
  ...RETIRED_PRODUCT_NAMES,
];

/** The P0-1 rule: decorative "@@" in titles, eyebrows and empty states. A real diff hunk header ("@@ -1,3 +1,4 @@") is fine. */
const DECORATIVE_AT = { term: "@@", use: "no decorative @@ (P0-1); titles, eyebrows and empty states never carry it" };

/** Cheap pre-filter: a file with none of these cannot have a violation. */
const TRIGGER = new RegExp([...NEVER_USE.map((b) => escapeRe(b.term)), ...RETIRED.map((p) => escapeRe(p.term)), ...RETIRED_PRODUCT_NAMES.map((p) => escapeRe(p.term)), "target", "changed", "review", "@@"].join("|"), "i");

/** A one-token lowercase code: a key, an enum value, a storage key or a path. */
const CODE_TOKEN = /^[a-z][a-z0-9_.:\/-]*$/;

/** Attributes a person reads, so a one-token string there is copy. */
const USER_ATTRIBUTE = /^(aria-label|title|placeholder|alt|label)$/;

/** setAttribute names whose value a person reads. */
const USER_SET_ATTRIBUTE = /^(aria-label|title|placeholder|alt)$/;

/** Attributes whose value is never screen text, wherever they stand (a direct attribute value only). */
const DIRECT_NON_TEXT = /^(data-(?!tip$|tooltip$|title$).*|testid|key|id|type|as|target|className|class|style|role|href|to|src|srcSet|htmlFor|method|autoComplete|inputMode|pattern|accept|tabIndex|ref|rel|d|viewBox|fill|stroke|transform|points|xmlns|width|height)$/;

/** Attributes that are code when they sit anywhere under them (a class list, a style object, a key, a test id). */
// data-tip, data-tooltip and data-title are the text a person reads on hover, so they are scanned.
const DEEP_NON_TEXT = /^(className|class|style|key|data-(?!tip$|tooltip$|title$).*|testid)$/;

/** `name` and `value` are form data on built-in tags only; on a component they are copy. */
const FORM_ATTRIBUTE = /^(name|value)$/;
const BUILTIN_TAG = /^(option|input|select|textarea|button|param)$/;

/** Style property names: a string under one of these is a CSS value, but only in a style object (see cssObject). */
const CSS_PROPERTY = /^(cursor|pointerEvents|pointer-events|touchAction|touch-action|userSelect|user-select|fontWeight|font-weight|fontFamily|textAlign|textDecoration|textTransform|whiteSpace|display|position|color|background|backgroundColor|background-color|borderColor|border|outline|boxShadow|transition|animation|transform|overflow|zIndex|gridTemplateColumns|justifyContent|alignItems)$/;

/** Object keys whose string value is copy a person reads. */
const COPY_KEY = /^(label|title|text|message|description|status|aside|hint|caption|content)$/;

/** Calls whose string argument is copy a person reads. */
const COPY_CALL = new Set(["toast", "notify"]);

/** A variable, property or export whose name says it holds copy: its string values are copy, through as const or satisfies. */
const COPY_NAME = /label|text|copy|title|word|message|caption|hint/i;

/** Class and style builders: a string anywhere inside is code. */
const DEEP_CODE_CALL = new Set(["cn", "clsx", "cva", "twMerge", "classNames", "twJoin", "cx"]);

/** Code calls whose own direct arguments are code (a comparison or an API name), not what sits deeper inside them. */
const DIRECT_CODE_CALL = new Set(["includes", "has", "startsWith", "endsWith", "indexOf", "test", "match", "querySelector", "querySelectorAll", "getElementById", "closest", "matches", "hasAttribute", "getAttribute", "addEventListener", "removeEventListener", "dispatchEvent"]);

/** classList methods: code only when the receiver is a classList. */
const CLASS_LIST_CALL = new Set(["add", "remove", "toggle", "contains"]);

/** A declaration or key whose name marks a class, style or variant table: its class-list strings are code. */
const CLASS_BEARING_NAME = /class|style|variant|item|menu|tone|size/i;

/** A Tailwind class token: lowercase, possibly with variants (`pointer-coarse:min-w-11`, `data-[state=open]:x`). */
const CLASS_TOKEN = /^[!a-z0-9[\]&@:()_\/.%#=,-]+$/;
const VARIANT_TOKEN = /^[a-z][\w[\]\/=-]*:[!a-z0-9[&@(][^\s]*$/;
/** A CSS rule block inside a string (a stylesheet in a template literal). */
const CSS_BLOCK = /\{[^{}]*:[^{}]*;/;

const EQUALITY = new Set([ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken, ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken]);

export interface Violation {
  /** Repo-relative path, or the fixture name in a test. */
  file: string;
  line: number;
  value: string;
  /** The banned term that matched. */
  term: string;
  /** The replacement. */
  use: string;
}

function ancestorMatches(node: ts.Node, test: (n: ts.Node) => boolean, depth: number): boolean {
  let current: ts.Node | undefined = node.parent;
  for (let i = 0; current && i < depth; i += 1, current = current.parent) if (test(current)) return true;
  return false;
}

function calleeName(call: ts.CallExpression): string {
  const callee = call.expression;
  if (ts.isIdentifier(callee)) return callee.text;
  if (ts.isPropertyAccessExpression(callee)) return callee.name.text;
  return "";
}

function isArgument(call: ts.CallExpression, node: ts.Node): boolean {
  return call.arguments.some((arg) => arg === node);
}

/** The value of setAttribute("aria-label", value): a copy argument. */
function isSetAttributeValue(call: ts.CallExpression, node: ts.Node): boolean {
  const [name] = call.arguments;
  return call.arguments[1] === node && Boolean(name && ts.isStringLiteral(name) && USER_SET_ATTRIBUTE.test(name.text));
}

function isDirectCodeCall(call: ts.CallExpression): boolean {
  const name = calleeName(call);
  if (DIRECT_CODE_CALL.has(name)) return true;
  return CLASS_LIST_CALL.has(name) && ts.isPropertyAccessExpression(call.expression) && call.expression.expression.getText().endsWith("classList");
}

/** A style object: inside a style attribute, or typed as CSSProperties. Its keys are CSS, so their values are code. */
function cssObject(obj: ts.Node): boolean {
  if (ancestorMatches(obj, (n) => ts.isJsxAttribute(n) && n.name.getText() === "style", 4)) return true;
  const parent = obj.parent;
  if (parent && ts.isVariableDeclaration(parent) && parent.type !== undefined) return /CSSProperties/.test(parent.type.getText());
  if (parent && ts.isAsExpression(parent)) return /CSSProperties/.test(parent.type.getText());
  return false;
}

/** A JSX attribute on a built-in lowercase tag (`<input name>`), not a component (`<Stat value>`). */
function onBuiltinTag(attr: ts.JsxAttribute): boolean {
  const element = attr.parent?.parent;
  return Boolean(element && (ts.isJsxOpeningElement(element) || ts.isJsxSelfClosingElement(element)) && BUILTIN_TAG.test(element.tagName.getText()));
}

/** A string literal in a position that is code, not copy. JSX text is never excluded here. */
function isNonText(node: ts.Node): boolean {
  const parent = node.parent;
  if (!parent) return false;
  if (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent) || ts.isExternalModuleReference(parent) || ts.isImportTypeNode(parent)) return true;
  if (ts.isTypeNode(parent)) return true;
  if ((ts.isPropertyAssignment(parent) || ts.isPropertySignature(parent) || ts.isMethodDeclaration(parent) || ts.isMethodSignature(parent) || ts.isPropertyDeclaration(parent)) && parent.name === node) return true;
  if (ts.isElementAccessExpression(parent) && parent.argumentExpression === node) return true;
  if (ts.isBinaryExpression(parent) && EQUALITY.has(parent.operatorToken.kind)) return true;
  // `el.style.display = "x"`: a CSS value assigned to a style property.
  if (ts.isBinaryExpression(parent) && parent.operatorToken.kind === ts.SyntaxKind.EqualsToken && /\.style\.\w+$/.test(parent.left.getText())) return true;
  if (ts.isCaseClause(parent)) return true;
  if (ts.isCallExpression(parent)) {
    if (calleeName(parent) === "setAttribute") return !isSetAttributeValue(parent, node);
    if (isDirectCodeCall(parent) && isArgument(parent, node)) return true;
  }
  if (ts.isPropertyAssignment(parent) && CSS_PROPERTY.test(parent.name.getText()) && cssObject(parent.parent)) return true;
  if (ts.isJsxAttribute(parent)) {
    if (DIRECT_NON_TEXT.test(parent.name.getText())) return true;
    if (FORM_ATTRIBUTE.test(parent.name.getText()) && onBuiltinTag(parent)) return true;
  }
  // Code further up: a class or style attribute or key at any reasonable depth; a class builder at any depth.
  if (ancestorMatches(node, (n) => ts.isJsxAttribute(n) && DEEP_NON_TEXT.test(n.name.getText()), 8)) return true;
  if (ancestorMatches(node, (n) => ts.isCallExpression(n) && DEEP_CODE_CALL.has(calleeName(n)), 50)) return true;
  return ancestorMatches(node, (n) => ts.isPropertyAssignment(n) && CSS_PROPERTY.test(n.name.getText()) && cssObject(n.parent), 4);
}

/** A copy position for a one-token lowercase string: a JSX expression (through a ternary, ??, ||, && or parentheses), a component prop, a copy key, a toast or notify argument, a setAttribute value. */
function isCopyPosition(node: ts.Node): boolean {
  let cur: ts.Node = node;
  for (;;) {
    const p: ts.Node | undefined = cur.parent;
    if (!p) break;
    if (ts.isParenthesizedExpression(p)) cur = p;
    else if (ts.isConditionalExpression(p) && cur !== p.condition) cur = p;
    else if (ts.isBinaryExpression(p) && [ts.SyntaxKind.QuestionQuestionToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.AmpersandAmpersandToken].includes(p.operatorToken.kind)) cur = p;
    else break;
  }
  const p = cur.parent;
  if (!p) return false;
  if (ts.isJsxExpression(p) || ts.isJsxAttribute(p)) return true;
  if (ts.isPropertyAssignment(p) && cur === p.initializer && COPY_KEY.test(p.name.getText())) return true;
  if (ts.isCallExpression(p) && COPY_CALL.has(calleeName(p)) && isArgument(p, cur)) return true;
  return ts.isCallExpression(p) && calleeName(p) === "setAttribute" && isSetAttributeValue(p, cur);
}

/** A stylesheet position: a template literal, a string passed to insertRule, or a <style> element's content. */
function isStyleSheetPosition(node: ts.Node): boolean {
  if (ts.isTemplateExpression(node) || ts.isNoSubstitutionTemplateLiteral(node)) return true;
  if (ancestorMatches(node, (n) => ts.isCallExpression(n) && calleeName(n) === "insertRule", 3)) return true;
  return ancestorMatches(node, (n) => ts.isJsxElement(n) && n.openingElement.tagName.getText() === "style", 4);
}

/** Text that is a class list with variants, or a stylesheet. The class-list form applies only in class-bearing positions. */
function isCodeText(trimmed: string, node: ts.Node): boolean {
  if (isStyleSheetPosition(node) && CSS_BLOCK.test(trimmed)) return true;
  const classBearing =
    ancestorMatches(node, (n) => ts.isJsxAttribute(n) && /^(className|class)$/.test(n.name.getText()), 8) ||
    ancestorMatches(node, (n) => (ts.isVariableDeclaration(n) && CLASS_BEARING_NAME.test(n.name.getText())) || (ts.isPropertyAssignment(n) && CLASS_BEARING_NAME.test(n.name.getText())), 6);
  if (!classBearing) return false;
  const tokens = trimmed.split(/\s+/);
  return tokens.some((token) => VARIANT_TOKEN.test(token)) && tokens.every((token) => CLASS_TOKEN.test(token));
}

/**
 * A string value inside an object or array literal whose variable, property or export name says copy: `const X_LABEL = { a: "x" }`.
 * The walk passes through object and array literals, `as`, `satisfies` and parentheses.
 */
function isCopyByName(node: ts.Node): boolean {
  let cur: ts.Node = node;
  const names: string[] = [];
  for (;;) {
    const p: ts.Node | undefined = cur.parent;
    if (!p) return false;
    if (ts.isPropertyAssignment(p) && p.initializer === cur) names.push(p.name.getText());
    else if (ts.isVariableDeclaration(p) && p.initializer === cur) {
      names.push(p.name.getText());
      break;
    } else if (!(ts.isObjectLiteralExpression(p) || ts.isArrayLiteralExpression(p) || ts.isAsExpression(p) || ts.isParenthesizedExpression(p) || p.kind === ts.SyntaxKind.SatisfiesExpression)) return false;
    cur = p;
  }
  return names.some((name) => COPY_NAME.test(name));
}

/** Copy: JSX text, anything with a space or a capital, a one-token string in a copy position, or a non-code one-token string. */
function isUserFacing(node: ts.Node, trimmed: string): boolean {
  if (ts.isJsxText(node)) return true;
  if (/\s/.test(trimmed) || /^[A-Z]/.test(trimmed)) return true;
  if (isCopyPosition(node) || isCopyByName(node)) return true;
  const parent = node.parent;
  if (parent && ts.isJsxAttribute(parent) && USER_ATTRIBUTE.test(parent.name.getText())) return true;
  return !CODE_TOKEN.test(trimmed);
}

/** Scans one file's text. `file` is the name reported in violations (repo-relative for the real tree). */
export function scanSource(file: string, text: string): Violation[] {
  if (!TRIGGER.test(text)) return [];
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const found: Violation[] = [];
  const check = (node: ts.Node, value: string, interpolated = false): void => {
    const trimmed = value.trim();
    if (!trimmed) return;
    const jsxText = ts.isJsxText(node);
    if (!jsxText && isNonText(node)) return;
    if (!jsxText && isCodeText(trimmed, node)) return;
    // A template with a substitution is copy unless it sits in a code position: its pieces are not judged as a code token.
    if (!interpolated && !isUserFacing(node, trimmed)) return;
    const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
    const note = (term: string, use: string) => found.push({ file, line, value: trimmed, term, use });
    // SPEC 2 allows exactly one "Not yet": the plan question on the Overview.
    const judged = value.replace(/Plan approved by you\? Not yet/g, "Plan approved by you?");
    for (const rule of PATTERNS) {
      if (rule.needsSpace && !/\s/.test(trimmed)) continue;
      if (rule.re.test(judged)) note(rule.term, rule.use);
    }
    for (const banned of NEVER_USE) {
      if (banned.whole && trimmed.replace(/[\s.,:;!?…]+$/, "").toLowerCase() === banned.term.toLowerCase()) note(banned.term, banned.use);
    }
    if (trimmed.includes(DECORATIVE_AT.term) && !trimmed.startsWith("@@ -")) note(DECORATIVE_AT.term, DECORATIVE_AT.use);
  };
  const visit = (node: ts.Node): void => {
    if (ts.isTemplateExpression(node)) {
      // Judged on the joined text (head plus every span), so a word split by an interpolation still matches.
      check(node, [node.head.text, ...node.templateSpans.map((span) => span.literal.text)].join(" "), true);
      for (const span of node.templateSpans) visit(span.expression);
      return;
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isJsxText(node)) check(node, node.text);
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(full, out);
    else if (/\.tsx?$/.test(entry.name) && !/\.(test|d|baseline)\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

/**
 * Surfaces outside Review, which keep their own copy. Each names what it renders.
 */
const OTHER_PRODUCT_AREAS = [
  // Analytics, Optimizations and Swarms freeze their own workflow, advisor and usage "snapshots", not Review revisions.
  /\/pages\/(Analytics|Optimizations|Swarms)\.tsx$/,
  /\/pages\/swarms\//,
  // The agent session page: transcript, gateway usage and cost. Nothing on it is Review copy. The other session files
  // (TurnRenderers, helpers, DiffView) render inside the Review's development session, so they are scanned.
  /\/pages\/sessions\/SessionDetail\.tsx$/,
];

/** Every violation in the real tree, keyed by repo-relative path. */
export function scanRepo(): Violation[] {
  return ROOTS.filter((root) => fs.existsSync(root))
    .flatMap((root) => sourceFiles(root))
    .filter((file) => !OTHER_PRODUCT_AREAS.some((pattern) => pattern.test(file)))
    .flatMap((file) => scanSource(path.relative(REPO_ROOT, file).split(path.sep).join("/"), fs.readFileSync(file, "utf8")));
}

/**
 * Ratchet: hits above a file's baseline count fail, and so does a baseline entry with fewer hits than recorded.
 * A file with no entry is held to zero.
 */
export function vocabularyDrift(found: readonly Violation[], baseline: readonly VocabularyBaselineEntry[]): string[] {
  const counts = new Map<string, { file: string; term: string; count: number; first: Violation }>();
  for (const v of found) {
    const key = JSON.stringify([v.file, v.term]);
    const row = counts.get(key) ?? { file: v.file, term: v.term, count: 0, first: v };
    row.count += 1;
    counts.set(key, row);
  }
  const allowed = new Map(baseline.map((entry) => [JSON.stringify([entry.file, entry.term]), entry]));
  const problems: string[] = [];
  for (const [key, row] of counts) {
    const cap = allowed.get(key)?.count ?? 0;
    if (row.count > cap) {
      problems.push(`${row.file}:${row.first.line}: "${row.term}" has ${row.count} hits, baseline allows ${cap}. Use ${row.first.use}.`);
    }
  }
  for (const entry of baseline) {
    const actual = counts.get(JSON.stringify([entry.file, entry.term]))?.count ?? 0;
    if (actual < entry.count) {
      problems.push(`${entry.file}: "${entry.term}" baseline ${entry.count}, now ${actual}. Lower or remove that entry (retiredBy ${entry.retiredBy}).`);
    }
  }
  return problems;
}

describe("user-facing review vocabulary", () => {
  it("holds the Review to SPEC 2: no new never-use hits, and the baseline only shrinks", () => {
    expect(vocabularyDrift(scanRepo(), VOCABULARY_BASELINE)).toEqual([]);
  });

  it("the scanner catches the banned words in user-facing positions", () => {
    const sample = [
      'export const title = <h2>Full diff</h2>;',
      'export const label = "Lower risk";',
      'export const note = <p>You glanced at this part</p>;',
      'export const hint = <p title="Pointer help">help</p>;',
      'export const heading = <h1>Guided</h1>;',
      'export const eyebrow = <p>@@ Summary</p>;',
      'export const label2 = <button aria-label="Show pointer help">x</button>;',
      'export const note2 = <p>Reach</p>;',
    ].join("\n");
    const hits = scanSource("sample.tsx", sample);
    expect(hits).toHaveLength(8);
    const values = hits.map((v) => v.value);
    for (const word of ["Full diff", "Lower risk", "glanced", "Pointer help", "Guided", "@@ Summary", "Show pointer help", "Reach"]) {
      expect(values.some((value) => value.includes(word))).toBe(true);
    }
  });

  it("the scanner allows the one \"Not yet\" SPEC 2 keeps, and flags any other", () => {
    expect(scanSource("sample.tsx", "export const a = <p>Plan approved by you? Not yet</p>;")).toEqual([]);
    expect(scanSource("sample.tsx", "export const a = <p>Not yet</p>;").map((v) => v.term)).toEqual(["Not yet"]);
    expect(scanSource("sample.tsx", "export const a = <p>Plan approved by you? Not yet. Not yet</p>;").map((v) => v.term)).toEqual(["Not yet"]);
  });

  it("the scanner ignores the same words in identifiers, CSS, class names, comments and imports", () => {
    const code = [
      'import { glanced } from "./glanced";',
      "// Full diff and Lower risk are only a comment here",
      "const cursorPointer = 1;",
      'const style = { cursor: "pointer" };',
      'export const cls = <div className="cursor-pointer select-none font-weight" />;',
      'export const kind = "guided";',
      'export const hunk = "@@ -1,3 +1,4 @@";',
      'export const parse = (line: string) => line.startsWith("@@");',
      'export const api = { measured: 1, snapshot_number: 2, inferred: "x" };',
    ].join("\n");
    expect(scanSource("sample.tsx", code)).toEqual([]);
  });

  it("keeps the base branch's revision guard: Snapshot, target(s), changed targets, review areas", () => {
    const body = 'export const a = "Snapshot 1"; export const b = `6 changed targets`; export const c = <p>target(s) left</p>; export const ok = "snapshot_number"; export const kind = "snapshot";';
    expect(scanSource("sample.tsx", body)).toHaveLength(3);
    expect(scanSource("sample.tsx", 'export const r = <p>Review areas</p>;').map((v) => v.term)).toEqual(["review areas"]);
    expect(scanSource("sample.tsx", '<p>Open the snapshot now</p>').map((v) => v.term)).toEqual(["snapshot"]);
  });

  it("fails closed on the probes that slipped through before", () => {
    const mustFlag = [
      'export const pair = ["Guided", "Classic"];',
      'export const item = { label: "Skim" };',
      'export const C = (x: boolean) => { const l = x ? "Guided" : "Classic"; return <p>{l}</p>; };',
      'declare const toast: (s: string) => void; toast("Skimmed");',
      'export const row = { inferred: "Inferred" };',
      'export const f = () => { return "Inferred"; };',
      'export const A = (n: boolean) => <i aria-label={n ? "Guided" : "Skim"} />;',
      "export const B = (n: number) => <i aria-label={`${n} pointers`} />;",
      "export const T = (n: number, m: number) => `${n} glanced ${m}`;",
    ];
    for (const source of mustFlag) expect(scanSource("sample.tsx", source), source).not.toHaveLength(0);
  });

  it("judges one-token lowercase strings in copy positions: JSX expressions, props, copy keys, toast, setAttribute", () => {
    const mustFlag = [
      'export const P = () => <p>{"measured"}</p>;',
      'export const Q = () => <p>{"checked/measured"}</p>;',
      'export const B = (x: boolean, y: boolean) => <Badge>{x ? "inferred" : "measured"}</Badge>;',
      'export const item = { label: "inferred" };',
      'export const s = { status: "glanced" };',
      'declare const toast: (s: string) => void; toast("skimmed");',
      'export const T = () => <Tip content="inferred" />;',
      'export const A = (x: string, y: string) => <i aside={x === "inferred" ? "inferred" : y} />;',
      'export const rows = [{ id: "full", display: "Full diff" }];',
      'el.setAttribute("aria-label", "Not measured");',
      'el.setAttribute("aria-label", "inferred");',
      'export const p = <p>pointer:on</p>;',
      'export const CSS_NOTE = "Not measured {callers: yes;}";',
      'export const sheet2 = "[data-line] { font-weight: 600; }";',
      'export const tip = <div data-tip={"Lower risk parts"} />;',
      'export const EXACTNESS_LABEL = { inferred: "inferred" };',
      'export const STATUS_TEXT: Record<string, string> = { a: "glanced" };',
      'export const WORDS = ["inferred"] as const;',
    ];
    for (const source of mustFlag) expect(scanSource("sample.tsx", source), source).not.toHaveLength(0);
  });

  it("flags a term in a non-form attribute value and in a nested value, and reads a form name or value on a built-in tag as code", () => {
    const mustFlag = [
      'export const V = () => <Stat label="Evidence" value="Not measured" />;',
      'export const R = (x: boolean) => <Row value={x ? "Not measured" : "Checked in code"} />;',
      'export const K = () => <KV value={<span>{"Not measured"}</span>} />;',
    ];
    for (const source of mustFlag) expect(scanSource("sample.tsx", source), source).not.toHaveLength(0);
    const mustPass = ['export const O = () => <option value="Guided">g</option>;', 'export const I = () => <input name="pointer" />;'];
    for (const source of mustPass) expect(scanSource("sample.tsx", source), source).toEqual([]);
  });

  it("does not flag code positions: comparisons, class names, imports, state values, CSS, storage keys, setAttribute names", () => {
    const mustPass = [
      'export const css = { cursor: "pointer" };',
      'export const eq = (kind: string) => kind === "deferred";',
      'export const st = { state: "later" };',
      'export const cls = <div className="pointer-events-none" />;',
      'import x from "./Guided";',
      'export const variant = "h-7 px-2 pointer-coarse:min-w-11 data-[state=open]:opacity-100";',
      'export const sheet = `.x { outline: 1px solid red; }`;',
      'export const key = "lc.review.guided.position." + "x";',
      'export const L = (t: string) => <li key={`not-measured:${t}`} data-testid="not-measured" />;',
      'export const has = ["a"].includes("Guided");',
      'export const STATUS = { inferred: "inferred" };',
      'el.setAttribute("data-x", "Not measured");',
      'el.style.display = "Full diff";',
      'export const sty: React.CSSProperties = { display: "Full diff" };',
    ];
    for (const source of mustPass) expect(scanSource("sample.tsx", source), source).toEqual([]);
  });

  it("matches a term next to - / or . and a git notes phrase, but not inside a longer word", () => {
    expect(scanSource("sample.tsx", '<p>non-measured</p>').map((v) => v.term)).toEqual(["measured"]);
    expect(scanSource("sample.tsx", '<p>checked/measured</p>').map((v) => v.term)).toEqual(["measured"]);
    expect(scanSource("sample.tsx", '<p>Save as git notes</p>').map((v) => v.term)).toEqual(["git notes"]);
    expect(scanSource("sample.tsx", '<p>Unmeasured</p>').map((v) => v.term)).toEqual(["measured"]);
    expect(scanSource("sample.tsx", '<p>nonmeasured</p>').map((v) => v.term)).toEqual(["measured"]);
    expect(scanSource("sample.tsx", 'const k = "measured_by";')).toEqual([]);
  });

  it("reads Reach as a whole label, ignoring case and trailing punctuation", () => {
    expect(scanSource("sample.tsx", '<p>reach.</p>').map((v) => v.term)).toEqual(["Reach"]);
    expect(scanSource("sample.tsx", '<p>Please reach the team</p>')).toEqual([]);
  });

  it("fails a file with one hit over its baseline count", () => {
    const file = "frontend/src/review/Fixture.tsx";
    const found = scanSource(file, "<p>Not measured</p>\n<p>Not measured again</p>");
    expect(vocabularyDrift(found, [{ file, term: "measured", count: 2, retiredBy: "P8-3" }])).toEqual([]);
    const none = vocabularyDrift(found.slice(0, 1), [{ file, term: "measured", count: 0, retiredBy: "P8-3" }]);
    expect(none).toHaveLength(1);
    expect(none[0]).toContain("baseline allows 0");
    const over = vocabularyDrift(found, [{ file, term: "measured", count: 1, retiredBy: "P8-3" }]);
    expect(over).toHaveLength(1);
    expect(over[0]).toContain("has 2 hits, baseline allows 1");
  });

  it("fails a stale baseline entry, so the list only shrinks", () => {
    const file = "frontend/src/review/Fixture.tsx";
    const found = scanSource(file, "<p>Not measured</p>");
    const stale = vocabularyDrift(found, [{ file, term: "measured", count: 2, retiredBy: "P8-3" }]);
    expect(stale).toHaveLength(1);
    expect(stale[0]).toContain("Lower or remove that entry");
  });
});
