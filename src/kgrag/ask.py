"""The hybrid question: who near me works on this, and how am I connected?

Three steps, each in the store built for it:

1. **pgvector** -- embed the question, pull the k works whose title+abstract
   sit closest to it. Similarity is the only thing that can turn a paragraph
   of prose into a set of papers.
2. **Neo4j** -- turn those works into people: every author on them with a
   current Sydney affiliation, ranked by how many of the matched works they
   are on. That is a two-hop traversal from a list of ids and takes
   milliseconds.
3. **Neo4j again** -- if you say who you are, the shortest co-authorship path
   from you to each of them: the warm intro. Neither store could do this
   alone; the vector store has no people, the graph has no prose.

    uv run python -m kgrag.ask "graph neural networks for drug discovery"
    uv run python -m kgrag.ask "..." --from "Kamal Dua"
    uv run python -m kgrag.ask "..." --k 40 --institution "UNSW Sydney"
"""
from __future__ import annotations

import argparse
import sys
import time

import psycopg
from neo4j import GraphDatabase
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from .config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, PG_DSN
from .embed import MODEL

# bge-en-v1.5 is trained with an instruction on the query side only.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

SIMILAR_WORKS = """
select id, title, year, 1 - (embedding <=> %s::vector) as score
from work_text order by embedding <=> %s::vector limit %s
"""

PEOPLE_ON_WORKS = """
unwind $ids as wid
match (w:Work {id: wid})<-[:AUTHORED]-(a:Author)-[af:AFFILIATED_WITH]->(i:Institution {is_sydney: true})
where af.last_year >= $since and ($inst is null or i.name = $inst)
with a, collect(distinct i.name) as insts, collect(distinct w) as ws
return a.id as id, a.name as name, insts as institutions, a.n_works as n_works,
       size(ws) as matched, [w in ws | w.title][..3] as titles
order by matched desc, n_works desc limit $limit
"""

# Name -> author node. Prefers the id with the most works, which is the
# right guess more often than not when a name has been split.
RESOLVE_AUTHOR = """
match (a:Author) where toLower(a.name) = toLower($name)
return a.id as id, a.name as name, a.n_works as n_works order by n_works desc limit 1
"""

SHORTEST_PATH = """
match (me:Author {id: $me}), (them:Author {id: $them})
match p = shortestPath((me)-[:AUTHORED*..8]-(them))
return length(p) / 2 as hops,
       [n in nodes(p) | case when n:Author then n.name else '(' + left(n.title, 60) + ')' end] as chain
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("question")
    ap.add_argument("--from", dest="me", help="your name as it appears on papers")
    ap.add_argument("--k", type=int, default=25, help="works to retrieve")
    ap.add_argument("--institution", help="restrict people to one Sydney institution")
    ap.add_argument("--since", type=int, default=2022, help="only affiliations active from this year")
    args = ap.parse_args()

    t0 = time.time()
    model = SentenceTransformer(MODEL, device="mps")
    qv = model.encode(QUERY_PREFIX + args.question, normalize_embeddings=True)

    pg = psycopg.connect(PG_DSN)
    register_vector(pg)
    works = pg.execute(SIMILAR_WORKS, (qv, qv, args.k)).fetchall()
    t1 = time.time()

    print(f"\n  closest works  [{t1 - t0:.1f}s incl. model load]")
    for wid, title, year, score in works[:8]:
        print(f"   {score:.3f}  {year}  {title[:90]}")
    if len(works) > 8:
        print(f"   … and {len(works) - 8} more")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    people = driver.execute_query(
        PEOPLE_ON_WORKS, ids=[w[0] for w in works], since=args.since,
        inst=args.institution, limit=12,
    ).records
    t2 = time.time()

    me = None
    if args.me:
        r = driver.execute_query(RESOLVE_AUTHOR, name=args.me).records
        if not r:
            print(f"\n  could not find an author named {args.me!r}")
        else:
            me = r[0]
            print(f"\n  you: {me['name']} ({me['id']}, {me['n_works']} works in corpus)")

    print(f"\n  people on those works, Sydney-affiliated since {args.since}  [{t2 - t1:.2f}s]")
    for p in people:
        inst = " / ".join(i.replace("The University of Sydney", "USyd").replace("University of Technology Sydney", "UTS")
                          .replace("UNSW Sydney", "UNSW").replace("Macquarie University", "Macquarie")
                          .replace("Western Sydney University", "WSU") for i in p["institutions"])
        line = f"   {p['matched']:>2} of {args.k}  {p['name']:<28} {inst[:24]:<24}"
        if me and me["id"] != p["id"]:
            sp = driver.execute_query(SHORTEST_PATH, me=me["id"], them=p["id"]).records
            if sp:
                hops = sp[0]["hops"]
                via = [n for n in sp[0]["chain"] if not n.startswith("(")][1:-1]
                line += f"  {hops} hop{'s' if hops != 1 else ''}" + (f" via {', '.join(via[:3])}" if via else " (co-author)")
            else:
                line += "  no path within 4 hops"
        print(line)
        print(f"              e.g. {p['titles'][0][:80]}")

    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
