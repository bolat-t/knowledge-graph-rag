"""Build-time load of the abstracts and embeddings into pgvector.

Runs once inside `docker build`, against a Postgres started for the purpose,
so the image ships with the table and its HNSW index already built and a cold
start is only a service start.
"""
import sys, time
import pyarrow.parquet as pq
import psycopg

dsn, path = sys.argv[1], sys.argv[2]
t = time.time()
tbl = pq.read_table(path)
con = psycopg.connect(dsn, autocommit=True)
con.execute("create extension if not exists vector")
con.execute("""create table if not exists work_text (
    id text primary key, title text not null, year int, abstract text not null, embedding vector(384))""")
with con.cursor() as cur, cur.copy("copy work_text (id, title, year, abstract, embedding) from stdin") as cp:
    for batch in tbl.to_batches(max_chunksize=5000):
        d = batch.to_pydict()
        for i in range(len(d["id"])):
            cp.write_row((d["id"][i], d["title"][i], d["year"][i], d["abstract"][i],
                          "[" + ",".join(f"{x:.6f}" for x in d["embedding"][i]) + "]"))
n = con.execute("select count(*) from work_text").fetchone()[0]
print(f"  {n:,} rows loaded in {time.time() - t:.0f}s", flush=True)
t = time.time()
# Serial build: a parallel one puts the graph in shared memory, and /dev/shm
# inside a docker build is 64 MB.
con.execute("set max_parallel_maintenance_workers = 0")
con.execute("set maintenance_work_mem = '768MB'")
con.execute("create index work_text_embedding_idx on work_text using hnsw (embedding vector_cosine_ops)")
print(f"  hnsw index in {time.time() - t:.0f}s", flush=True)
