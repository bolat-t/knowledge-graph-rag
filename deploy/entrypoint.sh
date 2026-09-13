#!/usr/bin/env bash
# Cold start: bring up Postgres and Neo4j from the stores baked into the image,
# then serve. Both databases were loaded at build time; nothing is imported here.
set -euo pipefail

pg_ctl -D /data/pg -l /data/pg.log -o "-c listen_addresses=127.0.0.1 -p 5433 -c unix_socket_directories=/tmp" start
until pg_isready -h 127.0.0.1 -p 5433 -q; do sleep 1; done
echo "postgres up"

neo4j start
until curl -sf -o /dev/null http://127.0.0.1:7474; do sleep 2; done
echo "neo4j up"

exec uvicorn kgrag.api:app --host 0.0.0.0 --port 7860
