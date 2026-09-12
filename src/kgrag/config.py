"""Scope, paths and connection settings.

The corpus is every journal article since 2019 with at least one author at a
Sydney university. That is ~219,000 works -- large enough that the graph has
real structure, small enough to pull through the public API in an afternoon.
Co-authors, cited works and institutions outside Sydney come along as nodes,
which is the point: the interesting paths leave the city.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
DB_PATH = DATA / "kgrag.duckdb"


def _env(name: str, default: str) -> str:
    # .env is read without a dependency: one KEY=value per line.
    env_file = ROOT / ".env"
    if name not in os.environ and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return os.environ.get(name, default)


OPENALEX = "https://api.openalex.org"
MAILTO = _env("OPENALEX_MAILTO", "")

# OpenAlex institution ids. `lineage` filtering catches departments and
# affiliated institutes that roll up to each university.
SYDNEY_INSTITUTIONS = {
    "I31746571": "UNSW Sydney",
    "I129604602": "The University of Sydney",
    "I114017466": "University of Technology Sydney",
    "I99043593": "Macquarie University",
    "I63525965": "Western Sydney University",
}
YEAR_FROM = 2019
WORK_TYPE = "article"

# Only the fields the graph and the embeddings need. A full work record is
# 5-10 KB; this is about a third of that across 219,000 of them.
WORK_FIELDS = ",".join([
    "id", "doi", "title", "publication_year", "publication_date", "type",
    "language", "cited_by_count", "fwci", "is_retracted",
    "authorships", "referenced_works", "topics", "primary_topic",
    "abstract_inverted_index", "funders", "awards", "open_access",
    "primary_location", "keywords",
])

NEO4J_URI = _env("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = _env("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = _env("NEO4J_PASSWORD", "kgrag-local")
PG_DSN = _env("PG_DSN", "postgresql://kgrag:kgrag@localhost:5433/kgrag")


def ensure_dirs() -> None:
    for d in (RAW, PROCESSED):
        d.mkdir(parents=True, exist_ok=True)
