-- Runs once on first container init.
create extension if not exists vector;

-- One row per work with an abstract. The graph in Neo4j holds the same work
-- id, which is how a similarity hit turns into a traversal start point.
create table if not exists work_text (
    id          text primary key,        -- 'W123', same key as the Neo4j node
    title       text not null,
    year        int,
    abstract    text not null,
    embedding   vector(384)              -- bge-small-en-v1.5
);

-- HNSW over cosine. Built after the bulk load, not before it.
-- create index work_text_embedding_idx on work_text using hnsw (embedding vector_cosine_ops);
