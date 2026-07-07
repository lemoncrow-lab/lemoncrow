from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.infra.code_intel.cross_lang.runner import CrossLangRunner


def _write_cross_lang_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "native").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "worker.py").write_text(
        "def plugin_entry() -> str:\n    return 'worker'\n",
        encoding="utf-8",
    )
    (root / "scripts" / "worker.py").write_text(
        "def main() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (root / "src" / "local_worker.py").write_text(
        "from scripts.worker import main\n\ndef call_local() -> int:\n    return main()\n",
        encoding="utf-8",
    )
    (root / "native" / "worker.c").write_text(
        "int foo_compute(int value) {\n    return value;\n}\n",
        encoding="utf-8",
    )
    (root / "src" / "ffi_user.py").write_text(
        "import cffi\n"
        "import ctypes\n\n"
        "def call_native(value: int) -> int:\n"
        "    lib = ctypes.CDLL('libworker.so'); return lib.foo_compute(value)\n\n"
        "def soft_native() -> str:\n"
        "    ffi = cffi.FFI()\n"
        "    ffi.cdef('int soft_missing(int value);')\n"
        "    return 'soft'\n",
        encoding="utf-8",
    )
    (root / "src" / "bootstrap.py").write_text(
        "import importlib\n"
        "import subprocess\n\n"
        "def load_plugin() -> object:\n"
        "    return importlib.import_module('plugins.worker')\n\n"
        "def load_dynamic(name: str) -> object:\n"
        "    return importlib.import_module(name)\n\n"
        "def launch_worker() -> None:\n"
        "    subprocess.run(['python', 'scripts/worker.py'], check=False)\n\n"
        "def launch_dynamic(script: str) -> None:\n"
        "    subprocess.run(['python', script], check=False)\n",
        encoding="utf-8",
    )


def test_literal_ctypes_and_cffi_resolvers_emit_confidence_tagged_edges(tmp_path: Path) -> None:
    _write_cross_lang_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    runner = CrossLangRunner(repo_root=tmp_path, repo_id=engine.repo_id, connection_factory=engine.connection)

    edges = runner.resolve_all()
    ffi_edges = [edge for edge in edges if edge.edge_kind in {"ffi_ctypes", "ffi_cffi"}]

    assert any(edge.tgt_symbol_name == "foo_compute" and edge.tgt_symbol_id for edge in ffi_edges)
    assert any(edge.tgt_symbol_name == "soft_missing" and edge.tgt_symbol_id is None for edge in ffi_edges)
    assert any(edge.confidence >= 0.8 for edge in ffi_edges)
    assert any(edge.confidence < 0.6 for edge in ffi_edges)


def test_literal_import_module_and_subprocess_resolvers_ignore_nonliteral_cases(tmp_path: Path) -> None:
    _write_cross_lang_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    runner = CrossLangRunner(repo_root=tmp_path, repo_id=engine.repo_id, connection_factory=engine.connection)

    edges = runner.resolve_all()

    assert any(edge.edge_kind == "dynamic_import" and edge.tgt_symbol_name == "plugins.worker" for edge in edges)
    assert any(edge.edge_kind == "subprocess" and edge.tgt_symbol_name == "main" for edge in edges)
    assert all(edge.src_symbol_name != "load_dynamic" for edge in edges)
    assert all(edge.src_symbol_name != "launch_dynamic" for edge in edges)
