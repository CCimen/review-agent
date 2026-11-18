"""Apply the review-agent PostgreSQL schema under one checksum-locked transaction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow


MIGRATION_DIRECTORY = Path(__file__).resolve().parent
_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9][a-z0-9_]*\.sql$")
_MIGRATION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"review-agent:postgres-migrations:v1").digest()[:8],
    byteorder="big",
    signed=True,
)


class MigrationError(RuntimeError):
    """A migration set is invalid, has drifted, or could not be applied."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: bytes


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    applied_version: int
    pending_versions: tuple[int, ...]
    database_ahead: bool


def discover_migrations(directory: Path = MIGRATION_DIRECTORY) -> tuple[Migration, ...]:
    if not directory.is_dir():
        raise MigrationError(f"migration directory does not exist: {directory}")

    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        content = path.read_bytes()
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"migration is not UTF-8: {path.name}") from exc
        if not content.strip():
            raise MigrationError(f"migration is empty: {path.name}")
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=path.name,
                checksum=hashlib.sha256(content).hexdigest(),
                sql=content,
            )
        )

    if not migrations:
        raise MigrationError(f"no migrations found in: {directory}")
    versions = tuple(migration.version for migration in migrations)
    expected = tuple(range(1, len(migrations) + 1))
    if versions != expected:
        raise MigrationError(
            "migration versions must be unique and contiguous from 001: "
            + ", ".join(f"{version:03d}" for version in versions)
        )
    return tuple(migrations)


def _create_ledger(connection: psycopg.Connection[TupleRow]) -> None:
    connection.execute("CREATE SCHEMA IF NOT EXISTS review_agent")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS review_agent.schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT schema_migrations_version_ck CHECK (version > 0),
            CONSTRAINT schema_migrations_name_ck CHECK (btrim(name) <> ''),
            CONSTRAINT schema_migrations_checksum_ck
                CHECK (checksum ~ '^[0-9a-f]{64}$')
        )
        """
    )


def _applied_migrations(
    connection: psycopg.Connection[TupleRow],
) -> dict[int, tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT version, name, checksum
        FROM review_agent.schema_migrations
        ORDER BY version
        """
    ).fetchall()
    applied: dict[int, tuple[str, str]] = {}
    for version, name, checksum in rows:
        if not isinstance(version, int) or not isinstance(name, str) or not isinstance(
            checksum, str
        ):
            raise MigrationError("migration ledger contains invalid values")
        applied[version] = (name, checksum)
    return applied


def _verify_applied(
    migrations: tuple[Migration, ...], applied: dict[int, tuple[str, str]]
) -> None:
    applied_versions = tuple(applied)
    expected_versions = tuple(range(1, len(applied) + 1))
    if applied_versions != expected_versions:
        raise MigrationError(
            "migration ledger versions must be contiguous from 001: "
            + ", ".join(f"{version:03d}" for version in applied_versions)
        )

    available = {migration.version: migration for migration in migrations}
    for version, (name, checksum) in applied.items():
        migration = available.get(version)
        if migration is None:
            continue
        if migration.name != name:
            raise MigrationError(
                f"migration name mismatch for {version:03d}: {name} != {migration.name}"
            )
        if migration.checksum != checksum:
            raise MigrationError(f"checksum mismatch for {migration.name}")


def inspect_migrations(
    connection: psycopg.Connection[TupleRow],
    *,
    directory: Path = MIGRATION_DIRECTORY,
) -> MigrationStatus:
    """Inspect migration health without creating or changing database objects."""
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise MigrationError("migration inspection requires an idle PostgreSQL connection")
    migrations = discover_migrations(directory)
    try:
        with connection.transaction():
            lock = connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,)
            ).fetchone()
            if lock != (True,):
                raise MigrationError("PostgreSQL migrations are currently running")
            ledger = connection.execute(
                "SELECT to_regclass('review_agent.schema_migrations') IS NOT NULL"
            ).fetchone()
            if ledger == (True,):
                applied = _applied_migrations(connection)
                _verify_applied(migrations, applied)
            elif ledger == (False,):
                applied = {}
            else:
                raise MigrationError("could not inspect the migration ledger")
    except psycopg.Error as exc:
        raise MigrationError("PostgreSQL migration inspection failed") from exc

    applied_version = max(applied, default=0)
    return MigrationStatus(
        applied_version=applied_version,
        pending_versions=tuple(
            migration.version
            for migration in migrations
            if migration.version not in applied
        ),
        database_ahead=applied_version > migrations[-1].version,
    )


def apply_migrations(
    connection: psycopg.Connection[TupleRow],
    *,
    directory: Path = MIGRATION_DIRECTORY,
) -> tuple[int, ...]:
    """Apply every pending migration atomically and return applied versions."""
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise MigrationError("migration runner requires an idle PostgreSQL connection")
    migrations = discover_migrations(directory)
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            connection.execute("SET LOCAL lock_timeout = '30s'")
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,)
            )
            _create_ledger(connection)
            applied = _applied_migrations(connection)
            _verify_applied(migrations, applied)

            pending = tuple(
                migration for migration in migrations if migration.version not in applied
            )
            for migration in pending:
                try:
                    connection.execute(migration.sql, prepare=False)
                    connection.execute(
                        """
                        INSERT INTO review_agent.schema_migrations (
                            version, name, checksum
                        ) VALUES (%s, %s, %s)
                        """,
                        (migration.version, migration.name, migration.checksum),
                    )
                except psycopg.Error as exc:
                    raise MigrationError(f"failed to apply {migration.name}") from exc
    except psycopg.Error as exc:
        raise MigrationError("PostgreSQL migration transaction failed") from exc
    return tuple(migration.version for migration in pending)
