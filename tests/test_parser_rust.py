"""Tests for the Rust parser support in astra.indexer.parser."""
from astra.indexer.parser import parse_file

RUST_SRC = '''
pub struct Shape {
    name: String,
}

pub trait Speaker {
    fn speak(&self) -> String;
}

fn helper(a: i32, b: i32) -> i32 {
    a + b
}

impl Shape {
    fn area(&self) -> i32 {
        helper(1, 2)
    }
}
'''


def test_parse_rust(tmp_path):
    f = tmp_path / "shapes.rs"
    f.write_text(RUST_SRC)

    result = parse_file(f)
    assert result is not None

    names = {s.name: s for s in result.symbols}
    assert "helper" in names
    assert names["helper"].type == "function"
    assert "area" in names
    assert names["area"].type == "function"
    assert "Shape" in names
    assert names["Shape"].type == "struct"
    assert "Speaker" in names
    assert names["Speaker"].type == "trait"

    area_sym = names["area"]
    helper_sym = names["helper"]
    assert any(
        e.src == area_sym.id and e.dst == helper_sym.id and e.relation == "CALLS"
        for e in result.edges
    )
