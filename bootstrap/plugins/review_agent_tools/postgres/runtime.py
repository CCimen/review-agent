"""Explicit lifecycle and readiness owner for the PostgreSQL connection pool."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from ..postgres_migrations import runner
from ..settings import PostgresDatabaseUrl


_POOL_CHECKOUT_TIMEOUT_SECONDS = 2.0
_POOL_RECONNECT_TIMEOUT_SECONDS = 10.0
_CONNECTION_OPTIONS = " ".join(
    (
        "-c timezone=UTC",
        "-c default_transaction_isolation=read\\ committed",
        "-c statement_timeout=15000",
        "-c lock_timeout=2000",
        "-c idle_in_transaction_session_timeout=60000",
    )
)


class PostgreSQLRuntimeError(RuntimeError):
    """The PostgreSQL runtime could not establish its required invariants."""


class PostgreSQLUnavailable(PostgreSQLRuntimeError):
    """The PostgreSQL server or connection pool is unavailable."""


class PostgreSQLNotReady(PostgreSQLRuntimeError):
    """PostgreSQL is reachable but not ready for this application image."""


class PostgreSQLRuntimeRole(StrEnum):
    REVIEWER = "reviewer"
    FEEDBACK = "feedback"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class _PoolShape:
    minimum_size: int
    maximum_size: int
    maximum_waiting: int


_POOL_SHAPES = {
    PostgreSQLRuntimeRole.REVIEWER: _PoolShape(1, 4, 8),
    PostgreSQLRuntimeRole.FEEDBACK: _PoolShape(1, 4, 8),
    PostgreSQLRuntimeRole.OPERATOR: _PoolShape(0, 1, 1),
}


@dataclass(frozen=True, slots=True)
class PostgreSQLReadiness:
    server_version: int
    applied_migration_version: int
    database_ahead: bool


@dataclass(frozen=True, slots=True)
class PostgreSQLPoolMetrics:
    open: bool
    minimum_size: int
    maximum_size: int
    size: int
    available: int
    waiting_requests: int


class PostgreSQLRuntime:
    """Own one bounded, explicitly opened reviewer connection pool."""

    def __init__(
        self,
        database_url: PostgresDatabaseUrl,
        *,
        role: PostgreSQLRuntimeRole = PostgreSQLRuntimeRole.REVIEWER,
    ) -> None:
        self._open_attempted = False
        self._database_url = database_url
        self._role = role
        shape = _POOL_SHAPES[role]
        application_name = f"review-agent-{role.value}"
        self._pool: ConnectionPool[psycopg.Connection[TupleRow]] = ConnectionPool(
            conninfo=database_url,
            connection_class=psycopg.Connection[TupleRow],
            kwargs={
                "application_name": application_name,
                # Keep checkouts idle; operation owners define transactions.
                "autocommit": True,
                "options": _CONNECTION_OPTIONS,
            },
            min_size=shape.minimum_size,
            max_size=shape.maximum_size,
            open=False,
            check=ConnectionPool.check_connection,
            name=application_name,
            timeout=_POOL_CHECKOUT_TIMEOUT_SECONDS,
            max_waiting=shape.maximum_waiting,
            reconnect_timeout=_POOL_RECONNECT_TIMEOUT_SECONDS,
        )

    @property
    def database_url(self) -> PostgresDatabaseUrl:
        return self._database_url

    def open(self, *, timeout: float = 10.0) -> PostgreSQLReadiness:
        """Open once, establish the minimum pool, and fail closed if unready."""
        if self._open_attempted:
            if self._pool.closed:
                raise PostgreSQLRuntimeError(
                    "PostgreSQL runtime cannot be reopened after close"
                )
            raise PostgreSQLRuntimeError("PostgreSQL runtime is already open")
        self._open_attempted = True
        try:
            self._pool.open(wait=True, timeout=timeout)
            return self.readiness()
        except PostgreSQLRuntimeError:
            self._pool.close()
            raise
        except psycopg.Error as exc:
            self._pool.close()
            raise PostgreSQLUnavailable(
                "PostgreSQL pool could not become ready"
            ) from exc

    def close(self, *, timeout: float = 5.0) -> None:
        self._open_attempted = True
        self._pool.close(timeout=timeout)

    def readiness(self) -> PostgreSQLReadiness:
        """Prove connection invariants and the read-only migration contract."""
        if self._pool.closed:
            raise PostgreSQLNotReady("PostgreSQL runtime is not open")
        try:
            with self._pool.connection() as connection:
                session = connection.execute(
                    """
                    SELECT
                        current_setting('TimeZone') = 'UTC',
                        current_setting('application_name') = %s,
                        current_setting('default_transaction_isolation')
                            = 'read committed',
                        current_setting('statement_timeout')::interval
                            = interval '15 seconds',
                        current_setting('lock_timeout')::interval
                            = interval '2 seconds',
                        current_setting(
                            'idle_in_transaction_session_timeout'
                        )::interval = interval '60 seconds',
                        current_setting('server_version_num')::integer
                    """,
                    (f"review-agent-{self._role.value}",),
                ).fetchone()
                if session is None or session[:6] != (
                    True,
                    True,
                    True,
                    True,
                    True,
                    True,
                ):
                    raise PostgreSQLNotReady(
                        "PostgreSQL connection settings do not match runtime invariants"
                    )
                server_version = session[6]
                if not isinstance(server_version, int):
                    raise PostgreSQLNotReady(
                        "PostgreSQL server version could not be read"
                    )
                migration = runner.inspect_migrations(connection)
        except runner.MigrationError as exc:
            raise PostgreSQLNotReady(str(exc)) from exc
        except psycopg.Error as exc:
            raise PostgreSQLUnavailable("PostgreSQL readiness check failed") from exc

        if migration.pending_versions:
            pending = ", ".join(
                f"{version:03d}" for version in migration.pending_versions
            )
            raise PostgreSQLNotReady(f"pending PostgreSQL migrations: {pending}")
        return PostgreSQLReadiness(
            server_version=server_version,
            applied_migration_version=migration.applied_version,
            database_ahead=migration.database_ahead,
        )

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[TupleRow]]:
        """Yield one checked-out connection inside one short transaction."""
        if self._pool.closed:
            raise PostgreSQLNotReady("PostgreSQL runtime is not open")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    yield connection
        # Preserve workload and lock failures; they do not mean PostgreSQL is down.
        except (
            psycopg.errors.DeadlockDetected,
            psycopg.errors.QueryCanceled,
            psycopg.errors.LockNotAvailable,
        ):
            raise
        except (PoolClosed, PoolTimeout, psycopg.OperationalError) as exc:
            raise PostgreSQLUnavailable(
                "PostgreSQL transaction could not complete"
            ) from exc

    def pool_metrics(self) -> PostgreSQLPoolMetrics:
        stats = self._pool.get_stats()
        return PostgreSQLPoolMetrics(
            open=not self._pool.closed,
            minimum_size=stats.get("pool_min", 0),
            maximum_size=stats.get("pool_max", 0),
            size=stats.get("pool_size", 0),
            available=stats.get("pool_available", 0),
            waiting_requests=stats.get("requests_waiting", 0),
        )
