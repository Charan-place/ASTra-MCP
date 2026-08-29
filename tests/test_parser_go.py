"""Tests for the Go parser support in astra.indexer.parser."""
from astra.indexer.parser import parse_file

GO_SRC = '''
package main

type Shape struct {
	Name string
}

type Speaker interface {
	Speak() string
}

func Add(a int, b int) int {
	return helper(a, b)
}

func helper(a, b int) int {
	return a + b
}

func (s *Shape) Area() int {
	return Add(1, 2)
}
'''


def test_parse_go(tmp_path):
    f = tmp_path / "shapes.go"
    f.write_text(GO_SRC)

    result = parse_file(f)
    assert result is not None

    names = {s.name: s for s in result.symbols}
    assert "Add" in names
    assert names["Add"].type == "function"
    assert "helper" in names
    assert names["helper"].type == "function"
    assert "Area" in names
    assert names["Area"].type == "function"
    assert "Shape" in names
    assert names["Shape"].type == "struct"
    assert "Speaker" in names
    assert names["Speaker"].type == "interface"

    # Add calls helper -> should produce a CALLS edge
    add_sym = names["Add"]
    helper_sym = names["helper"]
    assert any(
        e.src == add_sym.id and e.dst == helper_sym.id and e.relation == "CALLS"
        for e in result.edges
    )
