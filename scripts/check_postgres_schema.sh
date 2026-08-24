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
    --publish 127.0.0.1::5432 \
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

docker exec "$CONTAINER" \
    createdb --username=postgres review_agent_migration_test
HOST_PORT=$(docker port "$CONTAINER" 5432/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')
if [ -z "$HOST_PORT" ]; then
    printf '%s\n' "PostgreSQL loopback port was not assigned." >&2
    exit 1
fi

PYTHONDONTWRITEBYTECODE=1 \
REVIEW_AGENT_POSTGRES_CONTAINER="$CONTAINER" \
REVIEW_AGENT_POSTGRES_DSN="postgresql://postgres:postgres@127.0.0.1:$HOST_PORT/review_agent_migration_test" \
    python3 -m unittest \
        tests.test_postgres_schema \
        tests.test_postgres_migrations \
        tests.test_postgres_runtime \
        tests.test_postgres_review_lifecycle \
        tests.test_postgres_coverage \
        tests.test_postgres_findings \
        tests.test_postgres_publications \
        tests.test_postgres_suggestions_decisions \
        tests.test_postgres_verification_coaching

printf '%s\n' "PostgreSQL schema contract passed."
