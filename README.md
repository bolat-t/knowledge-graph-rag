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

Building. Both stores loaded and answering on 73% of the corpus; the rest of the pull lands when OpenAlex's daily quota resets.

| Stage | State |
|---|---|
| OpenAlex pull → gzipped JSONL | ⏸ **160,334 / 218,984** — free tier is 1,000 requests/day, resumes tomorrow |
| Shape in DuckDB → node/edge tables | ✅ `shape` — 26 s |
| Bulk load → Neo4j | ✅ `load_neo4j` — 751k nodes, 4.2 M edges in 8 s |
| Abstracts → pgvector | ✅ `embed` — 126,777 abstracts, bge-small on MPS, 30 min; HNSW in 54 s |
| Hybrid query (vector → graph) | ✅ `ask` — vector top-k → people → shortest path to you |
| Graph-only questions | ✅ `explore` — collaborators, path, bridges, reach |
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
- **3.2% of authorships have no author id.** They keep their raw name on the
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
  (under four letters or boilerplate), and does it mention any distinctive
  word — or the initials — of the institution's name. An institution failing
  either on most of its authorships is `suspect`: kept as a node, given no
  edges. 207 institutions, 30,000 authorships. Some are false positives
  (CERN is only ever written "CERN"); the fix is the institution's real
  acronyms from the API, five requests, pending tomorrow's quota.
- **Only 3.4% of references point inside the corpus** (313k of 9.2 M). The rest
  are stub ids for works outside it. `CITES` edges are corpus-internal; the
  full reference lists stay in DuckDB for expansion later.
- **Authorships are capped at 100 per work** in the API response. The
  4,000-author physics papers are in the corpus with their first hundred.

## Running it

```bash
cp .env.example .env               # add your mailto
docker compose up -d               # Neo4j on 7474/7687, Postgres on 5433
uv sync
uv run python -m kgrag.ingest_works   # resumable; per-year state files
uv run python -m kgrag.shape          # DuckDB tables + Neo4j CSVs
uv run python -m kgrag.load_neo4j     # stops Neo4j, bulk-imports, restarts, indexes
uv sync --extra embed
uv run python -m kgrag.embed          # abstracts -> pgvector (resumable, ~30 min on an M-series Mac)

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
