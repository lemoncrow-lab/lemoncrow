from __future__ import annotations

from atelier.core.capabilities.semantic_file_memory.typescript_ast import outline


def test_typescript_outline_extracts_class_function_method_and_arrow() -> None:
    source = """
import { z } from \"zod\";

export function topLevel(x: number): number {
  return x + 1;
}

export class Worker {
  run(): void {
    return;
  }
}

const localArrow = () => 1;
export const exportedArrow = () => 2;
""".strip()

    out = outline("sample.ts", source, lang="typescript")

    assert out.path == "sample.ts"
    assert out.lang == "typescript"
    assert "zod" in out.imports

    symbols = {(s.name, s.kind) for s in out.symbols}
    assert ("topLevel", "function") in symbols
    assert ("Worker", "class") in symbols
    assert ("Worker.run", "method") in symbols
    assert ("localArrow", "function") in symbols
    assert ("exportedArrow", "function") in symbols
