#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
POSTGRES_IMAGE="postgres:17.10-bookworm@sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f"
CONTAINER="review-agent-postgres-contract-$$"
RESTORE_CONTAINER="review-agent-postgres-restore-$$"

cleanup_postgres_contract() {
    docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
    docker rm --force "$RESTORE_CONTAINER" >/dev/null 2>&1 || true
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
        tests.test_postgres_feedback \
        tests.test_postgres_suggestions_decisions \
        tests.test_postgres_verification_coaching \
        tests.test_postgres_reporting_cli

# Seed one stable application row so backup/restore proves domain state, not
# merely that the migration ledger can be copied.
docker exec "$CONTAINER" \
    psql \
    --no-psqlrc \
    --set ON_ERROR_STOP=1 \
    --username=postgres \
    --dbname=review_agent_migration_test \
    --command "
        INSERT INTO review_agent.repositories (
            provider, provider_repository_id, owner, name, full_name,
            created_at, updated_at
        ) VALUES (
            'github', 987654321, 'recovery', 'probe', 'recovery/probe',
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        ON CONFLICT ON CONSTRAINT repositories_provider_identity_uk
        DO UPDATE SET full_name = EXCLUDED.full_name,
                      owner = EXCLUDED.owner,
                      name = EXCLUDED.name,
                      updated_at = CURRENT_TIMESTAMP;
    " >/dev/null

docker run \
    --detach \
    --rm \
    --name "$RESTORE_CONTAINER" \
    --publish 127.0.0.1::5432 \
    --env POSTGRES_PASSWORD=postgres \
    --env POSTGRES_DB=review_agent_restore \
    "$POSTGRES_IMAGE" >/dev/null

attempt=0
until docker exec "$RESTORE_CONTAINER" \
    psql \
    --no-psqlrc \
    --username=postgres \
    --dbname=review_agent_restore \
    --tuples-only \
    --quiet \
    --command "SELECT 1" >/dev/null 2>&1
do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs --tail 80 "$RESTORE_CONTAINER" >&2
        printf '%s\n' "Restore PostgreSQL did not become query-ready." >&2
        exit 1
    fi
    sleep 1
done

docker exec "$CONTAINER" \
    pg_dump \
    --username=postgres \
    --dbname=review_agent_migration_test \
    --format=custom | docker exec --interactive "$RESTORE_CONTAINER" \
    pg_restore \
    --username=postgres \
    --dbname=review_agent_restore \
    --exit-on-error

RESTORE_HOST_PORT=$(docker port "$RESTORE_CONTAINER" 5432/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')
if [ -z "$RESTORE_HOST_PORT" ]; then
    printf '%s\n' "Restore PostgreSQL loopback port was not assigned." >&2
    exit 1
fi

PYTHONDONTWRITEBYTECODE=1 \
REVIEW_AGENT_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:$RESTORE_HOST_PORT/review_agent_restore" \
    python3 tools/review_agent_database.py migrate
PYTHONDONTWRITEBYTECODE=1 \
REVIEW_AGENT_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:$RESTORE_HOST_PORT/review_agent_restore" \
    python3 tools/review_agent_database.py ready

RESTORED_MIGRATIONS=$(docker exec "$RESTORE_CONTAINER" \
    psql \
    --no-psqlrc \
    --username=postgres \
    --dbname=review_agent_restore \
    --tuples-only \
    --quiet \
    --command "SELECT count(*) FROM review_agent.schema_migrations")
if [ "$(printf '%s' "$RESTORED_MIGRATIONS" | tr -d '[:space:]')" -lt 1 ]; then
    printf '%s\n' "Restored database has no migration ledger." >&2
    exit 1
fi

RESTORED_CANARY=$(docker exec "$RESTORE_CONTAINER" \
    psql \
    --no-psqlrc \
    --username=postgres \
    --dbname=review_agent_restore \
    --tuples-only \
    --quiet \
    --command "
        SELECT full_name
        FROM review_agent.repositories
        WHERE provider = 'github' AND provider_repository_id = 987654321
    ")
if [ "$(printf '%s' "$RESTORED_CANARY" | tr -d '[:space:]')" != "recovery/probe" ]; then
    printf '%s\n' "Restored database is missing the application-state canary." >&2
    exit 1
fi

printf '%s\n' "PostgreSQL schema and recovery contract passed."
