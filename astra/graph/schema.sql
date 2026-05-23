CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT    PRIMARY KEY,
    type        TEXT    NOT NULL,       -- function|class|file|module
    name        TEXT    NOT NULL,
    file        TEXT    NOT NULL,
    signature   TEXT,
    docstring   TEXT,
    line_start  INTEGER,
    line_end    INTEGER,
    raw_text    TEXT,
    embedding   BLOB,                   -- 384-dim float32 as bytes
    indexed_at  REAL
);

CREATE TABLE IF NOT EXISTS edges (
    src         TEXT    NOT NULL,
    dst         TEXT    NOT NULL,
    relation    TEXT    NOT NULL,       -- CALLS|IMPORTS|INHERITS|DEFINES|REFERENCES
    PRIMARY KEY (src, dst, relation)
);

CREATE TABLE IF NOT EXISTS file_hashes (
    file        TEXT    PRIMARY KEY,
    hash        TEXT    NOT NULL,
    indexed_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_file   ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_name   ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_edges_src    ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst    ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_rel    ON edges(relation);
