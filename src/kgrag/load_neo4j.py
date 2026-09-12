"""Bulk-load the shaped CSVs into Neo4j and put constraints on top.

`neo4j-admin database import` is the fast path -- a few million edges in
seconds rather than the tens of minutes LOAD CSV takes -- but it writes the
store files directly, so the server has to be stopped while it runs. Community
edition has one database, so this overwrites it. The sequence is: stop, import
into a fresh store, start, then create the constraints and indexes online.

Docker's CLI on this machine lives inside Docker.app; DOCKER_BIN overrides.

    uv run python -m kgrag.load_neo4j
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
from neo4j import GraphDatabase

from .config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, PROCESSED, ROOT

DOCKER = os.environ.get("DOCKER_BIN", "/Applications/Docker.app/Contents/Resources/bin/docker")
NEO = PROCESSED / "neo4j"

NODES = {
    "Work": "work.csv", "Author": "author.csv", "Institution": "institution.csv",
    "Topic": "topic.csv", "Funder": "funder.csv", "Source": "source.csv",
}
RELS = {
    "AUTHORED": "authored.csv", "AFFILIATED_WITH": "affiliated_with.csv",
    "FROM_INSTITUTION": "from_institution.csv",
    "CITES": "cites.csv", "HAS_TOPIC": "has_topic.csv",
    "FUNDED_BY": "funded_by.csv", "PUBLISHED_IN": "published_in.csv",
}

CONSTRAINTS = [
    "create constraint work_id if not exists for (n:Work) require n.id is unique",
    "create constraint author_id if not exists for (n:Author) require n.id is unique",
    "create constraint institution_id if not exists for (n:Institution) require n.id is unique",
    "create constraint topic_id if not exists for (n:Topic) require n.id is unique",
    "create constraint funder_id if not exists for (n:Funder) require n.id is unique",
    "create constraint source_id if not exists for (n:Source) require n.id is unique",
    "create index author_name if not exists for (n:Author) on (n.name)",
    "create index institution_name if not exists for (n:Institution) on (n.name)",
    "create index work_year if not exists for (n:Work) on (n.year)",
    "create fulltext index work_title if not exists for (n:Work) on each [n.title]",
    "create fulltext index author_fullname if not exists for (n:Author) on each [n.name]",
]


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": os.path.dirname(DOCKER) + ":" + os.environ.get("PATH", "")}
    return subprocess.run([DOCKER, "compose", *args], cwd=ROOT, env=env, check=check,
                          capture_output=True, text=True)


def wait_for_bolt(timeout: int = 120) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get("http://localhost:7474", timeout=3).status_code == 200:
                with GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)) as d:
                    d.execute_query("return 1")
                return
        except Exception:
            pass
        time.sleep(3)
    raise SystemExit("neo4j did not come up")


def main() -> int:
    missing = [f for f in [*NODES.values(), *RELS.values()] if not (NEO / f).exists()]
    if missing:
        raise SystemExit(f"missing CSVs {missing} — run: uv run python -m kgrag.shape")

    print("  stopping neo4j...")
    compose("stop", "neo4j")

    # The database name goes before the options: --relationships takes a
    # variable number of values and would swallow it as another file.
    cmd = ["run", "--rm", "--no-deps", "-e", "NEO4J_PLUGINS=[]", "neo4j",
           "neo4j-admin", "database", "import", "full", "neo4j", "--overwrite-destination",
           "--id-type=string", "--skip-bad-relationships", "--bad-tolerance=100000",
           "--report-file=/import/import.report"]
    cmd += [f"--nodes={label}=/import/{f}" for label, f in NODES.items()]
    cmd += [f"--relationships={rel}=/import/{f}" for rel, f in RELS.items()]

    print("  importing (this rewrites the whole database)...")
    t = time.time()
    r = compose(*cmd, check=False)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
    print("    " + tail.replace("\n", "\n    "))
    if r.returncode != 0:
        raise SystemExit("import failed")
    print(f"  imported in {time.time() - t:.0f}s")

    print("  starting neo4j...")
    compose("start", "neo4j")
    wait_for_bolt()

    with GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)) as d:
        for c in CONSTRAINTS:
            d.execute_query(c)
        d.execute_query("call db.awaitIndexes(600)")
        print("\n  -- node counts")
        for rec in d.execute_query(
            "match (n) return labels(n)[0] as label, count(*) as n order by n desc"
        ).records:
            print(f"     {rec['label']:<12} {rec['n']:>10,}")
        print("  -- relationship counts")
        for rec in d.execute_query(
            "match ()-[r]->() return type(r) as type, count(*) as n order by n desc"
        ).records:
            print(f"     {rec['type']:<16} {rec['n']:>10,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
