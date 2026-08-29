"""Tests for the Java parser support in astra.indexer.parser."""
from astra.indexer.parser import parse_file

JAVA_SRC = '''
class Shape {
    String name;

    int area() {
        return helper(1, 2);
    }

    int helper(int a, int b) {
        return a + b;
    }
}

interface Speaker {
    String speak();
}
'''


def test_parse_java(tmp_path):
    f = tmp_path / "Shape.java"
    f.write_text(JAVA_SRC)

    result = parse_file(f)
    assert result is not None

    names = {s.name: s for s in result.symbols}
    assert "area" in names
    assert names["area"].type == "function"
    assert "helper" in names
    assert names["helper"].type == "function"
    assert "Shape" in names
    assert names["Shape"].type == "class"
    assert "Speaker" in names
    assert names["Speaker"].type == "interface"

    area_sym = names["area"]
    helper_sym = names["helper"]
    assert any(
        e.src == area_sym.id and e.dst == helper_sym.id and e.relation == "CALLS"
        for e in result.edges
    )
