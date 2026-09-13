"""Fetch institution records for every institution that matters, for their aliases.

The name-mismatch test in `shape` asks whether an affiliation string mentions
the institution it was resolved to. CERN is never written "European
Organization for Nuclear Research" in a byline; it is written "CERN". OpenAlex
keeps each institution's acronyms, alternative names and international names
on the institution record, which the works endpoint does not return. Fifty ids
a request, so every institution with twenty or more authorships is ~150
requests -- a fraction of the daily quota.

    uv run python -m kgrag.ingest_institutions
"""
from __future__ import annotations

import json
import sys
import time

import duckdb
import httpx

from .config import DB_PATH, MAILTO, OPENALEX, RAW, ensure_dirs

OUT = RAW / "institutions.jsonl"
MIN_AUTHORSHIPS = 20
BATCH = 50
FIELDS = "id,display_name,display_name_acronyms,display_name_alternatives,international,country_code,type,ror"


def main() -> int:
    ensure_dirs()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    ids = [r[0].replace("https://openalex.org/", "") for r in con.execute(
        "select id from institution where n_authorships >= ? order by n_authorships desc", [MIN_AUTHORSHIPS]
    ).fetchall()]

    have = set()
    if OUT.exists():
        have = {json.loads(l)["id"].replace("https://openalex.org/", "") for l in OUT.open()}
    todo = [i for i in ids if i not in have]
    print(f"  {len(ids):,} institutions with >= {MIN_AUTHORSHIPS} authorships, {len(have):,} on disk, {len(todo):,} to fetch")

    n = 0
    with httpx.Client(timeout=60) as client, OUT.open("a") as f:
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            params = {"filter": "ids.openalex:" + "|".join(batch), "select": FIELDS, "per-page": BATCH}
            if MAILTO:
                params["mailto"] = MAILTO
            for attempt in range(1, 6):
                r = client.get(f"{OPENALEX}/institutions", params=params)
                if r.status_code == 200:
                    break
                if r.status_code == 429 and int(r.headers.get("Retry-After", "0") or 0) > 600:
                    raise SystemExit("  daily quota used; re-run tomorrow (progress is on disk)")
                time.sleep(min(60, 3 * 2 ** attempt))
            r.raise_for_status()
            for rec in r.json()["results"]:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                n += 1
            f.flush()
            print(f"  {n:,} / {len(todo):,}", end="\r", flush=True)
            time.sleep(0.25)
    print(f"\n  wrote {n:,} institution records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
