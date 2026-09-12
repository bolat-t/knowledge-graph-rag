"""Raw OpenAlex JSONL -> DuckDB tables -> Neo4j import CSVs + an abstracts file.

DuckDB reads the gzipped JSONL directly and infers the nested structs, so the
whole reshaping is SQL. Everything the graph needs comes out as flat node and
edge tables, written as CSVs the way `neo4j-admin database import` wants them.

Modelling decisions, each of which is a trade-off:

* **Citations stay inside the corpus.** A work references 113 others on average
  and most are not Sydney papers. Loading every one as a stub node is ~10 M
  nodes and ~25 M edges for a laptop Neo4j; the within-corpus edges are the
  traversable ones anyway. The full reference lists stay in DuckDB for later.
* **Affiliation is per author, aggregated.** OpenAlex gives affiliation per
  authorship; the graph carries one AFFILIATED_WITH edge per (author,
  institution) with first year, last year and a paper count, which is what a
  "who is at UNSW" question actually wants.
* **Authors with no id are dropped from the graph** (5% of authorships). They
  keep their raw name on the authorship row in DuckDB, but a node without a
  key cannot be de-duplicated, and a graph of unresolvable people is worse
  than a graph with a stated gap.
* **The abstract is rebuilt from the inverted index** OpenAlex ships (a word ->
  positions map, for legal reasons). About 79% of works have one.
* **Mis-resolved institutions are quarantined.** OpenAlex's affiliation
  matcher has sinks: journal boilerplate ("Supplemental digital content is
  available...") resolves to *Apple (Israel)*, and real Sydney strings with a
  stray leading character ("ASchool of Medicine, Western Sydney University")
  resolve to *Human Growth Foundation*. Two tests on the raw string each pair
  was resolved from: is it garbage (under four letters, or boilerplate), and
  does it mention any distinctive word of the institution's name at all. An
  institution failing either test on most of its authorships is flagged
  `suspect`, kept as a node, and left out of every edge.
* **Institution edges are per work as well as per author.** AFFILIATED_WITH
  (author -> institution, aggregated) answers "who is at UNSW"; it cannot
  answer "how many papers do UNSW and Oxford share", because one author's
  edge reaches all of their papers. FROM_INSTITUTION (work -> institution) is
  the edge for that, and the first version of this project got the wrong
  answer by not having it.

    uv run python -m kgrag.shape
"""
from __future__ import annotations

import sys
import time

import duckdb

from .config import DB_PATH, PROCESSED, RAW, SYDNEY_INSTITUTIONS, ensure_dirs

PARTS = RAW / "works"
NEO = PROCESSED / "neo4j"
SYD = ", ".join(f"'https://openalex.org/{i}'" for i in SYDNEY_INSTITUTIONS)


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        create or replace table work_raw as
        select * from read_json('{(PARTS / "part_*.jsonl.gz").as_posix()}',
                                format='newline_delimited',
                                maximum_object_size=50000000, sample_size=5000)
        """
    )
    # Abstract from the inverted index: explode (word, positions), sort by
    # position, string_agg. One row per work.
    con.execute(
        """
        create or replace table work_abstract as
        with tok as (
            select id, e.key as word, unnest(e.value) as pos
            from (select id, unnest(map_entries(abstract_inverted_index)) as e
                  from work_raw where abstract_inverted_index is not null)
        )
        select id, string_agg(word, ' ' order by pos) as abstract
        from tok group by id
        """
    )
    con.execute(
        """
        create or replace table work as
        select w.id, w.doi, w.title, w.publication_year as year, w.publication_date,
               w.type, w.language, w.cited_by_count, w.fwci, w.is_retracted,
               w.open_access.is_oa as is_oa, w.open_access.oa_status,
               w.primary_location.source.id as source_id,
               w.primary_topic.id as primary_topic_id,
               len(w.referenced_works) as n_references,
               len(w.authorships) as n_authorships,
               a.abstract
        from work_raw w left join work_abstract a using (id)
        """
    )
    con.execute(
        """
        create or replace table authorship as
        select w.id as work_id, w.publication_year as year,
               a.author.id as author_id, a.author.display_name as author_name,
               a.author.orcid as orcid, a.author_position as position,
               a.is_corresponding, a.raw_author_name,
               list_transform(a.institutions, i -> i.id) as institution_ids,
               a.raw_affiliation_strings
        from (select id, publication_year, unnest(authorships) as a from work_raw) w
        """
    )
    con.execute(
        """
        create or replace table author as
        select author_id as id,
               arg_max(author_name, year) as name,      -- most recent spelling
               max(orcid) as orcid,
               count(*) as n_works, min(year) as first_year, max(year) as last_year
        from authorship where author_id is not null group by 1
        """
    )
    # One row per (authorship, institution) with the raw string OpenAlex
    # resolved it from, and two tests on that string. `garbage`: fewer than
    # four letters, or journal boilerplate. `mismatch`: none of the distinctive
    # words in the institution's name appear in it (generic words such as
    # "university" are ignored; a name with no distinctive word is not tested).
    con.execute(
        """
        create or replace table inst_raw as
        with x as (
            select work_id, i.id, i.display_name, i.ror, i.country_code, i.type,
                   lower(coalesce(array_to_string(a.raw_affiliation_strings, ' '), '')) as raw
            from (select id as work_id, unnest(authorships) as a from work_raw), unnest(a.institutions) as t(i)
        ),
        tok as (
            select *, list_filter(regexp_split_to_array(lower(display_name), '[^a-z]+'),
                        w -> length(w) >= 4 and w not in ('university','universität','universidad','universidade',
                              'universiti','institute','institut','instituto','school','college','hospital',
                              'centre','center','national','research','medical','health','sciences','science',
                              'technology','state','general','faculty','department','laboratory','foundation',
                              'academy','clinic','council','group','international','australia','australian',
                              'china','chinese','india','indian','united','federal','public','royal','city')) as toks
            from x
        )
        , acr as (
            -- CNRS is never written out in an affiliation string; its initials are.
            select *, list_aggregate(list_transform(
                        list_filter(regexp_split_to_array(lower(display_name), '[^a-z]+'),
                                    w -> length(w) >= 3 and w not in ('the','and','for','des','les','von','der','und')),
                        w -> w[1]), 'string_agg', '') as acronym
            from tok
        )
        select work_id, id, display_name, ror, country_code, type, raw,
               (length(regexp_replace(raw, '[^a-z]', '', 'g')) < 4
                or regexp_matches(raw, 'supplemental digital content|competing interests|sponsorships or|conflicts? of interest|available for this article')
               ) as garbage,
               (len(toks) > 0
                and not list_aggregate(list_transform(toks, t -> contains(raw, t)), 'bool_or')
                and not (length(acronym) >= 3 and regexp_matches(raw, '\\b' || acronym || '\\b'))
               ) as mismatch
        from acr
        """
    )
    con.execute(
        f"""
        create or replace table institution as
        select id, arg_max(display_name, 1) as name, max(ror) as ror,
               max(country_code) as country, max(type) as type,
               id in ({SYD}) as is_sydney,
               count(*) as n_authorships,
               round(avg(garbage::int), 3) as garbage_share,
               round(avg(mismatch::int), 3) as mismatch_share,
               (count(*) >= 20 and (avg(garbage::int) > 0.5 or avg(mismatch::int) > 0.8)) as suspect
        from inst_raw group by id
        """
    )
    con.execute(
        """
        create or replace table work_institution as
        select distinct r.work_id, r.id as institution_id
        from inst_raw r join institution i on i.id = r.id where not i.suspect
        """
    )
    con.execute(
        """
        create or replace table affiliation as
        select author_id, inst as institution_id,
               count(*) as n_works, min(year) as first_year, max(year) as last_year
        from (select author_id, year, unnest(institution_ids) as inst from authorship)
        join institution i on i.id = inst
        where author_id is not null and not i.suspect group by 1, 2
        """
    )
    con.execute(
        """
        create or replace table reference as
        select id as citing_id, unnest(referenced_works) as cited_id from work_raw
        """
    )
    con.execute(
        """
        create or replace table citation as     -- corpus-internal only
        select r.citing_id, r.cited_id from reference r
        join work w on w.id = r.cited_id
        """
    )
    con.execute(
        """
        create or replace table topic as
        select t.id, arg_max(t.display_name, 1) as name,
               max(t.subfield.display_name) as subfield, max(t.field.display_name) as field,
               max(t."domain".display_name) as "domain", count(*) as n_works
        from (select unnest(topics) as t from work_raw) group by t.id
        """
    )
    con.execute(
        """
        create or replace table work_topic as
        select id as work_id, t.id as topic_id, t.score,
               t.id = primary_topic.id as is_primary
        from (select id, primary_topic, unnest(topics) as t from work_raw)
        """
    )
    con.execute(
        """
        create or replace table funder as
        select f.id, arg_max(f.display_name, 1) as name, count(*) as n_works
        from (select unnest(funders) as f from work_raw) group by f.id
        """
    )
    con.execute(
        """
        create or replace table work_funder as
        select distinct id as work_id, f.id as funder_id
        from (select id, unnest(funders) as f from work_raw)
        """
    )
    con.execute(
        """
        create or replace table source as
        select primary_location.source.id as id,
               arg_max(primary_location.source.display_name, 1) as name,
               max(primary_location.source.type) as type, count(*) as n_works
        from work_raw where primary_location.source.id is not null group by 1
        """
    )


def export_neo4j(con: duckdb.DuckDBPyConnection) -> None:
    """CSV headers use neo4j-admin's `name:type` and `:ID(Label)` conventions."""
    NEO.mkdir(parents=True, exist_ok=True)
    out = lambda name, sql: con.execute(
        f"copy ({sql}) to '{(NEO / name).as_posix()}' (header, delimiter ',')"
    )
    # ids are shortened: 'https://openalex.org/W123' -> 'W123'
    out("work.csv", """
        select replace(id,'https://openalex.org/','') as "id:ID(Work)", doi, title,
               year as "year:int", cited_by_count as "cited_by_count:int",
               fwci as "fwci:double", is_oa as "is_oa:boolean",
               replace(primary_topic_id,'https://openalex.org/','') as primary_topic_id
        from work""")
    out("author.csv", """
        select replace(id,'https://openalex.org/','') as "id:ID(Author)", name, orcid,
               n_works as "n_works:int", first_year as "first_year:int", last_year as "last_year:int"
        from author""")
    out("institution.csv", """
        select replace(id,'https://openalex.org/','') as "id:ID(Institution)", name, ror, country, type,
               is_sydney as "is_sydney:boolean", n_authorships as "n_authorships:int",
               suspect as "suspect:boolean", garbage_share as "garbage_share:double",
               mismatch_share as "mismatch_share:double"
        from institution""")
    out("topic.csv", """
        select replace(id,'https://openalex.org/','') as "id:ID(Topic)", name, subfield, field, "domain",
               n_works as "n_works:int" from topic""")
    out("funder.csv", """
        select replace(id,'https://openalex.org/','') as "id:ID(Funder)", name, n_works as "n_works:int" from funder""")
    out("source.csv", """
        select replace(id,'https://openalex.org/','') as "id:ID(Source)", name, type, n_works as "n_works:int" from source""")

    out("authored.csv", """
        select replace(author_id,'https://openalex.org/','') as ":START_ID(Author)",
               replace(work_id,'https://openalex.org/','') as ":END_ID(Work)",
               position, is_corresponding as "is_corresponding:boolean"
        from authorship where author_id is not null""")
    out("affiliated_with.csv", """
        select replace(author_id,'https://openalex.org/','') as ":START_ID(Author)",
               replace(institution_id,'https://openalex.org/','') as ":END_ID(Institution)",
               n_works as "n_works:int", first_year as "first_year:int", last_year as "last_year:int"
        from affiliation""")
    out("from_institution.csv", """
        select replace(work_id,'https://openalex.org/','') as ":START_ID(Work)",
               replace(institution_id,'https://openalex.org/','') as ":END_ID(Institution)" from work_institution""")
    out("cites.csv", """
        select replace(citing_id,'https://openalex.org/','') as ":START_ID(Work)",
               replace(cited_id,'https://openalex.org/','') as ":END_ID(Work)" from citation""")
    out("has_topic.csv", """
        select replace(work_id,'https://openalex.org/','') as ":START_ID(Work)",
               replace(topic_id,'https://openalex.org/','') as ":END_ID(Topic)",
               score as "score:double", is_primary as "is_primary:boolean" from work_topic""")
    out("funded_by.csv", """
        select replace(work_id,'https://openalex.org/','') as ":START_ID(Work)",
               replace(funder_id,'https://openalex.org/','') as ":END_ID(Funder)" from work_funder""")
    out("published_in.csv", """
        select replace(id,'https://openalex.org/','') as ":START_ID(Work)",
               replace(source_id,'https://openalex.org/','') as ":END_ID(Source)"
        from work where source_id is not null""")


def report(con: duckdb.DuckDBPyConnection) -> None:
    print("\n  -- nodes")
    for t in ("work", "author", "institution", "topic", "funder", "source"):
        print(f"     {t:<12} {con.execute(f'select count(*) from {t}').fetchone()[0]:>10,}")
    print("  -- edges")
    for t in ("authorship", "affiliation", "work_institution", "citation", "work_topic", "work_funder"):
        print(f"     {t:<12} {con.execute(f'select count(*) from {t}').fetchone()[0]:>10,}")

    n, absn, nullauth, tot = con.execute(
        """select (select count(*) from work), (select count(abstract) from work),
                  (select count(*) filter (where author_id is null) from authorship),
                  (select count(*) from authorship)"""
    ).fetchone()
    refs, internal = con.execute("select (select count(*) from reference), (select count(*) from citation)").fetchone()
    print(f"\n  works with an abstract: {absn:,} / {n:,} ({100*absn/n:.1f}%)")
    print(f"  authorships without an author id: {nullauth:,} / {tot:,} ({100*nullauth/tot:.1f}%)")
    print(f"  references: {refs:,} total, {internal:,} inside the corpus ({100*internal/refs:.1f}%)")
    print("\n  -- suspect institutions (kept as nodes, no edges)")
    for name, n, g, m in con.execute(
        "select name, n_authorships, garbage_share, mismatch_share from institution where suspect order by n_authorships desc limit 10"
    ).fetchall():
        print(f"     {name[:40]:<40} {n:>8,} authorships   garbage {g:.0%}  name-mismatch {m:.0%}")
    ns, na = con.execute(
        "select count(*), sum(n_authorships) from institution where suspect"
    ).fetchone()
    print(f"     {ns} institutions, {na:,} authorships quarantined")


def main() -> int:
    ensure_dirs()
    con = duckdb.connect(str(DB_PATH))
    t = time.time()
    build(con)
    print(f"  shaped in {time.time() - t:.0f}s")
    report(con)
    t = time.time()
    export_neo4j(con)
    print(f"\n  neo4j CSVs written to {NEO.relative_to(PROCESSED.parent)}  [{time.time() - t:.0f}s]")
    con.execute("checkpoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
