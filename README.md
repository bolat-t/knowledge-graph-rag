---
title: Sydney Research Graph
emoji: 🕸️
colorFrom: gray
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

# Knowledge Graph RAG

*Sydney's research graph — repo for portfolio project 6, the graph lane.*

Every journal article since 2019 with an author at a Sydney university,
pulled from OpenAlex into two stores that answer different halves of a question:

- **Neo4j** holds the graph — works, authors, institutions, topics, funders,
  sources, and the edges between them — for questions whose answer is a *path*.
- **pgvector** holds the abstracts and their embeddings, for questions whose
  answer is *a passage that reads like the question*.

The hybrid question, which is the whole point:

> *Who near me works on something like this paragraph, and how am I connected to them?*

Embeddings find the works. The graph turns them into people and a path.

## Status

Live at **https://bolat-t-knowledge-graph-rag.hf.space**. Full corpus in both stores; web app in `web/` + `kgrag.api`; deployed as a Hugging Face Space from the `Dockerfile`.

| Stage | State |
|---|---|
| OpenAlex pull → gzipped JSONL | ✅ `ingest_works` — 218,984 works over two days of free-tier quota |
| Institution aliases | ✅ `ingest_institutions` — 7,180 records, 144 requests |
| Shape in DuckDB → node/edge tables | ✅ `shape` — 26 s |
| Bulk load → Neo4j | ✅ `load_neo4j` — 889k nodes, 5.7 M edges in 10 s |
| Abstracts → pgvector | ✅ `embed` — 170,894 abstracts, bge-small on MPS, ~40 min; HNSW in 54 s |
| Hybrid query (vector → graph) | ✅ `ask` — vector top-k → people → shortest path to you |
| Graph-only questions | ✅ `explore` — collaborators, path, bridges, reach |
| Web app | ✅ `kgrag.api` + `web/index.html` — ask box, author autocomplete, paths, ego network |
| Hosted demo | ✅ **https://bolat-t-knowledge-graph-rag.hf.space** — one container (Neo4j + pgvector + app), databases loaded at build, 19 s cold start |
| Write-up | ⬜ |

## Stack

| Layer | Choice | Why |
|---|---|---|
| **Source** | OpenAlex API | open catalogue of scholarly work; authors, citations, affiliations already linked |
| **Staging** | DuckDB | reads the gzipped JSONL directly, infers the nested structs, reshapes in SQL |
| **Graph** | Neo4j 5 community + GDS | `neo4j-admin import` for bulk load; Cypher for paths; GDS for centrality and communities |
| **Vectors** | Postgres 16 + pgvector | abstracts and embeddings next to the same work ids the graph uses |
| **Infra** | Docker Compose | both stores local, one command |

## The graph

```
(Author)-[:AUTHORED {position, corresponding}]->(Work)
(Author)-[:AFFILIATED_WITH {n_works, first_year, last_year}]->(Institution)
(Work)-[:FROM_INSTITUTION]->(Institution)
(Work)-[:CITES]->(Work)                       corpus-internal only
(Work)-[:HAS_TOPIC {score, is_primary}]->(Topic)
(Work)-[:FUNDED_BY]->(Funder)
(Work)-[:PUBLISHED_IN]->(Source)
```

Two institution edges, deliberately. `AFFILIATED_WITH` (per author, aggregated)
answers *who is at UNSW*. It cannot answer *how many papers do UNSW and Oxford
share* — one author's edge reaches all of their papers — and the first version
of this project got that question wrong by not having `FROM_INSTITUTION`.

## Data quality notes

Measured, not assumed — the numbers come from `shape`'s report.

- **OpenAlex's free tier is 1,000 requests a day**, not the 100k the older docs
  describe. The API moved to credits in 2026 (`x-ratelimit-limit: 1000`, each
  page one credit) and the public S3 snapshot is gone (`x-amz-delete-marker`).
  A 429 past the quota carries `Retry-After` of the seconds to midnight UTC;
  the first version of the ingest slept on it for twelve hours. It now aborts
  and says when to come back.
- **Eight parallel year-cursors drew 429s within minutes; four did not.**
  Years are disjoint filters, so parallel cursors need no de-duplication.
- **3.3% of authorships have no author id.** They keep their raw name on the
  authorship row in DuckDB and are absent from the graph: a node without a key
  cannot be de-duplicated, and a graph of unresolvable people is worse than a
  graph with a stated gap.
- **Author splits are real and measurable.** In the first 5,000 works,
  "T. Andeen" is 12 distinct author ids and "Wei Li" is 13 — some different
  people, some the same person split by initials-only collaboration bylines.
- **OpenAlex's affiliation matcher has sinks.** Journal boilerplate
  ("Supplemental digital content is available…") resolves to *Apple (Israel)*
  510 times. Real Sydney affiliation strings with a stray leading character
  ("ASchool of Medicine, Western Sydney University") resolve to *Human Growth
  Foundation*. University of Sydney infectious-disease affiliations resolve to
  *Taronga Conservation Society*. Each (authorship, institution) pair keeps the
  raw string it was resolved from, and two tests run on it: is it garbage
  (under four letters or boilerplate), and does it mention the institution
  at all — any distinctive word of its name, its initials, or any acronym,
  alternative or international name from its OpenAlex record. An institution
  failing either test on most of its authorships is `suspect`: kept as a
  node, given no edges. **168 institutions, 14,700 authorships.** Before the
  alias records it was 207 and 30,000, with CERN, CEA and IN2P3 wrongly
  caught — nobody writes "European Organization for Nuclear Research" in a
  byline. What remains are sinks: *Hunter Water* holding the University of
  Newcastle's Ourimbah campus, *St Vincents Institute of Medical Research*
  (Melbourne) holding St Vincent's Sydney, *Valongo Observatory* holding a
  different Rio de Janeiro lab.
- **Only 3.5% of references point inside the corpus** (378k of 10.8 M). The rest
  are stub ids for works outside it. `CITES` edges are corpus-internal; the
  full reference lists stay in DuckDB for expansion later.
- **The Space build container has less memory than the Space.** The image
  built fine locally under a 16 GB cap and was OOM-killed on Hugging Face at
  Neo4j's relationship-linking step: an uncapped `neo4j-admin import` sizes its
  JVM off the host. `HEAP_SIZE=1G --max-off-heap-memory=700m` fixed it.
- **A bare `initdb` gives you SQL_ASCII, and psycopg then returns bytes.** The
  first Space image loaded and indexed pgvector cleanly and answered every
  question with nobody: the work ids came back as `b'W4415108811'` and matched
  nothing in the graph. The local pgvector image initialises UTF-8 by default,
  which is why it never showed up before. `--encoding=UTF8 --locale=C.UTF-8`.
- **Authorships are capped at 100 per work** in the API response. The
  4,000-author physics papers are in the corpus with their first hundred.

## Deploying the demo

On a fresh Ubuntu 24.04 arm64 VM (Oracle's always-free Ampere A1 works):

```bash
curl -fsSL https://raw.githubusercontent.com/bolat-t/knowledge-graph-rag/main/deploy/vm/setup.sh | bash
```

That opens 80/443 in Oracle's iptables, installs Docker, builds the image on
the box from this repo and the public data bundle, and puts Caddy in front with
an automatic Let's Encrypt certificate on a free `sslip.io` hostname.


The `Dockerfile` builds one container for a Hugging Face Space: Ubuntu, a JRE,
Neo4j community unpacked from the tarball, Postgres 16 with pgvector, and the
app — all running as uid 1000. The data bundle (Neo4j import CSVs and the
embeddings as parquet, ~420 MB) lives in the public dataset
`bolat-t/kgrag-data`; the build downloads it, imports Neo4j, loads and indexes
pgvector, caches the embedding model, and throws the bundle away. A cold start
is just three services coming up. To test the build locally, put the bundle at
`deploy/bundle/` and the fetch step uses that instead.

```bash
docker build -t kgrag-space .
docker run -p 7860:7860 kgrag-space
```

## The picture

![One author's co-authorship neighbourhood](docs/ego_wide.png)

One author's co-authorship neighbourhood, drawn from the graph: filled circles
are Sydney-affiliated, hollow ones are everywhere else. `docs/ego.html` renders
`docs/ego.json` with d3-force; the JSON comes from two Cypher queries.

## Running it

```bash
cp .env.example .env               # add your mailto
docker compose up -d               # Neo4j on 7474/7687, Postgres on 5433
uv sync
uv run python -m kgrag.ingest_works          # resumable; per-year state files; ~1,100 requests
uv run python -m kgrag.ingest_institutions   # aliases for every institution with 20+ authorships
uv run python -m kgrag.shape                 # DuckDB tables + Neo4j CSVs
uv run python -m kgrag.load_neo4j     # stops Neo4j, bulk-imports, restarts, indexes
uv sync --extra embed
uv run python -m kgrag.embed          # abstracts -> pgvector (resumable, ~30 min on an M-series Mac)

uv sync --extra serve
uv run uvicorn kgrag.api:app --port 7860       # the web app, http://localhost:7860

uv run python -m kgrag.ask "spatial analysis of bushfire risk to homes" --from "Your Name"
uv run python -m kgrag.explore collaborators
uv run python -m kgrag.explore path "Kamal Dua" "Dacheng Tao"
uv run python -m kgrag.explore bridges "Computer Science" "Medicine"
```

## What it answers

```
$ uv run python -m kgrag.ask "spatial analysis of bushfire risk to homes using
    address-level data and census demographics" --from "Kamal Dua"

  closest works
   0.843  2024  Wildfire Loss Modeling: A Flexible Semiparametric Approach
   0.841  2021  Spatial Analysis, Interactive Visualisation and GIS-Based Dashboard …
   0.827  2021  Application of an Ensemble Statistical Approach in Spatial Predictions of Bushfire …

  people on those works, Sydney-affiliated since 2022  [0.15s]
    3 of 30  Sara Shirowzhan     UNSW          4 hops via Neha Jain, Perminder S. Sachdev, …
    2 of 30  Christopher Pettit  USyd / UNSW   3 hops via Gaurav Gupta, Rebecca Ivers
    2 of 30  Matthias M. Boer    WSU           3 hops via Philip M. Hansbro, Bradley Law
    2 of 30  Ross A. Bradstock   WSU           4 hops via Michelle L. Bell, Yuming Guo, …
```

The vector store found the papers; the graph turned them into people and a
path to each. Neither could have done the other's half.
