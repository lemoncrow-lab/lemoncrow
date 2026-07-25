from pathlib import Path
from typing import Any

from lemoncrow.gateway.adapters.mcp_server import _handle
from lemoncrow.pro.capabilities.prompt_compilation.tokens import count_tokens as _count_tokens
from lemoncrow.pro.capabilities.semantic_file_memory.capability import (
    SemanticFileMemoryCapability,
    claude_read_baseline_text,
)
from tests.helpers import init_store_at


def _seed_store(tmp_path: Path, monkeypatch: Any) -> Path:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    return root


def _smart_read(args: dict[str, Any]) -> str:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read", "arguments": args},
    }
    resp = _handle(req)
    assert resp is not None
    assert "result" in resp, resp
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    return text


def test_smart_read_outline_first_for_large_python_file(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "large_module.py"
    lines = ["import os", "", "class Demo:", "    def run(self):", "        return 1", ""]
    lines.extend(f"value_{i} = {i}" for i in range(1, 620))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    outline_md = _smart_read({"path": str(target), "include_meta": True})
    assert outline_md.startswith("(outline; :full = source)")
    assert "hint:" not in outline_md
    assert "Demo" in outline_md

    full_md = _smart_read({"path": str(target), "full": True})
    assert "hint:" not in full_md
    assert "value_619 = 619" in full_md

    # outline is shorter than full read (tokens saved)
    assert len(outline_md) < len(full_md)

    range_md = _smart_read({"path": str(target), "range": "42-118"})
    # range 42-118 contains value_36..value_112 = 77 value_ lines
    value_lines = [ln for ln in range_md.splitlines() if ln.startswith("value_")]
    assert len(value_lines) == 77


def test_smart_read_tolerates_open_ended_and_malformed_end_ranges(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "range_target.py"
    target.write_text("\n".join(f"line_{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

    open_ended = _smart_read({"path": str(target), "range": "L6-"})
    expected = [f"line_{i}" for i in range(6, 11)]
    for line in expected:
        assert line in open_ended

    malformed_end = _smart_read({"path": str(target), "range": "L6-foo"})
    for line in expected:
        assert line in malformed_end


def test_smart_read_small_file_defaults_to_full(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)

    target = tmp_path / "small.py"
    target.write_text("def ping():\n    return 'pong'\n", encoding="utf-8")

    payload = _smart_read({"path": str(target)})
    assert "(outline)" not in payload
    assert "def ping()" in payload


def test_smart_read_range_claims_no_savings_against_builtin_range_read(tmp_path: Path) -> None:
    target = tmp_path / "range_target.py"
    target.write_text("\n".join(f"value_{idx} = {idx}" for idx in range(300)), encoding="utf-8")

    payload = SemanticFileMemoryCapability(tmp_path).smart_read(target, range_spec="10-20")

    assert payload["mode"] == "range"


def test_smart_read_large_file_savings_use_claude_read_cap(tmp_path: Path) -> None:
    target = tmp_path / "large_module.py"
    source = "\n".join(
        ["class Demo:", "    def run(self):", "        return 1"] + [f"value_{idx} = {idx}" for idx in range(2600)]
    )
    target.write_text(source, encoding="utf-8")

    payload = SemanticFileMemoryCapability(tmp_path).smart_read(target, outline_threshold=10)
    baseline_tokens = _count_tokens(claude_read_baseline_text(source))
    full_file_tokens = _count_tokens(source)

    assert payload["mode"] == "outline"
    assert payload["tokens_saved"] <= baseline_tokens
    assert payload["tokens_saved"] < full_file_tokens


def test_smart_read_minified_projection_banner_for_safe_language(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)
    # Pin the outline threshold above this file's LOC so the read stays in
    # full mode — this test exercises the minified projection banner, not the
    # outline-by-default behavior.
    monkeypatch.setenv("LEMONCROW_OUTLINE_THRESHOLD", "200")

    # Big enough (comments + blank-line runs across multiple functions) that
    # minification's saving survives the gutter's own "N\t" per-line overhead
    # -- a trivial one-liner does NOT (see
    # test_smart_read_minified_projection_falls_back_when_gutter_not_worth_it),
    # so the gutter banner is conditional on actually netting a saving.
    target = tmp_path / "sample.go"
    target.write_text(
        "// Package main is the entry point.\n"
        "package main\n\n"
        'import "fmt"\n\n'
        "// greet prints a friendly greeting to stdout.\n"
        "func greet(name string) {\n"
        "\t// simple formatting, nothing fancy\n"
        '\tfmt.Println("Hello, " + name + "!")\n'
        "}\n\n"
        "// add returns the sum of two integers.\n"
        "func add(a int, b int) int {\n"
        "\t// straightforward addition\n"
        "\treturn a + b\n"
        "}\n\n"
        "func main() {\n"
        '\tgreet("world")\n'
        "\tfmt.Println(add(2, 3))\n"
        "}\n",
        encoding="utf-8",
    )

    rendered = _smart_read({"path": str(target)})

    # Gutter numbers are real disk lines now (safe to use in :Lx-Ly directly),
    # so the banner no longer warns they "differ from disk" -- it says they're
    # safe to use as-is instead.
    assert rendered.startswith("(minified; number = real disk line, safe in :Lx-Ly")
    assert "package main" in rendered
    assert "2\tpackage main" in rendered


def test_smart_read_minified_projection_falls_back_when_gutter_not_worth_it(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_store(tmp_path, monkeypatch)
    monkeypatch.setenv("LEMONCROW_OUTLINE_THRESHOLD", "200")

    # Trivial file: minification's own saving (one dropped blank line, a few
    # collapsed spaces) is smaller than the gutter's "N\t" prefix cost across
    # its few lines -- the guttered serve would be net LARGER than raw disk.
    # Falls back to the plain (ungutted) minified view instead of shipping a
    # projection that's worse than not minifying at all.
    target = tmp_path / "tiny.go"
    target.write_text(
        'package   main\n\nfunc   main()   {\n    println("hello")\n}\n',
        encoding="utf-8",
    )

    rendered = _smart_read({"path": str(target)})

    assert not rendered.startswith("(minified; number = real disk line")
    assert "package main" in rendered
    # No disk-line gutter prefix on this fallback path.
    assert "1\tpackage main" not in rendered
