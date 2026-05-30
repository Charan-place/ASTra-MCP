# Contributing to ASTra MCP

ASTra MCP is an open-source MCP server that gives AI coding assistants permanent structural memory of codebases. Contributions are welcome and appreciated.

## Ways to Contribute

| Area | Where | Skill level |
|---|---|---|
| New language parsers (Go, Rust, Java) | `astra/indexer/parser.py` | Intermediate |
| Dashboard UX improvements | `astra/dashboard/` | Frontend |
| Benchmarks on real codebases | `benchmarks/` | Any |
| Bug reports and fixes | Issues + any file | Any |
| Documentation improvements | `README.md`, `ARCHITECTURE.md` | Any |
| New MCP tool ideas | `astra/mcp/tools.py` | Intermediate |

## Development Setup

```bash
git clone https://github.com/Charan-place/ASTra-MCP.git
cd ASTra-MCP

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
python3 -m pytest tests/ -v
```

All 58 tests must pass before submitting a PR.

## Adding a New Language Parser

ASTra uses tree-sitter for AST parsing. To add a new language:

1. Add the tree-sitter grammar package to `pyproject.toml` dependencies
2. Add a handler in `astra/indexer/parser.py` — follow the Python handler as a template
3. Add the file extension to `SUPPORTED_EXTENSIONS` in `graph_builder.py`
4. Write tests in `tests/test_parser.py` covering function extraction, class extraction, and call detection
5. Update `README.md` languages list

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality
- Run `python3 -m pytest tests/ -v` and confirm all pass
- Update `README.md` or `ARCHITECTURE.md` if you change behavior
- PR title format: `feat: add Go parser` / `fix: handle empty docstrings` / `docs: update install steps`

## Reporting Bugs

Open an issue with:
- OS and Python version
- `astra --version` output
- Steps to reproduce
- Expected vs actual behavior
- Contents of `~/.astra-mcp/crash.log` if present

## Code Style

```bash
black astra/ tests/     # formatting
ruff check astra/       # linting
```

Line length: 100. No type: ignore without a comment explaining why.

## License

By contributing, you agree your contributions are licensed under Apache 2.0.
