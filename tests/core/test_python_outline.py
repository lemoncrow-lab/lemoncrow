from __future__ import annotations

from atelier.core.capabilities.semantic_file_memory.python_ast import outline


def test_python_outline_extracts_symbols_imports_and_ranges() -> None:
    source = """
import os
from pathlib import Path


def top_level(x: int) -> int:
    return x + 1


class Worker:
    def run(self) -> None:
        pass
""".strip()

    out = outline("sample.py", source)

    assert out.path == "sample.py"
    assert out.lang == "python"
    assert "os" in out.imports
    assert "pathlib" in out.imports

    names = [s.name for s in out.symbols]
    assert "top_level" in names
    assert "Worker" in names
    assert "Worker.run" in names

    run_symbol = next(s for s in out.symbols if s.name == "Worker.run")
    assert run_symbol.kind == "method"
    assert run_symbol.end_line >= run_symbol.start_line
