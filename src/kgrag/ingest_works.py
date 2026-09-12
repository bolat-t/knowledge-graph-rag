"""Pull the corpus off the OpenAlex API into gzipped JSONL parts.

Cursor pagination is sequential by nature and each 200-work page takes about
five seconds with abstracts and authorships included -- ninety minutes for
the corpus. Publication years are disjoint filters, so one cursor per year
runs in its own thread: eight streams, no overlap, nothing to de-duplicate,
and the whole pull comfortably under the polite pool's 10 req/s.

Each year keeps its own state file with the last cursor, written only after
the part it covers is on disk, so a killed run resumes per year.

**The free tier is 1,000 requests a day**, not the 100k the older docs
describe: OpenAlex moved to credits in 2026 and every page costs one. At 200
works a page the corpus is ~1,100 requests, so a full pull is a two-day job
on the free tier, or a few cents on a paid key. A 429 past the quota carries a
`Retry-After` of the seconds until midnight UTC; sleeping through that in a
retry loop is not a retry, so anything over ten minutes aborts and says when
to come back.

    uv run python -m kgrag.ingest_works
"""
from __future__ import annotations

import gzip
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx

from .config import (
    MAILTO, OPENALEX, RAW, SYDNEY_INSTITUTIONS, WORK_FIELDS, WORK_TYPE, YEAR_FROM,
    ensure_dirs,
)

PARTS = RAW / "works"
PER_PAGE = 200
PAGES_PER_PART = 25            # 5,000 works per file
MAX_ATTEMPTS = 8
WORKERS = 4                    # 8 drew 429s within minutes; 4 has not
TIMEOUT = httpx.Timeout(90.0, connect=15.0)
YEARS = list(range(YEAR_FROM, date.today().year + 1))

class QuotaExhausted(RuntimeError):
    pass


_print_lock = threading.Lock()
_progress: dict[int, tuple[int, int]] = {}   # year -> (done, expected)


def filter_expr(year: int) -> str:
    inst = "|".join(SYDNEY_INSTITUTIONS)
    return (
        f"authorships.institutions.lineage:{inst},"
        f"publication_year:{year},type:{WORK_TYPE}"
    )


def state_path(year: int):
    return PARTS / f"state_{year}.json"


def load_state(year: int) -> dict:
    p = state_path(year)
    if p.exists():
        return json.loads(p.read_text())
    return {"cursor": "*", "pages": 0, "works": 0, "part": 0}


def fetch_page(client: httpx.Client, year: int, cursor: str) -> dict:
    params = {"filter": filter_expr(year), "select": WORK_FIELDS,
              "per-page": PER_PAGE, "cursor": cursor}
    if MAILTO:
        params["mailto"] = MAILTO
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = client.get(f"{OPENALEX}/works", params=params)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            if attempt == MAX_ATTEMPTS:
                raise
            # 429s carry Retry-After. A short one is a burst limit; a long one
            # is the daily quota, and there is nothing to do but come back.
            hdrs = getattr(getattr(e, "response", None), "headers", {})
            retry_after = hdrs.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else min(120, 3 * 2 ** attempt)
            if wait > 600:
                raise QuotaExhausted(
                    f"daily quota used (limit {hdrs.get('x-ratelimit-limit')}); "
                    f"resets in {wait // 3600}h {wait % 3600 // 60}m"
                ) from e
            with _print_lock:
                print(f"\n  [{year}] {type(e).__name__} {e} — retry {attempt}/{MAX_ATTEMPTS} in {wait}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def show_progress() -> None:
    with _print_lock:
        done = sum(d for d, _ in _progress.values())
        exp = sum(e for _, e in _progress.values())
        cells = "  ".join(f"{y}:{d:,}/{e:,}" for y, (d, e) in sorted(_progress.items()))
        print(f"  {done:>8,} / {exp:,}   {cells}", end="\r", flush=True)


def pull_year(year: int) -> int:
    state = load_state(year)
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": f"kgrag ({MAILTO})"}) as client:
        expected = fetch_page(client, year, "*")["meta"]["count"]
        _progress[year] = (state["works"], expected)
        if state["cursor"] is None:
            return state["works"]

        buf: list[dict] = []
        pages_in_part = 0
        while state["cursor"]:
            page = fetch_page(client, year, state["cursor"])
            results = page.get("results") or []
            buf.extend(results)
            state["works"] += len(results)
            state["pages"] += 1
            pages_in_part += 1
            state["cursor"] = page["meta"].get("next_cursor")
            if not results:
                state["cursor"] = None

            if pages_in_part >= PAGES_PER_PART or not state["cursor"]:
                if buf:
                    out = PARTS / f"part_{year}_{state['part']:03d}.jsonl.gz"
                    with gzip.open(out, "wt", encoding="utf-8") as f:
                        for w in buf:
                            f.write(json.dumps(w, separators=(",", ":")) + "\n")
                    state["part"] += 1
                buf, pages_in_part = [], 0
                state_path(year).write_text(json.dumps(state))   # after the part is on disk

            _progress[year] = (state["works"], expected)
            show_progress()
            time.sleep(0.1)
    return state["works"]


def main() -> int:
    ensure_dirs()
    PARTS.mkdir(parents=True, exist_ok=True)
    if not MAILTO:
        print("  WARNING: OPENALEX_MAILTO not set; requests will not be in the polite pool")
    print(f"  {len(YEARS)} years, {WORKERS} at a time: {YEARS[0]}–{YEARS[-1]}")
    t0 = time.time()
    failed: list[int] = []

    def safe(year: int) -> int:
        try:
            return pull_year(year)
        except QuotaExhausted as e:
            with _print_lock:
                print(f"\n  [{year}] stopped: {e}")
            failed.append(year)
            return load_state(year)["works"]
        except Exception as e:      # one year's failure must not kill the rest
            with _print_lock:
                print(f"\n  [{year}] gave up: {type(e).__name__} {e}")
            failed.append(year)
            return load_state(year)["works"]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        totals = list(ex.map(safe, YEARS))
    print(f"\n  {sum(totals):,} works on disk in {(time.time() - t0) / 60:.1f} min")
    if failed:
        print(f"  incomplete years (re-run to resume): {failed}")
        return 1
    print("  done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
