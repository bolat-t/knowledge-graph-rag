"""Constraints and indexes, applied once at build against the imported store."""
from neo4j import GraphDatabase
from kgrag.load_neo4j import CONSTRAINTS

with GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j", "kgrag-local")) as d:
    for c in CONSTRAINTS:
        d.execute_query(c)
    d.execute_query("call db.awaitIndexes(600)")
    n = d.execute_query("match (n) return count(n) as n").records[0]["n"]
    print(f"  {n:,} nodes, indexes online", flush=True)
