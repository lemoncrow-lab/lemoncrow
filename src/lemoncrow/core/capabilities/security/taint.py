"""Bounded intra-procedural Python taint check (G11, first iteration).

This is a deliberately small, HEURISTIC source->sink dataflow analysis over a
single Python module using the stdlib ``ast``. It is NOT inter-procedural and
NOT a full taint engine:

* Sources -- request/argv/env/input-style expressions and parameters.
* Sinks   -- eval/exec, subprocess.*, os.system, and DB ``.execute`` calls.
* Propagation -- within one function body only, across direct assignment,
  string concatenation, f-strings, and ``.format``/``%`` building. Taint does
  NOT cross function boundaries.

Every finding is tagged ``heuristic=True`` and carries a confidence band so the
caller never mistakes this for sound analysis.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

Confidence = Literal["high", "medium", "low"]

# Attribute chains whose access yields tainted data (web/CLI/env input).
_SOURCE_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "request.args",
        "request.form",
        "request.values",
        "request.json",
        "request.data",
        "request.cookies",
        "request.headers",
        "request.params",
        "request.GET",
        "request.POST",
        "sys.argv",
        "os.environ",
    }
)
# Bare calls that return tainted data, keyed by the callable's dotted name.
_SOURCE_CALLS: frozenset[str] = frozenset(
    {
        "input",
        "os.getenv",
        "os.environ.get",
    }
)
# Parameter-name substrings that mark a function parameter as externally tainted.
_SOURCE_PARAM_HINTS: tuple[str, ...] = (
    "request",
    "user_input",
    "untrusted",
    "payload",
    "raw_input",
)


@dataclass(frozen=True)
class _Sink:
    rule_id: str
    cwe: str
    message: str


# Dotted callee name -> sink descriptor. Method sinks (``.execute``) are matched
# on attribute name only (any object).
_FUNCTION_SINKS: dict[str, _Sink] = {
    "eval": _Sink("taint-eval", "CWE-95", "tainted input reaches eval()"),
    "exec": _Sink("taint-exec", "CWE-95", "tainted input reaches exec()"),
    "os.system": _Sink("taint-os-system", "CWE-78", "tainted input reaches os.system()"),
    "subprocess.run": _Sink("taint-subprocess", "CWE-78", "tainted input reaches subprocess.run()"),
    "subprocess.call": _Sink("taint-subprocess", "CWE-78", "tainted input reaches subprocess.call()"),
    "subprocess.Popen": _Sink("taint-subprocess", "CWE-78", "tainted input reaches subprocess.Popen()"),
    "subprocess.check_output": _Sink("taint-subprocess", "CWE-78", "tainted input reaches subprocess.check_output()"),
    "subprocess.check_call": _Sink("taint-subprocess", "CWE-78", "tainted input reaches subprocess.check_call()"),
}
_METHOD_SINKS: dict[str, _Sink] = {
    "execute": _Sink("taint-sql-execute", "CWE-89", "tainted input reaches a SQL execute() call"),
    "executemany": _Sink("taint-sql-execute", "CWE-89", "tainted input reaches a SQL execute() call"),
}


@dataclass(frozen=True)
class TaintFinding:
    """A heuristic source->sink taint result."""

    rule_id: str
    cwe: str
    severity: str
    confidence: Confidence
    file_path: str
    line: int
    column: int
    message: str
    source_name: str
    function: str
    heuristic: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "cwe": self.cwe,
            "severity": self.severity,
            "confidence": self.confidence,
            "file_path": self.file_path,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "source_name": self.source_name,
            "function": self.function,
            "heuristic": self.heuristic,
        }


def _dotted_name(node: ast.AST) -> str | None:
    """Render a dotted attribute/name chain (``a.b.c``) or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _expr_is_source(node: ast.AST) -> bool:
    """True if the expression directly reads a known taint source."""
    if isinstance(node, ast.Call):
        callee = _dotted_name(node.func)
        if callee is not None and callee in _SOURCE_CALLS:
            return True
        # request.args.get(...), os.environ.get(...) etc.
        if isinstance(node.func, ast.Attribute):
            base = _dotted_name(node.func.value)
            if base is not None and base in _SOURCE_ATTRIBUTES:
                return True
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        target = node.value if isinstance(node, ast.Subscript) else node
        dotted = _dotted_name(target if isinstance(node, ast.Subscript) else node)
        if dotted is not None:
            for source in _SOURCE_ATTRIBUTES:
                if dotted == source or dotted.startswith(source + "."):
                    return True
    return False


class _FunctionTaintVisitor(ast.NodeVisitor):
    """Track tainted names within one function and flag tainted sink calls."""

    def __init__(self, file_path: str, function: str, tainted: set[str]) -> None:
        self.file_path = file_path
        self.function = function
        self.tainted: set[str] = set(tainted)
        self.findings: list[TaintFinding] = []

    # --- taint propagation -------------------------------------------------
    def _expr_tainted(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        if _expr_is_source(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in self.tainted
        if isinstance(node, ast.BinOp):  # concatenation / %-formatting
            return self._expr_tainted(node.left) or self._expr_tainted(node.right)
        if isinstance(node, ast.JoinedStr):  # f-string
            return any(self._expr_tainted(v) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            return self._expr_tainted(node.value)
        if isinstance(node, ast.Call):
            # str.format / str.join etc. propagate taint from their arguments.
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"format", "join"}:
                if self._expr_tainted(node.func.value):
                    return True
            return any(self._expr_tainted(arg) for arg in node.args)
        if isinstance(node, (ast.Subscript, ast.Attribute)):
            # Both Subscript and Attribute carry the base expression on `.value`;
            # taint propagates from the base (e.g. tainted[0], tainted.attr).
            return self._expr_tainted(node.value)
        return False

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._expr_tainted(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.tainted.add(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and self._expr_tainted(node.value) and isinstance(node.target, ast.Name):
            self.tainted.add(node.target.id)
        self.generic_visit(node)

    # --- sink detection ----------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        sink: _Sink | None = None
        callee = _dotted_name(node.func)
        if callee is not None and callee in _FUNCTION_SINKS:
            sink = _FUNCTION_SINKS[callee]
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _METHOD_SINKS:
            sink = _METHOD_SINKS[node.func.attr]
        if sink is not None and any(self._expr_tainted(arg) for arg in node.args):
            self.findings.append(
                TaintFinding(
                    rule_id=sink.rule_id,
                    cwe=sink.cwe,
                    severity="error",
                    confidence="medium",
                    file_path=self.file_path,
                    line=node.lineno,
                    column=node.col_offset,
                    message=sink.message,
                    source_name=self.function,
                    function=self.function,
                )
            )
        self.generic_visit(node)

    # Do not descend into nested function definitions: each is analyzed with
    # its own parameter-derived taint set by the module-level walker.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None


def _tainted_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    tainted: set[str] = set()
    args = func.args
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        all_args.append(args.vararg)
    if args.kwarg is not None:
        all_args.append(args.kwarg)
    for arg in all_args:
        lowered = arg.arg.lower()
        if any(hint in lowered for hint in _SOURCE_PARAM_HINTS):
            tainted.add(arg.arg)
    return tainted


def analyze_python_source(source: str, *, file_path: str) -> list[TaintFinding]:
    """Run the bounded intra-procedural taint check on one Python module.

    Returns an empty list (never raises) when the source cannot be parsed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return []

    findings: list[TaintFinding] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted = _tainted_params(func)
        visitor = _FunctionTaintVisitor(file_path, func.name, tainted)
        for stmt in func.body:
            visitor.visit(stmt)
        findings.extend(visitor.findings)
    return findings


__all__ = [
    "Confidence",
    "TaintFinding",
    "analyze_python_source",
]
