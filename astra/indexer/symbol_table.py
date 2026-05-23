"""Data classes for symbols (nodes) and relationships (edges)."""
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Optional


@dataclass
class Symbol:
    type: str           # function|class|method|module
    name: str
    file: str
    signature: str = ""
    docstring: str = ""
    line_start: int = 0
    line_end: int = 0
    raw_text: str = ""
    calls: list[str] = field(default_factory=list)      # names called by this symbol
    imports: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        key = f"{self.file}::{self.type}::{self.name}::{self.line_start}"
        return sha256(key.encode()).hexdigest()[:16]

    @property
    def embed_text(self) -> str:
        """Text fed to embedder — signature + docstring, not body."""
        parts = []
        if self.signature:
            parts.append(self.signature)
        if self.docstring:
            parts.append(self.docstring)
        if not parts:
            parts.append(f"{self.type} {self.name}")
        return " ".join(parts)


@dataclass
class Edge:
    src: str            # node id
    dst: str            # node id
    relation: str       # CALLS|IMPORTS|INHERITS|DEFINES


@dataclass
class FileSymbols:
    file: str
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
