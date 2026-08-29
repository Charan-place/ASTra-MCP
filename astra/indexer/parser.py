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

try:
    import tree_sitter_go as _tsgo
    import tree_sitter_rust as _tsrust
    import tree_sitter_java as _tsjava
    _HAS_EXTRA_LANGS = True
except ImportError:
    _HAS_EXTRA_LANGS = False


SUPPORTED = {".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java"}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "coverage", ".astra",
}

SKIP_FILES = {"d3.min.js", "d3.js"}


def _should_skip_file(path) -> bool:
    from pathlib import Path
    p = Path(path)
    return p.name in SKIP_FILES or any(skip in p.parts for skip in SKIP_DIRS)


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
    elif ext == ".go":
        if not _HAS_EXTRA_LANGS:
            raise RuntimeError("pip install tree-sitter-go tree-sitter-rust tree-sitter-java")
        L = Language(_tsgo.language())
    elif ext == ".rs":
        if not _HAS_EXTRA_LANGS:
            raise RuntimeError("pip install tree-sitter-go tree-sitter-rust tree-sitter-java")
        L = Language(_tsrust.language())
    elif ext == ".java":
        if not _HAS_EXTRA_LANGS:
            raise RuntimeError("pip install tree-sitter-go tree-sitter-rust tree-sitter-java")
        L = Language(_tsjava.language())
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
                    raw = _text(sub, src)
                    # Strip surrounding quote delimiters as substrings, not characters
                    for q in ('"""', "'''", '"', "'"):
                        if raw.startswith(q) and raw.endswith(q) and len(raw) >= len(q) * 2:
                            raw = raw[len(q):-len(q)]
                            break
                    return raw.strip()[:500]
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


# ── Go ─────────────────────────────────────────────────────────────────────

def _extract_go_calls(node, src: bytes) -> list[str]:
    calls = []
    for call_node in _walk(node, "call_expression"):
        fn_field = _field(call_node, "function")
        if not fn_field:
            continue
        if fn_field.type == "identifier":
            calls.append(_text(fn_field, src))
        elif fn_field.type == "selector_expression":
            field_node = _field(fn_field, "field")
            if field_node:
                calls.append(_text(field_node, src))
    return list(set(calls))


def _parse_go(tree, src: bytes, file_str: str) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    root = tree.root_node

    # struct/interface types
    for td_node in _walk(root, "type_declaration"):
        for spec in td_node.children:
            if spec.type != "type_spec":
                continue
            name_node = _field(spec, "name")
            type_node = _field(spec, "type")
            if not name_node or not type_node:
                continue
            name = _text(name_node, src)
            if type_node.type == "struct_type":
                sym_type = "struct"
                sig = f"type {name} struct"
            elif type_node.type == "interface_type":
                sym_type = "interface"
                sig = f"type {name} interface"
            else:
                continue
            symbols.append(Symbol(
                type=sym_type, name=name, file=file_str,
                signature=sig,
                line_start=spec.start_point[0] + 1,
                line_end=spec.end_point[0] + 1,
            ))

    # functions
    for fn_node in _walk(root, "function_declaration"):
        name_node = _field(fn_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        params_node = _field(fn_node, "parameters")
        params = _text(params_node, src) if params_node else "()"
        result_node = _field(fn_node, "result")
        ret = f" {_text(result_node, src)}" if result_node else ""
        calls = _extract_go_calls(fn_node, src)
        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"func {name}{params}{ret}",
            line_start=fn_node.start_point[0] + 1,
            line_end=fn_node.end_point[0] + 1,
            calls=calls,
        ))

    # methods (with receiver)
    for method_node in _walk(root, "method_declaration"):
        name_node = _field(method_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        receiver_node = _field(method_node, "receiver")
        receiver = _text(receiver_node, src) if receiver_node else "()"
        params_node = _field(method_node, "parameters")
        params = _text(params_node, src) if params_node else "()"
        result_node = _field(method_node, "result")
        ret = f" {_text(result_node, src)}" if result_node else ""
        calls = _extract_go_calls(method_node, src)
        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"func {receiver} {name}{params}{ret}",
            line_start=method_node.start_point[0] + 1,
            line_end=method_node.end_point[0] + 1,
            calls=calls,
        ))

    all_calls = _extract_go_calls(root, src)
    return symbols, all_calls


# ── Rust ───────────────────────────────────────────────────────────────────

def _extract_rust_calls(node, src: bytes) -> list[str]:
    calls = []
    for call_node in _walk(node, "call_expression"):
        fn_field = _field(call_node, "function")
        if not fn_field:
            continue
        if fn_field.type == "identifier":
            calls.append(_text(fn_field, src))
        elif fn_field.type == "field_expression":
            field_node = _field(fn_field, "field")
            if field_node:
                calls.append(_text(field_node, src))
        elif fn_field.type == "scoped_identifier":
            name_node = _field(fn_field, "name")
            if name_node:
                calls.append(_text(name_node, src))
    return list(set(calls))


def _parse_rust(tree, src: bytes, file_str: str) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    root = tree.root_node

    # structs
    for st_node in _walk(root, "struct_item"):
        name_node = _field(st_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        symbols.append(Symbol(
            type="struct", name=name, file=file_str,
            signature=f"struct {name}",
            line_start=st_node.start_point[0] + 1,
            line_end=st_node.end_point[0] + 1,
        ))

    # traits
    for tr_node in _walk(root, "trait_item"):
        name_node = _field(tr_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        symbols.append(Symbol(
            type="trait", name=name, file=file_str,
            signature=f"trait {name}",
            line_start=tr_node.start_point[0] + 1,
            line_end=tr_node.end_point[0] + 1,
        ))

    # modules (treated as classes, for grouping)
    for mod_node in _walk(root, "mod_item"):
        name_node = _field(mod_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        symbols.append(Symbol(
            type="module", name=name, file=file_str,
            signature=f"mod {name}",
            line_start=mod_node.start_point[0] + 1,
            line_end=mod_node.end_point[0] + 1,
        ))

    # free functions and impl-block (associated) functions/methods
    for fn_node in _walk(root, "function_item"):
        name_node = _field(fn_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        params_node = _field(fn_node, "parameters")
        params = _text(params_node, src) if params_node else "()"
        ret_node = _field(fn_node, "return_type")
        ret = f" -> {_text(ret_node, src)}" if ret_node else ""
        calls = _extract_rust_calls(fn_node, src)
        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"fn {name}{params}{ret}",
            line_start=fn_node.start_point[0] + 1,
            line_end=fn_node.end_point[0] + 1,
            calls=calls,
        ))

    all_calls = _extract_rust_calls(root, src)
    return symbols, all_calls


# ── Java ───────────────────────────────────────────────────────────────────

def _extract_java_calls(node, src: bytes) -> list[str]:
    calls = []
    for call_node in _walk(node, "method_invocation"):
        name_node = _field(call_node, "name")
        if name_node:
            calls.append(_text(name_node, src))
    return list(set(calls))


def _parse_java(tree, src: bytes, file_str: str) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    root = tree.root_node

    # classes
    for cls_node in _walk(root, "class_declaration"):
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

    # interfaces
    for if_node in _walk(root, "interface_declaration"):
        name_node = _field(if_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        symbols.append(Symbol(
            type="interface", name=name, file=file_str,
            signature=f"interface {name}",
            line_start=if_node.start_point[0] + 1,
            line_end=if_node.end_point[0] + 1,
        ))

    # methods
    for method_node in _walk(root, "method_declaration"):
        name_node = _field(method_node, "name")
        if not name_node:
            continue
        name = _text(name_node, src)
        params_node = _field(method_node, "parameters")
        params = _text(params_node, src) if params_node else "()"
        ret_node = _field(method_node, "type")
        ret = f"{_text(ret_node, src)} " if ret_node else ""
        calls = _extract_java_calls(method_node, src)
        symbols.append(Symbol(
            type="function", name=name, file=file_str,
            signature=f"{ret}{name}{params}",
            line_start=method_node.start_point[0] + 1,
            line_end=method_node.end_point[0] + 1,
            calls=calls,
        ))

    all_calls = _extract_java_calls(root, src)
    return symbols, all_calls


# ── Public API ─────────────────────────────────────────────────────────────

def parse_file(path: Path) -> Optional[FileSymbols]:
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        return None
    if path.name in SKIP_FILES:
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
    elif ext == ".go":
        symbols, all_calls = _parse_go(tree, src_bytes, file_str)
    elif ext == ".rs":
        symbols, all_calls = _parse_rust(tree, src_bytes, file_str)
    elif ext == ".java":
        symbols, all_calls = _parse_java(tree, src_bytes, file_str)
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
                if path.name not in SKIP_FILES:
                    yield path
