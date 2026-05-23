"""AST parser using manual node traversal (tree-sitter 0.25.x compatible)."""
from pathlib import Path
from typing import Optional

from astra.indexer.symbol_table import Symbol, Edge, FileSymbols

try:
    import tree_sitter_python as _tspy
    import tree_sitter_javascript as _tsjs
    import tree_sitter_typescript as _tsts
    from tree_sitter import Language, Parser as _TSParser
    _HAS_TS = True
except ImportError:
    _HAS_TS = False


SUPPORTED = {".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "coverage", ".astra",
}


def _make_parser(ext: str):
    if not _HAS_TS:
        raise RuntimeError("pip install tree-sitter tree-sitter-python tree-sitter-javascript tree-sitter-typescript")
    if ext == ".py":
        L = Language(_tspy.language())
    elif ext in (".js", ".jsx", ".mjs", ".cjs"):
        L = Language(_tsjs.language())
    elif ext in (".ts",):
        L = Language(_tsts.language_typescript())
    elif ext in (".tsx",):
        L = Language(_tsts.language_tsx())
    else:
        raise ValueError(f"Unsupported: {ext}")
    return _TSParser(L), L


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _field(node, field_name: str):
    return node.child_by_field_name(field_name)


def _walk(node, node_type: str) -> list:
    """Collect all descendant nodes of given type."""
    results = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == node_type:
            results.append(n)
        stack.extend(reversed(n.children))
    return results


def _walk_multi(node, node_types: set) -> list:
    results = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in node_types:
            results.append(n)
        stack.extend(reversed(n.children))
    return results


def _extract_docstring(body_node, src: bytes) -> str:
    """Get first string literal from function/class body."""
    if not body_node:
        return ""
    for child in body_node.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = _text(sub, src).strip("\"'").strip('"""').strip("'''").strip()
                    return raw[:500]
    return ""


def _extract_calls(fn_node, src: bytes) -> list[str]:
    """Extract all function call names within a node."""
    calls = []
    for call_node in _walk(fn_node, "call"):
        fn_field = _field(call_node, "function")
        if not fn_field:
            continue
        if fn_field.type == "identifier":
            calls.append(_text(fn_field, src))
        elif fn_field.type == "attribute":
            attr = _field(fn_field, "attribute")
            if attr:
                calls.append(_text(attr, src))
    return list(set(calls))


# ── Python ─────────────────────────────────────────────────────────────────

def _parse_python(tree, src: bytes, file_str: str) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    root = tree.root_node

    # classes
    for cls_node in _walk(root, "class_definition"):
        name_node = _field(cls_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        body = _field(cls_node, "body")
        doc = _extract_docstring(body, src)
        symbols.append(Symbol(
            type="class", name=name, file=file_str,
            signature=f"class {name}",
            docstring=doc,
            line_start=cls_node.start_point[0] + 1,
            line_end=cls_node.end_point[0] + 1,
        ))

    # functions and methods
    for fn_node in _walk(root, "function_definition"):
        name_node = _field(fn_node, "name")
        params_node = _field(fn_node, "parameters")
        if not name_node:
            continue
        name = _text(name_node, src)
        params = _text(params_node, src) if params_node else "()"

        ret = ""
        ret_node = _field(fn_node, "return_type")
        if ret_node:
            ret = f" -> {_text(ret_node, src)}"

        body = _field(fn_node, "body")
        doc = _extract_docstring(body, src)
        calls = _extract_calls(fn_node, src)

        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"def {name}{params}{ret}",
            docstring=doc,
            line_start=fn_node.start_point[0] + 1,
            line_end=fn_node.end_point[0] + 1,
            calls=calls,
        ))

    all_calls = _extract_calls(root, src)
    return symbols, all_calls


# ── JavaScript / TypeScript ────────────────────────────────────────────────

def _parse_js(tree, src: bytes, file_str: str) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    root = tree.root_node

    # classes
    for cls_node in _walk_multi(root, {"class_declaration", "class"}):
        name_node = _field(cls_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        symbols.append(Symbol(
            type="class", name=name, file=file_str,
            signature=f"class {name}",
            line_start=cls_node.start_point[0] + 1,
            line_end=cls_node.end_point[0] + 1,
        ))

    # function declarations
    for fn_node in _walk(root, "function_declaration"):
        name_node = _field(fn_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        params_node = _field(fn_node, "parameters")
        params = _text(params_node, src) if params_node else "()"
        calls = _extract_js_calls(fn_node, src)
        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"function {name}{params}",
            line_start=fn_node.start_point[0] + 1,
            line_end=fn_node.end_point[0] + 1,
            calls=calls,
        ))

    # const/let fn = () => {} and const fn = function() {}
    for decl in _walk(root, "variable_declarator"):
        name_node = _field(decl, "name")
        val_node = _field(decl, "value")
        if not name_node or not val_node:
            continue
        if val_node.type not in ("arrow_function", "function"):
            continue
        name = _text(name_node, src)
        params_node = _field(val_node, "parameters") or _field(val_node, "parameter")
        params = _text(params_node, src) if params_node else "()"
        calls = _extract_js_calls(val_node, src)
        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"const {name} = {val_node.type}{params}",
            line_start=decl.start_point[0] + 1,
            line_end=decl.end_point[0] + 1,
            calls=calls,
        ))

    # methods in classes
    for method_node in _walk(root, "method_definition"):
        key_node = _field(method_node, "name")
        if not key_node:
            continue
        name = _text(key_node, src)
        params_node = _field(method_node, "parameters")
        params = _text(params_node, src) if params_node else "()"
        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"{name}{params}",
            line_start=method_node.start_point[0] + 1,
            line_end=method_node.end_point[0] + 1,
        ))

    all_calls = _extract_js_calls(root, src)
    return symbols, all_calls


def _extract_js_calls(node, src: bytes) -> list[str]:
    calls = []
    for call_node in _walk(node, "call_expression"):
        fn_field = _field(call_node, "function")
        if not fn_field:
            continue
        if fn_field.type == "identifier":
            calls.append(_text(fn_field, src))
        elif fn_field.type == "member_expression":
            prop = _field(fn_field, "property")
            if prop:
                calls.append(_text(prop, src))
    return list(set(calls))


# ── Public API ─────────────────────────────────────────────────────────────

def parse_file(path: Path) -> Optional[FileSymbols]:
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        return None

    try:
        src_bytes = path.read_bytes()
    except (PermissionError, OSError):
        return None

    try:
        parser, _lang = _make_parser(ext)
    except Exception:
        return None

    tree = parser.parse(src_bytes)
    file_str = str(path)

    if ext == ".py":
        symbols, all_calls = _parse_python(tree, src_bytes, file_str)
    else:
        symbols, all_calls = _parse_js(tree, src_bytes, file_str)

    file_sym = Symbol(
        type="file", name=path.name, file=file_str,
        signature=file_str,
        line_start=1,
        line_end=tree.root_node.end_point[0] + 1,
    )
    symbols.insert(0, file_sym)

    # intra-file call edges
    name_to_id = {s.name: s.id for s in symbols if s.type != "file"}
    edges: list[Edge] = []
    for sym in symbols:
        for callee in sym.calls:
            if callee in name_to_id and callee != sym.name:
                edges.append(Edge(src=sym.id, dst=name_to_id[callee], relation="CALLS"))

    return FileSymbols(file=file_str, symbols=symbols, edges=edges)


def iter_source_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            if not any(skip in path.parts for skip in SKIP_DIRS):
                yield path
