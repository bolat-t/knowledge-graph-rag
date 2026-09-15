"""HTTP front for `ask` and `explore`, plus the static page that calls it.

One process, one model in memory, one driver each to Neo4j and Postgres.
Everything the CLI can do is an endpoint here; the page under /web is the
only client. Runs on 7860 because Hugging Face Spaces expects that.

    uv run uvicorn kgrag.api:app --port 7860 --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from neo4j import GraphDatabase
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from .ask import PEOPLE_ON_WORKS, QUERY_PREFIX, RESOLVE_AUTHOR, SHORTEST_PATH, SIMILAR_WORKS
from .config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, PG_DSN, ROOT
from .embed import MODEL
from .explore import Q as EXPLORE

WEB = ROOT / "web"
STATE: dict = {}

EGO_NEIGHBOURS = """
match (me:Author {id: $id})-[:AUTHORED]->(w:Work)<-[:AUTHORED]-(c:Author)
with c, count(w) as shared order by shared desc limit $limit
optional match (c)-[af:AFFILIATED_WITH]->(i:Institution)
with c, shared, i, af order by i.is_sydney desc, af.n_works desc
with c, shared, collect(i)[0] as inst
return c.id as id, c.name as name, shared,
       coalesce(inst.is_sydney, false) as sydney, inst.name as institution, c.n_works as n_works
"""
EGO_LINKS = """
unwind $ids as x unwind $ids as y with x, y where x < y
match (a:Author {id: x})-[:AUTHORED]->(w:Work)<-[:AUTHORED]-(b:Author {id: y})
return x as s, y as t, count(w) as n
"""
AUTHOR_BY_ID = """
match (a:Author {id: $id})
optional match (a)-[:AFFILIATED_WITH]->(i:Institution) with a, i order by i.is_sydney desc, i.n_authorships desc
return a.id as id, a.name as name, a.n_works as n_works, collect(i.name)[..3] as institutions
"""
SEARCH_AUTHORS = """
call db.index.fulltext.queryNodes('author_fullname', $q) yield node, score
with node order by score desc, node.n_works desc limit 8
optional match (node)-[:AFFILIATED_WITH]->(i:Institution {is_sydney: true})
return node.id as id, node.name as name, node.n_works as n_works, collect(distinct i.name)[..2] as institutions
"""
STATS = """
match (n) with labels(n)[0] as l, count(*) as c return collect({label: l, n: c}) as nodes
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE["model"] = SentenceTransformer(MODEL, device="cpu")
    STATE["neo4j"] = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    STATE["pg"] = psycopg.connect(PG_DSN, autocommit=True)
    register_vector(STATE["pg"])
    yield
    STATE["neo4j"].close()
    STATE["pg"].close()


app = FastAPI(title="Sydney research graph", lifespan=lifespan)


def cypher(query: str, **params):
    return [dict(r) for r in STATE["neo4j"].execute_query(query, **params).records]


def short_inst(name: str) -> str:
    return (name.replace("The University of Sydney", "USyd").replace("University of Technology Sydney", "UTS")
            .replace("UNSW Sydney", "UNSW").replace("Macquarie University", "Macquarie")
            .replace("Western Sydney University", "WSU"))


@app.get("/api/health")
def health():
    try:
        cypher("return 1")
        STATE["pg"].execute("select 1")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, str(e))


@app.get("/api/stats")
def stats():
    nodes = cypher(STATS)[0]["nodes"]
    rels = cypher("match ()-[r]->() return count(r) as n")[0]["n"]
    vecs = STATE["pg"].execute("select count(*) from work_text").fetchone()[0]
    return {"nodes": {x["label"]: x["n"] for x in nodes}, "relationships": rels, "abstracts": vecs}


@app.get("/api/authors")
def authors(q: str = Query(min_length=2)):
    # Fulltext query syntax: quote the phrase, add a fuzzy fallback.
    safe = q.replace('"', "")
    rows = cypher(SEARCH_AUTHORS, q=f'"{safe}" OR {safe}~')
    for r in rows:
        r["institutions"] = [short_inst(i) for i in r["institutions"]]
    return rows


@app.get("/api/ask")
def ask(q: str = Query(min_length=3), me: str | None = None, k: int = 25,
        institution: str | None = None, since: int = 2022):
    qv = STATE["model"].encode(QUERY_PREFIX + q, normalize_embeddings=True)
    works = STATE["pg"].execute(SIMILAR_WORKS, (qv, qv, k)).fetchall()
    people = cypher(PEOPLE_ON_WORKS, ids=[w[0] for w in works], since=since, inst=institution, limit=12)

    me_row = None
    if me:
        r = cypher(RESOLVE_AUTHOR, name=me)
        me_row = r[0] if r else None

    for p in people:
        p["institutions"] = [short_inst(i) for i in p["institutions"]]
        p["path"] = None
        if me_row and me_row["id"] != p["id"]:
            sp = cypher(SHORTEST_PATH, me=me_row["id"], them=p["id"])
            if sp:
                chain = sp[0]["chain"]
                p["path"] = {"hops": sp[0]["hops"],
                             "via": [n for n in chain if not n.startswith("(")][1:-1],
                             "chain": chain}
    return {
        "question": q,
        "me": me_row,
        "works": [{"id": w[0], "title": w[1], "year": w[2], "score": round(w[3], 3)} for w in works],
        "people": people,
    }


@app.get("/api/path")
def path(a: str, b: str):
    ra, rb = cypher(EXPLORE["resolve"], name=a), cypher(EXPLORE["resolve"], name=b)
    if not ra or not rb:
        raise HTTPException(404, f"unknown author: {a if not ra else b}")
    rows = cypher(EXPLORE["path"], a=ra[0]["id"], b=rb[0]["id"])
    return {"a": ra[0], "b": rb[0], "hops": rows[0]["hops"] if rows else None,
            "chain": rows[0]["chain"] if rows else []}


@app.get("/api/bridges")
def bridges(f1: str, f2: str):
    q = EXPLORE["bridges"].replace("apoc_min(n1, n2)", "case when n1 < n2 then n1 else n2 end")
    rows = cypher(q, f1=f1, f2=f2)
    for r in rows:
        r["institution"] = short_inst(r["institution"])
    return rows


@app.get("/api/collaborators")
def collaborators():
    return cypher(EXPLORE["collaborators"])


@app.get("/api/ego")
def ego(id: str, limit: int = 50):
    me = cypher(AUTHOR_BY_ID, id=id)
    if not me:
        raise HTTPException(404, "unknown author id")
    me = me[0]
    nodes = cypher(EGO_NEIGHBOURS, id=id, limit=limit)
    links = cypher(EGO_LINKS, ids=[n["id"] for n in nodes])
    return {
        "centre": me,
        "nodes": [{"id": me["id"], "name": me["name"], "shared": 0, "sydney": True, "me": True}] + nodes,
        "links": [{"s": me["id"], "t": n["id"], "n": n["shared"]} for n in nodes] + links,
    }


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/web", StaticFiles(directory=WEB), name="web")
