"""Graph-only questions -- the ones similarity search cannot express.

    uv run python -m kgrag.explore collaborators            # who Sydney publishes with
    uv run python -m kgrag.explore path "Kamal Dua" "Dacheng Tao"
    uv run python -m kgrag.explore bridges "Computer Science" "Medicine"
    uv run python -m kgrag.explore reach "Kamal Dua"        # how much of Sydney is within 2 hops
"""
from __future__ import annotations

import sys
import time

from neo4j import GraphDatabase

from .config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

Q = {
    "collaborators": """
        match (s:Institution {is_sydney: true})<-[:FROM_INSTITUTION]-(w:Work)-[:FROM_INSTITUTION]->(o:Institution)
        where not o.is_sydney
        with o, count(distinct w) as papers order by papers desc limit 15
        return o.name as institution, o.country as country, papers
    """,
    "resolve": """
        match (a:Author) where toLower(a.name) = toLower($name)
        return a.id as id, a.name as name, a.n_works as n_works order by n_works desc limit 1
    """,
    "path": """
        match (a:Author {id: $a}), (b:Author {id: $b})
        match p = shortestPath((a)-[:AUTHORED*..10]-(b))
        return length(p) / 2 as hops,
               [n in nodes(p) | case when n:Author then n.name else '[' + left(n.title, 50) + ']' end] as chain
    """,
    # Authors who publish in both fields, ranked by the smaller side, so a
    # cardiologist with one ML paper does not outrank someone who actually
    # works across the line.
    "bridges": """
        match (a:Author)-[:AUTHORED]->(w:Work)-[:HAS_TOPIC {is_primary: true}]->(t:Topic)
        where t.field in [$f1, $f2]
        with a, sum(case when t.field = $f1 then 1 else 0 end) as n1,
                sum(case when t.field = $f2 then 1 else 0 end) as n2
        where n1 >= 3 and n2 >= 3
        match (a)-[:AFFILIATED_WITH]->(i:Institution {is_sydney: true})
        with a, n1, n2, collect(distinct i.name)[0] as inst
        return a.name as name, inst as institution, n1, n2, apoc_min(n1, n2) as both
        order by both desc, n1 + n2 desc limit 15
    """,
    "reach": """
        match (me:Author {id: $a})
        match (me)-[:AUTHORED]->(:Work)<-[:AUTHORED]-(c1:Author) where c1 <> me
        with me, collect(distinct c1) as hop1
        unwind hop1 as c1
        match (c1)-[:AUTHORED]->(:Work)<-[:AUTHORED]-(c2:Author)
        where c2 <> me and not c2 in hop1
        with me, size(hop1) as one_hop, collect(distinct c2) as hop2
        match (s:Author)-[:AFFILIATED_WITH]->(:Institution {is_sydney: true})
        with one_hop, hop2, count(distinct s) as sydney_authors
        return one_hop, size(hop2) as two_hops,
               size([x in hop2 where exists((x)-[:AFFILIATED_WITH]->(:Institution {is_sydney: true}))]) as two_hop_sydney,
               sydney_authors
    """,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("collaborators", "path", "bridges", "reach"):
        print(__doc__)
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    t = time.time()

    def resolve(name):
        r = d.execute_query(Q["resolve"], name=name).records
        if not r:
            raise SystemExit(f"no author named {name!r}")
        return r[0]

    if cmd == "collaborators":
        rows = d.execute_query(Q["collaborators"]).records
        print("\n  institutions sharing the most papers with a Sydney university")
        for r in rows:
            print(f"   {r['papers']:>6,}  {r['institution'][:48]:<48} {r['country']}")

    elif cmd == "path":
        a, b = resolve(args[0]), resolve(args[1])
        rows = d.execute_query(Q["path"], a=a["id"], b=b["id"]).records
        if not rows:
            print(f"\n  no co-authorship path between {a['name']} and {b['name']} within 5 hops")
        else:
            print(f"\n  {a['name']} -> {b['name']}: {rows[0]['hops']} hops")
            for n in rows[0]["chain"]:
                print(f"     {'  ' if n.startswith('[') else ''}{n}")

    elif cmd == "bridges":
        q = Q["bridges"].replace("apoc_min(n1, n2)", "case when n1 < n2 then n1 else n2 end")
        rows = d.execute_query(q, f1=args[0], f2=args[1]).records
        print(f"\n  Sydney authors publishing in both {args[0]} and {args[1]} (primary topic, >=3 each)")
        print(f"   {'':<28} {'':<26} {args[0][:12]:>12} {args[1][:12]:>12}")
        for r in rows:
            print(f"   {r['name']:<28} {r['institution'][:26]:<26} {r['n1']:>12} {r['n2']:>12}")

    elif cmd == "reach":
        a = resolve(args[0])
        r = d.execute_query(Q["reach"], a=a["id"]).records[0]
        print(f"\n  {a['name']}: {r['one_hop']:,} co-authors; {r['two_hops']:,} people within two hops,")
        print(f"  {r['two_hop_sydney']:,} of them Sydney-affiliated — {100 * r['two_hop_sydney'] / r['sydney_authors']:.1f}% of the {r['sydney_authors']:,} Sydney authors in the corpus")

    print(f"\n  [{time.time() - t:.2f}s]")
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
