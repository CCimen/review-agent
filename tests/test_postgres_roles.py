from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.postgres import roles  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")
ROLE = f"review_agent_runtime_{os.getpid()}"
COLLISION_ROLE = f"{ROLE}_collision"
MIGRATION_OWNER = f"review_agent_owner_{os.getpid()}"
LEAST_PRIVILEGE_ROLE = f"review_agent_limited_{os.getpid()}"
FIRST_PASSWORD = "runtime-role-first-password"
SECOND_PASSWORD = "runtime-role-second-password"
MIGRATION_PASSWORD = "migration-owner-test-password"


def _role_dsn(password: str) -> str:
    return _dsn_for(ROLE, password)


def _dsn_for(role: str, password: str) -> str:
    values = conninfo_to_dict(DSN)
    values["user"] = role
    values["password"] = password
    return make_conninfo(**values)


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLRuntimeRoleTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        if not DSN:
            return
        with psycopg.connect(DSN, autocommit=True) as connection:
            database_name = connection.execute("SELECT current_database()").fetchone()
            assert database_name is not None
            for role_name in (
                MIGRATION_OWNER,
                LEAST_PRIVILEGE_ROLE,
                COLLISION_ROLE,
                ROLE,
            ):
                exists = connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                    (role_name,),
                ).fetchone()
                if exists == (True,):
                    connection.execute(
                        sql.SQL(
                            "REVOKE ALL PRIVILEGES ON DATABASE {} FROM {} CASCADE"
                        ).format(
                            sql.Identifier(str(database_name[0])),
                            sql.Identifier(role_name),
                        )
                    )
                    connection.execute(
                        sql.SQL("DROP OWNED BY {} CASCADE").format(
                            sql.Identifier(role_name)
                        )
                    )
                connection.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
                )

    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
            with connection.transaction():
                roles.configure_runtime_role(
                    connection,
                    role_name=ROLE,
                    password=FIRST_PASSWORD,
                )

    def test_runtime_role_can_use_all_domain_tables_but_cannot_change_schema(
        self,
    ) -> None:
        with psycopg.connect(_role_dsn(FIRST_PASSWORD)) as connection:
            privileges = connection.execute(
                """
                SELECT table_name,
                       has_table_privilege(current_user, format(
                           'review_agent.%I', table_name
                       ), 'SELECT,INSERT,UPDATE,DELETE')
                FROM information_schema.tables
                WHERE table_schema = 'review_agent'
                  AND table_name <> 'schema_migrations'
                ORDER BY table_name
                """
            ).fetchall()
            self.assertTrue(privileges)
            self.assertTrue(all(granted for _, granted in privileges))
            connection.execute(
                """
                INSERT INTO review_agent.repositories (
                    provider, provider_repository_id, owner, name, full_name,
                    created_at, updated_at
                ) VALUES (
                    'github', 99001, 'runtime', 'probe', 'runtime/probe',
                    statement_timestamp(), statement_timestamp()
                )
                """
            )

        with psycopg.connect(_role_dsn(FIRST_PASSWORD)) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "UPDATE review_agent.schema_migrations SET version = version"
                )

        with psycopg.connect(_role_dsn(FIRST_PASSWORD)) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("CREATE TABLE review_agent.forbidden (id integer)")

    def test_reconfiguration_rotates_the_login_credential(self) -> None:
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                result = roles.configure_runtime_role(
                    connection,
                    role_name=ROLE,
                    password=SECOND_PASSWORD,
                )

        self.assertEqual(result.role_name, ROLE)
        with psycopg.connect(_role_dsn(SECOND_PASSWORD)) as connection:
            self.assertEqual(connection.execute("SELECT 1").fetchone(), (1,))
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(_role_dsn(FIRST_PASSWORD), connect_timeout=2)

    def test_unmarked_role_collision_is_rejected_without_mutation(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
                    sql.Identifier(COLLISION_ROLE),
                    sql.Literal(FIRST_PASSWORD),
                )
            )
        try:
            with psycopg.connect(DSN) as connection:
                with self.assertRaises(roles.DatabaseRoleError):
                    with connection.transaction():
                        roles.configure_runtime_role(
                            connection,
                            role_name=COLLISION_ROLE,
                            password=SECOND_PASSWORD,
                        )

            with psycopg.connect(DSN) as connection:
                state = connection.execute(
                    """
                    SELECT rolcreaterole, shobj_description(oid, 'pg_authid')
                    FROM pg_roles
                    WHERE rolname = %s
                    """,
                    (COLLISION_ROLE,),
                ).fetchone()
            self.assertEqual(state, (True, None))
        finally:
            with psycopg.connect(DSN, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(COLLISION_ROLE)
                    )
                )

    def test_marked_role_with_unrelated_grant_is_rejected_without_mutation(
        self,
    ) -> None:
        def revoke_probe_grant() -> None:
            with psycopg.connect(DSN, autocommit=True) as cleanup:
                cleanup.execute(
                    sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(
                        sql.Identifier(ROLE)
                    )
                )

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(
                sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(
                    sql.Identifier(ROLE)
                )
            )
        self.addCleanup(revoke_probe_grant)

        with psycopg.connect(DSN) as connection:
            with self.assertRaises(roles.DatabaseRoleError):
                with connection.transaction():
                    roles.configure_runtime_role(
                        connection,
                        role_name=ROLE,
                        password=SECOND_PASSWORD,
                    )

        with psycopg.connect(_role_dsn(FIRST_PASSWORD)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
                ).fetchone(),
                (True,),
            )
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(_role_dsn(SECOND_PASSWORD), connect_timeout=2)

    def test_marked_role_with_column_grant_is_rejected_without_mutation(
        self,
    ) -> None:
        def drop_probe_table() -> None:
            with psycopg.connect(DSN, autocommit=True) as cleanup:
                cleanup.execute("DROP TABLE IF EXISTS public.runtime_acl_probe")

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(
                "CREATE TABLE public.runtime_acl_probe "
                "(id integer, protected_value text)"
            )
            connection.execute(
                sql.SQL(
                    "GRANT UPDATE (protected_value) "
                    "ON public.runtime_acl_probe TO {}"
                ).format(sql.Identifier(ROLE))
            )
        self.addCleanup(drop_probe_table)

        with psycopg.connect(DSN) as connection:
            with self.assertRaises(roles.DatabaseRoleError):
                with connection.transaction():
                    roles.configure_runtime_role(
                        connection,
                        role_name=ROLE,
                        password=SECOND_PASSWORD,
                    )

        with psycopg.connect(_role_dsn(FIRST_PASSWORD)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT has_column_privilege("
                    "current_user, 'public.runtime_acl_probe', "
                    "'protected_value', 'UPDATE')"
                ).fetchone(),
                (True,),
            )
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(_role_dsn(SECOND_PASSWORD), connect_timeout=2)

    def test_marked_role_with_grant_option_is_rejected_without_mutation(
        self,
    ) -> None:
        def revoke_probe_option() -> None:
            with psycopg.connect(DSN, autocommit=True) as cleanup:
                cleanup.execute(
                    sql.SQL(
                        "REVOKE GRANT OPTION FOR SELECT "
                        "ON review_agent.repositories FROM {}"
                    ).format(sql.Identifier(ROLE))
                )

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(
                sql.SQL(
                    "GRANT SELECT ON review_agent.repositories "
                    "TO {} WITH GRANT OPTION"
                ).format(sql.Identifier(ROLE))
            )
        self.addCleanup(revoke_probe_option)

        with psycopg.connect(DSN) as connection:
            with self.assertRaises(roles.DatabaseRoleError):
                with connection.transaction():
                    roles.configure_runtime_role(
                        connection,
                        role_name=ROLE,
                        password=SECOND_PASSWORD,
                    )

        with psycopg.connect(_role_dsn(FIRST_PASSWORD)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT has_table_privilege("
                    "current_user, 'review_agent.repositories', "
                    "'SELECT WITH GRANT OPTION')"
                ).fetchone(),
                (True,),
            )
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(_role_dsn(SECOND_PASSWORD), connect_timeout=2)

    def test_non_superuser_database_owner_can_prepare_the_runtime_role(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            database_name = connection.execute("SELECT current_database()").fetchone()
            self.assertIsNotNone(database_name)
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
                    sql.Identifier(MIGRATION_OWNER),
                    sql.Literal(MIGRATION_PASSWORD),
                )
            )
            connection.execute(
                sql.SQL(
                    "GRANT ALL PRIVILEGES ON DATABASE {} TO {} WITH GRANT OPTION"
                ).format(
                    sql.Identifier(str(database_name[0])),
                    sql.Identifier(MIGRATION_OWNER),
                )
            )

        try:
            with psycopg.connect(
                _dsn_for(MIGRATION_OWNER, MIGRATION_PASSWORD),
                autocommit=True,
            ) as verification:
                self.assertEqual(
                    verification.execute(
                        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                    ).fetchone(),
                    (False,),
                )
            with psycopg.connect(
                _dsn_for(MIGRATION_OWNER, MIGRATION_PASSWORD)
            ) as connection:
                runner.apply_migrations(connection)
                with connection.transaction():
                    roles.configure_runtime_role(
                        connection,
                        role_name=LEAST_PRIVILEGE_ROLE,
                        password=FIRST_PASSWORD,
                    )

            with psycopg.connect(
                _dsn_for(LEAST_PRIVILEGE_ROLE, FIRST_PASSWORD)
            ) as connection:
                connection.execute(
                    """
                    INSERT INTO review_agent.repositories (
                        provider, provider_repository_id, owner, name, full_name,
                        created_at, updated_at
                    ) VALUES (
                        'github', 99002, 'runtime', 'limited', 'runtime/limited',
                        statement_timestamp(), statement_timestamp()
                    )
                    """
                )
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(
                        "UPDATE review_agent.schema_migrations SET version = version"
                    )
        finally:
            with psycopg.connect(DSN, autocommit=True) as connection:
                database_name = connection.execute(
                    "SELECT current_database()"
                ).fetchone()
                assert database_name is not None
                connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
                for role_name in (MIGRATION_OWNER, LEAST_PRIVILEGE_ROLE):
                    exists = connection.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                        (role_name,),
                    ).fetchone()
                    if exists == (True,):
                        connection.execute(
                            sql.SQL(
                                "REVOKE ALL PRIVILEGES ON DATABASE {} FROM {} CASCADE"
                            ).format(
                                sql.Identifier(str(database_name[0])),
                                sql.Identifier(role_name),
                            )
                        )
                        connection.execute(
                            sql.SQL("DROP OWNED BY {} CASCADE").format(
                                sql.Identifier(role_name)
                            )
                        )
                    connection.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(
                            sql.Identifier(role_name)
                        )
                    )

    def test_role_name_and_password_are_validated_before_sql(self) -> None:
        connection = Mock()
        with self.assertRaises(roles.DatabaseRoleError):
            roles.configure_runtime_role(
                connection,
                role_name="unsafe-role;",
                password="long-enough-password",
            )
        with self.assertRaises(roles.DatabaseRoleError):
            roles.configure_runtime_role(
                connection,
                role_name="safe_role",
                password="short",
            )
        connection.execute.assert_not_called()
