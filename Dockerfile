# syntax=docker/dockerfile:1
# One container for Hugging Face Spaces: Neo4j + Postgres/pgvector + the app.
# Both databases are loaded during the build from the public dataset
# bolat-t/kgrag-data, so a cold start only starts services.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    NEO4J_HOME=/opt/neo4j HF_HOME=/data/hf \
    PATH=/opt/neo4j/bin:/usr/lib/postgresql/16/bin:/app/.venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gzip openjdk-21-jre-headless \
      postgresql-16 postgresql-16-pgvector \
      python3.12 python3.12-venv \
    && rm -rf /var/lib/apt/lists/*

# Neo4j community, unpacked rather than the docker image, so it can run as the
# same unprivileged user as everything else.
ARG NEO4J_VERSION=5.26.30
RUN curl -fsSL https://dist.neo4j.org/neo4j-community-${NEO4J_VERSION}-unix.tar.gz | tar -xz -C /opt \
    && mv /opt/neo4j-community-${NEO4J_VERSION} /opt/neo4j

# Spaces run the container as uid 1000.
RUN useradd -m -u 1000 app && mkdir -p /data /app && chown -R app:app /data /app /opt/neo4j
USER app
WORKDIR /app

# App + CPU torch (the default wheel drags in CUDA).
COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src ./src
RUN python3.12 -m venv .venv \
    && pip install --upgrade pip \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -e ".[embed,serve]" huggingface_hub

COPY --chown=app:app deploy ./deploy
# Data bundle: deploy/bundle/ if present in the context, else the public dataset repo.
RUN python deploy/fetch_bundle.py && rm -rf deploy/bundle

# Postgres: init, load, index, stop. Port 5433 to match the local compose.
RUN initdb -D /data/pg --auth=trust -U kgrag >/dev/null \
    && pg_ctl -D /data/pg -l /data/pg.log -o "-c listen_addresses=127.0.0.1 -p 5433" start \
    && until pg_isready -h 127.0.0.1 -p 5433 -q; do sleep 1; done \
    && createdb -h 127.0.0.1 -p 5433 -U kgrag kgrag \
    && python deploy/load_pg.py postgresql://kgrag@127.0.0.1:5433/kgrag /data/bundle/work_text.parquet \
    && pg_ctl -D /data/pg stop \
    && rm /data/bundle/work_text.parquet

# Neo4j: bulk import, set password, memory sized for a 16 GB Space.
RUN gunzip /data/bundle/neo4j/*.gz \
    && neo4j-admin database import full neo4j --overwrite-destination --id-type=string \
         --skip-bad-relationships --bad-tolerance=100000 \
         --nodes=Work=/data/bundle/neo4j/work.csv --nodes=Author=/data/bundle/neo4j/author.csv \
         --nodes=Institution=/data/bundle/neo4j/institution.csv --nodes=Topic=/data/bundle/neo4j/topic.csv \
         --nodes=Funder=/data/bundle/neo4j/funder.csv --nodes=Source=/data/bundle/neo4j/source.csv \
         --relationships=AUTHORED=/data/bundle/neo4j/authored.csv \
         --relationships=AFFILIATED_WITH=/data/bundle/neo4j/affiliated_with.csv \
         --relationships=FROM_INSTITUTION=/data/bundle/neo4j/from_institution.csv \
         --relationships=CITES=/data/bundle/neo4j/cites.csv \
         --relationships=HAS_TOPIC=/data/bundle/neo4j/has_topic.csv \
         --relationships=FUNDED_BY=/data/bundle/neo4j/funded_by.csv \
         --relationships=PUBLISHED_IN=/data/bundle/neo4j/published_in.csv \
    && neo4j-admin dbms set-initial-password kgrag-local \
    && rm -rf /data/bundle/neo4j \
    && printf 'server.memory.heap.initial_size=1G\nserver.memory.heap.max_size=1500M\nserver.memory.pagecache.size=1G\nserver.default_listen_address=127.0.0.1\nserver.http.listen_address=127.0.0.1:7474\nserver.bolt.listen_address=127.0.0.1:7687\ndbms.security.auth_enabled=true\n' >> /opt/neo4j/conf/neo4j.conf

# Indexes and constraints need a running server; do it once here.
RUN neo4j start && until curl -sf -o /dev/null http://127.0.0.1:7474; do sleep 2; done \
    && python deploy/indexes.py && neo4j stop

# Embedding model cached in the image.
RUN python -c "from sentence_transformers import SentenceTransformer as S; S('BAAI/bge-small-en-v1.5', device='cpu')"

COPY --chown=app:app web ./web
ENV NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=kgrag-local \
    PG_DSN=postgresql://kgrag@127.0.0.1:5433/kgrag
EXPOSE 7860
CMD ["bash", "deploy/entrypoint.sh"]
