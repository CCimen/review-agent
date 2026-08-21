#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
POSTGRES_IMAGE="postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f"
CONTAINER="review-agent-postgres-contract-$$"

cleanup_postgres_contract() {
    docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
}

trap cleanup_postgres_contract EXIT HUP INT TERM

docker run \
    --detach \
    --rm \
    --name "$CONTAINER" \
    --env POSTGRES_PASSWORD=postgres \
    --env POSTGRES_DB=review_agent_test \
    "$POSTGRES_IMAGE" >/dev/null

attempt=0
until docker exec "$CONTAINER" \
    psql \
    --no-psqlrc \
    --username=postgres \
    --dbname=review_agent_test \
    --tuples-only \
    --quiet \
    --command "SELECT 1" >/dev/null 2>&1
do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs --tail 80 "$CONTAINER" >&2
        printf '%s\n' "PostgreSQL did not become query-ready." >&2
        exit 1
    fi
    sleep 1
done

PYTHONDONTWRITEBYTECODE=1 \
REVIEW_AGENT_POSTGRES_CONTAINER="$CONTAINER" \
    python3 -m unittest tests.test_postgres_schema

printf '%s\n' "PostgreSQL schema contract passed."
