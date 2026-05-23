"""Convert ranked graph nodes → minimal token string for LLM injection."""
from astra.graph.store import GraphStore


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token."""
    return len(text) // 4


def serialize_node(node: dict) -> str:
    """Signature + docstring only. Never raw body. This is where tokens are saved."""
    parts = []
    loc = f"{node['file']}:{node['line_start']}-{node['line_end']}"
    parts.append(f"# {loc}")

    if node.get("signature"):
        parts.append(node["signature"])
    else:
        parts.append(f"{node['type']} {node['name']}")

    if node.get("docstring"):
        doc = node["docstring"]
        if len(doc) > 200:
            doc = doc[:200] + "..."
        parts.append(f'  """{doc}"""')

    return "\n".join(parts)


def build_context(
    store: GraphStore,
    ranked_nodes: list[tuple[str, float]],
    max_tokens: int = 4000,
) -> tuple[str, int]:
    """
    Build context string from ranked (node_id, score) list.
    Returns (context_str, token_estimate).
    Stops adding nodes when budget exhausted.
    """
    sections: list[str] = []
    total_tokens = 0
    included = 0

    for node_id, score in ranked_nodes:
        node = store.get_node(node_id)
        if not node:
            continue
        if node["type"] == "file":
            continue  # file-level nodes add noise, skip

        serialized = serialize_node(node)
        tok = estimate_tokens(serialized)

        if total_tokens + tok > max_tokens:
            break

        sections.append(serialized)
        total_tokens += tok
        included += 1

    header = f"# ASTra context — {included} symbols, ~{total_tokens} tokens\n"
    context = header + "\n\n".join(sections)
    return context, total_tokens
