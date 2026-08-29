"""Tests for astra.indexer.parser: parse_file symbol/edge extraction."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from astra.indexer.parser import parse_file, iter_source_files, SUPPORTED


PY_SRC = '''
def helper():
    """A helper function."""
    return 1


def main():
    """Entry point that calls helper."""
    return helper()


class Greeter:
    """Greets people."""

    def greet(self, name):
        """Say hello."""
        return helper()
'''

JS_SRC = '''
function helper() {
    return 1;
}

function main() {
    return helper();
}

const arrow = () => {
    return helper();
};

class Greeter {
    greet(name) {
        return helper();
    }
}
'''

TS_SRC = '''
function helper(): number {
    return 1;
}

function main(): number {
    return helper();
}

class Greeter {
    greet(name: string): number {
        return helper();
    }
}
'''


def test_parse_python_symbols(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(PY_SRC)

    result = parse_file(f)
    assert result is not None
    names = {s.name for s in result.symbols}

    assert "helper" in names
    assert "main" in names
    assert "Greeter" in names
    assert "greet" in names

    types_by_name = {s.name: s.type for s in result.symbols}
    assert types_by_name["helper"] == "function"
    assert types_by_name["Greeter"] == "class"
    assert types_by_name["mod.py"] == "file"


def test_parse_python_docstrings(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(PY_SRC)
    result = parse_file(f)
    docs = {s.name: s.docstring for s in result.symbols}
    assert docs["helper"] == "A helper function."
    assert docs["main"] == "Entry point that calls helper."


def test_parse_python_call_edges(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(PY_SRC)
    result = parse_file(f)

    by_name = {s.name: s for s in result.symbols}
    main_sym = by_name["main"]
    helper_sym = by_name["helper"]

    assert "helper" in main_sym.calls

    # An edge should exist main -> helper
    edge_pairs = {(e.src, e.dst) for e in result.edges}
    assert (main_sym.id, helper_sym.id) in edge_pairs


def test_parse_js_symbols(tmp_path):
    f = tmp_path / "mod.js"
    f.write_text(JS_SRC)

    result = parse_file(f)
    assert result is not None
    names = {s.name for s in result.symbols}

    assert "helper" in names
    assert "main" in names
    assert "arrow" in names
    assert "Greeter" in names
    assert "greet" in names


def test_parse_js_call_edges(tmp_path):
    f = tmp_path / "mod.js"
    f.write_text(JS_SRC)
    result = parse_file(f)

    by_name = {s.name: s for s in result.symbols}
    main_sym = by_name["main"]
    helper_sym = by_name["helper"]

    assert "helper" in main_sym.calls
    edge_pairs = {(e.src, e.dst) for e in result.edges}
    assert (main_sym.id, helper_sym.id) in edge_pairs


def test_parse_ts_symbols(tmp_path):
    f = tmp_path / "mod.ts"
    f.write_text(TS_SRC)

    result = parse_file(f)
    assert result is not None
    names = {s.name for s in result.symbols}
    assert "helper" in names
    assert "main" in names
    assert "Greeter" in names
    assert "greet" in names


def test_parse_unsupported_extension_returns_none(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello world")
    assert parse_file(f) is None


def test_parse_skip_file_by_name(tmp_path):
    f = tmp_path / "d3.min.js"
    f.write_text(JS_SRC)
    assert parse_file(f) is None


def test_iter_source_files_skips_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a(): pass")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "b.js").write_text("function b() {}")

    found = list(iter_source_files(tmp_path))
    found_names = {p.name for p in found}
    assert "a.py" in found_names
    assert "b.js" not in found_names


def test_iter_source_files_only_supported_ext(tmp_path):
    (tmp_path / "a.py").write_text("def a(): pass")
    (tmp_path / "readme.md").write_text("# hi")
    found = list(iter_source_files(tmp_path))
    for p in found:
        assert p.suffix.lower() in SUPPORTED


def test_file_symbol_has_full_line_span(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(PY_SRC)
    result = parse_file(f)
    file_sym = result.symbols[0]
    assert file_sym.type == "file"
    assert file_sym.line_start == 1
    assert file_sym.line_end >= 1
