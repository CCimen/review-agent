"""Idempotent least-privilege grants for the shared application runtime."""

from __future__ import annotations

from dataclasses import dataclass
import re

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow


_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MIN_PASSWORD_CHARACTERS = 16
_MANAGED_ROLE_COMMENT = "review-agent managed runtime role v1"


class DatabaseRoleError(ValueError):
    """A database role request is unsafe or cannot preserve least privilege."""


@dataclass(frozen=True, slots=True)
class RuntimeRoleResult:
    role_name: str
    database_name: str


def configure_runtime_role(
    connection: psycopg.Connection[TupleRow],
    *,
    role_name: str,
    password: str,
) -> RuntimeRoleResult:
    """Create or rotate one DML-only runtime login and its future grants."""
    normalized_role = role_name.strip()
    if _ROLE_RE.fullmatch(normalized_role) is None:
        raise DatabaseRoleError("role_name must be a safe PostgreSQL identifier")
    if len(password) < _MIN_PASSWORD_CHARACTERS or len(password) > 1024:
        raise DatabaseRoleError("password must be 16 to 1024 characters")
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise DatabaseRoleError("database role configuration requires a transaction")

    database_row = connection.execute("SELECT current_database()").fetchone()
    if database_row is None or not isinstance(database_row[0], str):
        raise DatabaseRoleError("current database could not be resolved")
    database_name = database_row[0]

    existing = connection.execute(
        """
        SELECT rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls, rolconnlimit,
               rolvaliduntil = 'infinity'::timestamptz, rolconfig,
               shobj_description(oid, 'pg_authid')
        FROM pg_roles
        WHERE rolname = %s
        """,
        (normalized_role,),
    ).fetchone()
    if existing is None:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(normalized_role))
        )
        connection.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(
                sql.Identifier(normalized_role),
                sql.Literal(_MANAGED_ROLE_COMMENT),
            )
        )
    else:
        safe_state = (
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            -1,
            True,
            None,
            _MANAGED_ROLE_COMMENT,
        )
        if existing != safe_state:
            raise DatabaseRoleError(
                "existing runtime role is not an unmodified Review Agent role"
            )
        memberships = connection.execute(
            """
            SELECT count(*)
            FROM pg_auth_members AS membership
            JOIN pg_roles AS member ON member.oid = membership.member
            WHERE member.rolname = %s
            """,
            (normalized_role,),
        ).fetchone()
        if memberships is None or memberships[0] != 0:
            raise DatabaseRoleError(
                "runtime role must not inherit another database role"
            )
        ownerships = connection.execute(
            """
            SELECT count(*)
            FROM pg_shdepend AS dependency
            JOIN pg_roles AS role
              ON role.oid = dependency.refobjid
             AND dependency.refclassid = 'pg_authid'::regclass
            WHERE role.rolname = %s
              AND dependency.deptype = 'o'
            """,
            (normalized_role,),
        ).fetchone()
        if ownerships is None or ownerships[0] != 0:
            raise DatabaseRoleError("runtime role must not own database objects")
        unexpected_acl_scope = connection.execute(
            """
            SELECT count(*)
            FROM pg_shdepend AS dependency
            JOIN pg_roles AS role
              ON role.oid = dependency.refobjid
             AND dependency.refclassid = 'pg_authid'::regclass
            JOIN pg_database AS database
              ON database.datname = current_database()
            WHERE role.rolname = %s
              AND dependency.deptype = 'a'
              AND (
                    dependency.dbid NOT IN (0, database.oid)
                 OR dependency.objsubid <> 0
                 OR dependency.classid NOT IN (
                        'pg_database'::regclass,
                        'pg_namespace'::regclass,
                        'pg_class'::regclass,
                        'pg_default_acl'::regclass
                    )
              )
            """,
            (normalized_role,),
        ).fetchone()
        if unexpected_acl_scope is None or unexpected_acl_scope[0] != 0:
            raise DatabaseRoleError(
                "runtime role has privileges outside the application contract"
            )
        unexpected_acl = connection.execute(
            """
            WITH target AS (
                SELECT oid
                FROM pg_roles
                WHERE rolname = %s
            ), direct_grants AS (
                SELECT 'database'::text AS object_type,
                       NULL::text AS schema_name,
                       database.datname AS object_name,
                       acl.privilege_type,
                       acl.is_grantable
                FROM pg_database AS database
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(
                        database.datacl,
                        acldefault('d'::"char", database.datdba)
                    )
                ) AS acl
                JOIN target ON target.oid = acl.grantee

                UNION ALL

                SELECT 'schema', NULL, namespace.nspname,
                       acl.privilege_type, acl.is_grantable
                FROM pg_namespace AS namespace
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(
                        namespace.nspacl,
                        acldefault('n'::"char", namespace.nspowner)
                    )
                ) AS acl
                JOIN target ON target.oid = acl.grantee

                UNION ALL

                SELECT CASE
                           WHEN relation.relkind = 'S' THEN 'sequence'
                           ELSE 'table'
                       END,
                       namespace.nspname,
                       relation.relname,
                       acl.privilege_type,
                       acl.is_grantable
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(
                        relation.relacl,
                        acldefault(
                            CASE
                                WHEN relation.relkind = 'S' THEN 'S'::"char"
                                ELSE 'r'::"char"
                            END,
                            relation.relowner
                        )
                    )
                ) AS acl
                JOIN target ON target.oid = acl.grantee

                UNION ALL

                SELECT CASE default_acl.defaclobjtype
                           WHEN 'S' THEN 'default_sequence'
                           ELSE 'default_table'
                       END,
                       namespace.nspname,
                       default_acl.oid::text,
                       acl.privilege_type,
                       acl.is_grantable
                FROM pg_default_acl AS default_acl
                LEFT JOIN pg_namespace AS namespace
                  ON namespace.oid = default_acl.defaclnamespace
                CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) AS acl
                JOIN target ON target.oid = acl.grantee
            )
            SELECT count(*)
            FROM direct_grants
            WHERE is_grantable OR NOT (
                (object_type = 'database'
                 AND object_name = current_database()
                 AND privilege_type = 'CONNECT')
                OR (object_type = 'schema'
                    AND object_name = 'review_agent'
                    AND privilege_type = 'USAGE')
                OR (object_type = 'table'
                    AND schema_name = 'review_agent'
                    AND (
                        (object_name = 'schema_migrations'
                         AND privilege_type = 'SELECT')
                        OR (object_name <> 'schema_migrations'
                            AND privilege_type IN (
                                'SELECT', 'INSERT', 'UPDATE', 'DELETE'
                            ))
                    ))
                OR (object_type = 'sequence'
                    AND schema_name = 'review_agent'
                    AND privilege_type IN ('SELECT', 'USAGE'))
                OR (object_type = 'default_table'
                    AND schema_name = 'review_agent'
                    AND privilege_type IN (
                        'SELECT', 'INSERT', 'UPDATE', 'DELETE'
                    ))
                OR (object_type = 'default_sequence'
                    AND schema_name = 'review_agent'
                    AND privilege_type IN ('SELECT', 'USAGE'))
            )
            """,
            (normalized_role,),
        ).fetchone()
        if unexpected_acl is None or unexpected_acl[0] != 0:
            raise DatabaseRoleError(
                "runtime role has privileges outside the application contract"
            )

    role = sql.Identifier(normalized_role)
    connection.execute(
        sql.SQL(
            "ALTER ROLE {} LOGIN NOINHERIT CONNECTION LIMIT -1 "
            "VALID UNTIL 'infinity' PASSWORD {}"
        ).format(role, sql.Literal(password))
    )
    connection.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
            sql.Identifier(database_name), role
        )
    )
    connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(database_name), role
        )
    )
    connection.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA review_agent FROM {}").format(role)
    )
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA review_agent TO {}").format(role))
    connection.execute(
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA review_agent FROM {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            "IN SCHEMA review_agent TO {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
            "ON review_agent.schema_migrations FROM {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA review_agent FROM {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA review_agent TO {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA review_agent "
            "REVOKE ALL PRIVILEGES ON TABLES FROM {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA review_agent "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA review_agent "
            "REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}"
        ).format(role)
    )
    connection.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA review_agent "
            "GRANT USAGE, SELECT ON SEQUENCES TO {}"
        ).format(role)
    )
    return RuntimeRoleResult(
        role_name=normalized_role,
        database_name=database_name,
    )
