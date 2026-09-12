"""Embed every abstract and load it into pgvector.

bge-small-en-v1.5: 384 dimensions, 33 M parameters, runs on the Mac's GPU
through MPS. Title and abstract are embedded together as one passage -- the
title is the densest sentence in the record and the abstract is the context
it needs. The HNSW index is built after the load, not before it; building it
incrementally over 127,000 inserts is many times slower than one pass.

Resumable: rows already in work_text are skipped, so a killed run continues.

    uv run python -m kgrag.embed
"""
from __future__ import annotations

import sys
import time

import duckdb
import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from .config import DB_PATH, PG_DSN

MODEL = "BAAI/bge-small-en-v1.5"
BATCH = 256
MAX_CHARS = 2500        # ~512 tokens; longer abstracts are truncated by the model anyway


def main() -> int:
    duck = duckdb.connect(str(DB_PATH), read_only=True)
    pg = psycopg.connect(PG_DSN, autocommit=True)
    register_vector(pg)

    done = {r[0] for r in pg.execute("select id from work_text")}
    rows = duck.execute(
        """
        select replace(id, 'https://openalex.org/', ''), title, year, abstract
        from work where abstract is not null and title is not null
        order by id
        """
    ).fetchall()
    todo = [r for r in rows if r[0] not in done]
    print(f"  {len(rows):,} works with abstracts, {len(done):,} already embedded, {len(todo):,} to go")
    if not todo:
        return 0

    model = SentenceTransformer(MODEL, device="mps")
    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        passages = [f"{t}. {a}"[:MAX_CHARS] for _, t, _, a in chunk]
        vecs = model.encode(passages, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
        with pg.cursor() as cur:
            with cur.copy("copy work_text (id, title, year, abstract, embedding) from stdin") as cp:
                for (wid, title, year, abstract), v in zip(chunk, vecs):
                    cp.write_row((wid, title, year, abstract, v))
        n = i + len(chunk)
        rate = n / (time.time() - t0)
        print(f"  {n:>8,} / {len(todo):,}   {rate:,.0f}/s   ~{(len(todo) - n) / max(rate, 1) / 60:.0f} min left", end="\r", flush=True)

    print(f"\n  embedded {len(todo):,} in {(time.time() - t0) / 60:.1f} min")
    print("  building HNSW index...")
    t = time.time()
    pg.execute("create index if not exists work_text_embedding_idx on work_text using hnsw (embedding vector_cosine_ops)")
    print(f"  index built in {time.time() - t:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
